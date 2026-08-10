import unittest
from unittest.mock import MagicMock, patch
from rc2014bridge.mcp_server import McpServer


class TestMcpServer(unittest.TestCase):
    @patch("serial.Serial")
    def test_mcp_server_initialization(self, mock_serial):
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.read.return_value = b""
        mock_serial.return_value = mock_ser

        mock_link = MagicMock()
        mock_link.hardware_info = {"version": "v3.7.0", "cpu": "Z80 @ 7.372MHz"}
        mock_link.get_screen.return_value = {"lines": ["A>DIR", "A>"]}

        server = McpServer(mock_link, host="0.0.0.0", port=8014)
        self.assertEqual(server.host, "0.0.0.0")
        self.assertEqual(server.port, 8014)
        self.assertIsNotNone(server.mcp)

    def test_send_text_append_enter(self):
        from rc2014bridge.link import SerialLink
        sl = SerialLink.__new__(SerialLink)
        sl._ser = MagicMock()
        sl._write_paced = MagicMock()
        sl._write_raw = MagicMock()
        sl.text_pacing = (1, 0.015)

        # Every form ends up as a single CR-terminated line, paced per the link's
        # configured text pacing.
        for text in ("Z 2", "Z 2\n", "Z 2\r"):
            sl.send_text(text)
            sl._write_paced.assert_called_with(b"Z 2\r", 1, 0.015)

        sl.send_text("DIR C:\\r")
        sl._write_paced.assert_called_with(b"DIR C:\r", 1, 0.015)

        sl.send_text("Y", append_enter=False)
        sl._write_raw.assert_called_with(b"Y")

    def test_send_text_honours_configured_pacing(self):
        from rc2014bridge.link import SerialLink
        sl = SerialLink.__new__(SerialLink)
        sl._ser = MagicMock()
        sl._write_paced = MagicMock()
        sl._write_raw = MagicMock()
        sl.text_pacing = (8, 0.002)   # as calibration might leave it

        sl.send_text("DIR")
        sl._write_paced.assert_called_with(b"DIR\r", 8, 0.002)


if __name__ == "__main__":
    unittest.main()
