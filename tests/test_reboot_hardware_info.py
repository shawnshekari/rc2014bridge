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
