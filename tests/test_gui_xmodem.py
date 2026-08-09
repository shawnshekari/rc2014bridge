import unittest
import threading
from rc2014bridge.display import MENU_DATA


class TestGuiXmodem(unittest.TestCase):
    def test_menu_data_structure(self):
        titles = [m["title"] for m in MENU_DATA]
        self.assertIn("File", titles)
        self.assertIn("Transfer", titles)
        self.assertIn("View", titles)

        file_actions = [item["action"] for item in MENU_DATA[0]["items"]]
        self.assertIn("PROMPT_SEND", file_actions)
        self.assertIn("PROMPT_RECEIVE", file_actions)
        self.assertIn("QUIT", file_actions)

    def test_async_xmodem_helpers_exist(self):
        from rc2014bridge.link import SerialLink
        self.assertTrue(hasattr(SerialLink, "xmodem_send_async"))
        self.assertTrue(hasattr(SerialLink, "xmodem_receive_async"))


if __name__ == "__main__":
    unittest.main()
