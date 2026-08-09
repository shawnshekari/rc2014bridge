import unittest
from rc2014bridge.link import _parse_zsdos_banner, _parse_cpm_dir_output, _classify_drive_purpose


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
        # ROM drive
        rom_p = _classify_drive_purpose("C:", ["XM.COM", "FLASH.COM", "REBOOT.COM"], "MD1:0")
        self.assertIn("ROM System Disk", rom_p)

        # RAM drive
        ram_p = _classify_drive_purpose("B:", [], "MD0:0")
        self.assertIn("RAM Volatile Disk", ram_p)

        # OS System drive
        sys_p = _classify_drive_purpose("A:", ["ZPATH.COM", "STAT.COM", "PIP.COM"], "IDE0:0")
        self.assertIn("ZSDOS / CP/M System Boot Disk", sys_p)

        # User code drive
        code_p = _classify_drive_purpose("D:", ["DEMO.Z80", "MAIN.C", "SRC.PAS"], "IDE0:1")
        self.assertIn("User Programming & Source Code", code_p)

        # Empty drive
        empty_p = _classify_drive_purpose("E:", [], "IDE0:2")
        self.assertIn("Empty / Unformatted Volume", empty_p)


if __name__ == "__main__":
    unittest.main()
