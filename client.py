#!/usr/bin/env python3
"""
Tiny CLI for talking to a running rc2014bridge over its Unix socket.

  client.py send_text '2\r'
  client.py get_screen
  client.py wait_for 'Boot \\[H=Help\\]:' --timeout 5
  client.py xmodem_send /path/to/file.rom
  client.py xmodem_receive /path/to/save.bin
"""
import argparse
import json
import socket
import sys

DEFAULT_SOCK = "/tmp/rc2014bridge.sock"


def call(sock_path: str, req: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sock", default=DEFAULT_SOCK)
    p.add_argument("cmd", choices=["send_text", "get_screen", "wait_for",
                                    "xmodem_send", "xmodem_receive"])
    p.add_argument("arg", nargs="?", help="text / pattern / path, depending on cmd")
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args()

    req = {"cmd": args.cmd}
    if args.cmd == "send_text":
        req["text"] = args.arg.encode().decode("unicode_escape")
    elif args.cmd == "wait_for":
        req["pattern"] = args.arg
        req["timeout"] = args.timeout
    elif args.cmd in ("xmodem_send", "xmodem_receive"):
        req["path"] = args.arg

    resp = call(args.sock, req)
    print(json.dumps(resp, indent=2))
    sys.exit(0 if resp.get("ok") else 1)


if __name__ == "__main__":
    main()
