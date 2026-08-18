"""Tests for the composite file operations - upload, download, and the text
file conventions they apply.

These exist because driving XM by hand is where an agent actually failed in
practice: it took four attempts across two sessions to get 'XM S D:LEDSHOW.COM'
issued from the right drive with the receiver armed in time. The sequence is
encoded here once instead.
"""

import io
import os
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from rc2014bridge.link import SUB, SerialLink

from fakes import FakeSerial

DRIVE_MAPPINGS = {"A": "IDE0:0", "B": "MD0:0", "C": "MD1:0", "D": "IDE0:1"}


def _scratch(name: str = "hardware_info.json") -> str:
    return os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), name)


class TransferTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSerial()
        with patch("serial.Serial", return_value=self.fake):
            self.link = SerialLink("/dev/fake", hw_info_file=_scratch())
        self.addCleanup(self.link.close)
        self.link.hardware_info["drive_mappings"] = dict(DRIVE_MAPPINGS)
        self.link._system_state = "cpm"
        self.commands: list[bytes] = []

        # Stub the wire protocol itself - it has its own tests - so these
        # exercise the command sequencing and zip/unzip handling around it.
        # Captured at call time, not after: upload() deletes its zip
        # staging file immediately after xmodem_send returns.
        self.sent_zip_bytes: list[bytes] = []
        self.sent_zip_path: list[str] = []
        self.link.xmodem_send = MagicMock(side_effect=self._fake_send)
        self.link.xmodem_receive = MagicMock(side_effect=self._fake_receive)
        self.received_payload = b"received bytes from the board"

    def _fake_send(self, path, **kwargs):
        self.sent_zip_path.append(path)
        with open(path, "rb") as f:
            data = f.read()
        self.sent_zip_bytes.append(data)
        return {"ok": True, "blocks": 2, "bytes": len(data)}

    def _fake_receive(self, path, **kwargs):
        with open(path, "wb") as f:
            f.write(self.received_payload)
        return {"ok": True, "bytes": len(self.received_payload), "blocks": 1,
                "block_size": 1024}

    def respond(self, table: dict[bytes, bytes], default: bytes = b"\r\nA>"):
        """Answer each command line with canned board output."""
        def _responder(line: bytes):
            self.commands.append(line)
            for needle, reply in table.items():
                if needle in line:
                    return reply
            return default if line else None
        self.fake.responder = _responder

    def command_text(self) -> list[str]:
        return [c.decode("latin-1") for c in self.commands if c]

    def last_zip_entry(self) -> bytes:
        """The content of the single entry in the zip most recently handed
        to the stubbed xmodem_send, so a test can assert on what upload()
        actually staged for transfer."""
        with zipfile.ZipFile(io.BytesIO(self.sent_zip_bytes[-1])) as z:
            names = z.namelist()
            return z.read(names[0])


class TestXmCommandResolution(TransferTestCase):
    def test_xm_is_invoked_from_the_rom_disk(self):
        # XM.COM lives on the ROM disk, which is whichever letter maps to MD1 -
        # C: here, but that is not guaranteed across machines.
        self.assertEqual(self.link._rom_disk_drive(), "C")
        self.assertEqual(self.link._xm_command(), "C:XM")

    def test_falls_back_to_bare_xm_when_no_rom_disk_is_mapped(self):
        self.link.hardware_info["drive_mappings"] = {"A": "IDE0:0"}
        self.assertEqual(self.link._xm_command(), "XM")


