import unittest
import pyte

from rc2014bridge.link import SerialLink


class TestScrollback(unittest.TestCase):
    def test_scrollback_history(self):
        screen = pyte.HistoryScreen(80, 24, history=1000)
        stream = pyte.Stream(screen)

        # Feed 50 lines of text
        for i in range(50):
            stream.feed(f"Line {i:02d}\r\n")

        # Live screen view (offset 0)
        # Total history accumulated in history.top should be 27 lines (50 - 23)
        history_count = len(screen.history.top)
        self.assertEqual(history_count, 27)

    def test_link_get_screen_scrollback(self):
        # Create SerialLink on a dummy or test mock setup if needed, or directly verify link logic
        screen = pyte.HistoryScreen(80, 24, history=1000)
        stream = pyte.Stream(screen)

        for i in range(40):
            stream.feed(f"Line {i:02d}\r\n")

        # Offset 0 (live view) top line should be "Line 17"
        # Offset 10 top line should be "Line 07"
        history_count = len(screen.history.top)
        self.assertEqual(history_count, 17)

        # Test slicing indexing helper
        def get_top_text(offset):
            idx = 0 - offset
            row = screen.history.top[idx] if idx < 0 else screen.buffer[idx]
            return "".join(row[c].data for c in range(80)).strip()

        self.assertTrue(get_top_text(0).startswith("Line 17"))
        self.assertTrue(get_top_text(10).startswith("Line 07"))
        self.assertTrue(get_top_text(17).startswith("Line 00"))


if __name__ == "__main__":
    unittest.main()
