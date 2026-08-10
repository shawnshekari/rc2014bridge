import unittest
import tempfile
import os
import json
from rc2014bridge.link import _parse_boot_banner, SerialLink


class TestRebootHardwareInfo(unittest.TestCase):
    def test_boot_banner_parser(self):
        sample_banner = """
        RomWBW HBIOS v3.0.1, 2020-05-30
        RC2014 Z80 @ 7.372MHz
        0 MEM W/S, 1 I/O W/S, INT MODE 1
        Z2 MMU, 512KB ROM, 512KB RAM

        HBIOS Devices:
          SIO0: IO=0x80 (Console)
          SIO1: IO=0x82
          IDE0: CompactFlash (123MB)
          MD0: RAM Disk (512KB)
          MD1: ROM Disk (384KB)

        Boot [H=Help]:
        """
        info = _parse_boot_banner(sample_banner)
        self.assertIn("v3.0.1", info["version"])
        self.assertIn("Z80 @ 7.372MHz", info["cpu"])
        self.assertIn("0 MEM W/S, 1 I/O W/S", info["wait_states"])
        self.assertIn("INT MODE 1", info["int_mode"])
        self.assertIn("Z2 MMU", info["memory"])
        self.assertEqual(len(info["devices"]), 5)
        self.assertIn("SIO0: IO=0x80 (Console)", info["devices"][0])

    def test_sc700_z180_banner(self):
        """Verbatim from a Small Computer Central SC700 (Z180), captured while
        testing against a second machine.

        The original CPU pattern enumerated Z80|Z180|eZ80 and matched nothing
        here, because the chip reports itself as "Z8S180-N" - which does not
        contain the string "Z180". This machine also prints its MMU next to the
        wait states rather than next to the RAM sizes.
        """
        banner = """RomWBW HBIOS v3.5.0, 2025-04-04
Small Computer SC700 [SCZ180_sc700_std] Z8S180-N @ 18.432MHz IO=0xC0
0 MEM W/S, 2 I/O W/S, INT MODE 2, Z180 MMU
512KB ROM, 512KB RAM, HEAP=0x20CA
ROM VERIFY: 00 00 00 00 PASS
LCD: IO=0xAA NOT PRESENT
ASCI0: IO=0xC0 ASCI W/BRG MODE=115200,8,N,1
ASCI1: IO=0xC1 ASCI W/BRG MODE=115200,8,N,1
MD: UNITS=2 ROMDISK=384KB RAMDISK=256KB
SD0: SDHC NAME=SL16G BLOCKS=0x01DACC00 SIZE=15193MB
"""
        info = _parse_boot_banner(banner)
        self.assertEqual(info["version"], "v3.5.0, 2025-04-04")
        self.assertEqual(info["cpu"], "Z8S180-N @ 18.432MHz")
        self.assertEqual(info["platform"], "Small Computer SC700")
        self.assertEqual(info["config"], "SCZ180_sc700_std")
        self.assertEqual(info["wait_states"], "0 MEM W/S, 2 I/O W/S")
        self.assertEqual(info["int_mode"], "INT MODE 2")
        self.assertEqual(info["mmu"], "Z180 MMU")
        self.assertIn("512KB ROM, 512KB RAM", info["memory"])
        # ASCI, not SIO - the Z180's on-chip serial
        self.assertTrue(any(d.startswith("ASCI0:") for d in info["devices"]))
        self.assertTrue(any("SDHC" in d for d in info["devices"]))

    def test_rc2014_z80_banner_still_parses(self):
        banner = ("RomWBW HBIOS v3.7.0-dev.13, 2026-08-08\n"
                  "RC2014 Pro [RCZ80_std] Z80 @ 7.372MHz\n"
                  "0 MEM W/S, 1 I/O W/S, INT MODE 1, Z2 MMU\n"
                  "512KB ROM, 512KB RAM\n"
                  "SIO0: IO=0x80 8440 MODE=115200,8,N,1\n")
        info = _parse_boot_banner(banner)
        self.assertEqual(info["cpu"], "Z80 @ 7.372MHz")
        self.assertEqual(info["platform"], "RC2014 Pro")
        self.assertEqual(info["config"], "RCZ80_std")
        self.assertEqual(info["mmu"], "Z2 MMU")

    def test_a_reflash_reports_the_new_version_not_the_old(self):
        """After a ROM update the board booted v3.7.0 while hardware_info still
        said v3.5.0: the boot buffer held both banners and the parser takes the
        first match. Feeding two boots must leave only the newest firmware."""
        import os
        import tempfile
        from unittest.mock import MagicMock, patch

        with patch("serial.Serial") as mock_serial:
            mock_serial.return_value.is_open = True
            mock_serial.return_value.read.return_value = b""
            link = SerialLink(
                "/dev/fake",
                hw_info_file=os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hw.json"))
        self.addCleanup(link.close)

        old = ("RomWBW HBIOS v3.5.0, 2025-04-04\r\n"
               "Small Computer SC700 [SCZ180_sc700_std] Z8S180-N @ 18.432MHz\r\n"
               "512KB ROM, 512KB RAM\r\n")
        new = ("RomWBW HBIOS v3.7.0-dev.13, 2026-08-09\r\n"
               "Small Computer SC700 [SCZ180_sc700_std] Z8S180-N @ 18.432MHz\r\n"
               "512KB ROM, 512KB RAM\r\n")

        link._update_system_state(old)
        self.assertEqual(link.hardware_info["version"], "v3.5.0, 2025-04-04")

        link._update_system_state(new)   # the board is reflashed and reboots
        self.assertEqual(link.hardware_info["version"], "v3.7.0-dev.13, 2026-08-09",
                         "the current firmware must win over a banner still in the buffer")

    def test_hardware_info_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hw_file = os.path.join(tmpdir, "test_hw.json")
            dummy_data = {
                "version": "v3.7.0",
                "cpu": "Z80 @ 10.0MHz",
                "devices": ["SIO0", "IDE0"]
            }
            with open(hw_file, "w") as f:
                json.dump(dummy_data, f)

            # Test loading existing JSON
            with open(hw_file, "r") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["version"], "v3.7.0")
            self.assertEqual(loaded["cpu"], "Z80 @ 10.0MHz")


if __name__ == "__main__":
    unittest.main()
