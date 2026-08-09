"""
Owns the serial connection to the RC2014. One background thread reads the
port continuously; everything else (the pygame display, the API server)
goes through this object rather than touching the port directly.

Two ways incoming bytes get interpreted, controlled by self._mode:
  - "terminal": fed into a pyte VT100 screen buffer (for display.py to
    render) and appended to a scrollback buffer (for wait_for()).
  - "xmodem": routed byte-by-byte to a queue that xmodem_send/receive
    consume directly, bypassing the terminal emulator entirely.

The port itself is opened once and held for the object's whole lifetime;
switching modes never closes or reacquires it.
"""

import logging
import os
import queue
import re
import threading
import time

import pyte
import serial

logger = logging.getLogger("rc2014bridge")

SOH, EOT, ACK, NAK, CAN, SUB = 0x01, 0x04, 0x06, 0x15, 0x18, 0x1A
BLOCK_SIZE = 128
MAX_RETRIES = 10


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _to_cpm_filename(path: str) -> str:
    base = os.path.basename(path)
    name, ext = os.path.splitext(base)
    ext = ext.lstrip(".")
    cpm_name = re.sub(r"[^A-Za-z0-9]", "", name)[:8].upper()
    cpm_ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:3].upper()
    return f"{cpm_name}.{cpm_ext}" if cpm_ext else cpm_name


import json


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

    m = re.search(r"((?:RC2014 )?(?:Z80|Z180|eZ80) @ [0-9\.]+MHz)", text, re.IGNORECASE)
    if m:
        info["cpu"] = m.group(1)

    m = re.search(r"(\d+ MEM W/S, \d+ I/O W/S)", text, re.IGNORECASE)
    if m:
        info["wait_states"] = m.group(1)

    m = re.search(r"(INT MODE \d+)", text, re.IGNORECASE)
    if m:
        info["int_mode"] = m.group(1)

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