class TestUpload(TransferTestCase):
    def _responder_for(self, exists_before: bool = False):
        state = {"exists": exists_before}

        def _responder(line: bytes):
            self.commands.append(line)
            cmd = line.decode("latin-1").strip().upper()
            if cmd.startswith("STAT") and cmd.endswith(":"):
                return b"\r\nBytes Remaining On B: 4000k\r\nA>"
            if cmd.startswith("STAT"):
                return b"\r\n Recs  Bytes  Ext Acc\r\n   10     4k    1 R/W B:LEDSHOW.COM\r\nA>"
            if cmd.startswith("DIR"):
                return (b"\r\nB: LEDSHOW  COM\r\nA>" if state["exists"]
                        else b"\r\nNO FILE\r\nA>")
            if cmd.startswith("ERA") and not cmd.endswith(".ZIP"):
                # Only the target's own erase (overwrite path) changes
                # existence - the transient zip's ERA cleanup must not.
                state["exists"] = False
                return b"\r\nA>"
            if "XM R" in cmd:
                return b"\r\nReceiving: B0:LEDSHOW.ZIP\r\nTo cancel: Ctrl-X\r\n"
            if cmd.startswith("UNZIP"):
                state["exists"] = True  # extraction landed the target file
                return b"\r\nExtracting\r\nA>"
            return b"\r\nA>"

        self.fake.responder = _responder
        return state

    def test_arms_xm_zips_transfers_unzips_and_verifies(self):
        self._responder_for(exists_before=False)
        res = self.link.upload("LEDSHOW.COM", "B", content="hello there\n")

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["target"], "B:LEDSHOW.COM")
        self.assertFalse(res["existed_before"])
        self.assertFalse(res["replaced_existing"])
        self.assertTrue(res["compressed"])
        self.assertTrue(res["verified"])
        self.assertIn("sha256", res)
        self.assertGreater(res["bytes_raw"], 0)
        self.assertGreater(res["bytes_wire"], 0)

        self.assertIn("C:XM R B:LEDSHOW.ZIP", self.command_text())
        self.assertIn("UNZIP B:LEDSHOW.ZIP B:", self.command_text())
        self.assertIn("ERA B:LEDSHOW.ZIP", self.command_text())
        self.link.xmodem_send.assert_called_once()

    def test_zips_the_crlf_and_eof_converted_content_for_text(self):
        self._responder_for(exists_before=False)
        self.link.upload("HELLO.TXT", "B", content="line one\nline two\n")
        self.assertEqual(self.last_zip_entry(), b"line one\r\nline two\r\n" + bytes([SUB]))

    def test_binary_content_skips_crlf_eof_conversion(self):
        import base64
        self._responder_for(exists_before=False)
        raw = bytes([SUB, 0, 1, 2, 10])  # would be mangled by text conversion
        self.link.upload("BLOB.BIN", "B", content=base64.b64encode(raw).decode("ascii"),
                         binary=True)
        self.assertEqual(self.last_zip_entry(), raw)

    def test_user_area_addressing(self):
        self._responder_for(exists_before=False)
        res = self.link.upload("HELLO.TXT", "B", user=1, content="hi")
        self.assertEqual(res["target"], "B1:HELLO.TXT")
        self.assertIn("UNZIP B:HELLO.ZIP B1:", self.command_text())

    def test_default_user_area_omits_the_digit(self):
        self._responder_for(exists_before=False)
        res = self.link.upload("HELLO.TXT", "B", user=0, content="hi")
        self.assertEqual(res["target"], "B:HELLO.TXT")

    def test_stops_when_target_exists_and_overwrite_is_disabled(self):
        self._responder_for(exists_before=True)
        res = self.link.upload("LEDSHOW.COM", "B", content="x", overwrite=False)

        self.assertFalse(res["ok"])
        self.assertTrue(res["existed_before"])
        self.assertIn("already exists", res["error"])
        self.assertEqual(res.get("records"), 10)
        self.assertEqual(res.get("bytes"), 4096)
        self.link.xmodem_send.assert_not_called()

    def test_erases_the_existing_target_before_unzipping_when_overwrite_is_enabled(self):
        # UNZIP refuses to replace an existing file (confirmed against real
        # hardware: it reports STATUS "EXISTS" and skips extraction), so
        # overwrite=True must erase the old target before UNZIP runs.
        self._responder_for(exists_before=True)
        res = self.link.upload("LEDSHOW.COM", "B", content="x", overwrite=True)

        self.assertTrue(res["ok"], res)
        self.assertTrue(res["existed_before"])
        self.assertTrue(res["replaced_existing"])
        commands = self.command_text()
        era_index = commands.index("ERA B:LEDSHOW.COM")
        unzip_index = next(i for i, c in enumerate(commands) if c.startswith("UNZIP"))
        self.assertLess(era_index, unzip_index, "must erase the old target before UNZIP")

    def test_stops_on_insufficient_space(self):
        def _responder(line: bytes):
            self.commands.append(line)
            cmd = line.decode("latin-1").strip().upper()
            if cmd.startswith("STAT") and cmd.endswith(":"):
                return b"\r\nBytes Remaining On B: 0k\r\nA>"
            if cmd.startswith("DIR"):
                return b"\r\nNO FILE\r\nA>"
            return b"\r\nA>"

        self.fake.responder = _responder
        res = self.link.upload("BIG.BIN", "B", content="x" * 1000)

        self.assertFalse(res["ok"])
        self.assertIn("insufficient space", res["error"])
        self.assertEqual(res["available"], 0)
        self.link.xmodem_send.assert_not_called()

    def test_fails_fast_when_xm_reports_an_error(self):
        def _responder(line: bytes):
            self.commands.append(line)
            cmd = line.decode("latin-1").strip().upper()
            if cmd.startswith("STAT") and cmd.endswith(":"):
                return b"\r\nBytes Remaining On C: 4000k\r\nA>"
            if cmd.startswith("DIR"):
                return b"\r\nNO FILE\r\nA>"
            if "XM R" in cmd:
                return b"\r\nFile error - read-only disk\r\nA>"
            return b"\r\nA>"

        self.fake.responder = _responder
        res = self.link.upload("X.COM", "C", content="x")

        self.assertFalse(res["ok"])
        self.assertIn("XM refused", res["error"])
        self.link.xmodem_send.assert_not_called()

    def test_refuses_when_no_os_is_running(self):
        self.link._system_state = "hbios"
        res = self.link.upload("X.COM", "B", content="x")
        self.assertFalse(res["ok"])
        self.assertIn("not at a CP/M prompt", res["error"])
        self.link.xmodem_send.assert_not_called()

    def test_rejects_an_invalid_cpm_name(self):
        res = self.link.upload("this name has spaces.txt", "B", content="x")
        self.assertFalse(res["ok"])
        self.assertIn("not a valid 8.3 CP/M filename", res["error"])
        self.link.xmodem_send.assert_not_called()

    def test_rejects_an_out_of_range_user_area(self):
        res = self.link.upload("X.COM", "B", user=16, content="x")
        self.assertFalse(res["ok"])
        self.assertIn("user area must be 0-15", res["error"])

    def test_cleans_up_its_zip_tempfile(self):
        self._responder_for(exists_before=False)
        self.link.upload("HELLO.TXT", "B", content="hi")
        self.assertFalse(os.path.exists(self.sent_zip_path[-1]),
                         "the zip staging file must be cleaned up")


