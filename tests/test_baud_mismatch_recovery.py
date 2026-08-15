"""Tests for SerialLink._check_for_baud_mismatch() - falling back to the
board's default baud when it resets (reset button, REBOOT, a crashed
program) while the bridge is still following a runtime-only HBIOS "i 0
<baud>" change, and the board comes back up producing nothing but line
noise at the bridge's (now wrong) rate.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from rc2014bridge.link import SerialLink

from fakes import FakeSerial


def _hw_path() -> str:
    return os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hw.json")


def _link(**kwargs) -> tuple[SerialLink, FakeSerial]:
    fake = FakeSerial()
    with patch("serial.Serial", return_value=fake):
        link = SerialLink("/dev/fake", hw_info_file=_hw_path(), **kwargs)
    return link, fake


def _constructing_serial():
    return patch("serial.Serial", side_effect=lambda *a, **kw: FakeSerial(*a, **kw))


def _wait_for_baud(link: SerialLink, expected: int, timeout: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if link.baud == expected:
            return
        time.sleep(0.02)


# 100% outside PLAUSIBLE_TEXT_RE's [\t\n\r\x07\x08\x1b\x20-\x7e] - a clean
# stand-in for line noise from a baud-misframed byte stream.
GARBAGE_CHUNK = bytes(range(0x80, 0xB0))


class TestBaudMismatchRecovery(unittest.TestCase):
    def test_falls_back_to_default_baud_after_sustained_garbage(self):
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)
        self.assertEqual(link._default_baud, 115200)

        # Simulate having followed an HBIOS "i 0 230400" earlier: current
        # baud moves, but the resting default does not.
        with _constructing_serial():
            res = link.reconfigure(baud=230400, is_default_change=False)
        self.assertTrue(res["ok"])
        self.assertEqual(link._default_baud, 115200)

        with _constructing_serial():
            for _ in range(6):
                link._ser.feed(GARBAGE_CHUNK)
                time.sleep(0.03)
            _wait_for_baud(link, 115200)

        self.assertEqual(link.baud, 115200)
        self.assertEqual(link._default_baud, 115200)  # unchanged by the fallback itself

    def test_does_not_act_while_already_at_default_baud(self):
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)

        for _ in range(10):
            fake.feed(GARBAGE_CHUNK)
            time.sleep(0.02)

        self.assertEqual(link.baud, 115200)  # nothing to fall back to/from

    def test_normal_console_text_does_not_trigger_fallback(self):
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)

        with _constructing_serial():
            res = link.reconfigure(baud=230400, is_default_change=False)
        self.assertTrue(res["ok"])

        # Legitimate, ANSI-heavy-ish output at the (still correct) 230400
        # rate must not get mistaken for noise and bounced back down.
        with _constructing_serial():
            for _ in range(10):
                link._ser.feed(b"\x1b[2J\x1b[H  RomWBW HBIOS  Boot [H=Help]: \r\n")
                time.sleep(0.02)
            time.sleep(0.2)

        self.assertEqual(link.baud, 230400)

    def test_short_chunks_do_not_count_toward_the_streak(self):
        """A lone stray byte shouldn't be enough to act on - only a
        sustained run of garbage chunks long enough to be a real sample."""
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)

        with _constructing_serial():
            res = link.reconfigure(baud=230400, is_default_change=False)
        self.assertTrue(res["ok"])

        with _constructing_serial():
            for _ in range(10):
                link._ser.feed(b"\x80\x81")  # below GARBAGE_MIN_CHUNK_LEN
                time.sleep(0.02)
            time.sleep(0.2)

        self.assertEqual(link.baud, 230400)


if __name__ == "__main__":
    unittest.main()
