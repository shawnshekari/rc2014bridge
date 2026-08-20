"""
Model Context Protocol (MCP) server for the RC2014 bridge - the only control
surface. Serves the stateless streamable HTTP transport (protocol 2026-07-28
and later) at /mcp from one uvicorn instance, bound to 0.0.0.0:8014 by
default. Every request is self-contained - no initialize handshake, no
Mcp-Session-Id, no persistent connection - so there is no legacy SSE
transport to maintain either.

The tool surface is built around rc2014_run_command: one call sends a command,
waits for the CP/M prompt to come back, and returns just that command's output.
The lower-level rc2014_send_text / rc2014_get_screen pair remains for things
that never return to a shell prompt - interactive programs, the HBIOS boot
menu, answering a Y/N question mid-command.
"""

import functools
import json
import logging
import threading
from typing import Optional

import anyio
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.middleware.cors import CORSMiddleware

logger = logging.getLogger("rc2014bridge.mcp")

SERVER_INSTRUCTIONS = """
You are connected to a Z80 retro computer running RomWBW (HBIOS) with ZSDOS /
CP/M 2.2, over a serial console this bridge owns permanently. A human may be
watching the same session in a terminal window, so what you type is visible.

How to drive it:
- `rc2014_run_command` is the tool to reach for. It sends a command, waits for
  the prompt to return, and gives back only that command's output - no polling,
  no guessing when it finished.
- `rc2014_send_text` + `rc2014_get_screen` are for anything that does NOT
  return to a shell prompt: interactive programs (MBASIC, ED), the HBIOS
  `Boot [H=Help]:` menu, or answering a prompt mid-command. `run_command` will
  time out on those.
- `rc2014_send_keys` sends control characters and is the way to interrupt
  something - `^C` to break out of a program, `^X<PAUSE>^X` to abort a stuck
  XM transfer. It is the one tool allowed to run while something else owns the
  wire.
- `rc2014_upload` / `rc2014_download` handle file transfer end to end - content
  in, content out. They zip, run XM, transfer, and verify; do not drive XM by
  hand. `binary=false` (the default) handles text CRLF/EOF conversion for you.
- `rc2014_read_text_file` is a cheaper alternative for a quick peek at a text
  file, but not byte-exact (TYPE expands tabs and stops at 0x1A) - use
  `rc2014_download` when the exact bytes matter.

What to expect:
- Filenames are CP/M 8.3 uppercase (`MBASIC.COM`, `DEMO.Z80`). Drives are `A:`
  through `P:`; programs are run by bare name, without the `.COM`.
- Only one operation can own the serial wire at a time. A tool returning
  `{"busy": true}` means wait and retry, not that the machine is broken.
- Read `rc2014://docs/hardware-overview` for this specific machine's captured
  configuration rather than assuming a standard build. `rc2014_get_hardware_info`
  returns the same data as JSON.
- Text sent to the board is paced one byte at a time with a 15ms gap, because
  an unpaced burst at 115200 baud overruns the UART while the Z80 is busy.
"""

