import unittest
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from rc2014bridge.mcp_server import McpServer

_ACCEPT = "application/json, text/event-stream"
_PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
_CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"


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


class TestStatelessHttp(unittest.TestCase):
    """Real HTTP-level coverage of the 2026-07-28 stateless transport.

    Guards against `stateless_http=True` silently regressing on a future
    `mcp` SDK bump - the whole point of removing the legacy stateful path
    was that no request, of any protocol version, ever gets an
    Mcp-Session-Id back.
    """

    def _client(self) -> TestClient:
        link = MagicMock()
        link.hardware_info = {"version": "v3.7.0-dev.13", "cpu": "Z80 @ 7.372MHz"}
        server = McpServer(link, host="0.0.0.0", port=8014)
        return TestClient(server._build_app())

    def test_modern_client_gets_a_stateless_json_reply(self):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "rc2014_get_hardware_info",
                "arguments": {},
                "_meta": {
                    _PROTOCOL_VERSION_META_KEY: "2026-07-28",
                    _CLIENT_CAPABILITIES_META_KEY: {},
                },
            },
        }
        with self._client() as client:
            resp = client.post(
                "/mcp",
                json=body,
                headers={
                    "Accept": _ACCEPT,
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "rc2014_get_hardware_info",
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.headers.get("mcp-session-id"))
        payload = resp.json()
        self.assertEqual(payload["id"], 1)
        self.assertIn("result", payload)

    def test_legacy_protocol_client_gets_no_session_id_either(self):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "legacy-test-client", "version": "0.0.1"},
            },
        }
        with self._client() as client:
            resp = client.post("/mcp", json=body, headers={"Accept": _ACCEPT})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.headers.get("mcp-session-id"))


if __name__ == "__main__":
    unittest.main()
