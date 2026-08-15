"""Tests for the MCP surface itself: tool registration, annotations, transport
wiring, and calling the tools the way a client does."""

import asyncio
import unittest
from unittest.mock import MagicMock

from rc2014bridge.mcp_server import McpServer, _render_hardware_doc

HW_INFO = {
    "version": "v3.7.0-dev.13, 2026-08-08",
    "cpu": "Z80 @ 7.372MHz",
    "memory": "512KB ROM, 512KB RAM",
    "devices": ["SIO0: IO=0x80 8440 MODE=115200,8,N,1"],
    "drive_mappings": {"A": "IDE0:0", "B": "MD0:0", "C": "MD1:0"},
    "drives": [{"drive": "A:", "device": "IDE0:0", "files_count": 50,
                "free_space": "4412k", "access": "R/W", "purpose": "CP/M / ZSDOS System Disk"}],
    "last_scan_time": "2026-08-09 09:19:21",
}


def _server() -> tuple[McpServer, MagicMock]:
    link = MagicMock()
    link.hardware_info = dict(HW_INFO)
    link.busy_reason.return_value = None
    link.progress_snapshot.return_value = {
        "op": "", "xmodem": {"active": False}, "scan": {"active": False}}
    return McpServer(link), link


def _text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


class TestToolRegistration(unittest.TestCase):
    def test_expected_tools_are_registered(self):
        server, _link = _server()
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        self.assertLessEqual(
            {"rc2014_run_command", "rc2014_get_screen", "rc2014_send_text",
             "rc2014_send_keys", "rc2014_wait_for", "rc2014_wait_until_ready",
             "rc2014_upload",
             "rc2014_download", "rc2014_read_text_file",
             "rc2014_scan_drives", "rc2014_survey", "rc2014_get_hardware_info",
             "rc2014_reboot"},
            names)
        self.assertNotIn("rc2014_write_text_file", names)
        self.assertNotIn("rc2014_xmodem_send", names)
        self.assertNotIn("rc2014_xmodem_receive", names)

    def test_annotations_mark_safe_and_destructive_tools(self):
        server, _link = _server()
        tools = {t.name: t.annotations for t in asyncio.run(server.mcp.list_tools())}

        for name in ("rc2014_get_screen", "rc2014_get_hardware_info",
                     "rc2014_wait_for", "rc2014_read_text_file", "rc2014_scan_drives",
                     "rc2014_survey", "rc2014_wait_until_ready"):
            self.assertTrue(tools[name].read_only_hint, f"{name} should be read-only")

        for name in ("rc2014_reboot", "rc2014_upload", "rc2014_download"):
            self.assertTrue(tools[name].destructive_hint, f"{name} should be destructive")
            self.assertFalse(tools[name].read_only_hint, f"{name} is not read-only")

    def test_context_is_not_exposed_as_a_tool_parameter(self):
        server, _link = _server()
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        params = (tools["rc2014_upload"].input_schema.get("properties") or {}).keys()
        self.assertNotIn("ctx", params)
        self.assertEqual(set(params),
                         {"name", "drive", "user", "content", "binary", "overwrite"})