class TestUploadZpm3(TransferTestCase):
    """ZPM3 (ZCPR/CP/M+) speaks a different dialect: SDZ instead of DIR (DIR
    can't address user areas), ERASE instead of ERA, STAT has no drive-space
    form (SDZ's 'Free: Nk' trailer is the source), and its UNZIPZ only
    CRC-checks an archive unless given /E - all confirmed live on an SC126
    running ZPM3."""

    def setUp(self):
        super().setUp()
        self.link._zpm3 = True

    def _responder_for(self, exists_before: bool = False):
        files = {"LEDSHOW.COM"} if exists_before else set()
        zpm3_prompt = b"\r\n15:45 B0>"

        def _responder(line: bytes):
            self.commands.append(line)
            cmd = line.decode("latin-1").strip().upper()
            if cmd.startswith("SDZ"):
                # "SDZ B:" (free-space probe) or "SDZ B:LEDSHOW.COM"
                stem = cmd.split(":")[-1].replace(".", "").strip()
                if stem and stem not in {f.replace(".", "") for f in files}:
                    return b"\r\n >> No detectable file(s) on B0:   Free: 4000k \r\n" + zpm3_prompt
                listing = b"".join(b"\r\nB0: " + f.encode() for f in files)
                return listing + b"\r\nFree: 4000k\r\n" + zpm3_prompt
            if cmd.startswith("STAT"):
                return b"\r\nSTAT ?\r\n" + zpm3_prompt  # ZPM3 STAT has no space form
            if cmd.startswith("ERASE"):
                fname = cmd.split(None, 1)[1]
                files.discard(fname)
                return zpm3_prompt
            if "XM R" in cmd:
                return b"\r\nReceiving: B0:LEDSHOW.ZIP\r\nTo cancel: Ctrl-X\r\n"
            if cmd.startswith("UNZIP"):
                if "/E" not in cmd:
                    # UNZIPZ's default is a CRC check - nothing is extracted.
                    return b"\r\nChecking...\r\nDone.\r\n" + zpm3_prompt
                files.add("LEDSHOW.COM")
                return b"\r\nExtracting\r\n" + zpm3_prompt
            return zpm3_prompt

        self.fake.responder = _responder

    def test_upload_uses_the_zpm3_dialect(self):
        self._responder_for(exists_before=False)
        res = self.link.upload("LEDSHOW.COM", "B", content="x")

        self.assertTrue(res["ok"], res)
        self.assertTrue(res["verified"])
        commands = self.command_text()
        self.assertIn("UNZIP B:LEDSHOW.ZIP B: /E", commands)
        self.assertTrue(any(c.startswith("SDZ") for c in commands))
        self.assertFalse(any(c.startswith("DIR ") for c in commands))
        self.assertFalse(any(c.startswith("ERA ") for c in commands))

    def test_overwrite_erases_with_bare_filename_after_drive_switch(self):
        self._responder_for(exists_before=True)
        res = self.link.upload("LEDSHOW.COM", "B", content="x", overwrite=True)

        self.assertTrue(res["ok"], res)
        commands = self.command_text()
        self.assertIn("ERASE LEDSHOW.COM", commands)
        erase_index = commands.index("ERASE LEDSHOW.COM")
        unzip_index = next(i for i, c in enumerate(commands) if c.startswith("UNZIP"))
        self.assertLess(erase_index, unzip_index, "must erase the old target before UNZIP")

    def test_free_space_comes_from_sdz_when_stat_has_no_space_form(self):
        self._responder_for()
        res = self.link.upload("LEDSHOW.COM", "B", content="x")
        self.assertTrue(res["ok"], res)
        self.assertIn("SDZ B:", self.command_text())


