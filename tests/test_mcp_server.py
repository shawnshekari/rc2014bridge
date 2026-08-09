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


if __name__ == "__main__":
    unittest.main()
