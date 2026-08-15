"""
Owns the serial connection to the RC2014. One background thread reads the
port continuously; everything else (the pygame display, the MCP server)
goes through this object rather than touching the port directly.

Two ways incoming bytes get interpreted, controlled by self._mode:
  - "terminal": fed into a pyte VT100 screen buffer (for display.py to
    render) and appended to a bounded receive log (for wait_for() and
    run_command() to search).
  - "xmodem": routed byte-by-byte to a queue that xmodem_send/receive
    consume directly, bypassing the terminal emulator entirely.

The port is normally opened once and held for the object's whole lifetime;
switching modes never closes or reacquires it. reconfigure() is the one
deliberate exception - it closes and reopens the port (e.g. for a different
baud rate, device path, or RTS/CTS setting) at the user's request from the
GUI's Settings screen.

Operations that own the wire for more than one write - run_command(),
upload(), download(), the XMODEM transfers, scan_drives(), reboot() - are
serialized behind _op_lock and report BusyError as a structured result, so
an agent stacking calls gets told to wait instead of corrupting a
transfer. Human keystrokes deliberately bypass that lock: a person must
always be able to type, including Ctrl-X to cancel a stuck transfer.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import io
import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import zipfile

import pyte
import serial
import serial.tools.list_ports

from rc2014bridge.pixel_stream import VERSION_BANNER, PixelStreamDecoder

logger = logging.getLogger("rc2014bridge")

SOH, STX, EOT, ACK, NAK, CAN, SUB = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18, 0x1A

# Write pacing. These conservative values were derived on an RC2014 Pro (Z80 @
# 7.372MHz behind an external SIO) and are safe everywhere, but they cost dearly:
# 8-byte chunks with a 10ms gap runs a 115200 line at roughly 5% of its rate, so
# a 512KB ROM image takes ~15 minutes. Faster hardware tolerates far more -
# calibrate_pacing() measures what a given board actually accepts.
DEFAULT_TEXT_CHUNK, DEFAULT_TEXT_DELAY = 1, 0.015
DEFAULT_XMODEM_CHUNK, DEFAULT_XMODEM_DELAY = 8, 0.010

# Progressively faster candidates, conservative first. Each is proved by a
# byte-exact round trip before the next is tried.
PACING_CANDIDATES = [
    (8, 0.010),
    (16, 0.010),
    (32, 0.005),
    (64, 0.005),
    (128, 0.002),
    (256, 0.000),   # effectively unpaced: one write per block
]
BLOCK_SIZE = 128
LONG_BLOCK_SIZE = 1024
MAX_RETRIES = 10

# How much console output to keep for wait_for()/run_command() to search.
RX_LOG_MAX = 256 * 1024

# Drive-scan command timeouts. STAT probes every mapped drive before it prints
# anything, which takes several seconds on real hardware.
STAT_TIMEOUT = 25.0
DIR_TIMEOUT = 15.0
# SURVEY walks every drive and probes the I/O map; ~6.5s measured, so allow slack.
SURVEY_TIMEOUT = 40.0
# How long to wait for a boot profile to stop producing output before deciding
# the machine is not going to settle at a prompt.
BOOT_SETTLE_TIMEOUT = 20.0

# ----------------------------------------------------------------------
# Prompt patterns - the single source of truth. _update_system_state(),
# reboot(), run_command() and the XMODEM verification all use these
# rather than each carrying its own slightly different alternation.
# ----------------------------------------------------------------------
# CP/M prompts carry the user area when it isn't 0, and the two orderings both
# occur in the wild: "2A>" and - on RomWBW/ZSDOS - "C2>". Accept either, or every
# command run outside user area 0 waits out its timeout.
_CPM = r"[0-9]*[A-P][0-9]*>"
_HBIOS = r"HBIOS>|Boot(?:\s*\[[^\]]*\])?\s*:"
_FLASH = r"FDU>|FLASH>|Command\?"

CPM_PROMPT_RE = re.compile(rf"^{_CPM}")
# Looser: the boot banner's "A:=IDE0:0" drive-configuration lines also mean
# an OS is coming up, so state detection accepts them too.
CPM_STATE_RE = re.compile(rf"^{_CPM}|^[A-P][0-9]*:")
HBIOS_PROMPT_RE = re.compile(rf"^(?:{_HBIOS}|Select \(A-F)", re.IGNORECASE)
FLASH_PROMPT_RE = re.compile(rf"^(?:{_FLASH})", re.IGNORECASE)

# A prompt sitting at the very end of the output - "the machine is waiting
# for input again" - which is how run_command() knows a command finished.
# Case-insensitivity is scoped to the HBIOS/FLASH alternatives so a lowercase
# "d>" in ordinary program output can't be mistaken for a CP/M prompt.
TRAILING_PROMPT_RE = re.compile(
    rf"(?:^|[\r\n])[ \t]*({_CPM}|(?i:{_HBIOS})|(?i:{_FLASH}))[ \t]*\Z"
)
PROMPT_ONLY_RE = re.compile(rf"^(?:{_CPM}|(?i:{_HBIOS})|(?i:{_FLASH}))[ \t]*$")

# SUBMIT echoes each line it is about to run as "<drive>$<command>", e.g.
# "A$LDTIM". Seeing that means a startup script is mid-flight, and ANY console
# input would abort it.
SUBMIT_ECHO_RE = re.compile(r"^[A-P][0-9]*\$\S")

# The HBIOS boot loader's "i <unit> <baud>" command reconfigures one SIO's
# line speed and echoes the typed command right after its own prompt, e.g.
# "Boot [H=Help]: i 0 230400". Only unit 0 is the console SIO the bridge is
# actually wired to - see _update_system_state(). Anchored on an actual
# trailing newline (not just end-of-buffer/"$") on purpose: input is echoed
# one character at a time, so the buffer's tail is "...i 0 2", then
# "...i 0 23", etc. while a baud is still being typed - matching "$" there
# would fire on that partial number. A newline only appears once Enter has
# actually been pressed and the whole line is final.
HBIOS_BAUD_CMD_RE = re.compile(rf"(?:{_HBIOS})\s*i\s+(\d+)\s+(\d+)[ \t]*\r?\n", re.IGNORECASE)
# What the loader prints once it has accepted that command - by this point
# its own UART has already switched, so the bridge must follow before
# anything else is sent, not after.
HBIOS_BAUD_CONFIRM_RE = re.compile(r"Change speed now.*?Press a key to resume", re.IGNORECASE | re.DOTALL)

# Baud-mismatch line noise detection (_check_for_baud_mismatch): a board
# reset/reboot/crash while the bridge is still following a runtime-only
# HBIOS "i 0 <baud>" change (see _follow_hbios_baud_change) comes back up
# at its power-on default with no recognizable text to pattern-match on -
# every byte is misframed. PLAUSIBLE_TEXT_RE is what "this looks like real
# console output" means: printable ASCII plus the handful of controls a
# terminal actually uses (CR/LF/TAB/BEL/BS/ESC). A byte stream misframed by
# the wrong baud lands in that ~101/256-value set by chance alone only
# ~40% of the time, so GARBAGE_RATIO_THRESHOLD sits well below true noise's
# ~60% expected miss rate while comfortably above what normal console
# output (including ANSI-heavy screens) produces.
PLAUSIBLE_TEXT_RE = re.compile(r"[\t\n\r\x07\x08\x1b\x20-\x7e]")
GARBAGE_MIN_CHUNK_LEN = 12
GARBAGE_RATIO_THRESHOLD = 0.5
GARBAGE_STREAK_TO_ACT = 4
GARBAGE_RECOVERY_COOLDOWN_S = 3.0

# Text XM prints when it has failed to start a transfer, so upload()/download()
# can fail fast instead of waiting out the full handshake timeout. RomWBW's XM
# prints its "Receiving:" banner *before* deciding it can't proceed, so the
# banner alone is not proof it is armed - see _arm_xm.
XM_ERROR_RE = re.compile(
    r"no file|not found|file error|file exists|invalid|bad |read.only|R/O|\?\?",
    re.IGNORECASE,
)

# How long to watch XM after its banner to see whether it bails out.
XM_ARM_SETTLE = 2.0

# Named keys accepted by send_keys(), for characters that can't be typed into a
# JSON string safely or legibly.
KEY_NAMES = {
    "NUL": 0x00, "BS": 0x08, "TAB": 0x09, "LF": 0x0A, "CR": 0x0D,
    "ESC": 0x1B, "CAN": 0x18, "EOF": 0x1A, "SPACE": 0x20, "DEL": 0x7F,
}
# Control characters reachable as ^<char> beyond the ^A..^Z range.
CTRL_SYMBOLS = {"[": 0x1B, "]": 0x1D, "\\": 0x1C, "?": 0x7F}
# What <PAUSE> is worth. XM's own cancel hint is "Ctrl-X, pause, Ctrl-X".
PAUSE_SECONDS = 0.3


class BusyError(RuntimeError):
    """Raised when an operation is attempted while another one owns the wire."""


def _exclusive(op_name: str):
    """Serialize a public operation behind _op_lock.

    The lock is an RLock, so composites (upload -> xmodem_send -> run_command)
    nest freely within one thread while a second thread is turned away with a
    structured busy result rather than an exception.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                with self._operation(op_name):
                    return fn(self, *args, **kwargs)
            except BusyError as e:
                logger.warning("%s rejected: %s", fn.__name__, e)
                return {"ok": False, "busy": True, "error": str(e)}
        return wrapper
    return decorator


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


_CPM_NAME_RE = re.compile(r"^[A-Z0-9$_]{1,8}(\.[A-Z0-9$_]{1,3})?$")


def _validate_cpm_name(name: str) -> str | None:
    """Check an 8.3 CP/M filename, uppercased. Returns an error string, or
    None if valid - a caller error should be reported, not silently mangled."""
    if not _CPM_NAME_RE.match(name.upper()):
        return (f"{name!r} is not a valid 8.3 CP/M filename (up to 8 chars, "
                f"optional 1-3 char extension, letters/digits/$/_ only)")
    return None


def _format_target(drive: str, user: int, name: str) -> str:
    """<drive>:<name> for user 0 (the usual case), <drive><user>:<name>
    otherwise - confirmed working directly against real hardware without a
    separate USER switch (DIR A1:*.* against a real user-1 area)."""
    return f"{drive}{user or ''}:{name}"


