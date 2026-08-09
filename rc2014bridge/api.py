"""
Local control surface for the SerialLink: a Unix domain socket speaking
newline-delimited JSON. One request per line in, one JSON response per
line out. Deliberately no framework - this is a small, fixed set of
commands and doesn't need one.

Commands:
  {"cmd": "send_text", "text": "..."}
  {"cmd": "get_screen"}
  {"cmd": "wait_for", "pattern": "...", "timeout": 10.0}
  {"cmd": "xmodem_send", "path": "..."}
  {"cmd": "xmodem_receive", "path": "..."}
"""

import json
import os
import socket
import threading


class ApiServer:
    def __init__(self, link, sock_path: str):
        self.link = link
        self.sock_path = sock_path
        if os.path.exists(sock_path):
            os.remove(sock_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(sock_path)
        self._sock.listen(4)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        with conn, conn.makefile("rwb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                    resp = self._dispatch(req)
                except Exception as e:  # noqa: BLE001 - report back to caller, don't crash the server
                    resp = {"ok": False, "error": str(e)}
                f.write((json.dumps(resp) + "\n").encode())
                f.flush()

    def _dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "send_text":
            self.link.send_text(req["text"])
            return {"ok": True}
        if cmd == "get_screen":
            return {"ok": True, **self.link.get_screen(req.get("scroll_offset", 0))}
        if cmd == "wait_for":
            return {"ok": True, **self.link.wait_for(req["pattern"], req.get("timeout", 10.0))}
        if cmd == "xmodem_send":
            return self.link.xmodem_send(req["path"])
        if cmd == "xmodem_receive":
            return self.link.xmodem_receive(req["path"])
        if cmd == "reboot":
            self.link.reboot()
            return {"ok": True}
        if cmd == "get_hardware_info":
            return {"ok": True, "info": getattr(self.link, "hardware_info", {})}
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}
