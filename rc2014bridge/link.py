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

import os
import queue
import re
import threading
import time

import pyte
import serial

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


class SerialLink:
    def __init__(self, port: str, baud: int = 115200, cols: int = 80, rows: int = 24):
        self.port = port
        self.baud = baud
        self.cols, self.rows = cols, rows
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

        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def close(self):
        self._stop.set()
        self._reader.join(timeout=2)
        self._ser.close()

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
        self._write_raw(text.encode("latin-1"))

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
                return {"ok": False, "error": "handshake timeout waiting for receiver"}

            while self._xq_get(timeout=0.4) is not None:
                pass

            try:
                blocknum = 1
                for idx, block in enumerate(blocks, start=1):
                    for _attempt in range(MAX_RETRIES):
                        pkt = bytes([SOH, blocknum & 0xFF, (~blocknum) & 0xFF]) + block
                        pkt += bytes([_crc16(block) >> 8, _crc16(block) & 0xFF]) if use_crc \
                            else bytes([_checksum(block)])
                        self._write_paced(pkt)
                        resp = self._xq_get(timeout=10.0)
                        if resp == ACK:
                            with self._mode_lock:
                                self._xmodem_progress["current_block"] = idx
                                self._xmodem_progress["bytes"] = idx * BLOCK_SIZE
                            break
                        if resp == CAN:
                            return {"ok": False, "error": "receiver cancelled transfer"}
                    else:
                        self._write_raw(bytes([CAN, CAN]))
                        return {"ok": False, "error": f"block {blocknum} failed after {MAX_RETRIES} retries"}
                time.sleep(0.15)  # Allow Z80 receiver time to flush last block to disk/flash
                for _attempt in range(10):
                    self._write_raw(bytes([EOT]))
                    resp = self._xq_get(timeout=2.0)
                    if resp == ACK:
                        return {"ok": True, "blocks": len(blocks)}
                    if resp == NAK:
                        # Some CP/M receivers NAK the 1st EOT; send 2nd EOT immediately
                        continue
                self._write_raw(bytes([CAN, CAN]))
                return {"ok": False, "error": "EOT not acknowledged"}
            except Exception as e:  # noqa: BLE001 - never leave the receiver hanging
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

