import unittest
from unittest.mock import MagicMock, patch
from rc2014bridge.link import SerialLink


class TestApiScrollback(unittest.TestCase):
    @patch("serial.Serial")
    def test_get_screen_max_lines(self, mock_serial):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.read.return_value = b""
        mock_serial.return_value = mock_ser

        link = SerialLink(port="/dev/null", baud=115200)

        with link._screen_lock:
            for i in range(50):
                link._stream.feed(f"Line {i+1}\r\n")

        # 1. Default call (no max_lines) returns rows=24 lines
        sc_default = link.get_screen()
        self.assertEqual(len(sc_default["lines"]), 24)

        # 2. Call with max_lines=0 (all history) returns > 24 lines
        sc_all = link.get_screen(max_lines=0)
        non_empty = [l.strip() for l in sc_all["lines"] if l.strip()]
        self.assertEqual(len(non_empty), 50)
        self.assertEqual(non_empty[0], "Line 1")
        self.assertEqual(non_empty[-1], "Line 50")

        # 3. Call with max_lines=10 returns 10 lines
        sc_10 = link.get_screen(max_lines=10)
        self.assertEqual(len(sc_10["lines"]), 10)

        link.close()


if __name__ == "__main__":
    unittest.main()
