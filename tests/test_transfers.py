"""Tests for the composite file operations - upload, download, and the text
file helpers.

These exist because driving XM by hand is where an agent actually failed in
practice: it took four attempts across two sessions to get 'XM S D:LEDSHOW.COM'
issued from the right drive with the receiver armed in time. The sequence is
encoded here once instead.
"""

import os
import tempfile
import threading
import time
import unittest
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
        # exercise the command sequencing around it.
        self.link.xmodem_send = MagicMock(return_value={"ok": True, "blocks": 2, "bytes": 187})
        self.link.xmodem_receive = MagicMock(side_effect=self._fake_receive)
        self.received_payload = b"received bytes from the board"

    def _fake_receive(self, path, **kwargs):
        with open(path, "wb") as f:
            f.write(self.received_payload)
        return {"ok": True, "bytes": len(self.received_payload), "blocks": 1}

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
    def _source(self, content: bytes = b"x" * 187, name: str = "ledshow.com") -> str:
        path = _scratch(name)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_arms_xm_transfers_and_verifies(self):
        # The board has no such file yet; it appears once the transfer lands, so
        # the pre-flight existence check and the post-flight verify differ.
        landed = {"yet": False}
        self.link.xmodem_send = MagicMock(
            side_effect=lambda path, **kw: landed.update(yet=True) or {"ok": True, "blocks": 2})

        def _responder(line: bytes):
            self.commands.append(line)
            cmd = line.decode("latin-1").strip().upper()
            if cmd.startswith("DIR"):
                return b"\r\nB: LEDSHOW  COM\r\nA>" if landed["yet"] else b"\r\nNO FILE\r\nA>"
            if "XM R" in cmd:
                return b"\r\nReceiving: LEDSHOW.COM\r\nTo cancel: Ctrl-X\r\n"
            return b"\r\nA>"

        self.fake.responder = _responder
        res = self.link.upload(self._source(), dest_drive="B:")

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["cpm_name"], "LEDSHOW.COM")
        self.assertEqual(res["target"], "B:LEDSHOW.COM")
        self.assertEqual(res["bytes"], 187)
        self.assertTrue(res["verified"], "DIR showed the file, so it should verify")
        self.assertFalse(res["replaced_existing"])
        self.assertIn("sha256", res)

        self.assertIn("C:XM R B:LEDSHOW.COM", self.command_text())
        self.link.xmodem_send.assert_called_once()

    def test_derives_a_cpm_filename_from_the_host_filename(self):
        self.respond({b"XM R": b"\r\nReceiving\r\n", b"DIR": b"\r\nNO FILE\r\nA>"})
        res = self.link.upload(self._source(name="my long name!.text"))
        # 8.3, uppercase, punctuation dropped
        self.assertEqual(res["cpm_name"], "MYLONGNA.TEX")

    def test_explicit_cpm_name_wins(self):
        self.respond({b"XM R": b"\r\nReceiving\r\n", b"DIR": b"\r\nA>"})
        res = self.link.upload(self._source(), dest_drive="D:", cpm_name="demo.com")
        self.assertEqual(res["target"], "D:DEMO.COM")

    def test_reports_unverified_when_dir_does_not_show_the_file(self):
        self.respond({b"XM R": b"\r\nReceiving\r\n", b"DIR": b"\r\nNO FILE\r\nA>"})
        res = self.link.upload(self._source())
        self.assertTrue(res["ok"])
        self.assertFalse(res["verified"])

    def test_fails_fast_when_xm_reports_an_error(self):
        # Don't burn the 30s handshake timeout when XM has already refused.
        self.respond({b"XM R": b"\r\nFile error - read-only disk\r\nA>"})
        res = self.link.upload(self._source(), dest_drive="C:")

        self.assertFalse(res["ok"])
        self.assertIn("XM refused", res["error"])
        self.link.xmodem_send.assert_not_called()

    def test_recovers_the_console_when_xm_refuses(self):
        """A refusal doesn't mean XM exited. Uploading to the ROM disk leaves
        ZSDOS waiting on "Bad Sector", and dismissing that hands control back to
        XM, which arms and pokes forever. The failure path must cancel XM and get
        the shell back, or every later tool call talks to an armed receiver."""
        CAN = 0x18

        def _responder(line: bytes):
            self.commands.append(line)
            cmd = line.decode("latin-1").strip().upper()
            if "XM R" in cmd:
                return (b"\r\nReceiving: C0:ROTEST.BIN\r\n22k available for uploads\r\n"
                        b"\r\nZSDOS error on C: Bad Sector\r\nCall: 22  File: ROTEST  .BIN")
            if bytes([CAN]) in line:
                return b"\r\nA>"   # the cancel got XM to let go
            return b"\r\nNO FILE\r\nA>"

        self.fake.responder = _responder
        res = self.link.upload(self._source(), dest_drive="C:")

        self.assertFalse(res["ok"])
        self.assertIn("Bad Sector", res["error"], "report the whole offending line")
        self.assertTrue(res["recovered"])
        self.assertIn(bytes([CAN, CAN]), bytes(self.fake.written).replace(b"\r", b""),
                      "XM's documented cancel is Ctrl-X, pause, Ctrl-X")
        self.link.xmodem_send.assert_not_called()

    def test_detects_xm_bailing_out_after_printing_its_banner(self):
        """Observed on real hardware: XM prints "Receiving:" and only then
        decides it cannot proceed, so the banner is not proof it is armed. The
        shell prompt coming back is the signal that XM has gone."""
        self.respond({b"XM R": b"\r\nReceiving: B0:X.COM\r\n230k available for uploads\r\n"
                               b"++ Some unrecognised complaint ++\r\nA>"})
        res = self.link.upload(self._source(), dest_drive="B:", overwrite=False)

        self.assertFalse(res["ok"])
        self.assertIn("XM exited without starting a transfer", res["error"])
        self.link.xmodem_send.assert_not_called()

    def test_erases_an_existing_target_before_uploading(self):
        # XM will not overwrite, so replacing a file means erasing it first.
        state = {"exists": True}

        def _responder(line: bytes):
            self.commands.append(line)
            cmd = line.decode("latin-1").strip().upper()
            if cmd.startswith("ERA"):
                state["exists"] = False
                return b"\r\nA>"
            if cmd.startswith("DIR"):
                return b"\r\nB: LEDSHOW  COM\r\nA>" if state["exists"] else b"\r\nNO FILE\r\nA>"
            if "XM R" in cmd:
                return b"\r\nReceiving: B0:LEDSHOW.COM\r\n"
            return b"\r\nA>"

        self.fake.responder = _responder
        res = self.link.upload(self._source(), dest_drive="B:", verify=False)

        self.assertTrue(res["ok"], res)
        self.assertTrue(res["replaced_existing"])
        self.assertIn("ERA B:LEDSHOW.COM", self.command_text())
        self.link.xmodem_send.assert_called_once()

    def test_refuses_to_replace_when_overwrite_is_disabled(self):
        self.respond({b"DIR": b"\r\nB: LEDSHOW  COM\r\nA>"})
        res = self.link.upload(self._source(), dest_drive="B:", overwrite=False)

        self.assertFalse(res["ok"])
        self.assertIn("already exists", res["error"])
        self.link.xmodem_send.assert_not_called()

    def test_does_not_erase_when_the_target_is_absent(self):
        self.respond({b"DIR": b"\r\nNO FILE\r\nA>",
                      b"XM R": b"\r\nReceiving: B0:LEDSHOW.COM\r\n"})
        res = self.link.upload(self._source(), dest_drive="B:", verify=False)

        self.assertTrue(res["ok"], res)
        self.assertFalse(res["replaced_existing"])
        self.assertNotIn("ERA B:LEDSHOW.COM", self.command_text())

    def test_refuses_when_no_os_is_running(self):
        self.link._system_state = "hbios"
        res = self.link.upload(self._source())
        self.assertFalse(res["ok"])
        self.assertIn("not at a CP/M prompt", res["error"])
        self.link.xmodem_send.assert_not_called()

    def test_reports_a_missing_local_file(self):
        res = self.link.upload("/nonexistent/nope.com")
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])


