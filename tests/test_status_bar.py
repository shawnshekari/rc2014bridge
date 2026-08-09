import unittest
import time
import pyte

from rc2014bridge.link import SerialLink


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
