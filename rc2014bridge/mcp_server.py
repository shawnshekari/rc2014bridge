"""
Model Context Protocol (MCP) Server for RC2014 Bridge.
Exposes MCP Tools, Resources, and Prompts over HTTP/SSE (bound to 0.0.0.0:8014 by default)
and stdio for integration with LLMs (Claude Desktop, Cursor, Ollama agents, Antigravity, etc.).
"""

import json
import logging
import threading
from typing import Optional

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
import uvicorn

logger = logging.getLogger("rc2014bridge.mcp")

SERVER_INSTRUCTIONS = """
You are connected to an RC2014 Z80 Vintage Modular Computer running RomWBW HBIOS, ZSDOS, and CP/M 2.2.

Key System Characteristics:
1. Architecture: Zilog Z80 CPU @ 7.372MHz with Z2 MMU, 512KB ROM, 512KB RAM, 8440 SIO dual-channel serial controller, and IDE CompactFlash storage.
2. Operating Systems:
   - RomWBW HBIOS: System ROM bootloader and diagnostic console (`HBIOS>`, `Boot [H=Help]:`).
   - ZSDOS / CP/M 2.2: Operating system prompts like `A>`, `B>`, `C>`, `D:`.
3. Commands & Utilities:
   - `DIR <drive>:`: Lists files (8.3 uppercase filenames like `MBASIC.COM`, `STAT.COM`, `PIP.COM`, `DEMO.Z80`).
   - `STAT`: Displays drive access mode (`R/W`, `R/O`) and remaining free space capacity (e.g. `4412k free`).
   - `TYPE <file>`: Displays plain text file content.
   - `MBASIC`: Launches Microsoft BASIC interpreter.
4. Transmission Pacing: All text sent to the RC2014 via `rc2014_send_text` is automatically paced with 1-byte chunking and 15ms delays to prevent Z80 SIO UART buffer overruns.
"""

HARDWARE_OVERVIEW_DOC = """
# RC2014 Hardware Architecture Overview

The RC2014 is a modular 8-bit retro computer built around the Zilog Z80 processor and the RomWBW operating system framework.

## Core Modules & Specifications
- **CPU**: Zilog Z80 CPU running at 7.3728 MHz.
- **Memory**: Z2 MMU managing 512 KB Flash ROM and 512 KB Static RAM partitioned into bank-switched slices.
- **Serial Communications**: Dual SIO/0 (8440) UART running at 115,200 baud, 8 data bits, no parity, 1 stop bit (8N1).
- **Disk Drives**:
  - `MD0:0` (Drive `B:`): High-speed RAM Volatile Disk (256 KB).
  - `MD1:0` (Drive `C:`): Built-in Read-Only ROM Disk (384 KB) containing system utilities (`XM.COM`, `FLASH.COM`, `REBOOT.COM`).
  - `IDE0:0`..`IDE0:7` (Drives `A:`, `D:`, `E:`, `F:`, etc.): IDE CompactFlash storage slices (123 MB total).

## Communications & Pacing
Because the Z80 CPU services display and disk interrupts at 7.372 MHz, command strings transmitted at 115,200 baud must be paced 1 character at a time with a 15ms inter-character delay to avoid UART buffer overrun.
"""

CPM_GUIDE_DOC = """
# CP/M 2.2 & ZSDOS Quick Reference Guide

## Filename Conventions
Files use standard CP/M 8.3 uppercase format (e.g. `MYPROG.COM`, `SRC.Z80`, `README.TXT`). Drive letters range from `A:` to `J:`.

## Essential Built-in & Utility Commands
- `DIR <drive>:` : List directory contents for specified drive.
- `STAT`         : Display drive access modes (R/W vs R/O) and remaining free space capacity.
- `STAT <file>`  : Display file size and record allocation.
- `TYPE <file>`  : Print ASCII text file contents to terminal.
- `PIP <dest>=<src>` : File copy utility (e.g. `PIP B:=A:MYPROG.COM`).
- `MBASIC`       : Microsoft BASIC-80 interpreter (`SYSTEM` to exit).
- `ED <file>`    : CP/M context line editor.
- `XM <file>`    : XMODEM file transfer utility (`XM R <file>` to receive, `XM S <file>` to send).
- `REBOOT`       : Perform clean hardware reboot back to RomWBW boot menu.
"""


