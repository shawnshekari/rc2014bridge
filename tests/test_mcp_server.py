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

        sl.send_text("Z 2")
        sl._write_paced.assert_called_with(b"Z 2\r", chunk=1, delay=0.015)

        sl.send_text("Z 2\n")
        sl._write_paced.assert_called_with(b"Z 2\r", chunk=1, delay=0.015)

        sl.send_text("Z 2\r")
        sl._write_paced.assert_called_with(b"Z 2\r", chunk=1, delay=0.015)

        sl.send_text("DIR C:\\r")
        sl._write_paced.assert_called_with(b"DIR C:\r", chunk=1, delay=0.015)

        sl.send_text("Y", append_enter=False)
        sl._write_raw.assert_called_with(b"Y")


if __name__ == "__main__":
    unittest.main()