class TestDownload(TransferTestCase):
    def test_arms_xm_with_1k_blocks_receives_and_hashes(self):
        self.respond({b"XM SK": b"\r\nSending: LEDSHOW.COM\r\nTo cancel: Ctrl-X\r\n",
                      b"DIR": b"\r\nB: LEDSHOW  COM\r\nA>"})
        res = self.link.download("LEDSHOW.COM", "B")

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["target"], "B:LEDSHOW.COM")
        self.assertEqual(res["content"], self.received_payload.decode("latin-1"))
        self.assertFalse(res["binary"])
        self.assertFalse(res["compressed"])
        self.assertEqual(res["block_size"], 1024)
        self.assertIn("sha256", res)
        self.assertIn("C:XM SK B:LEDSHOW.COM", self.command_text())

    def test_binary_download_returns_base64(self):
        import base64
        self.received_payload = bytes([0, 1, 2, 255, 254])
        self.respond({b"XM SK": b"\r\nSending\r\n", b"DIR": b"\r\nB: BLOB.BIN\r\nA>"})
        res = self.link.download("BLOB.BIN", "B", binary=True)

        self.assertTrue(res["ok"], res)
        self.assertTrue(res["binary"])
        self.assertEqual(base64.b64decode(res["content"]), self.received_payload)

    def test_text_download_strips_the_eof_marker_and_crlf(self):
        self.received_payload = b"line one\r\nline two\r\n" + bytes([SUB])
        self.respond({b"XM SK": b"\r\nSending\r\n", b"DIR": b"\r\nB: X.TXT\r\nA>"})
        res = self.link.download("X.TXT", "B")
        self.assertEqual(res["content"], "line one\nline two\n")

    def test_user_area_addressing(self):
        self.respond({b"XM SK": b"\r\nSending\r\n", b"DIR": b"\r\nA1: X.TXT\r\nA>"})
        self.link.download("X.TXT", "A", user=1)
        self.assertIn("C:XM SK A1:X.TXT", self.command_text())

    def test_binary_download_does_not_strip_trailing_0x1a_bytes(self):
        # xmodem_receive's strip_padding removes trailing 0x1A indiscriminately -
        # right for text (our own EOF marker convention), wrong for binary's
        # "raw bytes in, raw bytes out" promise if real data ends in 0x1A.
        self.respond({b"XM SK": b"\r\nSending\r\n", b"DIR": b"\r\nB: BLOB.BIN\r\nA>"})
        self.link.download("BLOB.BIN", "B", binary=True)
        self.link.xmodem_receive.assert_called_once_with(
            self.link.xmodem_receive.call_args.args[0], strip_padding=False)

    def test_text_download_still_strips_padding(self):
        self.respond({b"XM SK": b"\r\nSending\r\n", b"DIR": b"\r\nB: X.TXT\r\nA>"})
        self.link.download("X.TXT", "B", binary=False)
        self.link.xmodem_receive.assert_called_once_with(
            self.link.xmodem_receive.call_args.args[0], strip_padding=True)

    def test_fails_fast_when_the_file_does_not_exist(self):
        self.respond({b"DIR": b"\r\nNO FILE\r\nA>"})
        res = self.link.download("MISSING.TXT", "B")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "not found")
        self.link.xmodem_receive.assert_not_called()

    def test_refuses_when_no_os_is_running(self):
        self.link._system_state = "hbios"
        res = self.link.download("X.COM", "B")
        self.assertFalse(res["ok"])
        self.assertIn("not at a CP/M prompt", res["error"])

    def test_rejects_an_invalid_cpm_name(self):
        res = self.link.download("this name has spaces.txt", "B")
        self.assertFalse(res["ok"])
        self.assertIn("not a valid 8.3 CP/M filename", res["error"])