class McpServer:
    def __init__(self, link, host: str = "0.0.0.0", port: int = 8014):
        self.link = link
        self.host = host
        self.port = port
        self._server_thread: Optional[threading.Thread] = None

        self.mcp = MCPServer(
            "RC2014 Bridge",
            instructions=SERVER_INSTRUCTIONS,
        )

        self._register_resources()
        self._register_prompts()
        self._register_tools()

    def _register_resources(self):
        @self.mcp.resource("rc2014://docs/hardware-overview")
        def get_hardware_overview_doc() -> str:
            """Detailed documentation on RC2014 Z80 hardware architecture, memory map, and serial communications."""
            logger.info("MCP Resource requested: rc2014://docs/hardware-overview")
            return HARDWARE_OVERVIEW_DOC

        @self.mcp.resource("rc2014://docs/cpm-guide")
        def get_cpm_guide_doc() -> str:
            """Quick reference manual for CP/M 2.2 and ZSDOS commands, utilities, and filename conventions."""
            logger.info("MCP Resource requested: rc2014://docs/cpm-guide")
            return CPM_GUIDE_DOC

        @self.mcp.resource("rc2014://system/hardware-info")
        def get_hardware_info_resource() -> str:
            """Live JSON snapshot of connected RC2014 hardware specs, ZSDOS specs, and drive inventory."""
            logger.info("MCP Resource requested: rc2014://system/hardware-info")
            info = getattr(self.link, "hardware_info", {})
            return json.dumps(info, indent=2)

    def _register_prompts(self):
        @self.mcp.prompt("rc2014_assistant_instructions")
        def rc2014_assistant_prompt() -> str:
            """Prompt template configuring the LLM as an expert RC2014 retro-computing assistant."""
            logger.info("MCP Prompt requested: rc2014_assistant_instructions")
            return (
                "You are an expert retro-computing assistant operating an RC2014 Z80 vintage computer via the rc2014bridge MCP toolset.\n"
                "Always format CP/M filenames in uppercase 8.3 format. Use `rc2014_send_text` to execute commands, "
                "`rc2014_get_screen` to inspect terminal output, `rc2014_scan_drives` to catalog volumes, and `rc2014_get_hardware_info` for hardware specs."
            )

    def _register_tools(self):
        link = self.link

        @self.mcp.tool()
        def rc2014_send_text(text: str) -> str:
            """Send a text string or command to the RC2014 serial terminal (e.g. 'DIR A:\\r', 'STAT\\r'). Automatically paced with 15ms delays."""
            logger.info("MCP Tool called: rc2014_send_text(text=%r)", text)
            link.send_text(text)
            return f"Sent text to RC2014: {text!r}"

        @self.mcp.tool()
        def rc2014_get_screen(max_lines: int = 0) -> str:
            """Get rendered 80x48 screen lines and terminal scrollback history. Pass max_lines=0 for all history lines."""
            logger.info("MCP Tool called: rc2014_get_screen(max_lines=%d)", max_lines)
            sc = link.get_screen(max_lines=max_lines)
            lines = sc.get("lines", [])
            return "\n".join(lines)

        @self.mcp.tool()
        def rc2014_wait_for(pattern: str, timeout: float = 10.0) -> dict:
            """Block until a regex pattern (e.g. 'A>', 'HBIOS>', 'Boot:') appears on the screen within timeout seconds."""
            logger.info("MCP Tool called: rc2014_wait_for(pattern=%r, timeout=%s)", pattern, timeout)
            return link.wait_for(pattern, timeout=timeout)

        @self.mcp.tool()
        def rc2014_scan_drives() -> dict:
            """Scan CP/M drives A: through J:, query STAT free space capacity, and classify disk volume purposes."""
            logger.info("MCP Tool called: rc2014_scan_drives()")
            return link.scan_drives()

        @self.mcp.tool()
        def rc2014_get_hardware_info() -> dict:
            """Get RomWBW version, Z80 CPU clock speed, MMU RAM/ROM sizes, SIO ports, ZSDOS specs, and drive inventory."""
            logger.info("MCP Tool called: rc2014_get_hardware_info()")
            return getattr(link, "hardware_info", {})

        @self.mcp.tool()
        def rc2014_reboot() -> str:
            """Send hardware reboot command to the RC2014 (C:REBOOT /C in CP/M mode, R in HBIOS mode)."""
            logger.info("MCP Tool called: rc2014_reboot()")
            link.reboot()
            return "Reboot command issued to RC2014"

        @self.mcp.tool()
        def rc2014_xmodem_send(path: str) -> dict:
            """Send a local file to the RC2014 using XMODEM CRC protocol."""
            logger.info("MCP Tool called: rc2014_xmodem_send(path=%r)", path)
            return link.xmodem_send(path)

        @self.mcp.tool()
        def rc2014_xmodem_receive(path: str) -> dict:
            """Receive a file from the RC2014 using XMODEM CRC protocol."""
            logger.info("MCP Tool called: rc2014_xmodem_receive(path=%r)", path)
            return link.xmodem_receive(path)

    def start(self):
        def _run_server():
            try:
                logger.info("Starting MCP HTTP/SSE server on %s:%d", self.host, self.port)
                sec_settings = TransportSecuritySettings(
                    enable_dns_rebinding_protection=False,
                    allowed_hosts=["*"],
                    allowed_origins=["*"],
                )
                sse_app = self.mcp.sse_app(host=self.host, transport_security=sec_settings)
                sse_app.add_middleware(
                    CORSMiddleware,
                    allow_origins=["*"],
                    allow_credentials=True,
                    allow_methods=["*"],
                    allow_headers=["*"],
                )
                uvicorn.run(sse_app, host=self.host, port=self.port, log_level="info")
            except Exception as e:
                logger.exception("MCP Server error: %s", e)

        self._server_thread = threading.Thread(target=_run_server, daemon=True)
        self._server_thread.start()

    def stop(self):
        pass
