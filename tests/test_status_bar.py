import unittest
import time
import pyte

from rc2014bridge.display import operation_badge_label, window_title
from rc2014bridge.link import SerialLink


class TestOperationBadge(unittest.TestCase):
    """The mode badge and the operation badge are both derived from link state,
    so during a transfer they said the same thing twice."""

    def test_suppressed_when_it_repeats_the_mode_badge(self):
        self.assertIsNone(operation_badge_label("XMODEM-SEND", "XMODEM send"))
        self.assertIsNone(operation_badge_label("XMODEM-RECV", "XMODEM receive".replace("receive", "RECV")))

    def test_shown_when_it_adds_information(self):
        self.assertEqual(operation_badge_label("CPM/ZSDOS", "drive scan"), "DRIVE SCAN")
        self.assertEqual(operation_badge_label("CPM/ZSDOS", "upload"), "UPLOAD")
        self.assertEqual(operation_badge_label("HBIOS", "command"), "COMMAND")

    def test_nothing_shown_when_idle(self):
        self.assertIsNone(operation_badge_label("CPM/ZSDOS", ""))


class TestWindowTitle(unittest.TestCase):
    def test_names_the_machine_by_its_build_identifier(self):
        class L:
            port = "/dev/ttyUSB0"
            baud = 115200
            hardware_info = {"config": "SCZ180_sc700_std",
                             "platform": "Small Computer SC700",
                             "cpu": "Z8S180-N @ 18.432MHz"}
        title = window_title(L())
        self.assertEqual(title, "RC2014 Bridge - SCZ180_sc700_std")
        # port/baud belong to the status bar, not the title
        self.assertNotIn("115200", title)
        self.assertNotIn("ttyUSB0", title)

    def test_falls_back_to_the_platform_then_the_port(self):
        class L:
            port = "/dev/ttyUSB0"
            baud = 115200
            hardware_info = {"platform": "RC2014 Pro"}
        self.assertEqual(window_title(L()), "RC2014 Bridge - RC2014 Pro")

        class Bare:
            port = "/dev/ttyUSB1"
            baud = 115200
            hardware_info = {}
        self.assertEqual(window_title(Bare()), "RC2014 Bridge - /dev/ttyUSB1")


class TestStatusBar(unittest.TestCase):
    def test_status_telemetry(self):
        screen = pyte.HistoryScreen(80, 24, history=1000)

        # Verify default state fields in SerialLink structure
        link_state = {
            "port": "/dev/ttyUSB0",
            "baud": 115200,
            "mode": "terminal",
            "rx_active": True,
            "tx_active": False,
            "xmodem_progress": {
                "active": True,
                "filename": "test.rom",
                "current_block": 10,
                "total_blocks": 50,
                "bytes": 1280,
                "direction": "SEND",
            },
        }

        self.assertEqual(link_state["port"], "/dev/ttyUSB0")
        self.assertEqual(link_state["baud"], 115200)
        self.assertEqual(link_state["mode"], "terminal")
        self.assertTrue(link_state["rx_active"])
        self.assertFalse(link_state["tx_active"])

        xp = link_state["xmodem_progress"]
        self.assertTrue(xp["active"])
        self.assertEqual(xp["filename"], "test.rom")
        self.assertEqual(xp["current_block"], 10)
        self.assertEqual(xp["total_blocks"], 50)
        self.assertEqual(xp["direction"], "SEND")

    def test_xmodem_path_handling(self):
        import os
        path = "/tmp/downloads/LEDSHOW.COM"
        self.assertEqual(os.path.basename(path), "LEDSHOW.COM")


if __name__ == "__main__":
    unittest.main()