CPM_GUIDE_DOC = """
# CP/M 2.2 & ZSDOS Quick Reference

## Filenames
CP/M 8.3, uppercase (`MYPROG.COM`, `SRC.Z80`, `README.TXT`). A drive prefix is
`X:` (`B:HELLO.TXT`). Run a program by bare name - `MBASIC`, not `MBASIC.COM`.

## Built-ins and common utilities
- `DIR <drive>:`     List directory contents.
- `DIR <file>`       Check whether one file exists.
- `STAT`             Drive access mode (R/W vs R/O) and free space.
- `STAT <file>`      File size and record allocation.
- `TYPE <file>`      Print a text file to the console.
- `ERA <file>`       Erase (supports `*` wildcards - destructive).
- `REN <new>=<old>`  Rename.
- `PIP <dest>=<src>` Copy (e.g. `PIP B:=A:MYPROG.COM`).
- `XM R|S <file>`    XMODEM transfer. Prefer the upload/download tools, which
                     drive this correctly and verify the result.
- `MBASIC`           Microsoft BASIC-80. `SYSTEM` returns to CP/M.
- `ED <file>`        CP/M line editor (interactive - not for run_command).
- `SUBMIT <file>`    Run a `.SUB` batch file.
- `ASSIGN`           Show or change drive-to-device mappings.
- `REBOOT`           Clean restart back to the RomWBW boot loader.

## The HBIOS boot loader (`Boot [H=Help]:`)

Single keystrokes only - it acts on one key, so sending a word runs its first
letter and then returns a prompt, which looks like success. Use
`rc2014_send_text` with one character, never `rc2014_run_command`.

- `H` - help. Lists `L D R W I V` and `<unit>[.<slice>]` to boot a disk.
- `L` - **list ROM applications**, and the only route to several of them. The
  XModem Flash Updater is *not* on the help menu: it is `X` from this list.
  Confirmed on an SC700 running RomWBW v3.5.0, which offers:
  `M` Monitor, `C` CP/M 2.2, `Z` Z-System, `B` BASIC, `T` Tasty BASIC,
  `F` Forth, `P` Play a Game, `N` Network Boot, `X` XModem Flash Updater,
  `U` User App.
- `D` - device inventory: console devices and disk units with capacities. The
  quickest way to learn which unit number to boot.
- `R` - reboot.
- Booting is by unit number from `D`, optionally `unit.slice` - and it differs
  per machine (unit 2 on one RC2014, unit 4 on an SC700). Never assume.

After booting, call `rc2014_wait_until_ready`: the boot profile keeps running
programs after the prompt appears, and commands sent during that window are lost.

## Things that surprise people
- A `.COM` file is loaded at 0x0100 in the TPA; TPA size limits program size.
- The RAM disk (MD0) survives a *reset* - it is static RAM, and only a real
  power cycle clears it. Don't assume a reset gives a clean slate.
- The ROM disk (MD1) is genuinely read-only; writes to it fail.
- Text files conventionally end with a 0x1A (Ctrl-Z) EOF marker, and `TYPE`
  stops there.
"""


def _render_hardware_doc(info: dict) -> str:
    """Render the hardware overview from what this bridge actually captured.

    Deliberately not a hardcoded description: the boot banner is the truth for
    whichever board is plugged in, and a fixed doc goes stale or describes a
    different machine.
    """
    unknown = "(not captured yet - reboot the machine to capture its boot banner)"
    out = [
        "# Connected System Overview",
        "",
        "Captured from this machine's RomWBW boot banner by the bridge.",
        "",
        "## Core",
        f"- **RomWBW version**: {info.get('version') or unknown}",
    ]
    if info.get("platform"):
        out.append(f"- **Board**: {info['platform']}"
                   + (f" (build `{info['config']}`)" if info.get("config") else ""))
    out += [
        f"- **CPU**: {info.get('cpu') or unknown}",
        f"- **Memory**: {info.get('memory') or unknown}"
        + (f", {info['mmu']}" if info.get("mmu") else ""),
        f"- **Wait states**: {info.get('wait_states') or unknown}",
        f"- **Interrupt mode**: {info.get('int_mode') or unknown}",
    ]
    if info.get("zsdos_version"):
        out.append(f"- **Operating system**: {info['zsdos_version']}")
    if info.get("cbios_version"):
        out.append(f"- **CBIOS**: {info['cbios_version']}")
    if info.get("tpa"):
        out.append(f"- **TPA (space for programs)**: {info['tpa']}")
    if info.get("timestamp"):
        out.append(f"- **Banner captured**: {info['timestamp']}")

    devices = info.get("devices") or []
    if devices:
        out += ["", "## HBIOS device inventory", ""]
        out += [f"- `{d}`" for d in devices]

    mappings = info.get("drive_mappings") or {}
    if mappings:
        out += ["", "## Drive to device mapping", ""]
        out += [f"- `{letter}:` -> `{device}`" for letter, device in mappings.items()]

    drives = info.get("drives") or []
    if drives:
        out += ["", f"## Catalogued volumes (last scan: {info.get('last_scan_time', 'unknown')})", "",
                "| Drive | Device | Files | Free | Access | Purpose |",
                "|---|---|---|---|---|---|"]
        for d in drives:
            out.append(
                f"| {d.get('drive','')} | {d.get('device','')} | {d.get('files_count','?')} "
                f"| {d.get('free_space','?')} | {d.get('access','?')} | {d.get('purpose','')} |"
            )
        out += ["", "Run `rc2014_scan_drives` to refresh this table."]
    else:
        out += ["", "No volumes catalogued yet - run `rc2014_scan_drives`."]

    survey = info.get("survey") or {}
    if survey:
        out += ["", f"## SURVEY report ({survey.get('timestamp', 'unknown')})", ""]
        if survey.get("tpa_bytes"):
            out.append(f"- **TPA**: {survey['tpa_bytes']} bytes available to programs")
        if survey.get("ram_bytes") is not None:
            out.append(f"- **RAM / ROM in the 64K map**: {survey.get('ram_bytes')} / "
                       f"{survey.get('rom_bytes')} bytes")
        for label, key in (("BIOS", "bios_addr"), ("BDOS", "bdos_addr"), ("iobyte", "iobyte")):
            if survey.get(key):
                out.append(f"- **{label}**: 0x{survey[key]}")
        if survey.get("io_ports"):
            out.append(f"- **Active I/O ports** ({survey.get('io_ports_active', '?')}): "
                       + " ".join(f"0x{p}" for p in survey["io_ports"]))
        if survey.get("drives"):
            out += ["",
                    "Per-drive totals below count **every CP/M user area**, unlike the",
                    "volume table above, which reflects only the user area that was current",
                    "during the scan.", "",
                    "| Drive | Used | Files | Free |", "|---|---|---|---|"]
            for letter, d in survey["drives"].items():
                out.append(f"| {letter}: | {d.get('used','?')} | {d.get('files','?')} "
                           f"| {d.get('free','?')} |")

    out += [
        "",
        "## Serial pacing",
        "",
        "The console runs at 115200 8N1. The bridge paces outbound text one byte",
        "at a time with a ~15ms gap: an unpaced burst overruns the UART's receive",
        "buffer while the Z80 is servicing interrupts, and shows up as dropped",
        "characters. XMODEM blocks are paced in 8-byte chunks for the same reason.",
    ]
    return "\n".join(out)