class TestDownload(TransferTestCase):
    def test_arms_xm_receives_and_hashes(self):
        self.respond({b"XM S": b"\r\nSending: LEDSHOW.COM\r\nTo cancel: Ctrl-X\r\n"})
        dest = _scratch("out.com")
        res = self.link.download("B:LEDSHOW.COM", local_path=dest)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["bytes"], len(self.received_payload))
        self.assertEqual(res["local_path"], dest)
        self.assertIn("C:XM S B:LEDSHOW.COM", self.command_text())
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), self.received_payload)

    def test_defaults_the_host_filename_to_the_cpm_name(self):
        self.respond({b"XM S": b"\r\nSending\r\n"})
        cwd = tempfile.mkdtemp(prefix="rc2014-test-")
        old = os.getcwd()
        os.chdir(cwd)
        try:
            res = self.link.download("B:TEST.TXT")
        finally:
            os.chdir(old)
        self.assertEqual(os.path.basename(res["local_path"]), "TEST.TXT")

    def test_fails_fast_when_the_file_does_not_exist(self):
        self.respond({b"XM S": b"\r\nNo file\r\nA>"})
        res = self.link.download("B:MISSING.TXT")
        self.assertFalse(res["ok"])
        self.assertIn("XM refused", res["error"])
        self.link.xmodem_receive.assert_not_called()


class TestTextFiles(TransferTestCase):
    def test_write_converts_newlines_and_appends_the_eof_marker(self):
        uploaded = {}

        def _capture_upload(local_path, **kwargs):
            with open(local_path, "rb") as f:
                uploaded["bytes"] = f.read()
            uploaded["kwargs"] = kwargs
            return {"ok": True, "verified": True}

        with patch.object(self.link, "upload", side_effect=_capture_upload):
            res = self.link.write_text_file("B:HELLO.TXT", "line one\nline two\n")

        self.assertTrue(res["ok"])
        self.assertEqual(uploaded["bytes"], b"line one\r\nline two\r\n" + bytes([SUB]))
        self.assertEqual(uploaded["kwargs"]["dest_drive"], "B:")
        self.assertEqual(uploaded["kwargs"]["cpm_name"], "HELLO.TXT")

    def test_write_without_a_drive_prefix(self):
        captured = {}
        with patch.object(self.link, "upload",
                          side_effect=lambda p, **kw: captured.update(kw) or {"ok": True}):
            self.link.write_text_file("NOTES.TXT", "hi")
        self.assertIsNone(captured["dest_drive"])
        self.assertEqual(captured["cpm_name"], "NOTES.TXT")

    def test_write_removes_its_temporary_file(self):
        paths = []
        with patch.object(self.link, "upload",
                          side_effect=lambda p, **kw: paths.append(p) or {"ok": True}):
            self.link.write_text_file("B:X.TXT", "data")
        self.assertFalse(os.path.exists(paths[0]), "the staging file must be cleaned up")

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
