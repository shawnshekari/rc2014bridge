"""Test doubles for the serial port, so link.py can be exercised without hardware."""

import threading
import time

import serial


class FakeSerial:
    """Minimal pyserial stand-in.

    Writes accumulate in `written`. Reads drain `_rx`, which tests fill via
    feed() - or which a `peer` fills automatically when two FakeSerials are
    wired together with pair(), giving an in-process loopback.
    """

    def __init__(self, *args, **kwargs):
        self.written = bytearray()
        self._rx = bytearray()
        self._lock = threading.Lock()
        self.peer: "FakeSerial | None" = None
        self.is_open = True
        self.port = args[0] if args else kwargs.get("port")
        self.baudrate = kwargs.get("baudrate")
        self.rtscts = kwargs.get("rtscts", False)
        # Optional line-oriented device behaviour: called with each complete
        # CR-terminated line written to the port; whatever bytes it returns are
        # fed back as if the board had printed them. The line itself is echoed
        # first, because a real CP/M console echoes what you type - and
        # run_command relies on that echo to tell its own prompt from the
        # previous command's.
        self.responder = None
        self.echo = True
        self._line = bytearray()

    @classmethod
    def pair(cls) -> tuple["FakeSerial", "FakeSerial"]:
        a, b = cls(), cls()
        a.peer, b.peer = b, a
        return a, b

    def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            # Mirrors real pyserial: reading a closed port raises rather than
            # returning empty - lets tests exercise link.py's handling of a
            # port closed out from under an in-flight read (see reconfigure()).
            raise serial.SerialException("port closed")
        with self._lock:
            if self._rx:
                data = bytes(self._rx[:size])
                del self._rx[:size]
                return data
        time.sleep(0.005)  # stand in for the port's read timeout
        return b""

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.extend(data)
        if self.peer is not None:
            self.peer.feed(data)
        if self.responder is not None:
            self._dispatch_lines(data)
        return len(data)

    def _dispatch_lines(self, data: bytes):
        self._line.extend(data)
        while b"\r" in self._line:
            line, _, rest = bytes(self._line).partition(b"\r")
            self._line = bytearray(rest)
            if self.echo and line:
                self.feed(line + b"\r\n")
            reply = self.responder(line)
            if reply:
                self.feed(reply)

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    # -- test-side helpers ------------------------------------------------
    def feed(self, data: bytes):
        """Make bytes available to whoever is reading this port."""
        with self._lock:
            self._rx.extend(data)

    def written_since(self, offset: int) -> bytes:
        with self._lock:
            return bytes(self.written[offset:])