def _zip_single_entry(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return buf.getvalue()


_STAT_FREE_SPACE_RE = re.compile(r"Bytes Remaining On \w:\s*(\d+)k", re.IGNORECASE)


def _parse_stat_free_space(text: str) -> int | None:
    """Bytes free on a drive from 'STAT <drive>:' output, or None if the
    wording doesn't match - callers should skip the precheck rather than
    block a valid transfer on an unparsed reply.

    Not 'XM A': that command's own space report ignores any drive argument
    and always reports a hardcoded 'A0:' private area - confirmed live
    against real hardware (XM A B: and XM A while logged into B: both still
    reported A0:'s free space). STAT <drive>: is what actually varies by
    drive."""
    m = _STAT_FREE_SPACE_RE.search(text)
    return int(m.group(1)) * 1024 if m else None


_STAT_FILE_INFO_RE = re.compile(r"^\s*(\d+)\s+(\d+)k\s+\d+\s+\S+", re.MULTILINE)


def _parse_stat_file_info(text: str) -> dict:
    """Records/bytes from 'STAT <file>' output, e.g.
    ' Recs  Bytes  Ext Acc\\n   10     4k    1 R/W J:MANDEL.COM'."""
    m = _STAT_FILE_INFO_RE.search(text)
    return {"records": int(m.group(1)), "bytes": int(m.group(2)) * 1024} if m else {}


def _parse_keys(keys: str) -> list[tuple[str, object]]:
    """Turn a key string into a sequence of ("bytes", b"..") / ("pause", secs).

    Control characters can't be written legibly into a JSON string - and some
    tool layers reject them outright - so send_keys() takes mnemonics instead:

      ^X          Ctrl-X (^A..^Z, plus ^[ ^] ^\\ ^?)
      ^^          a literal caret
      <ESC>       named key: NUL BS TAB LF CR ESC CAN EOF SPACE DEL
      <PAUSE>     wait PAUSE_SECONDS before continuing
      \\r \\n \\t   the usual escapes
      anything else is sent literally

    Raises ValueError on an unknown mnemonic rather than silently sending the
    wrong bytes.
    """
    parts: list[tuple[str, object]] = []
    pending = bytearray()

    def flush():
        if pending:
            parts.append(("bytes", bytes(pending)))
            pending.clear()

    i = 0
    while i < len(keys):
        char = keys[i]

        if char == "^" and i + 1 < len(keys):
            nxt = keys[i + 1]
            if nxt == "^":
                pending.append(ord("^"))
            elif nxt.upper().isalpha() and nxt.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                pending.append(ord(nxt.upper()) - 64)
            elif nxt in CTRL_SYMBOLS:
                pending.append(CTRL_SYMBOLS[nxt])
            else:
                raise ValueError(f"unknown control key {char + nxt!r}")
            i += 2
            continue

        if char == "<":
            end = keys.find(">", i)
            if end != -1:
                name = keys[i + 1:end].strip().upper()
                if name == "PAUSE":
                    flush()
                    parts.append(("pause", PAUSE_SECONDS))
                    i = end + 1
                    continue
                if name in KEY_NAMES:
                    pending.append(KEY_NAMES[name])
                    i = end + 1
                    continue
                raise ValueError(
                    f"unknown key name <{name}>; known names: "
                    + ", ".join(sorted(KEY_NAMES) + ["PAUSE"]))

        if char == "\\" and i + 1 < len(keys):
            escapes = {"r": 0x0D, "n": 0x0A, "t": 0x09, "\\": 0x5C, "0": 0x00}
            nxt = keys[i + 1]
            if nxt in escapes:
                pending.append(escapes[nxt])
                i += 2
                continue

        pending.extend(char.encode("latin-1", errors="replace"))
        i += 1

    flush()
    return parts


def _strip_echo_and_prompt(text: str, command: str) -> str:
    """Reduce a run_command() capture to just the command's own output.

    The board echoes what we typed and prints a fresh prompt when it's done;
    neither is information the caller asked for.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # Drop leading blanks first, or a stray newline ahead of the echo hides it.
    while lines and not lines[0].strip():
        lines.pop(0)

    cmd = command.strip()
    if cmd and lines and lines[0].strip().upper().endswith(cmd.upper()):
        lines.pop(0)

    while lines and (not lines[-1].strip() or PROMPT_ONLY_RE.match(lines[-1].strip())):
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)

    return "\n".join(line.rstrip() for line in lines)


def _parse_boot_banner(text: str) -> dict:
    info = {
        "version": "",
        "cpu": "",
        "wait_states": "",
        "int_mode": "",
        "memory": "",
        "devices": [],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    m = re.search(r"RomWBW (?:HBIOS )?(v[0-9\.\w-]+(?:, \d{4}-\d{2}-\d{2})?)", text, re.IGNORECASE)
    if m:
        info["version"] = m.group(1)

    # Match "<part> @ <clock>MHz" generically rather than enumerating chip names.
    # Real RomWBW builds print designations an enumeration keeps missing -
    # "Z8S180-N @ 18.432MHz" on an SC700 doesn't contain the string "Z180".
    m = re.search(r"([A-Z][A-Z0-9]*(?:[-/][A-Z0-9]+)*\s*@\s*[0-9.]+\s*MHz)", text, re.IGNORECASE)
    if m:
        info["cpu"] = m.group(1).strip()

    # The platform line names the board and its RomWBW build config, e.g.
    # "Small Computer SC700 [SCZ180_sc700_std]" - the most direct statement of
    # which machine this actually is.
    m = re.search(r"^(.*?)\s*\[([A-Za-z0-9_]+)\]", text, re.MULTILINE)
    if m and m.group(1).strip():
        info["platform"] = m.group(1).strip()
        info["config"] = m.group(2)

    m = re.search(r"(\d+ MEM W/S, \d+ I/O W/S)", text, re.IGNORECASE)
    if m:
        info["wait_states"] = m.group(1)

    m = re.search(r"(INT MODE \d+)", text, re.IGNORECASE)
    if m:
        info["int_mode"] = m.group(1)

    # The MMU is named next to the wait states on some builds and next to the
    # RAM/ROM sizes on others, so pick it up on its own.
    m = re.search(r"([A-Z0-9]+ MMU)", text, re.IGNORECASE)
    if m:
        info["mmu"] = m.group(1)

    m = re.search(r"([A-Z0-9]+ MMU, \d+[KMB]* ROM, \d+[KMB]* RAM|\d+[KMB]* ROM, \d+[KMB]* RAM)", text, re.IGNORECASE)
    if m:
        info["memory"] = m.group(1)

    for line in text.splitlines():
        line_s = line.strip()
        if re.match(r"^[A-Z0-9]{2,8}:\s+", line_s):
            if line_s not in info["devices"]:
                info["devices"].append(line_s)
    return info


def _parse_zsdos_banner(text: str) -> dict:
    info = {}
    m = re.search(r"ZSDOS\s+(v[0-9\.\w-]+(?:,\s*[0-9\.\w]+\s*TPA)?)", text, re.IGNORECASE)
    if m:
        info["zsdos_version"] = m.group(0)

    m = re.search(r"CBIOS\s+(v[0-9\.\w-]+(?:\s*\[\w+\])?)", text, re.IGNORECASE)
    if m:
        info["cbios_version"] = m.group(0)

    m = re.search(r"(\d+(?:\.\d+)?K\s+TPA)", text, re.IGNORECASE)
    if m:
        info["tpa"] = m.group(1)

    drives = {}
    for line in text.splitlines():
        line_s = line.strip()
        dm = re.match(r"^([A-J]):=([A-Z0-9]+:\d+)", line_s)
        if dm:
            drives[dm.group(1)] = dm.group(2)
    if drives:
        info["drive_mappings"] = drives
    return info


def _parse_survey_output(text: str) -> dict:
    """Parse RomWBW's SURVEY.COM report.

    SURVEY sees things the boot banner doesn't: per-drive totals that count
    *every* user area (a plain DIR only shows the current one), the memory map
    with BIOS/BDOS addresses, and the live I/O port map.
    """
    info: dict = {}

    m = re.search(r"\*{3}\s*(.+?System Survey.*?)\s*\*{3}", text)
    if m:
        info["survey_version"] = m.group(1).strip()

    drives = {}
    for m in re.finditer(
            r"Drive\s+([A-P]):\s+(\d+K?)\s+bytes\s+in\s+(\d+)\s+files"
            r"(?:\s+with\s+(\d+K?)\s+bytes\s+remaining)?", text, re.IGNORECASE):
        drives[m.group(1).upper()] = {
            "used": m.group(2),
            "files": int(m.group(3)),
            "free": m.group(4) or "?",
        }
    if drives:
        info["drives"] = drives

    m = re.search(r"BIOS\s+at\s+([0-9A-F]+)", text, re.IGNORECASE)
    if m:
        info["bios_addr"] = m.group(1).upper()
    m = re.search(r"BDOS\s+at\s+([0-9A-F]+)", text, re.IGNORECASE)
    if m:
        info["bdos_addr"] = m.group(1).upper()
    m = re.search(r"iobyte\s+([0-9A-F]+)", text, re.IGNORECASE)
    if m:
        info["iobyte"] = m.group(1).upper()

    m = re.search(r"(\d+)\s+Bytes\s+RAM", text, re.IGNORECASE)
    if m:
        info["ram_bytes"] = int(m.group(1))
    m = re.search(r"(\d+)\s+Bytes\s+ROM", text, re.IGNORECASE)
    if m:
        info["rom_bytes"] = int(m.group(1))
    m = re.search(r"(\d+)\s+Bytes\s+in\s+TPA", text, re.IGNORECASE)
    if m:
        info["tpa_bytes"] = int(m.group(1))

    # The T/C/B/R band showing what occupies each 1K of the 64K address space.
    m = re.search(r"^([TCBR]{16,})\s*$", text, re.MULTILINE)
    if m:
        info["memory_map"] = m.group(1)

    m = re.search(r"Active I/O ports:(.*?)(\d+)\s+Ports?\s+active", text,
                  re.IGNORECASE | re.DOTALL)
    if m:
        ports = re.findall(r"\b([0-9A-F]{2})\b", m.group(1), re.IGNORECASE)
        if ports:
            info["io_ports"] = [p.upper() for p in ports]
        info["io_ports_active"] = int(m.group(2))

    return info


def _parse_stat_output(text: str) -> dict:
    info = {}
    for line in text.splitlines():
        m = re.search(r"([A-P]):\s+(R/[WO]),\s+Space:\s+(\d+k)", line, re.IGNORECASE)
        if m:
            info[m.group(1).upper()] = {
                "access": m.group(2).upper(),
                "free_space": m.group(3),
            }
    return info


def _parse_cpm_dir_output(text: str) -> list[str]:
    files = []
    ignored = {"NO", "FILE", "DIR", "BYTES", "FREE", "USAGE", "KB", "DRIVE", "UNIT", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "STAT"}
    for line in text.splitlines():
        line_clean = re.sub(r"^[A-P]>", "", line.strip())
        if "No File" in line_clean or "NO FILE" in line_clean or "Space:" in line_clean:
            continue
        parts = re.split(r"[|:]", line_clean)
        for part in parts:
            p_str = part.strip()
            if not p_str:
                continue
            m = re.match(r"^([A-Z0-9_\$]{1,8})\s+\.?\s*([A-Z0-9_\$]{1,3})$", p_str, re.IGNORECASE)
            if m:
                name, ext = m.group(1).upper(), m.group(2).upper()
                if name not in ignored:
                    fname = f"{name}.{ext}"
                    if fname not in files:
                        files.append(fname)
    return files


def _classify_drive_purpose(drive: str, files: list[str], device_map: str = "",
                            access: str = "") -> str:
    """Describe what a drive is for.

    The device mapping is authoritative: only RomWBW's MD1 memory-disk unit is
    the ROM disk. Classifying by filename instead mislabels every CF slice that
    happens to carry XM.COM and FLASH.COM - which is most of them - as a
    read-only ROM disk, and then contradicts STAT's own R/W reading.
    """
    dev = device_map.upper()
    files_upper = [f.upper() for f in files]

    if dev.startswith("MD1"):
        return "ROM Disk (read-only, RomWBW utilities)"
    if dev.startswith("MD0"):
        return "RAM Disk (volatile, cleared on power cycle)"

    suffix = " (read-only)" if access.upper() == "R/O" else ""

    if not files_upper:
        return "Empty / Unformatted Volume"
    if any(f in files_upper for f in ("ZPATH.COM", "STAT.COM", "PIP.COM", "SUBMIT.COM", "CCP.COM", "CPM.SYS")):
        return f"CP/M / ZSDOS System Disk{suffix}"

    code_exts = (".Z80", ".PAS", ".C", ".BAS", ".ASM", ".HEX", ".TXT", ".MAC", ".SUB", ".PRN")
    has_code = any(any(f.endswith(ext) for ext in code_exts) for f in files_upper)
    if has_code or not any(f.endswith(".COM") for f in files_upper):
        return f"User Programming & Source Code{suffix}"
    return f"General Application / Data Volume{suffix}"


def list_serial_ports() -> list[str]:
    """Device paths of currently-attached serial adapters, for the GUI
    Settings screen's port dropdown. Best-effort: an empty result just means
    the picker falls back to whatever port is already configured."""
    try:
        return sorted(p.device for p in serial.tools.list_ports.comports())
    except Exception:
        logger.exception("Failed to enumerate serial ports")
        return []


def probe_serial_connection(port: str, baud: int, rtscts: bool = False) -> dict:
    """Open `port` at `baud`/`rtscts` and immediately close it again, independent
    of any existing SerialLink. Lets the GUI's Settings screen validate a
    port/baud/rtscts combination (permissions, wrong device, port already in
    use) before committing to it with SerialLink.reconfigure(). Named "probe"
    rather than "test" so it doesn't look like a pytest test function to
    anything that imports it into a test module's namespace."""
    try:
        probe = serial.Serial(port, baudrate=baud, bytesize=8, parity="N",
                               stopbits=1, timeout=0.1, rtscts=rtscts)
        probe.close()
        return {"ok": True}
    except (serial.SerialException, OSError) as e:
        return {"ok": False, "error": str(e)}


class SerialLink:
    def __init__(self, port: str, baud: int = 115200, cols: int = 80, rows: int = 48,
                 hw_info_file: str = "hardware_info.json",
                 text_pacing: tuple[int, float] = None,
                 xmodem_pacing: tuple[int, float] = None,
                 rtscts: bool = False):
        self.port = port
        self.baud = baud
        # The board's "resting" rate - what --baud/the config file say it
        # normally runs at. A deliberate reconfigure() (Settings screen, or
        # a fresh launch) updates this; _follow_hbios_baud_change()'s
        # runtime-only follow deliberately does not, so
        # _check_for_baud_mismatch() has something stable to fall back to
        # if the board resets back to its power-on rate mid-session.
        self._default_baud = baud
        self._garbage_streak = 0
        self._last_garbage_recovery_at = 0.0
        self.cols, self.rows = cols, rows
        self.hw_info_file = hw_info_file
        self._ser = serial.Serial(port, baudrate=baud, bytesize=8, parity="N",
                                   stopbits=1, timeout=0.1, rtscts=rtscts)
        self._write_lock = threading.Lock()

        self._screen = pyte.HistoryScreen(cols, rows, history=1000)
        self._stream = pyte.Stream(self._screen)
        self._screen_lock = threading.Lock()

        # Bounded, monotonic log of console output. Readers take a position
        # from rx_position() and ask for everything after it, so concurrent
        # waiters never consume each other's bytes and memory stays capped.
        self._rx_text = ""
        self._rx_seq = 0
        self._rx_cond = threading.Condition()

        self._mode = "terminal"
        self._mode_lock = threading.Lock()
        # Display-only: which viewport display.py draws. Deliberately
        # separate from _mode - _mode auto-reverts to "terminal" the
        # instant a pixel-stream frame's END_OF_FRAME is seen (so the
        # *next* bytes, e.g. this program's own post-render text, parse
        # correctly), but the just-completed image should stay on screen
        # until a new render starts or the user explicitly switches back,
        # not vanish the moment decoding finishes.
        self._show_pixel_view = False
        self._xmodem_q: "queue.Queue[int]" = queue.Queue()
        self._pixel_decoder = PixelStreamDecoder()

        self._op_lock = threading.RLock()
        self._current_op = ""

        self._last_rx_time = 0.0
        self._last_tx_time = 0.0
        self._xmodem_progress = {
            "active": False,
            "filename": "",
            "current_block": 0,
            "total_blocks": 0,
            "bytes": 0,
            "direction": "",
        }
        self._scan_progress = {"active": False, "drive": "", "index": 0, "total": 0}

        self._system_state = "unknown"
        self._last_prompt = ""
        # Set when the HBIOS boot loader echoes an "i 0 <baud>" command for
        # the console SIO (unit 0), cleared once its "Change speed now"
        # confirmation is seen and the bridge has followed suit - see
        # _update_system_state()/_follow_hbios_baud_change().
        self._pending_hbios_baud: int | None = None
        self.hardware_info = self._load_hardware_info()

        # An explicit setting wins; otherwise reuse whatever calibrate_pacing()
        # last proved on this machine, falling back to the safe defaults.
        stored = self.hardware_info.get("pacing") or {}
        self.text_pacing = text_pacing or tuple(
            stored.get("text", (DEFAULT_TEXT_CHUNK, DEFAULT_TEXT_DELAY)))
        self.xmodem_pacing = xmodem_pacing or tuple(
            stored.get("xmodem", (DEFAULT_XMODEM_CHUNK, DEFAULT_XMODEM_DELAY)))
        logger.info("Write pacing: text=%s xmodem=%s", self.text_pacing, self.xmodem_pacing)
        self._hw_snapshot = json.dumps(self.hardware_info, sort_keys=True)
        self._boot_buffer = ""

        # Bumped by reconfigure() each time it swaps in a freshly opened
        # _ser. _read_loop compares against this to tell "port was closed
        # out from under me on purpose" apart from a real disconnect.
        self._reinit_epoch = 0

        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        # Send an initial CR to refresh prompt and detect system state immediately
        def _initial_prompt_probe():
            time.sleep(0.15)
            self._write_raw(b"\r")
        threading.Thread(target=_initial_prompt_probe, daemon=True).start()

    # ------------------------------------------------------------------
    # operation serialization
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _operation(self, name: str):
        if not self._op_lock.acquire(blocking=False):
            raise BusyError(f"busy: {self._current_op or 'another operation'} in progress")
        previous = self._current_op
        self._current_op = name
        try:
            yield
        finally:
            self._current_op = previous
            self._op_lock.release()

    @property
    def current_op(self) -> str:
        return self._current_op

    @property
    def rtscts(self) -> bool:
        return self._ser.rtscts

    def is_transferring(self) -> bool:
        """True while XMODEM owns the wire. Cheap enough to call per keystroke."""
        with self._mode_lock:
            return self._mode == "xmodem"

    def busy_reason(self) -> str | None:
        """Why a caller should back off right now, or None if the wire is free."""
        with self._mode_lock:
            if self._mode == "xmodem":
                direction = self._xmodem_progress.get("direction", "transfer")
                return f"XMODEM {direction} in progress"
        if self._current_op:
            return f"{self._current_op} in progress"
        if self.submit_running():
            return ("a startup/submit script is running (any keystroke aborts it) - "
                    "use rc2014_wait_until_ready")
        return None

    def submit_running(self) -> bool:
        """Whether a SUBMIT script looks to be mid-flight.

        Worth knowing before sending anything: console input aborts a CP/M submit
        file, so a keystroke during PROFILE.SUB silently truncates a machine's
        whole startup - clock driver, paths and all.
        """
        lines = [l.strip() for l in self.get_screen().get("lines", []) if l.strip()]
        return bool(lines and SUBMIT_ECHO_RE.match(lines[-1]))

    def progress_snapshot(self) -> dict:
        with self._mode_lock:
            xmodem = dict(self._xmodem_progress)
        return {"op": self._current_op, "xmodem": xmodem, "scan": dict(self._scan_progress)}

    # ------------------------------------------------------------------
    # hardware info & reboot lifecycle
    # ------------------------------------------------------------------
    def _load_hardware_info(self) -> dict:
        if os.path.exists(self.hw_info_file):
            try:
                with open(self.hw_info_file, "r") as f:
                    return json.load(f)
            except Exception:
                logger.warning("Could not read %s; starting from defaults", self.hw_info_file)
        return {
            "version": "",
            "cpu": "",
            "wait_states": "",
            "int_mode": "",
            "memory": "",
            "devices": [],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _save_hardware_info(self):
        # The reader thread calls this on every chunk that looks like a banner
        # line, so skip the write unless something actually changed.
        snapshot = json.dumps(self.hardware_info, sort_keys=True)
        if snapshot == self._hw_snapshot:
            return
        try:
            with open(self.hw_info_file, "w") as f:
                json.dump(self.hardware_info, f, indent=2)
            self._hw_snapshot = snapshot
            logger.info("Saved hardware info to %s", self.hw_info_file)
        except Exception as e:
            logger.warning("Failed to save hardware info: %s", e)

    def _rom_disk_drive(self) -> str:
        """Drive letter of the ROM disk (MD1), where XM.COM always lives."""
        for letter, device in (self.hardware_info.get("drive_mappings") or {}).items():
            if str(device).upper().startswith("MD1"):
                return letter.upper()
        return ""

    def _ram_disk_drive(self) -> str:
        """Drive letter of the volatile RAM disk (MD0) - the natural scratch area."""
        for letter, device in (self.hardware_info.get("drive_mappings") or {}).items():
            if str(device).upper().startswith("MD0"):
                return f"{letter.upper()}:"
        return ""

    def _xm_command(self) -> str:
        rom = self._rom_disk_drive()
        return f"{rom}:XM" if rom else "XM"

    @_exclusive("reboot")
    def reboot(self) -> dict:
        screen = self.get_screen()
        lines = [l.strip() for l in screen.get("lines", []) if l.strip()]
        last_line = lines[-1] if lines else ""
        logger.info("reboot() called. Last line on screen: %r, current system_state: %r", last_line, self._system_state)

        with self._mode_lock:
            self._mode = "terminal"

        self._system_state = "unknown"

        # REBOOT.COM lives on the ROM disk, which is not always drive C: - resolve
        # it from the captured drive mappings rather than assuming.
        rom = self._rom_disk_drive()
        reboot_cmd = f"{rom}:REBOOT /C\r" if rom else "REBOOT /C\r"

        # Check prompt directly from current rendered screen
        if CPM_PROMPT_RE.search(last_line):
            logger.info("Direct prompt match CP/M (%r). Sending %s", last_line, reboot_cmd.strip())
            self._write_raw(reboot_cmd.encode("latin-1"))
        elif HBIOS_PROMPT_RE.search(last_line) or re.search(r"HBIOS>|Boot:|Boot\s*\[", last_line, re.IGNORECASE):
            logger.info("Direct prompt match HBIOS (%r). Sending R", last_line)
            self._write_raw(b"R\r")
        elif FLASH_PROMPT_RE.search(last_line):
            logger.info("Direct prompt match FLASH_UTIL (%r). Sending R", last_line)
            self._write_raw(b"R\r")
        else:
            logger.info("Prompt ambiguous (%r). Sending %s and fallback R",
                        last_line, reboot_cmd.strip())
            self._write_raw(reboot_cmd.encode("latin-1"))
            time.sleep(0.4)
            self._write_raw(b"R\r")
        return {"ok": True}

    def _get_full_screen_history_text(self) -> str:
        lines = []
        with self._screen_lock:
            for row_cells in self._screen.history.top:
                line_str = "".join(char.data for char in row_cells.values()).rstrip()
                lines.append(line_str)
            for r in range(self.rows):
                row_cells = self._screen.buffer[r]
                line_str = "".join(char.data for char in row_cells.values()).rstrip()
                lines.append(line_str)
        return "\n".join(lines)

    def scan_drives_async(self, callback=None):
        def _worker():
            try:
                res = self.scan_drives()
                if callback:
                    callback(res)
            except Exception as e:
                logger.exception("Drive scan failed: %s", e)
                if callback:
                    callback({"ok": False, "error": str(e)})

        threading.Thread(target=_worker, daemon=True).start()

    @_exclusive("drive scan")
    def scan_drives(self) -> dict:
        logger.info("Starting CP/M drive scan...")
        # Fail fast, not through _ensure_cpm_prompt's up-to-20s boot-settle
        # wait: that wait exists for wait_until_ready()'s job of waiting out
        # an in-progress boot, where "not cpm yet" is the expected starting
        # state. hbios/flash_util are conclusively known the instant their
        # banner arrives (_update_system_state, updated synchronously as
        # bytes come in), so there's nothing to wait for. "unknown" is not
        # the same as "conclusively not cpm" - e.g. a screen caught mid
        # boot-profile-output before any prompt-shaped line has arrived yet -
        # so that case still goes through the full wait (and its nudge
        # fallback) rather than failing fast on a guess.
        if self._system_state in ("hbios", "flash_util"):
            return {"ok": False, "error": "System is not at CP/M prompt (A> .. P>)"}
        if not self._ensure_cpm_prompt():
            return {"ok": False, "error": "System is not at CP/M prompt (A> .. P>)"}

        mapped = self.hardware_info.get("drive_mappings", {})
        drives_to_scan = list(mapped.keys()) if mapped else ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]

        self._scan_progress = {"active": True, "drive": "", "index": 0,
                               "total": len(drives_to_scan) + 1}
        listings: dict[str, list[str]] = {}
        try:
            # DIR every drive *first*. CP/M's STAT only reports drives that have
            # been logged in, and a DIR is what logs one in - so asking STAT
            # before the DIRs reports nothing at all for any drive untouched
            # since boot. (Easy to miss on a machine whose drives a previous scan
            # already logged in.)
            for position, drv in enumerate(drives_to_scan, start=1):
                self._scan_progress.update({"drive": f"{drv}:", "index": position})
                # run_command already isolates this command's output, so there's
                # no need to fish the DIR block back out of the screen history.
                listing = self.run_command(f"DIR {drv}:", timeout=DIR_TIMEOUT)
                listings[drv] = _parse_cpm_dir_output(listing.get("output", ""))

            self._scan_progress.update({"drive": "STAT", "index": len(drives_to_scan) + 1})
            # STAT probes every logged-in drive before printing, so it is
            # genuinely slow - ~6s for ten drives. Don't race it.
            stat = self.run_command("STAT", timeout=STAT_TIMEOUT)
            stat_data = _parse_stat_output(stat.get("output", ""))
            if not stat_data:
                # If STAT still outran its timeout, salvage whatever reached the
                # screen rather than reporting every capacity as unknown.
                logger.warning("STAT output not parseable (timed_out=%s); falling back to screen history",
                               stat.get("timed_out"))
                stat_data = _parse_stat_output(self._get_full_screen_history_text())
        finally:
            self._scan_progress = {"active": False, "drive": "", "index": 0, "total": 0}

        results = []
        for drv in drives_to_scan:
            dev_map = mapped.get(drv, "")
            files = listings.get(drv, [])
            st_info = stat_data.get(drv, {})
            access = st_info.get("access", "R/W")
            results.append({
                "drive": f"{drv}:",
                "device": dev_map or "Unknown",
                "files_count": len(files),
                "files_sample": files[:6],
                "free_space": st_info.get("free_space", "?"),
                "access": access,
                "purpose": _classify_drive_purpose(f"{drv}:", files, dev_map, access),
            })

        self.hardware_info["drives"] = results
        self.hardware_info["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_hardware_info()
        logger.info("Drive scan completed. %d drives scanned.", len(results))
        return {"ok": True, "drives": results}

    def _ensure_cpm_prompt(self, settle: float = 1.0,
                           timeout: float = BOOT_SETTLE_TIMEOUT) -> bool:
        """Wait until an OS is running and waiting for a command.

        Deliberately passive. Any console input aborts a CP/M SUBMIT file, and
        PROFILE.SUB is a submit file - so nudging for a prompt during startup
        kills the very script we are waiting for. That is not theoretical: it
        silently stopped a PROFILE.SUB after its first line, leaving the clock
        driver unloaded and file datestamping dead.

        So: wait for the console to fall quiet *and* show a prompt. A CR is sent
        only if the whole timeout passes with no prompt at all, which means
        nothing is running that we could interrupt.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._wait_until_quiet(settle, timeout=max(0.1, deadline - time.time()))
            lines = [l.strip() for l in self.get_screen().get("lines", []) if l.strip()]
            if lines and CPM_PROMPT_RE.search(lines[-1]):
                return True
            if lines and SUBMIT_ECHO_RE.match(lines[-1]):
                logger.debug("Startup submit still running (%r); waiting rather than "
                             "sending a key, which would abort it", lines[-1])
            time.sleep(0.25)

        # Nothing has printed for the whole window and there is no prompt: the
        # console is idle, so a CR is safe and may simply redraw a prompt that
        # scrolled past before we attached.
        logger.info("No prompt after %.0fs of quiet; nudging once", timeout)
        self.run_command("", timeout=5.0)
        lines = [l.strip() for l in self.get_screen().get("lines", []) if l.strip()]
        return bool(lines and CPM_PROMPT_RE.search(lines[-1]))

    def wait_until_ready(self, settle: float = 1.0,
                         timeout: float = BOOT_SETTLE_TIMEOUT) -> dict:
        """Wait for the machine to finish booting and settle at a prompt.

        A boot profile keeps running programs after the OS banner appears, and a
        command sent during that window is simply lost - the board is not reading
        input yet. Seen for real: a 'DIR B:' sent moments after boot arrived as
        'IR B:', its first character swallowed by the profile's own output.

        Waits passively, because PROFILE.SUB is a SUBMIT file and any console
        input aborts one.
        """
        ready = self._ensure_cpm_prompt(settle=settle, timeout=timeout)
        return {"ok": ready, "state": self._system_state,
                "prompt": self._last_prompt if ready else "",
                "error": None if ready else "no prompt after waiting for the machine to settle"}

    @_exclusive("pacing calibration")
    def calibrate_pacing(self, dest_drive: str = None, test_bytes: int = 16384) -> dict:
        """Find the fastest XMODEM write pacing this board actually accepts.

        Tries progressively faster settings, and only accepts one if a file
        survives a full round trip byte-for-byte. Stops at the first failure and
        keeps the last proven setting, so a board is never left configured with
        pacing it cannot handle.

        Writes a temporary file to a scratch drive - the RAM disk by default,
        since it is volatile and cheap to write.

        `test_bytes` needs to be reasonably large: each transfer carries ~15s of
        fixed cost (starting XM, waiting for its handshake poke, mode settles), so
        a small sample makes every setting look equally slow and produces a
        throughput figure several times below the real rate.
        """
        if not self._ensure_cpm_prompt():
            return {"ok": False, "error": "System is not at a CP/M prompt (A> .. P>)"}

        drive = dest_drive or self._ram_disk_drive() or "B:"
        payload = bytes((i * 31 + (0x1A if i % 61 == 0 else 0)) & 0xFF for i in range(test_bytes))
        payload = payload[:-1] + b"\x77"  # must not end in the padding byte
        expected = hashlib.sha256(payload).hexdigest()

        src = os.path.join(tempfile.mkdtemp(prefix="rc2014-pace-"), "PACETEST.BIN")
        with open(src, "wb") as f:
            f.write(payload)
        back = src + ".back"

        original = self.xmodem_pacing
        attempts = []
        best = None
        try:
            for chunk, delay in PACING_CANDIDATES:
                self.xmodem_pacing = (chunk, delay)
                # Time the send phase alone. A whole round trip on a small file is
                # dominated by fixed costs - XM startup, the handshake poke wait,
                # the mode-switch settles - which mask the difference between
                # pacing settings and understate the real gain.
                started = time.time()
                up = self.upload(src, dest_drive=drive, cpm_name="PACETEST.BIN", verify=False)
                send_seconds = time.time() - started
                if not up.get("ok"):
                    attempts.append({"chunk": chunk, "delay": delay, "ok": False,
                                     "error": up.get("error", "upload failed")})
                    logger.info("Pacing %s/%.3fs failed on upload: %s", chunk, delay, up.get("error"))
                    break

                down = self.download(f"{drive}PACETEST.BIN", local_path=back)
                matched = down.get("ok") and down.get("sha256") == expected
                attempts.append({"chunk": chunk, "delay": delay, "ok": bool(matched),
                                 "send_seconds": round(send_seconds, 1),
                                 "bytes_per_sec": int(test_bytes / send_seconds) if send_seconds else 0,
                                 **({} if matched else {"error": down.get("error", "checksum mismatch")})})
                if not matched:
                    logger.info("Pacing %s/%.3fs failed verification", chunk, delay)
                    break
                best = (chunk, delay)
                logger.info("Pacing %s/%.3fs verified (%.1fs to send %d bytes)",
                            chunk, delay, send_seconds, test_bytes)
        finally:
            self.xmodem_pacing = best or original
            self.run_command(f"ERA {drive}PACETEST.BIN", timeout=DIR_TIMEOUT)
            for path in (src, back):
                if os.path.exists(path):
                    os.unlink(path)

        if best is None:
            return {"ok": False, "error": "no pacing setting passed verification",
                    "attempts": attempts, "pacing": list(original)}

        self.hardware_info.setdefault("pacing", {})
        self.hardware_info["pacing"]["xmodem"] = list(best)
        self.hardware_info["pacing"]["text"] = list(self.text_pacing)
        self.hardware_info["pacing"]["calibrated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_hardware_info()

        fastest = next(a for a in reversed(attempts) if a["ok"])
        rate = fastest.get("bytes_per_sec") or 0
        result = {"ok": True, "xmodem_pacing": list(best),
                  "bytes_per_sec": rate,
                  "speedup_vs_default": round(rate / max(attempts[0].get("bytes_per_sec") or 1, 1), 2),
                  "attempts": attempts}
        # Only project from a sample big enough that per-transfer overhead isn't
        # the thing being measured; a 4KB test understated the real rate 5x.
        if rate and test_bytes >= 16384:
            result["projected_512kb_minutes"] = round((512 * 1024) / rate / 60, 1)
        else:
            result["note"] = (f"test_bytes={test_bytes} is too small to project throughput; "
                              "fixed per-transfer overhead dominates. Use >= 16384.")
        return result

    @_exclusive("survey")
    def survey(self) -> dict:
        """Run RomWBW's SURVEY.COM and record what it reports.

        One command, and it covers ground scan_drives can't: per-drive totals
        across *all* user areas (DIR only shows the current one), the memory map
        with BIOS/BDOS addresses, and the active I/O ports.
        """
        # See scan_drives()'s comment: fail fast on a conclusively-not-cpm
        # state rather than paying _ensure_cpm_prompt's up-to-20s boot-settle
        # wait. "unknown" still goes through the full wait/nudge path.
        if self._system_state in ("hbios", "flash_util"):
            return {"ok": False, "error": "System is not at a CP/M prompt (A> .. P>)"}
        if not self._ensure_cpm_prompt():
            return {"ok": False, "error": "System is not at a CP/M prompt (A> .. P>)"}

        rom = self._rom_disk_drive()
        command = f"{rom}:SURVEY" if rom else "SURVEY"
        res = self.run_command(command, timeout=SURVEY_TIMEOUT)
        if res.get("busy"):
            return res

        parsed = _parse_survey_output(res.get("output", ""))
        if not parsed.get("drives") and not parsed.get("memory_map"):
            return {"ok": False, "error": "could not parse SURVEY output",
                    "output": (res.get("output") or "")[:600],
                    "timed_out": res.get("timed_out")}

        parsed["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.hardware_info["survey"] = parsed
        self._save_hardware_info()
        return {"ok": True, "survey": parsed}

    def reconfigure_async(self, port: str = None, baud: int = None, rtscts: bool = None, callback=None):
        def _worker():
            res = self.reconfigure(port=port, baud=baud, rtscts=rtscts)
            if callback:
                callback(res)

        threading.Thread(target=_worker, daemon=True).start()

    @_exclusive("reconfigure connection")
    def reconfigure(self, port: str = None, baud: int = None, rtscts: bool = None,
                     is_default_change: bool = True) -> dict:
        """Re-init the serial connection: closes the current port and opens
        a new one, optionally at a different device path / baud rate / flow
        control setting (any left as None keeps its current value). Used by
        the GUI's Settings screen. @_exclusive so this can't run out from
        under an in-flight XMODEM transfer or drive scan.

        is_default_change controls whether this also updates
        self._default_baud - the rate _check_for_baud_mismatch() falls back
        to if the board resets mid-session. A Settings-screen change is a
        deliberate new resting rate (default True); _follow_hbios_baud_change()
        passes False because that's a runtime-only follow of the board's own
        HBIOS "i" command, not necessarily what it comes back up at.
        """
        new_port = self.port if port is None else port
        new_baud = self.baud if baud is None else baud
        new_rtscts = self._ser.rtscts if rtscts is None else rtscts
        try:
            new_ser = serial.Serial(new_port, baudrate=new_baud, bytesize=8, parity="N",
                                     stopbits=1, timeout=0.1, rtscts=new_rtscts)
        except (serial.SerialException, OSError) as e:
            logger.warning("reconfigure(port=%r, baud=%r, rtscts=%r) failed: %s",
                            port, baud, rtscts, e)
            return {"ok": False, "error": str(e)}

        # Swap under _write_lock so an in-flight _write_raw()/_write_paced()
        # (which read self._ser under the same lock) can't observe a
        # half-updated state - it lands entirely on the old port or entirely
        # on the new one, never a write() call split across the two.
        with self._write_lock:
            old_ser = self._ser
            self._reinit_epoch += 1
            self._ser = new_ser
            self.port = new_port
            self.baud = new_baud
        old_ser.close()
        if is_default_change:
            self._default_baud = new_baud
        logger.info("Serial connection reconfigured: %s @ %d baud, rtscts=%s", new_port, new_baud, new_rtscts)
        return {"ok": True, "port": new_port, "baud": new_baud, "rtscts": new_rtscts}

    def close(self):
        self._stop.set()
        self._reader.join(timeout=1.0)
        self._ser.close()

    def _check_for_baud_mismatch(self, text: str):
        """Recover from the board resetting (reset button, REBOOT, a crashed
        program) while the bridge is still at a baud it only got to via
        _follow_hbios_baud_change()'s runtime-only follow. The board comes
        back up at self._default_baud with no HBIOS text to pattern-match
        on - every byte is misframed - so this looks for sustained garbage
        (see PLAUSIBLE_TEXT_RE) instead and falls back once it's confident.

        No-ops entirely once already at _default_baud, so this can't fight
        a deliberate Settings-screen choice to run somewhere else.
        """
        if self.baud == self._default_baud or len(text) < GARBAGE_MIN_CHUNK_LEN:
            self._garbage_streak = 0
            return

        plausible = len(PLAUSIBLE_TEXT_RE.findall(text))
        if (len(text) - plausible) / len(text) < GARBAGE_RATIO_THRESHOLD:
            self._garbage_streak = 0
            return

        self._garbage_streak += 1
        if self._garbage_streak < GARBAGE_STREAK_TO_ACT:
            return
        now = time.time()
        if now - self._last_garbage_recovery_at < GARBAGE_RECOVERY_COOLDOWN_S:
            return

        self._garbage_streak = 0
        self._last_garbage_recovery_at = now
        logger.warning("Received mostly-unreadable data at %d baud (board may have reset to "
                        "its power-on rate) - falling back to %d baud", self.baud, self._default_baud)
        self.reconfigure(baud=self._default_baud, is_default_change=False)

    def _update_system_state(self, text: str):
        self._check_for_baud_mismatch(text)

        self._boot_buffer += text
        # A fresh banner means a new boot: drop everything before it, or the
        # buffer keeps older banners and the parsers - which take the first match
        # they find - report the previous firmware. That bit for real after a ROM
        # update, where the board had booted v3.7.0 and this still said v3.5.0.
        last_banner = self._boot_buffer.rfind("RomWBW HBIOS v")
        if last_banner > 0:
            self._boot_buffer = self._boot_buffer[last_banner:]
        if len(self._boot_buffer) > 8192:
            self._boot_buffer = self._boot_buffer[-4096:]

        if "RomWBW" in text or "HBIOS" in text or "Boot:" in text or "Boot [" in text:
            parsed = _parse_boot_banner(self._boot_buffer)
            if parsed.get("version") or parsed.get("devices"):
                self.hardware_info.update({k: v for k, v in parsed.items() if v})
                self._save_hardware_info()

        if "ZSDOS" in text or "CBIOS" in text or "Configuring Drives" in text:
            parsed_z = _parse_zsdos_banner(self._boot_buffer)
            if parsed_z.get("zsdos_version") or parsed_z.get("drive_mappings"):
                self.hardware_info.update({k: v for k, v in parsed_z.items() if v})
                self._save_hardware_info()

        for line in text.splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            if CPM_STATE_RE.search(line_s):
                if self._system_state != "cpm":
                    logger.info("Detected RC2014 system state: CPM (prompt: %r)", line_s)
                self._system_state = "cpm"
                self._last_prompt = line_s
            elif HBIOS_PROMPT_RE.search(line_s):
                if self._system_state != "hbios":
                    logger.info("Detected RC2014 system state: HBIOS (prompt: %r)", line_s)
                self._system_state = "hbios"
                self._last_prompt = line_s
            elif FLASH_PROMPT_RE.search(line_s):
                if self._system_state != "flash_util":
                    logger.info("Detected RC2014 system state: FLASH_UTIL (prompt: %r)", line_s)
                self._system_state = "flash_util"
                self._last_prompt = line_s
            elif re.search(r"([A-Za-z0-9_-]+[:>])", line_s):
                self._last_prompt = line_s

        # Checked against the accumulated boot buffer, not text.splitlines()
        # above - the RC2014 echoes typed input one character (or a small
        # handful) at a time, so "Boot [H=Help]: i 0 230400" is essentially
        # never present together in a single read's `text`; only the buffer
        # reliably has the whole line once every character has arrived.
        # HBIOS_BAUD_CMD_RE is MULTILINE so this also matches a command that
        # isn't the buffer's last line (e.g. test input, or a fast/looped-
        # back connection where it arrives alongside later text in one go).
        # Consumed (sliced out of the buffer) the moment it's matched, so it
        # can never be found and re-actioned a second time by a later,
        # unrelated call - re.search() only ever needs to look for one.
        cmd_m = HBIOS_BAUD_CMD_RE.search(self._boot_buffer)
        if cmd_m:
            unit, new_baud = int(cmd_m.group(1)), int(cmd_m.group(2))
            self._boot_buffer = self._boot_buffer[cmd_m.end():]
            if unit == 0:
                logger.info("Detected HBIOS console baud-change command: %d", new_baud)
                self._pending_hbios_baud = new_baud
            else:
                logger.info("Detected HBIOS baud-change command for unit %d "
                            "(not the bridge's console port) - ignoring", unit)

        # Same reasoning: search the buffer (a short confirmation string can
        # straddle two reads), and consume it once matched.
        if self._pending_hbios_baud is not None:
            confirm_m = HBIOS_BAUD_CONFIRM_RE.search(self._boot_buffer)
            if confirm_m:
                new_baud = self._pending_hbios_baud
                self._pending_hbios_baud = None
                self._boot_buffer = self._boot_buffer[confirm_m.end():]
                self._follow_hbios_baud_change(new_baud)

    def _follow_hbios_baud_change(self, new_baud: int):
        """The HBIOS boot loader has switched the console SIO to `new_baud`
        and is waiting for a keystroke to resume - by this point its UART
        has already changed speed, so the bridge must match immediately or
        that resume keystroke (and everything after it) goes out unheard.
        Deliberately does not persist this to the config file: this is a
        boot-loader runtime override, not necessarily what the board will
        come up at next power-on, so the saved default stays under the
        user's explicit control via the Settings screen.
        """
        logger.info("HBIOS switched its console UART to %d baud - following automatically", new_baud)
        res = self.reconfigure(baud=new_baud, is_default_change=False)
        if not res.get("ok"):
            logger.warning("Auto-follow to %d baud failed: %s", new_baud, res.get("error"))

    def enable_pixel_stream(self, enabled: bool = True):
        """Manually force Mandel Pixel-Stream mode on/off - a manual
        override on top of the automatic VERSION_BANNER/END_OF_FRAME
        detection in _read_loop (see its comments), useful for testing or
        to force the view back to terminal early."""
        with self._mode_lock:
            if enabled:
                self._mode = "pixel_stream"
                self._show_pixel_view = True
                self._pixel_decoder.reset()
                logger.info("Enabled Mandel Pixel-Stream decoder mode")
            else:
                if self._mode == "pixel_stream":
                    self._mode = "terminal"
                self._show_pixel_view = False
                logger.info("Disabled Pixel-Stream mode; reverted to terminal")

    def get_pixel_frame(self) -> dict:
        """Get snapshot of current decoded Mandel pixel frame."""
        return self._pixel_decoder.get_current_frame_snapshot()

    # ------------------------------------------------------------------
    # reader thread
    # ------------------------------------------------------------------
    def _read_loop(self):
        while not self._stop.is_set():
            ser = self._ser
            epoch = self._reinit_epoch
            try:
                data = ser.read(4096)
            except Exception as e:
                # pyserial's read() isn't atomic about a concurrent close():
                # depending on timing it raises SerialException, OSError, or
                # (observed on POSIX) a bare TypeError from os.read() seeing
                # fd=None mid-call. Anything here is treated as recoverable
                # if reconfigure() is what caused it (epoch bumped); only a
                # failure with no epoch change is a real disconnect.
                if self._stop.is_set():
                    break
                if self._reinit_epoch != epoch:
                    # reconfigure() closed this ser and swapped in a new one
                    # while the read above was in flight - not a real
                    # disconnect, loop back and read from the fresh self._ser.
                    continue
                logger.warning("Serial read failed on %s - reader thread stopping: %s", self.port, e)
                break
            if not data:
                continue
            self._last_rx_time = time.time()
            with self._mode_lock:
                mode = self._mode
            if mode == "xmodem":
                for b in data:
                    self._xmodem_q.put(b)
                continue

            # terminal/pixel_stream mode-switching is automatic, driven
            # entirely by bytes on the wire (VERSION_BANNER in, END_OF_FRAME
            # out) - no MCP call or human toggle required, so a Mandel
            # pixel-stream render decodes correctly even with nobody
            # watching. A single 4096-byte read can contain a mode
            # transition mid-chunk (e.g. this program's plain-text welcome
            # banner immediately followed by VERSION_BANNER, or the binary
            # frame's END_OF_FRAME immediately followed by its "Computations
            # completed" text), so this walks the chunk and routes each
            # sub-span to whichever pipeline owns it at that point - see
            # protocol/DESIGN.md's Activation section.
            pos = 0
            n = len(data)
            while pos < n:
                with self._mode_lock:
                    mode = self._mode
                if mode == "pixel_stream":
                    consumed_to = self._pixel_decoder.feed(data[pos:])
                    if consumed_to is None:
                        pos = n
                    else:
                        pos += consumed_to
                        with self._mode_lock:
                            self._mode = "terminal"
                else:
                    banner_at = data.find(bytes([VERSION_BANNER]), pos)
                    if banner_at == -1:
                        text = data[pos:].decode("latin-1")
                        self._update_system_state(text)
                        with self._screen_lock:
                            self._stream.feed(text)
                        # Appended last, so a waiter woken by this chunk sees
                        # a screen buffer that already reflects it.
                        self._rx_append(text)
                        pos = n
                    else:
                        if banner_at > pos:
                            text = data[pos:banner_at].decode("latin-1")
                            self._update_system_state(text)
                            with self._screen_lock:
                                self._stream.feed(text)
                            self._rx_append(text)
                        with self._mode_lock:
                            self._mode = "pixel_stream"
                            self._show_pixel_view = True
                        self._pixel_decoder.reset()
                        logger.info("Auto-detected Mandel Pixel-Stream VERSION_BANNER; switching to pixel_stream mode")
                        pos = banner_at

    def _xq_get(self, timeout):
        try:
            return self._xmodem_q.get(timeout=timeout)
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # receive log
    # ------------------------------------------------------------------
    def _rx_append(self, text: str):
        with self._rx_cond:
            self._rx_text += text
            self._rx_seq += len(text)
            if len(self._rx_text) > RX_LOG_MAX:
                self._rx_text = self._rx_text[-RX_LOG_MAX:]
            self._rx_cond.notify_all()

    def rx_position(self) -> int:
        """Opaque marker for 'everything received so far'."""
        with self._rx_cond:
            return self._rx_seq

    def _read_since_locked(self, pos: int) -> tuple[str, int, bool]:
        base = self._rx_seq - len(self._rx_text)
        truncated = pos < base
        start = max(0, pos - base)
        return self._rx_text[start:], self._rx_seq, truncated

    def read_since(self, pos: int) -> tuple[str, int, bool]:
        """Console output received after `pos`, the new position, and whether
        output was dropped from the log before we got to it."""
        with self._rx_cond:
            return self._read_since_locked(pos)

    # ------------------------------------------------------------------
    # raw write (used by both terminal and xmodem paths)
    # ------------------------------------------------------------------
    def _write_raw(self, data: bytes):
        self._last_tx_time = time.time()
        with self._write_lock:
            self._ser.write(data)
            self._ser.flush()

    def _write_paced(self, data: bytes, chunk: int = None, delay: float = None):
        """Write in chunks with a gap between them, so a slow UART keeps up.

        Defaults to this link's XMODEM pacing; callers that want the (slower)
        keyboard-style pacing pass it explicitly.
        """
        if chunk is None or delay is None:
            chunk, delay = self.xmodem_pacing
        self._last_tx_time = time.time()
        with self._write_lock:
            for i in range(0, len(data), chunk):
                self._ser.write(data[i:i + chunk])
                if i + chunk < len(data) and delay > 0:
                    time.sleep(delay)
            self._ser.flush()

    # ------------------------------------------------------------------
    # terminal-mode API
    # ------------------------------------------------------------------
    def send_text(self, text: str, append_enter: bool = True):
        """Write text to the console. Deliberately not serialized behind
        _op_lock: this is the human keystroke path, and a person must be able
        to type (or send Ctrl-X) at any time, including mid-transfer."""
        text = text.replace("\\r", "\r").replace("\\n", "\n")
        if text.endswith("\n") and not text.endswith("\r"):
            text = text[:-1] + "\r"
        elif append_enter and not text.endswith("\r"):
            text += "\r"
        data = text.encode("latin-1")
        if len(data) > 1:
            self._write_paced(data, *self.text_pacing)
        else:
            self._write_raw(data)

    def send_keys(self, keys: str) -> dict:
        """Send raw keystrokes, with no Enter appended.

        The escape hatch for control characters: aborting a stuck XM with
        `^X<PAUSE>^X`, leaving a program with `^C`, answering a pager. See
        _parse_keys() for the accepted mnemonics.

        Deliberately *not* refused while another operation owns the wire - the
        whole point is to be able to interrupt one. That does mean keys sent
        mid-transfer go into the XMODEM stream, which is exactly what a cancel
        needs to do.
        """
        parts = _parse_keys(keys)
        sent = bytearray()
        for kind, value in parts:
            if kind == "pause":
                time.sleep(value)
                continue
            sent.extend(value)
            if len(value) > 1:
                self._write_paced(value, *self.text_pacing)
            else:
                self._write_raw(value)

        logger.info("send_keys(%r) -> %s", keys, sent.hex(" "))
        return {"ok": True, "keys": keys, "bytes_sent": len(sent),
                "hex": sent.hex(" "), "during": self.busy_reason() or "idle"}

    def _rows_to_runs(self, row_cells) -> tuple[list[dict], str]:
        """Collapse one screen row into style runs plus its plain text."""
        row_runs: list[dict] = []
        current_run = None
        line_chars = []
        for c in range(self.cols):
            char = row_cells[c]
            line_chars.append(char.data)
            style = (char.fg, char.bg, char.bold, char.underscore, char.reverse)
            if current_run is not None and (
                current_run["fg"], current_run["bg"], current_run["bold"],
                current_run["underscore"], current_run["reverse"],
            ) == style:
                current_run["text"] += char.data
                continue
            if current_run is not None:
                row_runs.append(current_run)
            current_run = {
                "text": char.data,
                "fg": char.fg,
                "bg": char.bg,
                "bold": char.bold,
                "underscore": char.underscore,
                "reverse": char.reverse,
            }
        if current_run is not None:
            row_runs.append(current_run)
        return row_runs, "".join(line_chars)

    def get_screen(self, scroll_offset: int = 0, max_lines: int = None) -> dict:
        now = time.time()
        rx_active = (now - self._last_rx_time) < 0.250
        tx_active = (now - self._last_tx_time) < 0.250
        with self._screen_lock:
            history_count = len(self._screen.history.top)
            cx, cy = self._screen.cursor.x, self._screen.cursor.y

            if max_lines is not None:
                rows = list(self._screen.history.top) + [self._screen.buffer[r] for r in range(self.rows)]
                if max_lines > 0 and len(rows) > max_lines:
                    rows = rows[-max_lines:]
                offset = 0
            else:
                offset = max(0, min(scroll_offset, history_count))
                rows = []
                for r in range(self.rows):
                    idx = r - offset
                    if idx < 0:
                        if abs(idx) <= history_count:
                            rows.append(self._screen.history.top[idx])
                        else:
                            rows.append(self._screen.buffer[0])
                    else:
                        rows.append(self._screen.buffer[idx])

            runs, lines = [], []
            for row_cells in rows:
                row_runs, line = self._rows_to_runs(row_cells)
                runs.append(row_runs)
                lines.append(line)

        with self._mode_lock:
            current_mode = self._mode

        return {"lines": lines, "cursor": {"x": cx, "y": cy},
                "cols": self.cols, "rows": self.rows, "runs": runs,
                "history_count": history_count, "scroll_offset": offset,
                "port": self.port, "baud": self.baud, "mode": current_mode,
                "rx_active": rx_active, "tx_active": tx_active,
                "system_state": self._system_state, "last_prompt": self._last_prompt,
                "current_op": self._current_op,
                "xmodem_progress": dict(self._xmodem_progress)}

    def wait_for(self, pattern: str, timeout: float = 10.0, since: int = None) -> dict:
        """Wait for `pattern` in output arriving after this call (or after
        `since`). Non-destructive, so concurrent waiters don't interfere, and
        stale scrollback can't produce an instant false match."""
        regex = re.compile(pattern)
        start = self.rx_position() if since is None else since
        deadline = time.time() + timeout
        with self._rx_cond:
            while True:
                text, pos, truncated = self._read_since_locked(start)
                m = regex.search(text)
                if m:
                    return {"matched": True, "text": text, "match": m.group(0),
                            "position": pos, "truncated": truncated}
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"matched": False, "text": text, "position": pos,
                            "truncated": truncated}
                self._rx_cond.wait(min(remaining, 0.25))

    def _wait_until_quiet(self, quiet_for: float, timeout: float = 3.0) -> bool:
        """Wait until nothing has arrived for `quiet_for` seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if time.time() - self._last_rx_time >= quiet_for:
                return True
            time.sleep(0.02)
        return False

    def _wait_for_idle_prompt(self, start_pos: int, timeout: float,
                              idle_settle: float = 0.25, after_echo: str = "",
                              nudge_after: float = None, nudge_bytes: bytes = b" ",
                              max_nudges: int = 500) -> dict:
        """Wait until a prompt is the last thing on the wire and the board has
        gone quiet.

        The quiet period matters: CP/M output can contain something prompt-shaped
        mid-stream, and only a pause tells us it's really done.

        `after_echo` matters more. The previous command's trailing prompt can
        still be on the wire when a new command is sent; it then lands inside
        this window and reads as "done", returning empty for any command slow to
        start printing. The board echoes what we type, so the prompt that ends
        *our* command is the one after our echo - anchor on that.

        `nudge_after` opts into handling a silent pager: some CP/M programs
        (ZCPR3's console driver, used by TYPE on this ZSDOS build) page their
        output by CRT height and block on a keystroke with **no visible
        prompt at all** - not "-- more --", nothing. Left alone, that reads
        indistinguishably from "the command is still working" until the
        overall timeout, so run_command() silently returns just the first
        page. When set, output going quiet for `nudge_after` without a
        matched prompt is treated as a paged wait and answered with one
        `nudge_bytes` keystroke (which the pager consumes without echoing),
        capped at `max_nudges` so a command that's genuinely hung - not
        paged - still times out rather than looping forever.
        """
        deadline = time.time() + timeout
        nudges = 0
        with self._rx_cond:
            while True:
                text, _pos, _trunc = self._read_since_locked(start_pos)
                search_from = 0
                if after_echo:
                    echoed = text.upper().find(after_echo.upper())
                    search_from = -1 if echoed < 0 else echoed + len(after_echo)
                if search_from >= 0:
                    m = TRAILING_PROMPT_RE.search(text, search_from)
                    if m and (time.time() - self._last_rx_time) >= idle_settle:
                        return {"text": text, "prompt": m.group(1).strip(), "timed_out": False,
                                "nudges": nudges}
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"text": text, "prompt": "", "timed_out": True, "nudges": nudges}
                if (nudge_after is not None and nudges < max_nudges
                        and (time.time() - self._last_rx_time) >= nudge_after):
                    logger.debug("no prompt after %.1fs idle; nudging a possible pager (#%d)",
                                nudge_after, nudges + 1)
                    self._write_raw(nudge_bytes)
                    nudges += 1
                # Always wait a beat here, nudge or not - sending one and
                # immediately re-checking would fire a burst of keystrokes
                # before the board has had any chance to react to the first.
                self._rx_cond.wait(min(remaining, idle_settle))

    @_exclusive("command")
    def run_command(self, command: str, timeout: float = 15.0,
                    idle_settle: float = 0.25) -> dict:
        """Send a command and return only the output it produced.

        One call per command: sends, waits for the prompt to come back, strips
        the board's echo and the trailing prompt. Interactive programs that
        don't return to a shell prompt (MBASIC, ED) will time out here - use
        send_text()/get_screen() for those.
        """
        started = time.time()
        # Let anything still in flight land before snapshotting. Otherwise the
        # previous command's trailing prompt arrives inside this call's window
        # and reads as "this command is done" - which silently truncated a slow
        # command's output (seen as a half-captured STAT during a drive scan).
        self._wait_until_quiet(idle_settle)
        start_pos = self.rx_position()
        self.send_text(command)
        res = self._wait_for_idle_prompt(start_pos, timeout, idle_settle,
                                        after_echo=command.strip())
        result = {
            "ok": not res["timed_out"],
            "command": command,
            "output": _strip_echo_and_prompt(res["text"], command),
            "prompt": res["prompt"],
            "state": self._system_state,
            "duration_s": round(time.time() - started, 2),
            "timed_out": res["timed_out"],
        }

        # The HBIOS boot loader acts on single letters, so a multi-character
        # command sent there silently does something else entirely: "DIR B:"
        # triggers D (device inventory), and "REN A=B" would trigger R - a
        # reboot. The prompt comes back either way, so the call looks fine.
        if self._system_state in ("hbios", "flash_util") and len(command.strip()) > 1:
            result["warning"] = (
                f"sent a multi-character command while at the {self._system_state} prompt, "
                f"which acts on single keys - only {command.strip()[0]!r} took effect. "
                "Boot an OS first, or use rc2014_send_text for menu keys."
            )
            logger.warning("run_command(%r) issued at %s prompt", command, self._system_state)
        return result

    # ------------------------------------------------------------------
    # XMODEM sender
    # ------------------------------------------------------------------
    def _xmodem_cancel(self):
        try:
            self._write_raw(bytes([CAN, CAN]))
        except Exception:
            logger.exception("Failed to send XMODEM cancel")

    @_exclusive("XMODEM send")
    def xmodem_send(self, path: str, handshake_timeout: float = 30.0,
                    nudge_prompt: bool = None) -> dict:
        """Send a file over XMODEM to a receiver that is already armed.

        `nudge_prompt` sends a CR afterwards so the console redraws its prompt.
        It defaults to on only when an OS is running: at the HBIOS flash updater
        an unsolicited keystroke lands in a menu at the worst possible moment -
        right after a ROM write, where the documented recovery is to retry the
        transfer rather than touch anything else.
        """
        filename = os.path.basename(path)
        logger.info("xmodem_send initiated for file: %s (path: %s)", filename, path)
        pre_state = self._system_state
        if nudge_prompt is None:
            nudge_prompt = (pre_state == "cpm")
        pre_screen = self.get_screen()
        pre_lines = [l.strip() for l in pre_screen.get("lines", []) if l.strip()]
        pre_prompt = pre_lines[-1] if pre_lines else ""
        logger.debug("Pre-transfer prompt snapshot: %r (state=%s, nudge=%s)",
                     pre_prompt, pre_state, nudge_prompt)

        with self._mode_lock:
            self._mode = "xmodem"
            self._xmodem_progress = {
                "active": True,
                "filename": filename,
                "current_block": 0,
                "total_blocks": 0,
                "bytes": 0,
                "direction": "SEND",
            }
        try:
            time.sleep(0.3)
            with self._xmodem_q.mutex:
                self._xmodem_q.queue.clear()

            with open(path, "rb") as f:
                data = f.read()
            blocks = [data[i:i + BLOCK_SIZE] for i in range(0, len(data), BLOCK_SIZE)] or [b""]
            if len(blocks[-1]) < BLOCK_SIZE:
                blocks[-1] = blocks[-1] + bytes([SUB]) * (BLOCK_SIZE - len(blocks[-1]))

            with self._mode_lock:
                self._xmodem_progress["total_blocks"] = len(blocks)

            logger.info("xmodem_send: file loaded (%d bytes, %d blocks)", len(data), len(blocks))
            logger.info("Waiting for receiver handshake ('C' or NAK, timeout %.1fs)...", handshake_timeout)

            use_crc = None
            deadline = time.time() + handshake_timeout
            while time.time() < deadline:
                b = self._xq_get(timeout=1.0)
                if b == ord("C"):
                    use_crc = True
                    break
                if b == NAK:
                    use_crc = False
                    break
            if use_crc is None:
                logger.error("xmodem_send: handshake timeout waiting for receiver")
                self._xmodem_cancel()
                return {"ok": False, "error": "handshake timeout waiting for receiver"}

            logger.info("xmodem_send: handshake established using %s", "CRC16" if use_crc else "Checksum")

            # RomWBW's XM pokes 'C' then a trailing 'K' (it would prefer 1K
            # blocks). Drain that, or it gets read as the response to block 1.
            # Bounded on both count and time: a receiver that keeps poking while
            # it waits for data - which is ordinary XMODEM behaviour - must not
            # be able to hold us here indefinitely. A poke that slips through
            # afterwards just costs one retry of block 1.
            drain_deadline = time.time() + 0.4
            for _ in range(8):
                if time.time() >= drain_deadline or self._xq_get(timeout=0.1) is None:
                    break

            try:
                for idx, block in enumerate(blocks, start=1):
                    blocknum = idx & 0xFF  # XMODEM sequence numbers wrap at 256
                    pkt = bytes([SOH, blocknum, (~blocknum) & 0xFF]) + block
                    if use_crc:
                        crc = _crc16(block)
                        pkt += bytes([crc >> 8, crc & 0xFF])
                    else:
                        pkt += bytes([_checksum(block)])

                    for attempt in range(1, MAX_RETRIES + 1):
                        self._write_paced(pkt)
                        resp = self._xq_get(timeout=10.0)
                        if resp == ACK:
                            logger.debug("Block %d/%d ACKed", idx, len(blocks))
                            with self._mode_lock:
                                self._xmodem_progress["current_block"] = idx
                                self._xmodem_progress["bytes"] = idx * BLOCK_SIZE
                            break
                        if resp == CAN:
                            logger.warning("Receiver cancelled transfer on block %d", idx)
                            self._xmodem_cancel()
                            return {"ok": False, "error": "receiver cancelled transfer"}
                        logger.warning("Block %d/%d (attempt %d) got response %r, retrying", idx, len(blocks), attempt, hex(resp) if resp else None)
                    else:
                        self._xmodem_cancel()
                        logger.error("Block %d failed after %d retries", idx, MAX_RETRIES)
                        return {"ok": False, "error": f"block {idx} failed after {MAX_RETRIES} retries"}

                logger.info("All %d blocks transmitted successfully. Sending EOT...", len(blocks))
                time.sleep(0.15)  # Allow Z80 receiver time to flush last block to disk/flash

                eot_ack = False
                for attempt in range(1, 6):
                    self._write_raw(bytes([EOT]))
                    resp = self._xq_get(timeout=1.0)
                    logger.debug("EOT attempt %d response: %r", attempt, hex(resp) if resp else None)
                    if resp == ACK:
                        eot_ack = True
                        break
                    if resp == NAK:
                        continue

                logger.info("EOT sequence finished (ACKed: %s). Switching back to terminal mode.", eot_ack)

                # Switch back to terminal mode so incoming serial output feeds
                # into the screen buffer again.
                with self._mode_lock:
                    self._mode = "terminal"
                if nudge_prompt:
                    start_pos = self.rx_position()
                    self._write_raw(b"\r")
                    settled = self._wait_for_idle_prompt(start_pos, timeout=4.0)
                    if not settled["timed_out"]:
                        logger.info("Verified prompt return after transfer: %r", settled["prompt"])
                else:
                    logger.info("Skipping post-transfer prompt nudge (state=%s) - not sending "
                                "keystrokes to a non-OS receiver such as the flash updater",
                                pre_state)

                logger.info("xmodem_send complete (%d blocks).", len(blocks))
                return {"ok": True, "blocks": len(blocks), "bytes": len(data),
                        "eot_acked": eot_ack, "prompt_nudged": nudge_prompt}
            except Exception as e:  # noqa: BLE001 - never leave the receiver hanging
                logger.exception("Unexpected exception during xmodem_send: %s", e)
                self._xmodem_cancel()
                return {"ok": False, "error": f"unexpected error: {e}"}
        finally:
            with self._mode_lock:
                self._mode = "terminal"
                self._xmodem_progress["active"] = False

    # ------------------------------------------------------------------
    # XMODEM receiver
    # ------------------------------------------------------------------
    @_exclusive("XMODEM receive")
    def xmodem_receive(self, path: str, handshake_timeout: float = 30.0,
                        overall_timeout: float = 120.0,
                        strip_padding: bool = True) -> dict:
        filename = os.path.basename(path)
        with self._mode_lock:
            self._mode = "xmodem"
            self._xmodem_progress = {
                "active": True,
                "filename": filename,
                "current_block": 0,
                "total_blocks": 0,
                "bytes": 0,
                "direction": "RECV",
            }
        try:
            time.sleep(0.3)  # see comment in xmodem_send
            with self._xmodem_q.mutex:
                self._xmodem_q.queue.clear()

            out = bytearray()
            use_crc = True
            pokes = 0
            started = time.time()
            deadline = started + overall_timeout
            next_poke = 0.0
            expect_block = 1
            got_first = False
            last_block_size = BLOCK_SIZE

            while time.time() < deadline:
                if not got_first:
                    if time.time() - started > handshake_timeout:
                        self._xmodem_cancel()
                        return {"ok": False, "error": "handshake timeout waiting for sender"}
                    if time.time() >= next_poke:
                        # Some senders only speak the original checksum protocol
                        # and never answer a 'C'. Fall back rather than stall.
                        if use_crc and pokes >= 4:
                            logger.info("No reply to %d CRC pokes; falling back to checksum mode", pokes)
                            use_crc = False
                        self._write_raw(bytes([ord("C") if use_crc else NAK]))
                        pokes += 1
                        next_poke = time.time() + 3.0

                b0 = self._xq_get(timeout=1.0)
                if b0 is None:
                    continue
                if b0 == EOT:
                    self._write_raw(bytes([ACK]))
                    if strip_padding:
                        while out and out[-1] == SUB:
                            out.pop()
                    with open(path, "wb") as f:
                        f.write(bytes(out))
                    return {"ok": True, "bytes": len(out), "blocks": expect_block - 1,
                            "block_size": last_block_size}
                if b0 == CAN:
                    if self._xq_get(timeout=1.0) == CAN:
                        return {"ok": False, "error": "sender cancelled transfer"}
                    continue
                if b0 not in (SOH, STX):
                    continue  # ignore stray bytes between blocks

                # RomWBW's XM asks for 1K blocks; honour STX if it sends them.
                size = BLOCK_SIZE if b0 == SOH else LONG_BLOCK_SIZE
                last_block_size = size
                got_first = True
                blk = self._xq_get(timeout=5.0)
                nblk = self._xq_get(timeout=5.0)
                payload = bytearray()
                for _ in range(size):
                    byte = self._xq_get(timeout=5.0)
                    if byte is None:
                        break
                    payload.append(byte)
                if use_crc:
                    c1, c2 = self._xq_get(timeout=5.0), self._xq_get(timeout=5.0)
                    ok_sum = (c1 is not None and c2 is not None
                              and (c1 << 8 | c2) == _crc16(bytes(payload)))
                else:
                    c1 = self._xq_get(timeout=5.0)
                    ok_sum = c1 is not None and c1 == _checksum(bytes(payload))

                valid = (blk is not None and nblk is not None and (blk ^ nblk) == 0xFF
                         and len(payload) == size and ok_sum)
                if valid and blk == (expect_block & 0xFF):
                    out.extend(payload)
                    expect_block += 1
                    with self._mode_lock:
                        self._xmodem_progress["current_block"] = expect_block - 1
                        self._xmodem_progress["bytes"] = len(out)
                    self._write_raw(bytes([ACK]))
                elif valid and blk == ((expect_block - 1) & 0xFF):
                    # sender already sent this block (our ACK got lost) - ack again, don't re-append
                    self._write_raw(bytes([ACK]))
                else:
                    self._write_raw(bytes([NAK]))

            self._xmodem_cancel()
            return {"ok": False, "error": "timed out waiting for sender"}
        finally:
            with self._mode_lock:
                self._mode = "terminal"
                self._xmodem_progress["active"] = False

    def xmodem_send_async(self, path: str, callback=None):
        def _worker():
            res = self.xmodem_send(path)
            if callback:
                callback(res)
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    def xmodem_receive_async(self, path: str, callback=None):
        def _worker():
            res = self.xmodem_receive(path)
            if callback:
                callback(res)
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------
    # composite file operations
    # ------------------------------------------------------------------
    def _recover_console(self) -> bool:
        """Get back to a shell prompt after XM failed to start.

        A refusal does not necessarily mean XM exited. Uploading to the ROM disk,
        for instance, leaves ZSDOS sitting on "Bad Sector" waiting for a
        keypress, and dismissing that hands control *back* to XM, which then arms
        and pokes forever. Send XM's own documented cancel - Ctrl-X, pause,
        Ctrl-X, which is the CAN CAN pair - and only then nudge for a prompt.
        """
        for _ in range(3):
            self._write_raw(bytes([CAN]))
            time.sleep(0.3)
            self._write_raw(bytes([CAN]))
            time.sleep(0.2)
            start = self.rx_position()
            self._write_raw(b"\r")
            if not self._wait_for_idle_prompt(start, timeout=3.0)["timed_out"]:
                return True
        logger.warning("Could not get the console back to a prompt after XM failed")
        return False

    def _arm_xm(self, direction: str, target: str, block_1k: bool = False) -> dict:
        """Run XM on the board and decide whether it's really ready to transfer.

        XM's banner is not a commitment. RomWBW's XM prints

            Receiving: B0:BUSY.BIN
            230k available for uploads
            ++ File exists, use a different name ++
            A>

        so matching "Receiving:" and diving straight into the protocol means
        waiting out the full handshake timeout against a program that has
        already exited. The reliable negative signal is the shell prompt coming
        back: if XM is still running there is no prompt, and if it gave up there
        is. Watch for that (or an explicit error) before trusting the banner.

        An unrecognised banner is still not treated as failure - the wording
        varies by RomWBW version, and the transfer's own handshake is the
        backstop.
        """
        verb = "R" if direction == "receive" else "S"
        if block_1k:
            if direction == "receive":
                raise ValueError("no 1K receive mode exists on the R verb (RK is not real XM)")
            verb += "K"
        start = self.rx_position()
        self.send_text(f"{self._xm_command()} {verb} {target}")
        res = self.wait_for(r"Receiving|Sending|File open|To cancel|Ctrl-X",
                            timeout=8.0, since=start)

        deadline = time.time() + XM_ARM_SETTLE
        while True:
            text, _pos, _truncated = self.read_since(start)
            err = XM_ERROR_RE.search(text)
            if err:
                # Report the whole offending line: the matched fragment on its own
                # ("Bad", from "ZSDOS error on C: Bad Sector") isn't actionable.
                line = next((l.strip() for l in reversed(text.splitlines())
                             if l.strip() and XM_ERROR_RE.search(l)), err.group(0))
                return {"ok": False, "error": f"XM refused: {line}",
                        "screen": text[-400:].strip(),
                        "recovered": self._recover_console()}
            if TRAILING_PROMPT_RE.search(text):
                # XM is already gone and the shell is back; nothing to recover.
                return {"ok": False,
                        "error": "XM exited without starting a transfer",
                        "screen": text[-400:].strip(), "recovered": True}
            if time.time() >= deadline:
                break
            time.sleep(0.1)

        if not res["matched"]:
            logger.warning("XM banner not recognised; proceeding on handshake. Saw: %r", text[-200:])
        return {"ok": True, "armed": res["matched"]}

    def _file_exists(self, target: str) -> bool:
        listing = self.run_command(f"DIR {target}", timeout=DIR_TIMEOUT)
        output = (listing.get("output") or "").upper()
        stem = target.split(":")[-1].split(".")[0].upper()
        return "NO FILE" not in output and bool(stem) and stem in output

    def _encode_upload_content(self, content: str, binary: bool) -> bytes:
        if binary:
            return base64.b64decode(content)
        body = content.replace("\r\n", "\n").replace("\n", "\r\n")
        data = body.encode("latin-1", errors="replace")
        if not data.endswith(bytes([SUB])):
            data += bytes([SUB])  # CP/M text EOF marker
        return data

    @_exclusive("upload")
    def upload(self, name: str, drive: str, user: int = 0, content: str = "",
               binary: bool = False, overwrite: bool = False) -> dict:
        """Copy content to the board: zip it, arm XM, transfer, UNZIP, confirm.

        Always zips - the container overhead is noise at these file sizes,
        and extraction buys a whole-file CRC check on the way in that a raw
        transfer wouldn't get. Text content (binary=False) gets CRLF line
        endings and a trailing 0x1A CP/M EOF marker before zipping, matching
        what TYPE and other tools expect.

        RomWBW's UNZIP refuses to overwrite an existing file (reports
        "EXISTS" and skips it - confirmed against real hardware) just like
        XM does, so overwrite=True erases the existing target first.
        """
        if self._system_state != "cpm":
            return {"ok": False, "error": f"system is not at a CP/M prompt "
                                          f"(state={self._system_state!r}); boot an OS first"}
        err = _validate_cpm_name(name)
        if err:
            return {"ok": False, "error": err}
        if not (0 <= user <= 15):
            return {"ok": False, "error": f"user area must be 0-15, got {user}"}

        name = name.upper()
        drive = drive.rstrip(":").upper()
        target = _format_target(drive, user, name)
        started = time.time()

        exists = self._file_exists(target)
        if exists and not overwrite:
            stat = self.run_command(f"STAT {target}", timeout=DIR_TIMEOUT)
            return {"ok": False, "target": target, "existed_before": True,
                    "error": f"{target} already exists and overwrite is disabled",
                    **_parse_stat_file_info(stat.get("output", ""))}

        raw = self._encode_upload_content(content, binary)
        zip_bytes = _zip_single_entry(name, raw)

        stat = self.run_command(f"STAT {drive}:", timeout=DIR_TIMEOUT)
        free = _parse_stat_free_space(stat.get("output", ""))
        needed = len(zip_bytes) + len(raw)  # zip and its extracted copy briefly coexist
        if free is not None and free < needed:
            return {"ok": False, "target": target, "error": "insufficient space",
                    "available": free, "needed": needed}

        stem = name.split(".")[0]
        zip_target = f"{drive}:{stem}.ZIP"

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="rc2014-", suffix=".zip", delete=False) as tf:
                tf.write(zip_bytes)
                tmp_path = tf.name

            armed = self._arm_xm("receive", zip_target)
            if not armed["ok"]:
                return {**armed, "target": target}

            send_res = self.xmodem_send(tmp_path)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        if not send_res.get("ok"):
            return {**send_res, "target": target}

        if exists:
            # UNZIP won't replace an existing file (see docstring); erase it
            # first, same as the old XM-direct path had to.
            self.run_command(f"ERA {target}", timeout=DIR_TIMEOUT)

        self.run_command(f"UNZIP {zip_target} {_format_target(drive, user, '')}",
                         timeout=DIR_TIMEOUT)
        self.run_command(f"ERA {zip_target}", timeout=DIR_TIMEOUT)

        verified = self._file_exists(target)
        return {
            "ok": verified,
            "target": target,
            "existed_before": exists,
            "replaced_existing": exists,
            "bytes_raw": len(raw),
            "bytes_wire": len(zip_bytes),
            "compressed": True,
            "blocks": send_res.get("blocks"),
            "block_size": BLOCK_SIZE,
            "duration_s": round(time.time() - started, 2),
            "verified": verified,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    @_exclusive("download")
    def download(self, name: str, drive: str, user: int = 0, binary: bool = False) -> dict:
        """Copy a file off the board: arm XM to send in 1K blocks, receive it.

        1K blocks (XM's SK mode) only exist on the send verb - since the
        board sends on download, this gets them for free, unlike upload
        which is stuck at 128-byte blocks regardless. Content comes back
        inline, not written to a host path.

        binary=True is byte-exact only to the file's CP/M record granularity
        (128 bytes) - confirmed against real hardware. CP/M/ZSDOS has no
        sub-record length anywhere in the filesystem (STAT only ever reports
        whole records), so a binary file whose true length isn't a multiple
        of 128 comes back padded with whatever trailing bytes that record
        holds (typically, but not reliably, 0x1A). This is what the CRLF +
        0x1A EOF-marker convention exists to solve for text (binary=False);
        there is no equivalent for arbitrary binary content, and nothing this
        bridge does can recover the precision CP/M itself never stored.
        """
        if self._system_state != "cpm":
            return {"ok": False, "error": f"system is not at a CP/M prompt "
                                          f"(state={self._system_state!r}); boot an OS first"}
        err = _validate_cpm_name(name)
        if err:
            return {"ok": False, "error": err}
        if not (0 <= user <= 15):
            return {"ok": False, "error": f"user area must be 0-15, got {user}"}

        name = name.upper()
        drive = drive.rstrip(":").upper()
        target = _format_target(drive, user, name)
        started = time.time()

        if not self._file_exists(target):
            return {"ok": False, "target": target, "error": "not found"}

        armed = self._arm_xm("send", target, block_1k=True)
        if not armed["ok"]:
            return {**armed, "target": target}

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="rc2014-", delete=False) as tf:
                tmp_path = tf.name
            # strip_padding removes trailing 0x1A indiscriminately - right for
            # text (that's our own EOF marker), wrong for binary=True's "raw
            # bytes in, raw bytes out" promise if real data ends in 0x1A.
            res = self.xmodem_receive(tmp_path, strip_padding=not binary)
            if not res.get("ok"):
                return {**res, "target": target}
            with open(tmp_path, "rb") as f:
                raw = f.read()
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        if binary:
            content = base64.b64encode(raw).decode("ascii")
        else:
            text = raw.split(bytes([SUB]), 1)[0].decode("latin-1")
            content = text.replace("\r\n", "\n")

        return {
            "ok": True,
            "target": target,
            "content": content,
            "binary": binary,
            "bytes_raw": len(raw),
            "bytes_wire": len(raw),
            "compressed": False,
            "blocks": res.get("blocks"),
            "block_size": res.get("block_size"),
            "duration_s": round(time.time() - started, 2),
            "verified": True,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    @_exclusive("read file")
    def read_text_file(self, cpm_path: str, max_bytes: int = 8192,
                       timeout: float = 30.0) -> dict:
        """Read a text file with CP/M's TYPE.

        Cheap and needs no transfer, but TYPE expands tabs and stops at the
        0x1A EOF marker, so this is not byte-exact - use download() when it
        has to be.

        This ZSDOS build's console driver pages TYPE's output by CRT height
        and blocks for a keystroke between pages - silently, with no visible
        "-- more --" or any other marker. A plain wait-for-prompt reads that
        as "still running" right up to the timeout and returns just the
        first page, so this sends a nudge keystroke whenever output goes
        quiet without a prompt in sight (see _wait_for_idle_prompt) rather
        than going through the unpaged run_command().
        """
        command = f"TYPE {cpm_path}"
        self._wait_until_quiet(0.25)
        start_pos = self.rx_position()
        self.send_text(command)
        waited = self._wait_for_idle_prompt(start_pos, timeout, idle_settle=0.25,
                                            after_echo=command.strip(), nudge_after=1.0)
        res = {
            "ok": not waited["timed_out"],
            "command": command,
            "output": _strip_echo_and_prompt(waited["text"], command),
            "prompt": waited["prompt"],
            "timed_out": waited["timed_out"],
        }
        content = res.get("output", "")
        if XM_ERROR_RE.search(content[:200]):
            return {"ok": False, "error": f"could not read {cpm_path}",
                    "output": content[:400], "cpm_path": cpm_path}
        truncated = len(content) > max_bytes
        return {
            "ok": res["ok"],
            "cpm_path": cpm_path,
            "content": content[:max_bytes],
            "truncated": truncated,
            "timed_out": res["timed_out"],
        }