class TestToolCalls(unittest.TestCase):
    def test_run_command_delegates_to_the_link(self):
        server, link = _server()
        link.run_command.return_value = {"ok": True, "output": "A: TEST TXT", "prompt": "A>"}

        result = asyncio.run(server.mcp.call_tool("rc2014_run_command", {"command": "DIR A:"}))
        link.run_command.assert_called_once_with("DIR A:", timeout=15.0)
        self.assertIn("A: TEST TXT", _text(result))

    def test_get_screen_prefixes_a_state_header(self):
        server, link = _server()
        link.get_screen.return_value = {
            "lines": ["A>DIR", "A: TEST TXT", "A>", "", ""],
            "system_state": "cpm", "last_prompt": "A>", "mode": "terminal",
            "current_op": "", "xmodem_progress": {"active": False},
        }

        out = _text(asyncio.run(server.mcp.call_tool("rc2014_get_screen", {"max_lines": 40})))
        lines = out.splitlines()
        self.assertIn("state=cpm", lines[0])
        self.assertIn("operation=idle", lines[0])
        self.assertEqual(lines[-1], "A>", "trailing blank rows are trimmed")
        link.get_screen.assert_called_once_with(max_lines=40)

    def test_get_screen_default_is_a_window_not_the_whole_scrollback(self):
        server, link = _server()
        link.get_screen.return_value = {"lines": ["A>"], "system_state": "cpm",
                                        "last_prompt": "A>", "mode": "terminal",
                                        "current_op": "", "xmodem_progress": {}}
        asyncio.run(server.mcp.call_tool("rc2014_get_screen", {}))
        link.get_screen.assert_called_once_with(max_lines=40)

    def test_send_text_refuses_while_a_transfer_owns_the_wire(self):
        server, link = _server()
        link.busy_reason.return_value = "XMODEM SEND in progress"

        out = _text(asyncio.run(server.mcp.call_tool("rc2014_send_text", {"text": "DIR"})))
        self.assertIn("Not sent", out)
        self.assertIn("XMODEM SEND", out)
        link.send_text.assert_not_called()

    def test_send_keys_is_not_gated_on_busy(self):
        # send_text refuses mid-transfer; send_keys must not, since cancelling a
        # transfer is precisely what it is for.
        server, link = _server()
        link.busy_reason.return_value = "XMODEM SEND in progress"
        link.send_keys.return_value = {"ok": True, "keys": "^X<PAUSE>^X", "hex": "18 18"}

        asyncio.run(server.mcp.call_tool("rc2014_send_keys", {"keys": "^X<PAUSE>^X"}))
        link.send_keys.assert_called_once_with("^X<PAUSE>^X")

    def test_send_keys_reports_a_bad_mnemonic_as_an_error(self):
        server, link = _server()
        link.send_keys.side_effect = ValueError("unknown key name <BOGUS>")

        out = _text(asyncio.run(server.mcp.call_tool("rc2014_send_keys", {"keys": "<BOGUS>"})))
        self.assertIn("BOGUS", out)
        self.assertIn("false", out.lower())

    def test_upload_forwards_defaults(self):
        server, link = _server()
        link.upload.return_value = {"ok": True, "bytes_raw": 12, "verified": True}

        asyncio.run(server.mcp.call_tool(
            "rc2014_upload", {"name": "X.COM", "drive": "B", "content": "hi"}))
        link.upload.assert_called_once_with("X.COM", "B", user=0, content="hi",
                                            binary=False, overwrite=False)

    def test_upload_forwards_all_parameters(self):
        server, link = _server()
        link.upload.return_value = {"ok": True}

        asyncio.run(server.mcp.call_tool(
            "rc2014_upload", {"name": "LEDSHOW.COM", "drive": "B", "user": 1,
                              "content": "aGVsbG8=", "binary": True, "overwrite": True}))
        link.upload.assert_called_once_with("LEDSHOW.COM", "B", user=1, content="aGVsbG8=",
                                            binary=True, overwrite=True)

    def test_download_forwards_defaults(self):
        server, link = _server()
        link.download.return_value = {"ok": True, "content": "hi"}

        asyncio.run(server.mcp.call_tool(
            "rc2014_download", {"name": "X.COM", "drive": "B"}))
        link.download.assert_called_once_with("X.COM", "B", user=0, binary=False)


class TestHardwareDoc(unittest.TestCase):
    def test_doc_is_rendered_from_captured_info(self):
        doc = _render_hardware_doc(HW_INFO)
        self.assertIn("v3.7.0-dev.13", doc)
        self.assertIn("Z80 @ 7.372MHz", doc)
        self.assertIn("`C:` -> `MD1:0`", doc)
        self.assertIn("CP/M / ZSDOS System Disk", doc)
        self.assertIn("Serial pacing", doc)

    def test_doc_says_so_rather_than_inventing_specs(self):
        doc = _render_hardware_doc({})
        self.assertIn("not captured yet", doc)
        self.assertNotIn("7.372MHz", doc)
        self.assertIn("No volumes catalogued yet", doc)


class TestTransports(unittest.TestCase):
    def test_only_the_stateless_mcp_endpoint_is_served(self):
        server, _link = _server()
        paths = [getattr(r, "path", None) for r in server._build_app().router.routes]
        self.assertIn("/mcp", paths)
        self.assertNotIn("/sse", paths)
        self.assertEqual(server.endpoints(), ["http://0.0.0.0:8014/mcp"])


if __name__ == "__main__":
    unittest.main()