class McpServer:
    def __init__(self, link, host: str = "0.0.0.0", port: int = 8014):
        self.link = link
        self.host = host
        self.port = port
        self._server_thread: Optional[threading.Thread] = None
        self._uvicorn: Optional[uvicorn.Server] = None

        self.mcp = MCPServer(
            "RC2014 Bridge",
            instructions=SERVER_INSTRUCTIONS,
        )

        self._register_resources()
        self._register_prompts()
        self._register_tools()

    # ------------------------------------------------------------------
    # resources & prompts
    # ------------------------------------------------------------------
    def _register_resources(self):
        @self.mcp.resource("rc2014://docs/hardware-overview")
        def get_hardware_overview_doc() -> str:
            """This machine's captured hardware configuration: CPU, memory, HBIOS devices, drives, serial pacing."""
            logger.info("MCP Resource requested: rc2014://docs/hardware-overview")
            return _render_hardware_doc(getattr(self.link, "hardware_info", {}) or {})

        @self.mcp.resource("rc2014://docs/cpm-guide")
        def get_cpm_guide_doc() -> str:
            """Quick reference for CP/M 2.2 and ZSDOS commands, utilities, and filename conventions."""
            logger.info("MCP Resource requested: rc2014://docs/cpm-guide")
            return CPM_GUIDE_DOC

        @self.mcp.resource("rc2014://system/hardware-info")
        def get_hardware_info_resource() -> str:
            """Live JSON snapshot of captured hardware specs, OS versions, and drive inventory."""
            logger.info("MCP Resource requested: rc2014://system/hardware-info")
            return json.dumps(getattr(self.link, "hardware_info", {}), indent=2)

    def _register_prompts(self):
        @self.mcp.prompt("rc2014_assistant_instructions")
        def rc2014_assistant_prompt() -> str:
            """Prompt template configuring the model as an RC2014 retro-computing operator."""
            logger.info("MCP Prompt requested: rc2014_assistant_instructions")
            return (
                "You are operating a Z80 retro computer running RomWBW and CP/M through the "
                "rc2014bridge MCP tools.\n\n"
                "Use `rc2014_run_command` for anything that returns to a shell prompt - it sends "
                "the command and gives you back its output in one call. Fall back to "
                "`rc2014_send_text` plus `rc2014_get_screen` only for interactive programs and "
                "the HBIOS boot menu. Use `rc2014_upload`/`rc2014_download` for file transfer "
                "rather than driving XM yourself.\n\n"
                "Read `rc2014://docs/hardware-overview` before assuming anything about this "
                "machine's drives or memory. Write CP/M filenames as uppercase 8.3. If a tool "
                "returns `busy`, another operation owns the serial wire - wait and retry."
            )

    # ------------------------------------------------------------------
    # progress plumbing
    # ------------------------------------------------------------------
    async def _with_progress(self, ctx: Context, fn, *args, **kwargs):
        """Run a blocking link operation in a worker thread, streaming the
        link's own progress state to the client while it runs."""

        async def _watch():
            try:
                while True:
                    await anyio.sleep(0.5)
                    snap = self.link.progress_snapshot()
                    xmodem, scan = snap["xmodem"], snap["scan"]
                    if xmodem.get("active") and xmodem.get("total_blocks"):
                        await ctx.report_progress(
                            xmodem["current_block"], xmodem["total_blocks"],
                            f"{xmodem.get('direction', 'transfer')} {xmodem.get('filename', '')}: "
                            f"block {xmodem['current_block']}/{xmodem['total_blocks']}",
                        )
                    elif scan.get("active") and scan.get("total"):
                        await ctx.report_progress(
                            scan["index"], scan["total"], f"scanning {scan.get('drive', '')}",
                        )
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:  # noqa: BLE001 - progress is best-effort
                logger.debug("progress reporting stopped", exc_info=True)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch)
            try:
                return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
            finally:
                tg.cancel_scope.cancel()

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def _register_tools(self):
        link = self.link

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Run a CP/M command", read_only_hint=False, open_world_hint=True))
        def rc2014_run_command(command: str, timeout: float = 15.0) -> dict:
            """Run one command and return its output. THE tool to use for CP/M work.

            Sends `command`, waits for the prompt to return, and returns only what
            that command printed (the board's echo and the trailing prompt are
            stripped). No polling needed. Examples: 'DIR A:', 'STAT', 'TYPE B:README.TXT'.

            Times out on anything that does not come back to a shell prompt -
            interactive programs like MBASIC or ED, and the HBIOS boot menu. Use
            rc2014_send_text + rc2014_get_screen for those.
            """
            logger.info("MCP Tool called: rc2014_run_command(command=%r, timeout=%s)", command, timeout)
            return link.run_command(command, timeout=timeout)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Read the screen", read_only_hint=True, idempotent_hint=True))
        def rc2014_get_screen(max_lines: int = 40) -> str:
            """Read the terminal screen, newest content last, prefixed with a state line.

            Use for interactive programs, the boot menu, or to see what a human has
            been doing - rc2014_run_command already returns command output. Pass
            max_lines=0 for the entire scrollback (can be thousands of lines).
            """
            logger.info("MCP Tool called: rc2014_get_screen(max_lines=%d)", max_lines)
            sc = link.get_screen(max_lines=max_lines)
            # Screen rows are padded to the full grid width; sending that padding
            # would double the cost of every read for no information.
            lines = [line.rstrip() for line in sc.get("lines", [])]
            while lines and not lines[-1]:
                lines.pop()
            xmodem = sc.get("xmodem_progress", {}) or {}
            transfer = (f"{xmodem.get('direction', 'transfer')} "
                        f"{xmodem.get('current_block', 0)}/{xmodem.get('total_blocks', 0)}"
                        if xmodem.get("active") else "idle")
            header = (f"[state={sc.get('system_state', 'unknown')} "
                      f"os={sc.get('os') or '-'} "
                      f"prompt={sc.get('last_prompt', '')!r} "
                      f"mode={sc.get('mode', 'terminal')} "
                      f"operation={sc.get('current_op') or 'idle'} "
                      f"xmodem={transfer}]")
            return "\n".join([header] + lines)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Send raw text", read_only_hint=False, open_world_hint=True))
        def rc2014_send_text(text: str) -> str:
            """Send text to the console without waiting for a reply. Enter is appended.

            The escape hatch for things rc2014_run_command cannot do: answering a
            program's prompt, driving MBASIC or ED, choosing an entry in the HBIOS
            boot menu. Follow it with rc2014_get_screen or rc2014_wait_for to see
            what happened. For ordinary commands prefer rc2014_run_command; for
            control characters, or to interrupt something, use rc2014_send_keys.
            """
            logger.info("MCP Tool called: rc2014_send_text(text=%r)", text)
            busy = link.busy_reason()
            if busy:
                return f"Not sent - {busy}. Wait for it to finish and retry."
            link.send_text(text, append_enter=True)
            return f"Sent to RC2014: {text!r}"

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Send raw keystrokes", read_only_hint=False, open_world_hint=True))
        def rc2014_send_keys(keys: str) -> dict:
            """Send raw keystrokes with NO Enter appended - including control characters.

            This is how you interrupt something. Unlike every other tool it is
            allowed to run while a transfer or command is in progress, because
            that is the situation it exists for.

            Mnemonics, since control characters can't go in a JSON string:
              ^X ^C ^Z    Ctrl-X, Ctrl-C, Ctrl-Z (^A..^Z, plus ^[ ^] ^?)
              <ESC>       named key: NUL BS TAB LF CR ESC CAN EOF SPACE DEL
              <PAUSE>     brief wait, for sequences that need one
              ^^          a literal caret
            Anything else is sent as typed.

            Recipes:
              '^X<PAUSE>^X'  cancel a stuck XM transfer (XM's own documented hint)
              '^C'           interrupt a running program
              'Y<CR>'        answer a Y/N prompt
              '<ESC>'        leave an editor's insert mode
            """
            logger.info("MCP Tool called: rc2014_send_keys(keys=%r)", keys)
            try:
                return link.send_keys(keys)
            except ValueError as e:
                return {"ok": False, "error": str(e), "keys": keys}

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Wait until ready", read_only_hint=True))
        def rc2014_wait_until_ready(settle: float = 1.0) -> dict:
            """Wait for the machine to finish booting and settle at a prompt.

            Call this after booting a disk (rc2014_send_text with the unit number)
            before sending commands. A boot profile keeps running programs after
            the OS banner appears, and anything sent during that window is simply
            lost - the board is not reading input yet, so a command can arrive
            with its first characters missing.
            """
            logger.info("MCP Tool called: rc2014_wait_until_ready(settle=%s)", settle)
            return link.wait_until_ready(settle=settle)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Wait for output", read_only_hint=True))
        def rc2014_wait_for(pattern: str, timeout: float = 10.0) -> dict:
            """Wait for a regex to appear in output arriving from now on.

            Only searches output produced after this call starts, so an old prompt
            already on screen will not match. Useful after rc2014_send_text - e.g.
            wait for 'Boot \\[H=Help\\]:' after a reboot.
            """
            logger.info("MCP Tool called: rc2014_wait_for(pattern=%r, timeout=%s)", pattern, timeout)
            return link.wait_for(pattern, timeout=timeout)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Upload a file to the board", read_only_hint=False, destructive_hint=True))
        async def rc2014_upload(ctx: Context, name: str, drive: str, user: int = 0,
                                content: str = "", binary: bool = False,
                                overwrite: bool = False) -> dict:
            """Copy content to the RC2014, zipped over XMODEM, verifying it arrived.

            Handles the whole sequence: zips content, runs XM on the board,
            transfers, UNZIPs, confirms with DIR. Do NOT run 'XM R' yourself first.

            name: 8.3 CP/M filename ONLY, e.g. 'HELLO.TXT' - no drive prefix
              (unlike cpm_path-style tools such as rc2014_read_text_file).
              Put the drive in `drive` instead.
            drive: target drive, 'A'..'P'.
            user: CP/M user area, 0-15 (default 0).
            content: text, or base64 when binary=true. binary=false encodes as
              latin-1 - characters outside it (em-dashes, curly quotes, most
              non-ASCII text) are silently replaced with '?'. Use binary=true
              (base64) for anything that must survive exactly.
            binary: false (default) applies CP/M's CRLF + 0x1A EOF-marker
              convention for text; true passes raw bytes through untouched.
            overwrite: UNZIP (like XM) refuses to replace an existing file, so
              an existing target is erased first. Pass false to fail instead.
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_upload(name=%r, drive=%r, user=%d, "
                        "binary=%s, overwrite=%s)", name, drive, user, binary, overwrite)
            return await self._with_progress(
                ctx, link.upload, name, drive, user=user, content=content,
                binary=binary, overwrite=overwrite)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Download a file from the board", read_only_hint=False, destructive_hint=True))
        async def rc2014_download(ctx: Context, name: str, drive: str, user: int = 0,
                                  binary: bool = False) -> dict:
            """Copy a file off the RC2014 over XMODEM (1K blocks), content inline.

            Handles the whole sequence: runs XM on the board, receives, returns the
            content and its sha256. Do NOT run 'XM S' yourself first.

            name: 8.3 CP/M filename ONLY on the board, e.g. 'LEDSHOW.COM' - no
              drive prefix (unlike cpm_path-style tools such as
              rc2014_read_text_file). Put the drive in `drive` instead.
            drive: source drive, 'A'..'P'.
            user: CP/M user area, 0-15 (default 0).
            binary: false (default) returns text (latin-1 decoded - anything
              outside it comes back mangled, matching how it was written);
              true returns base64, byte-exact only to the file's 128-byte
              CP/M record size (CP/M has no sub-record length anywhere, so a
              file whose true length isn't a multiple of 128 may come back
              with trailing padding).
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_download(name=%r, drive=%r, user=%d, "
                        "binary=%s)", name, drive, user, binary)
            return await self._with_progress(
                ctx, link.download, name, drive, user=user, binary=binary)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Read a text file", read_only_hint=True))
        def rc2014_read_text_file(cpm_path: str, max_bytes: int = 8192) -> dict:
            """Read a text file off the board with TYPE - no transfer needed.

            Cheap, but not byte-exact: TYPE expands tabs and stops at the 0x1A EOF
            marker. Use rc2014_download when the exact bytes matter (any binary,
            or a file you intend to write back).
            """
            logger.info("MCP Tool called: rc2014_read_text_file(cpm_path=%r)", cpm_path)
            return link.read_text_file(cpm_path, max_bytes=max_bytes)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Catalogue the drives", read_only_hint=True))
        async def rc2014_scan_drives(ctx: Context) -> dict:
            """Catalogue every mapped drive: free space, access mode, file sample, purpose.

            Runs STAT plus a DIR per drive, so it takes tens of seconds and reports
            progress as it goes. Results are cached in hardware_info - for a single
            drive, 'DIR B:' via rc2014_run_command is much faster.

            NOTE: file names and counts here cover the **current CP/M user area
            only** (usually 0). A drive can hold far more - this system's A: shows
            50 files in user 0 but 210 in total. Use rc2014_survey for true
            per-drive totals.
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_scan_drives()")
            return await self._with_progress(ctx, link.scan_drives)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Run SURVEY", read_only_hint=True))
        def rc2014_survey() -> dict:
            """Run RomWBW's SURVEY and return the system report, structured.

            One command (~7s) and the best single source of system detail:
            per-drive bytes used / file count / bytes free counting **all CP/M
            user areas**, the 64K memory map with BIOS and BDOS addresses, TPA
            size, and the active I/O port map.

            Complements rc2014_scan_drives, which is slower but returns actual
            filenames - and only for the current user area.
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_survey()")
            return link.survey()

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Get hardware info", read_only_hint=True, idempotent_hint=True))
        def rc2014_get_hardware_info() -> dict:
            """Captured hardware: RomWBW version, CPU, memory/MMU, HBIOS devices, OS
            versions, drive mappings, and the last drive catalogue."""
            logger.info("MCP Tool called: rc2014_get_hardware_info()")
            return getattr(link, "hardware_info", {})

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Reboot the machine", read_only_hint=False, destructive_hint=True))
        def rc2014_reboot() -> dict:
            """Reboot the RC2014, choosing the right method for its current state
            (C:REBOOT /C at a CP/M prompt, R at the HBIOS boot menu).

            Anything unsaved on the RAM disk survives a reboot but not a power cycle.
            Follow with rc2014_wait_for on the boot banner to know when it is back.
            """
            logger.info("MCP Tool called: rc2014_reboot()")
            return link.reboot()

    # ------------------------------------------------------------------
    # transports
    # ------------------------------------------------------------------
    def _build_app(self):
        # DNS rebinding protection off and CORS wide open: this is a LAN tool on
        # a trusted network by design. See the security note in README.md.
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
            allowed_hosts=["*"],
            allowed_origins=["*"],
        )

        # stateless_http=True: every request gets its own fresh transport and
        # connection, for every protocol version - no Mcp-Session-Id is ever
        # minted or expected. 2026-07-28+ clients already route to the
        # single-exchange handler unconditionally on protocol version; this
        # flag is what removes the legacy stateful path for everything else.
        app = self.mcp.streamable_http_app(
            host=self.host, stateless_http=True, transport_security=security)

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        return app

    def endpoints(self) -> list[str]:
        return [f"http://{self.host}:{self.port}/mcp"]

    def start(self):
        def _run_server():
            try:
                logger.info("Starting MCP server on %s:%d", self.host, self.port)
                config = uvicorn.Config(self._build_app(), host=self.host,
                                        port=self.port, log_level="info")
                self._uvicorn = uvicorn.Server(config)
                self._uvicorn.run()
            except Exception as e:
                logger.exception("MCP Server error: %s", e)

        self._server_thread = threading.Thread(target=_run_server, daemon=True)
        self._server_thread.start()

    def stop(self):
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._server_thread is not None:
            self._server_thread.join(timeout=3.0)
            if self._server_thread.is_alive():
                logger.warning("MCP server thread did not stop within 3s")
