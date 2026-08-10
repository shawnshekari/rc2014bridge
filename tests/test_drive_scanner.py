import os
import tempfile
import time
import unittest
from unittest.mock import patch

from rc2014bridge.link import (SerialLink, _classify_drive_purpose,
                               _parse_cpm_dir_output, _parse_zsdos_banner)

from fakes import FakeSerial


class TestDriveScanner(unittest.TestCase):
    def test_parse_zsdos_banner(self):
        sample_banner = """
        Booting Disk Unit 2, Slice 0, Sector 0x00000800...
        Volume "ZSDOS 1.1" [0xD000-0xFE00, entry @ 0xE600]...
        CBIOS v3.7.0-dev.13 [WBW]
        Configuring Drives...
          A:=IDE0:0
          B:=MD0:0
          C:=MD1:0
          D:=IDE0:1
          1859 Disk Buffer Bytes Free
        ZSDOS v1.1, 54.0K TPA
        """
        info = _parse_zsdos_banner(sample_banner)
        self.assertIn("ZSDOS v1.1", info["zsdos_version"])
        self.assertIn("CBIOS v3.7.0", info["cbios_version"])
        self.assertEqual(info["tpa"], "54.0K TPA")
        self.assertEqual(info["drive_mappings"]["A"], "IDE0:0")
        self.assertEqual(info["drive_mappings"]["B"], "MD0:0")
        self.assertEqual(info["drive_mappings"]["C"], "MD1:0")

    def test_parse_cpm_dir_output(self):
        sample_dir = """
        A: ZPATH    COM : REQUIREM TXT : TEST     TXT : DEMO     Z80
        A: HELLO    PAS : MYPROG   C   : CONFIG   SYS
        B: XM       COM : FLASH    COM : REBOOT   COM
        """
        files = _parse_cpm_dir_output(sample_dir)
        self.assertIn("ZPATH.COM", files)
        self.assertIn("TEST.TXT", files)
        self.assertIn("DEMO.Z80", files)
        self.assertIn("HELLO.PAS", files)
        self.assertIn("XM.COM", files)

    def test_classify_drive_purpose(self):
        # ROM drive - identified by its device mapping, not its contents
        rom_p = _classify_drive_purpose("C:", ["XM.COM", "FLASH.COM", "REBOOT.COM"], "MD1:0")
        self.assertIn("ROM Disk", rom_p)
        self.assertIn("read-only", rom_p)

        # RAM drive
        ram_p = _classify_drive_purpose("B:", [], "MD0:0")
        self.assertIn("RAM Disk", ram_p)

        # OS System drive
        sys_p = _classify_drive_purpose("A:", ["ZPATH.COM", "STAT.COM", "PIP.COM"], "IDE0:0", "R/W")
        self.assertIn("CP/M / ZSDOS System Disk", sys_p)

        # User code drive
        code_p = _classify_drive_purpose("D:", ["DEMO.Z80", "MAIN.C", "SRC.PAS"], "IDE0:1", "R/W")
        self.assertIn("User Programming & Source Code", code_p)

        # Empty drive
        empty_p = _classify_drive_purpose("E:", [], "IDE0:2", "R/W")
        self.assertIn("Empty / Unformatted Volume", empty_p)

    def test_cf_slice_carrying_rom_utilities_is_not_a_rom_disk(self):
        """Most CF slices carry copies of XM.COM and FLASH.COM. Classifying by
        filename labelled them read-only ROM disks while STAT reported R/W -
        contradictory output for a model to reason over."""
        purpose = _classify_drive_purpose(
            "D:", ["XM.COM", "FLASH.COM", "ZPATH.COM", "STAT.COM"], "IDE0:1", "R/W")
        self.assertNotIn("ROM Disk", purpose)
        self.assertNotIn("read-only", purpose)
        self.assertIn("CP/M / ZSDOS System Disk", purpose)

    def test_read_only_label_follows_stat(self):
        writable = _classify_drive_purpose("D:", ["GAME.COM"], "IDE0:1", "R/W")
        readonly = _classify_drive_purpose("E:", ["GAME.COM"], "IDE0:2", "R/O")
        self.assertNotIn("read-only", writable)
        self.assertIn("read-only", readonly)