class TestReadTextFile(TransferTestCase):
    def test_read_uses_type_and_returns_the_contents(self):
        self.respond({b"TYPE": b"\r\nhello from the board\r\nsecond line\r\nA>"})
        res = self.link.read_text_file("B:HELLO.TXT")

        self.assertTrue(res["ok"], res)
        self.assertIn("hello from the board", res["content"])
        self.assertIn("second line", res["content"])
        self.assertFalse(res["truncated"])
        self.assertIn("TYPE B:HELLO.TXT", self.command_text())

    def test_read_truncates_at_max_bytes(self):
        self.respond({b"TYPE": b"\r\n" + b"z" * 500 + b"\r\nA>"})
        res = self.link.read_text_file("B:BIG.TXT", max_bytes=100)
        self.assertEqual(len(res["content"]), 100)
        self.assertTrue(res["truncated"])

    def test_read_reports_a_missing_file(self):
        self.respond({b"TYPE": b"\r\nNo file\r\nA>"})
        res = self.link.read_text_file("B:MISSING.TXT")
        self.assertFalse(res["ok"])

    def test_read_advances_a_silent_pager(self):
        """This ZSDOS build's console driver pages TYPE by CRT height and
        blocks for a keystroke between pages with no visible marker at all -
        not even "-- more --". Confirmed against real hardware: a file just
        past one page returned only page one, marked ok/not-timed-out, with
        no hint anything was missing. read_text_file must notice the wire
        went quiet without a prompt in sight and nudge it, same as it did on
        the real board."""
        page_one = b"\r\n" + b"a" * 40 + b"\r\n"
        page_two = b"b" * 40 + b"\r\nA>"
        nudged = threading.Event()

        def _responder(line: bytes):
            self.commands.append(line)
            if b"TYPE" not in line:
                return None
            checkpoint = len(self.fake.written)

            def _wait_for_nudge():
                deadline = time.time() + 5
                while time.time() < deadline:
                    if len(self.fake.written) > checkpoint:
                        nudged.set()
                        self.fake.feed(page_two)
                        return
                    time.sleep(0.01)

            threading.Thread(target=_wait_for_nudge, daemon=True).start()
            return page_one

        self.fake.responder = _responder
        res = self.link.read_text_file("B:BIG.TXT", timeout=10)

        self.assertTrue(nudged.wait(timeout=1), "no nudge keystroke was ever sent")
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["timed_out"], res)
        self.assertIn("a" * 40, res["content"])
        self.assertIn("b" * 40, res["content"])


if __name__ == "__main__":
    unittest.main()