def _extract_drive_dir_block(full_text: str, drive_letter: str) -> str:
    pattern = rf"[A-P]>(?:DIR\s+)?{drive_letter}:?(.*?)(?=[A-P]>|\Z)"
    m = re.search(pattern, full_text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


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


def _classify_drive_purpose(drive: str, files: list[str], device_map: str = "") -> str:
    files_upper = [f.upper() for f in files]
    dev_upper = device_map.upper()

    if "MD1" in dev_upper or ("XM.COM" in files_upper and "FLASH.COM" in files_upper):
        return "ROM System Disk (Read-Only)"
    if "MD0" in dev_upper:
        return "RAM Volatile Disk (MD0)"
    if any(f in files_upper for f in ["ZPATH.COM", "STAT.COM", "PIP.COM", "SUBMIT.COM", "CCP.COM"]):
        return "ZSDOS / CP/M System Boot Disk"

    code_exts = (".Z80", ".PAS", ".C", ".BAS", ".ASM", ".HEX", ".TXT", ".MAC", ".SUB", ".PRN")
    has_code = any(any(f.endswith(ext) for ext in code_exts) for f in files_upper)
    if has_code or (files_upper and not any(f.endswith(".COM") for f in files_upper)):
        return "User Programming & Source Code"
    if files_upper:
        return "General Application / Data Volume"
    return "Empty / Unformatted Volume"


class SerialLink:
    def __init__(self, port: str, baud: int = 115200, cols: int = 80, rows: int = 24, hw_info_file: str = "hardware_info.json"):
        self.port = port
        self.baud = baud
        self.cols, self.rows = cols, rows
        self.hw_info_file = hw_info_file
        self._ser = serial.Serial(port, baudrate=baud, bytesize=8, parity="N",
                                   stopbits=1, timeout=0.1)
        self._write_lock = threading.Lock()

        self._screen = pyte.HistoryScreen(cols, rows, history=1000)
        self._stream = pyte.Stream(self._screen)
        self._screen_lock = threading.Lock()

        self._pending = []
        self._pending_lock = threading.Lock()

        self._mode = "terminal"
        self._mode_lock = threading.Lock()
        self._xmodem_q: "queue.Queue[int]" = queue.Queue()

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

        self._system_state = "unknown"
        self._last_prompt = ""
        self.hardware_info = self._load_hardware_info()
        self._boot_buffer = ""

        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        # Send an initial CR to refresh prompt and detect system state immediately
        def _initial_prompt_probe():
            time.sleep(0.15)
            self._write_raw(b"\r")
        threading.Thread(target=_initial_prompt_probe, daemon=True).start()

    # ------------------------------------------------------------------
    # hardware info & reboot lifecycle
    # ------------------------------------------------------------------
    def _load_hardware_info(self) -> dict:
        if os.path.exists(self.hw_info_file):
            try:
                with open(self.hw_info_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "version": "RomWBW v3.0.1",
            "cpu": "RC2014 Z80 @ 7.372MHz",
            "wait_states": "0 MEM W/S, 1 I/O W/S",
            "int_mode": "INT MODE 1",
            "memory": "Z2 MMU, 512KB ROM, 512KB RAM",
            "devices": ["SIO0: IO=0x80 (Console)", "SIO1: IO=0x82", "IDE0: CompactFlash (123MB)", "MD0: RAM Disk (512KB)", "MD1: ROM Disk (384KB)"],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _save_hardware_info(self):
        try:
            with open(self.hw_info_file, "w") as f:
                json.dump(self.hardware_info, f, indent=2)
            logger.info("Saved hardware info to %s", self.hw_info_file)
        except Exception as e:
            logger.warning("Failed to save hardware info: %s", e)

    def reboot(self):
        screen = self.get_screen()
        lines = [l.strip() for l in screen.get("lines", []) if l.strip()]
        last_line = lines[-1] if lines else ""
        logger.info("reboot() called. Last line on screen: %r, current system_state: %r", last_line, self._system_state)

        with self._mode_lock:
            self._mode = "terminal"

        self._system_state = "unknown"

        # Check prompt directly from current rendered screen
        if re.search(r"^[A-P]>|^[0-9]+[A-P]>", last_line):
            logger.info("Direct prompt match CP/M (%r). Sending C:REBOOT /C", last_line)
            self._write_raw(b"C:REBOOT /C\r")
        elif re.search(r"HBIOS>|Boot:|Boot\s*\[", last_line, re.IGNORECASE):
            logger.info("Direct prompt match HBIOS (%r). Sending R", last_line)
            self._write_raw(b"R\r")
        elif re.search(r"FDU>|FLASH>", last_line, re.IGNORECASE):
            logger.info("Direct prompt match FLASH_UTIL (%r). Sending R", last_line)
            self._write_raw(b"R\r")
        else:
            logger.info("Prompt ambiguous (%r). Sending C:REBOOT /C and fallback R", last_line)
            self._write_raw(b"C:REBOOT /C\r")
            time.sleep(0.4)
            self._write_raw(b"R\r")

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

    def scan_drives(self) -> dict:
        logger.info("Starting CP/M drive scan...")
        screen = self.get_screen()
        lines = [l.strip() for l in screen.get("lines", []) if l.strip()]
        last_line = lines[-1] if lines else ""

        if not re.search(r"^[A-P]>|^[0-9]+[A-P]>", last_line):
            return {"ok": False, "error": "System is not at CP/M prompt (A> .. P>)"}

        # Query STAT first for drive capacity and access modes
        time.sleep(0.2)
        self._write_paced(b"STAT\r", chunk=1, delay=0.015)
        deadline = time.time() + 3.0
        sc = {}
        while time.time() < deadline:
            time.sleep(0.15)
            sc = self.get_screen()
            sc_lines = [l.strip() for l in sc.get("lines", []) if l.strip()]
            if sc_lines and re.search(r"^[A-P]>|^[0-9]+[A-P]>", sc_lines[-1]):
                break

        full_hist_text = self._get_full_screen_history_text()
        stat_data = _parse_stat_output(full_hist_text)

        mapped = self.hardware_info.get("drive_mappings", {})
        drives_to_scan = list(mapped.keys()) if mapped else ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]

        results = []
        for drv in drives_to_scan:
            dev_map = mapped.get(drv, "")
            time.sleep(0.2)

            self._write_paced(f"DIR {drv}:\r".encode("latin-1"), chunk=1, delay=0.015)

            deadline = time.time() + 3.0
            sc = {}
            while time.time() < deadline:
                time.sleep(0.15)
                sc = self.get_screen()
                sc_lines = [l.strip() for l in sc.get("lines", []) if l.strip()]
                if sc_lines and re.search(r"^[A-P]>|^[0-9]+[A-P]>", sc_lines[-1]):
                    break

            full_history_text = self._get_full_screen_history_text()
            dir_block = _extract_drive_dir_block(full_history_text, drv)
            files = _parse_cpm_dir_output(dir_block) if dir_block else []
            purpose = _classify_drive_purpose(f"{drv}:", files, dev_map)
            st_info = stat_data.get(drv, {})

            drv_info = {
                "drive": f"{drv}:",
                "device": dev_map or "Unknown",
                "files_count": len(files),
                "files_sample": files[:6],
                "free_space": st_info.get("free_space", "?"),
                "access": st_info.get("access", "R/W"),
                "purpose": purpose,
            }
            results.append(drv_info)

        self.hardware_info["drives"] = results
        self.hardware_info["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_hardware_info()
        logger.info("Drive scan completed. %d drives scanned.", len(results))
        return {"ok": True, "drives": results}

    def close(self):
        self._stop.set()
        self._reader.join(timeout=1.0)
        self._ser.close()

    def _update_system_state(self, text: str):
        self._boot_buffer += text
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
            if re.search(r"^[A-P]>|^[0-9]+[A-P]>|^[A-P][0-9]*:", line_s):
                if self._system_state != "cpm":
                    logger.info("Detected RC2014 system state: CPM (prompt: %r)", line_s)
                self._system_state = "cpm"
                self._last_prompt = line_s
            elif re.search(r"^HBIOS>|^Boot:|^Boot\s*\[|^Select \(A-F", line_s, re.IGNORECASE):
                if self._system_state != "hbios":
                    logger.info("Detected RC2014 system state: HBIOS (prompt: %r)", line_s)
                self._system_state = "hbios"
                self._last_prompt = line_s
            elif re.search(r"^FDU>|^FLASH>|^Command\?", line_s, re.IGNORECASE):
                if self._system_state != "flash_util":
                    logger.info("Detected RC2014 system state: FLASH_UTIL (prompt: %r)", line_s)
                self._system_state = "flash_util"
                self._last_prompt = line_s
            elif re.search(r"([A-Za-z0-9_-]+[:>])", line_s):
                self._last_prompt = line_s

    # ------------------------------------------------------------------
    # reader thread
    # ------------------------------------------------------------------
    def _read_loop(self):
        while not self._stop.is_set():
            try:
                data = self._ser.read(4096)
            except serial.SerialException:
                break
            if not data:
                continue
            self._last_rx_time = time.time()
            with self._mode_lock:
                mode = self._mode
            if mode == "xmodem":
                for b in data:
                    self._xmodem_q.put(b)
            else:
                text = data.decode("latin-1")
                self._update_system_state(text)
                with self._screen_lock:
                    self._stream.feed(text)
                with self._pending_lock:
                    self._pending.append(text)

    def _xq_get(self, timeout):
        try:
            return self._xmodem_q.get(timeout=timeout)
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # raw write (used by both terminal and xmodem paths)
    # ------------------------------------------------------------------
    def _write_raw(self, data: bytes):
        self._last_tx_time = time.time()
        with self._write_lock:
            self._ser.write(data)
            self._ser.flush()

    def _write_paced(self, data: bytes, chunk: int = 8, delay: float = 0.010):
        self._last_tx_time = time.time()
        with self._write_lock:
            for i in range(0, len(data), chunk):
                self._ser.write(data[i:i + chunk])
                if i + chunk < len(data):
                    time.sleep(delay)
            self._ser.flush()

    # ------------------------------------------------------------------
    # terminal-mode API
    # ------------------------------------------------------------------
    def send_text(self, text: str):
        data = text.encode("latin-1")
        if len(data) > 1:
            self._write_paced(data, chunk=1, delay=0.015)
        else:
            self._write_raw(data)

    def get_screen(self, scroll_offset: int = 0) -> dict:
        now = time.time()
        rx_active = (now - self._last_rx_time) < 0.250
        tx_active = (now - self._last_tx_time) < 0.250
        with self._screen_lock:
            history_count = len(self._screen.history.top)
            offset = max(0, min(scroll_offset, history_count))
            cx, cy = self._screen.cursor.x, self._screen.cursor.y
            runs = []
            lines = []
            for r in range(self.rows):
                idx = r - offset
                if idx < 0:
                    if abs(idx) <= history_count:
                        row_cells = self._screen.history.top[idx]
                    else:
                        row_cells = self._screen.buffer[0]
                else:
                    row_cells = self._screen.buffer[idx]

                row_runs = []
                current_run = None
                line_chars = []
                for c in range(self.cols):
                    char = row_cells[c]
                    line_chars.append(char.data)
                    style = (char.fg, char.bg, char.bold, char.underscore, char.reverse)
                    if current_run is None:
                        current_run = {
                            "text": char.data,
                            "fg": char.fg,
                            "bg": char.bg,
                            "bold": char.bold,
                            "underscore": char.underscore,
                            "reverse": char.reverse,
                        }
                    elif (current_run["fg"], current_run["bg"], current_run["bold"], current_run["underscore"], current_run["reverse"]) == style:
                        current_run["text"] += char.data
                    else:
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
                runs.append(row_runs)
                lines.append("".join(line_chars))

        with self._mode_lock:
            current_mode = self._mode

        return {"lines": lines, "cursor": {"x": cx, "y": cy},
                "cols": self.cols, "rows": self.rows, "runs": runs,
                "history_count": history_count, "scroll_offset": offset,
                "port": self.port, "baud": self.baud, "mode": current_mode,
                "rx_active": rx_active, "tx_active": tx_active,
                "system_state": self._system_state, "last_prompt": self._last_prompt,
                "xmodem_progress": dict(self._xmodem_progress)}


    def get_new_output(self) -> str:
        with self._pending_lock:
            chunks, self._pending = self._pending, []
        return "".join(chunks)

    def wait_for(self, pattern: str, timeout: float = 10.0) -> dict:
        regex = re.compile(pattern)
        acc = ""
        deadline = time.time() + timeout
        while True:
            acc += self.get_new_output()
            m = regex.search(acc)
            if m:
                return {"matched": True, "text": acc, "match": m.group(0)}
            if time.time() >= deadline:
                return {"matched": False, "text": acc}
            time.sleep(0.05)


    # ------------------------------------------------------------------
    # XMODEM sender
    # ------------------------------------------------------------------
    def xmodem_send(self, path: str, handshake_timeout: float = 30.0) -> dict:
        filename = os.path.basename(path)
        logger.info("xmodem_send initiated for file: %s (path: %s)", filename, path)
        pre_screen = self.get_screen()
        pre_lines = [l.strip() for l in pre_screen.get("lines", []) if l.strip()]
        pre_prompt = pre_lines[-1] if pre_lines else ""
        generic_prompt_pattern = re.compile(r"([A-P]>|[0-9]+[A-P]>|HBIOS>|Boot:|FDU>|FLASH>|\w+>|\w+:)")
        logger.debug("Pre-transfer prompt snapshot: %r", pre_prompt)

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
                return {"ok": False, "error": "handshake timeout waiting for receiver"}

            logger.info("xmodem_send: handshake established using %s", "CRC16" if use_crc else "Checksum")

            while self._xq_get(timeout=0.4) is not None:
                pass

            try:
                blocknum = 1
                for idx, block in enumerate(blocks, start=1):
                    for attempt in range(1, MAX_RETRIES + 1):
                        pkt = bytes([SOH, blocknum & 0xFF, (~blocknum) & 0xFF]) + block
                        pkt += bytes([_crc16(block) >> 8, _crc16(block) & 0xFF]) if use_crc \
                            else bytes([_checksum(block)])
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
                            return {"ok": False, "error": "receiver cancelled transfer"}
                        logger.warning("Block %d/%d (attempt %d) got response %r, retrying", idx, len(blocks), attempt, hex(resp) if resp else None)
                    else:
                        self._write_raw(bytes([CAN, CAN]))
                        logger.error("Block %d failed after %d retries", blocknum, MAX_RETRIES)
                        return {"ok": False, "error": f"block {blocknum} failed after {MAX_RETRIES} retries"}

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

                # Switch back to terminal mode so incoming serial output feeds into screen buffer
                with self._mode_lock:
                    self._mode = "terminal"

                # Send a single CR to refresh prompt
                self._write_raw(b"\r")

                # Multi-environment verification loop: verify return to pre-prompt or any valid system prompt
                deadline = time.time() + 4.0
                while time.time() < deadline:
                    time.sleep(0.3)
                    screen = self.get_screen()
                    lines = [l.strip() for l in screen.get("lines", []) if l.strip()]
                    if lines:
                        last_few = " ".join(lines[-4:])
                        if ("Receiving:" not in last_few and "File open" not in last_few and "To cancel:" not in last_few) and \
                           ((pre_prompt and pre_prompt in last_few) or generic_prompt_pattern.search(last_few)):
                            logger.info("Verified system prompt return on screen: %r", last_few)
                            return {"ok": True, "blocks": len(blocks)}

                logger.info("xmodem_send complete (%d blocks).", len(blocks))
                return {"ok": True, "blocks": len(blocks)}
            except Exception as e:  # noqa: BLE001 - never leave the receiver hanging
                logger.exception("Unexpected exception during xmodem_send: %s", e)
                self._write_raw(bytes([CAN, CAN]))
                return {"ok": False, "error": f"unexpected error: {e}"}
        finally:
            with self._mode_lock:
                self._mode = "terminal"
                self._xmodem_progress["active"] = False

    # ------------------------------------------------------------------
    # XMODEM receiver
    # ------------------------------------------------------------------
    def xmodem_receive(self, path: str, handshake_timeout: float = 30.0,
                        overall_timeout: float = 120.0) -> dict:
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
            deadline = time.time() + overall_timeout
            next_poke = 0.0
            expect_block = 1
            got_first = False

            while time.time() < deadline:
                if not got_first and time.time() >= next_poke:
                    self._write_raw(bytes([ord("C") if use_crc else NAK]))
                    next_poke = time.time() + 3.0

                b0 = self._xq_get(timeout=1.0)
                if b0 is None:
                    continue
                if b0 == EOT:
                    self._write_raw(bytes([ACK]))
                    while out and out[-1] == SUB:
                        out.pop()
                    with open(path, "wb") as f:
                        f.write(bytes(out))
                    return {"ok": True, "bytes": len(out)}
                if b0 != SOH:
                    continue  # ignore stray bytes between blocks

                got_first = True
                blk = self._xq_get(timeout=5.0)
                nblk = self._xq_get(timeout=5.0)
                payload = bytearray()
                for _ in range(BLOCK_SIZE):
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
                         and len(payload) == BLOCK_SIZE and ok_sum)
                if valid and blk == (expect_block & 0xFF):
                    out.extend(payload)
                    expect_block += 1
                    with self._mode_lock:
                        self._xmodem_progress["current_block"] = expect_block - 1
                        self._xmodem_progress["bytes"] = len(out)
                    self._write_raw(bytes([ACK]))
                elif valid and blk == ((expect_block - 1) & 0xFF):
                    # receiver already had this block (our ACK got lost) - ack again, don't re-append
                    self._write_raw(bytes([ACK]))
                else:
                    self._write_raw(bytes([NAK]))

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

