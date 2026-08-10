import os
import tempfile
import unittest
from unittest.mock import patch

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


class TestGetScreenMaxLines(unittest.TestCase):
    """max_lines is how the MCP layer keeps a screen read from dumping the
    entire scrollback; 0 still means 'everything', but only on request."""

    def test_get_screen_max_lines(self):
        hw_info = os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hardware_info.json")
        with patch("serial.Serial") as mock_serial:
            mock_serial.return_value.is_open = True
            mock_serial.return_value.read.return_value = b""
            link = SerialLink(port="/dev/fake", baud=115200, hw_info_file=hw_info)
        self.addCleanup(link.close)

        with link._screen_lock:
            for i in range(50):
                link._stream.feed(f"Line {i+1}\r\n")

        # No max_lines: the live viewport, one entry per screen row
        self.assertEqual(len(link.get_screen()["lines"]), 48)

        # max_lines=0: the whole scrollback
        all_lines = [l.strip() for l in link.get_screen(max_lines=0)["lines"] if l.strip()]
        self.assertEqual(len(all_lines), 50)
        self.assertEqual(all_lines[0], "Line 1")
        self.assertEqual(all_lines[-1], "Line 50")

        # An explicit cap returns exactly that many rows, the newest ones. The
        # very last row is the blank line the cursor sits on, so the newest
        # content is just above it.
        capped = link.get_screen(max_lines=10)["lines"]
        self.assertEqual(len(capped), 10)
        newest = [l.strip() for l in capped if l.strip()]
        self.assertEqual(newest[-1], "Line 50")
        self.assertEqual(newest[0], "Line 42")


if __name__ == "__main__":
    unittest.main()