class TestScanDrives(unittest.TestCase):
    """The scan drives one DIR per drive through run_command, so each drive's
    listing is isolated by the prompt rather than regex-carved out of the
    shared screen history."""

    def setUp(self):
        self.fake = FakeSerial()
        with patch("serial.Serial", return_value=self.fake):
            self.link = SerialLink(
                "/dev/fake",
                hw_info_file=os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hw.json"))
        self.addCleanup(self.link.close)
        self.link._system_state = "cpm"
        self.link.hardware_info["drive_mappings"] = {"A": "IDE0:0", "B": "MD0:0", "C": "MD1:0"}

        def _responder(line: bytes):
            cmd = line.decode("latin-1").strip().upper()
            if cmd == "STAT":
                return (b"\r\nA: R/W, Space: 4412k\r\n"
                        b"B: R/W, Space: 244k\r\n"
                        b"C: R/O, Space: 0k\r\nA>")
            if cmd == "DIR A:":
                return b"\r\nA: ZPATH    COM : STAT     COM : XM       COM\r\nA>"
            if cmd == "DIR B:":
                return b"\r\nB: NO FILE\r\nA>"
            if cmd == "DIR C:":
                return b"\r\nC: XM       COM : FLASH    COM\r\nA>"
            # A bare CR reprints the prompt - which is exactly what the bridge's
            # startup probe is for, and how the scan learns an OS is running.
            return b"\r\nA>"

        self.fake.responder = _responder
        self._await_prompt()

    def _await_prompt(self, timeout: float = 2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            lines = [l.strip() for l in self.link.get_screen()["lines"] if l.strip()]
            if lines and lines[-1].startswith("A>"):
                return
            time.sleep(0.01)
        self.fail("the fake board never printed a prompt")

    def test_scan_reports_each_drive(self):
        res = self.link.scan_drives()
        self.assertTrue(res["ok"], res)
        drives = {d["drive"]: d for d in res["drives"]}
        self.assertEqual(set(drives), {"A:", "B:", "C:"})

        # A: is a CF slice carrying XM.COM - a system disk, not a ROM disk
        self.assertEqual(drives["A:"]["free_space"], "4412k")
        self.assertEqual(drives["A:"]["access"], "R/W")
        self.assertIn("ZPATH.COM", drives["A:"]["files_sample"])
        self.assertIn("System Disk", drives["A:"]["purpose"])
        self.assertNotIn("ROM Disk", drives["A:"]["purpose"])

        # B: is the RAM disk and is empty
        self.assertEqual(drives["B:"]["files_count"], 0)
        self.assertIn("RAM Disk", drives["B:"]["purpose"])

        # C: is the real ROM disk
        self.assertIn("ROM Disk", drives["C:"]["purpose"])

    def test_scan_persists_results_and_clears_progress(self):
        self.link.scan_drives()
        self.assertEqual(len(self.link.hardware_info["drives"]), 3)
        self.assertIn("last_scan_time", self.link.hardware_info)
        self.assertTrue(os.path.exists(self.link.hw_info_file))
        self.assertFalse(self.link.progress_snapshot()["scan"]["active"])

    def test_stat_runs_after_the_dirs_that_log_drives_in(self):
        """CP/M's STAT only reports drives that have been logged in, and a DIR is
        what logs one in. Asking STAT first reported '?' for every drive
        untouched since boot - invisible on a machine whose drives an earlier
        scan had already logged in, obvious on a freshly booted one.
        """
        logged_in = set()

        def _responder(line: bytes):
            cmd = line.decode("latin-1").strip().upper()
            if cmd.startswith("DIR "):
                logged_in.add(cmd[4].upper())
                return b"\r\nNO FILE\r\nA>"
            if cmd == "STAT":
                rows = "".join(f"{d}: R/W, Space: 100k\r\n" for d in sorted(logged_in))
                return b"\r\n" + rows.encode("latin-1") + b"A>"
            return b"\r\nA>"

        self.fake.responder = _responder
        res = self.link.scan_drives()

        self.assertTrue(res["ok"], res)
        for drive in res["drives"]:
            self.assertEqual(drive["free_space"], "100k",
                             f"{drive['drive']} should have been logged in before STAT ran")

    def test_slow_stat_still_yields_capacities(self):
        """STAT probes every drive before printing and took ~6s on real
        hardware. When it outruns its timeout, fall back to the screen rather
        than reporting every drive's capacity as unknown."""
        original = self.link.run_command

        def _slow_stat(command, **kwargs):
            if command.strip().upper() == "STAT":
                result = original(command, **kwargs)
                # Simulate the timeout: the text reached the screen, but
                # run_command gave up before seeing the prompt.
                return {**result, "output": "", "ok": False, "timed_out": True}
            return original(command, **kwargs)

        with patch.object(self.link, "run_command", side_effect=_slow_stat):
            res = self.link.scan_drives()

        drives = {d["drive"]: d for d in res["drives"]}
        self.assertEqual(drives["A:"]["free_space"], "4412k")
        self.assertEqual(drives["C:"]["access"], "R/O")

    def test_scan_refuses_when_not_at_a_cpm_prompt(self):
        # No OS booted: the boot loader's prompt is on screen, so there is
        # nothing to run DIR against.
        self.fake.responder = lambda line: b"\r\nBoot [H=Help]:"
        self.link.run_command("", timeout=2.0)

        res = self.link.scan_drives()
        self.assertFalse(res["ok"])
        self.assertIn("not at CP/M prompt", res["error"])


if __name__ == "__main__":
    unittest.main()
