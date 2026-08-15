"""
Mandel Pixel-Stream Protocol (v1) Decoder.

Parses binary RLE pixel streams according to mandel_pixel_stream.yaml.
Maps 5-bit pixel indices (0-30) to RGB colors and handles control tokens:
- END_OF_ROW (0xF8)
- END_OF_FRAME (0xF9)
- VERSION_BANNER (0xFA)
"""

import threading
import time

# 31 RGB color entries matching mandel_pixel_stream.yaml
PALETTE: list[tuple[int, int, int]] = [
    (0, 0, 0),        # index 0
    (255, 255, 255),  # index 1
    (255, 255, 255),  # index 2
    (215, 255, 255),  # index 3
    (215, 255, 255),  # index 4
    (175, 255, 255),  # index 5
    (175, 255, 255),  # index 6
    (175, 255, 255),  # index 7
    (135, 255, 255),  # index 8
    (135, 255, 255),  # index 9
    (95, 255, 255),   # index 10
    (95, 255, 255),   # index 11
    (0, 255, 255),    # index 12
    (0, 255, 255),    # index 13
    (0, 215, 255),    # index 14
    (0, 215, 255),    # index 15
    (0, 215, 255),    # index 16
    (0, 175, 255),    # index 17
    (0, 175, 255),    # index 18
    (0, 135, 255),    # index 19
    (0, 135, 255),    # index 20
    (0, 135, 255),    # index 21
    (0, 95, 255),     # index 22
    (0, 95, 255),     # index 23
    (0, 0, 255),      # index 24
    (0, 0, 215),      # index 25
    (0, 0, 175),      # index 26
    (0, 0, 135),      # index 27
    (0, 0, 135),      # index 28
    (0, 0, 95),       # index 29
    (0, 0, 0),        # index 30
]

END_OF_ROW = 0xF8
END_OF_FRAME = 0xF9
VERSION_BANNER = 0xFA


class PixelStreamDecoder:
    """Decodes stream of bytes into pixel frames and RGB surface buffers."""

    def __init__(self):
        self._lock = threading.Lock()
        # Monotonically increasing "which render is this" counter - unlike
        # everything else reset() clears, this must survive across resets:
        # display.py's multi-window support (one popup per render) uses a
        # change in this value to know a *new* render just started, distinct
        # from "this render is still in progress" or "still showing the
        # last completed one".
        self.render_id = 0
        self.reset()

    def reset(self):
        """Reset all decoder state (except render_id, which this bumps)."""
        with self._lock:
            self.render_id += 1
            self._state = "NORMAL"  # "NORMAL", "EXTENDED_RUN", "VERSION_BYTE"
            self._pending_index = None
            self.version = None
            self._current_raw_row: list[int] = []
            self._current_rgb_row: list[tuple[int, int, int]] = []
            self._current_raw_frame: list[list[int]] = []
            self._current_rgb_frame: list[list[tuple[int, int, int]]] = []
            self.last_complete_raw_frame: list[list[int]] = []
            self.last_complete_rgb_frame: list[list[tuple[int, int, int]]] = []
            self.frame_count = 0
            self.total_bytes_processed = 0
            # Render timing: started_at is set here (reset() runs right as
            # the first byte, VERSION_BANNER, is detected - see link.py's
            # auto-detect); completed_at is set in feed() the instant
            # END_OF_FRAME is consumed. Neither claims to be a true "time
            # on the wire" measurement (this is host-side wall clock, not
            # a device-side timestamp), but it brackets the render as seen
            # by the bridge, which is what a popup window's title wants.
            self.started_at = time.time()
            self.completed_at = None

    def feed(self, data: bytes | list[int] | bytearray) -> int | None:
        """Feed a chunk of bytes to the decoder.

        Returns the index into `data` just past an END_OF_FRAME token if
        one was completed during this call, and stops consuming right
        there - the protocol has nothing to say about bytes after a
        frame ends, so the caller (link.py's auto mode-switching) is
        expected to route the remainder elsewhere, e.g. back to the
        terminal pipeline. Returns None if the whole chunk was consumed
        without completing a frame."""
        if not data:
            return None

        with self._lock:
            i = 0
            n = len(data)
            while i < n:
                b = data[i]
                i += 1
                self.total_bytes_processed += 1

                if self._state == "EXTENDED_RUN":
                    run_length = b
                    idx = self._pending_index
                    if idx is not None and 0 <= idx <= 30:
                        rgb = PALETTE[idx]
                        self._current_raw_row.extend([idx] * run_length)
                        self._current_rgb_row.extend([rgb] * run_length)
                    self._state = "NORMAL"
                    self._pending_index = None

                elif self._state == "VERSION_BYTE":
                    self.version = b
                    self._state = "NORMAL"

                elif self._state == "NORMAL":
                    idx = (b >> 3) & 0x1F
                    code = b & 0x07

                    if idx == 31:  # Control code
                        if code == 0:  # END_OF_ROW (0xF8)
                            self._current_raw_frame.append(self._current_raw_row)
                            self._current_rgb_frame.append(self._current_rgb_row)
                            self._current_raw_row = []
                            self._current_rgb_row = []
                        elif code == 1:  # END_OF_FRAME (0xF9)
                            if self._current_raw_row:
                                self._current_raw_frame.append(self._current_raw_row)
                                self._current_rgb_frame.append(self._current_rgb_row)
                                self._current_raw_row = []
                                self._current_rgb_row = []
                            self.last_complete_raw_frame = list(self._current_raw_frame)
                            self.last_complete_rgb_frame = list(self._current_rgb_frame)
                            self.frame_count += 1
                            self._current_raw_frame = []
                            self._current_rgb_frame = []
                            self.completed_at = time.time()
                            return i
                        elif code == 2:  # VERSION_BANNER (0xFA)
                            self._state = "VERSION_BYTE"
                    else:  # Pixel token
                        if code <= 6:
                            run_length = code + 1
                            rgb = PALETTE[idx]
                            self._current_raw_row.extend([idx] * run_length)
                            self._current_rgb_row.extend([rgb] * run_length)
                        elif code == 7:
                            self._pending_index = idx
                            self._state = "EXTENDED_RUN"
            return None

    def get_rgb_frame(self) -> list[list[tuple[int, int, int]]]:
        """Return the latest complete RGB frame (or active frame if incomplete)."""
        with self._lock:
            if self.last_complete_rgb_frame:
                return list(self.last_complete_rgb_frame)
            active_rgb = list(self._current_rgb_frame)
            if self._current_rgb_row:
                active_rgb.append(list(self._current_rgb_row))
            return active_rgb

    def get_current_frame_snapshot(self) -> dict:
        """Snapshot of current frame decoding state."""
        with self._lock:
            active_rgb = list(self._current_rgb_frame)
            if self._current_rgb_row:
                active_rgb.append(list(self._current_rgb_row))

            target_frame = self.last_complete_rgb_frame if self.last_complete_rgb_frame else active_rgb

            rows = len(target_frame)
            cols = max((len(r) for r in target_frame), default=0)
            elapsed_s = (self.completed_at - self.started_at) if self.completed_at else None
            return {
                "render_id": self.render_id,
                "version": self.version,
                "frame_count": self.frame_count,
                "total_bytes": self.total_bytes_processed,
                "rows": rows,
                "cols": cols,
                "rgb_data": target_frame,
                "is_complete": bool(self.last_complete_rgb_frame),
                "elapsed_s": elapsed_s,
            }
