"""Tests for auto-following the HBIOS boot loader's own "i <unit> <baud>"
console speed change (SerialLink._follow_hbios_baud_change), so the bridge
doesn't need a manual trip to the Settings screen every time someone runs
that command at the "Boot [H=Help]:" prompt.
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


def _feed_byte_by_byte(fake: FakeSerial, data: bytes, delay: float = 0.02):
    """Feed one byte at a time with a gap long enough that the reader
    thread's read() call drains each one separately, the way a real
    console's character-at-a-time echo actually arrives on the wire -
    unlike a single feed() of the whole string, which a fast/local
    connection could plausibly deliver in one read()."""
    for b in data:
        fake.feed(bytes([b]))
        time.sleep(delay)


class TestHbiosBaudFollow(unittest.TestCase):
    def test_follows_console_sio_baud_change(self):
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)

        with _constructing_serial():
            fake.feed(
                b"Boot [H=Help]: i 0 230400\r\n"
                b"  Change speed now.  Press a key to resume.\r\n"
            )
            _wait_for_baud(link, 230400)

        self.assertEqual(link.baud, 230400)
        self.assertIsNone(link._pending_hbios_baud)

    def test_follows_when_command_is_echoed_one_character_at_a_time(self):
        """Regression test for the real failure: the RC2014 echoes typed
        input character by character, so "Boot [H=Help]: i 0 230400" is
        (almost) never present in a single read()'s worth of text. Matching
        only had to scan whichever chunk just arrived, it would see isolated
        single characters and never the full command - this must instead
        catch it once the accumulated boot buffer's tail spells it out."""
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)

        with _constructing_serial():
            _feed_byte_by_byte(fake, b"Boot [H=Help]: i 0 230400\r\n")
            _feed_byte_by_byte(fake, b"  Change speed now.  Press a key to resume.\r\n")
            _wait_for_baud(link, 230400)

        self.assertEqual(link.baud, 230400)
        self.assertIsNone(link._pending_hbios_baud)

    def test_confirmation_split_across_two_reads_still_matches(self):
        """The confirmation text is checked against the accumulated boot
        buffer rather than a single chunk, so it survives landing in two
        separate reads - a real possibility since it isn't fed atomically."""
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)

        with _constructing_serial():
            fake.feed(b"Boot [H=Help]: i 0 230400\r\n  Change speed now.  Press")
            time.sleep(0.15)
            self.assertEqual(link.baud, 115200)  # not yet - confirmation incomplete
            fake.feed(b" a key to resume.\r\n")
            _wait_for_baud(link, 230400)

        self.assertEqual(link.baud, 230400)

    def test_does_not_refire_on_unrelated_later_text(self):
        """Once matched, the command/confirmation pair is sliced out of the
        boot buffer - unrelated later text must not resurrect it and bounce
        the baud back on its own."""
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)

        with _constructing_serial():
            fake.feed(b"Boot [H=Help]: i 0 230400\r\n  Change speed now.  Press a key to resume.\r\n")
            _wait_for_baud(link, 230400)
            self.assertEqual(link.baud, 230400)

            link.reconfigure(baud=115200)  # e.g. a manual Settings switch back
            self.assertEqual(link.baud, 115200)

            link._ser.feed(b"Boot [H=Help]: \r\n")
            time.sleep(0.3)

        self.assertEqual(link.baud, 115200)

    def test_ignores_non_console_unit(self):
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)

        with _constructing_serial():
            fake.feed(
                b"Boot [H=Help]: i 1 230400\r\n"
                b"  Change speed now.  Press a key to resume.\r\n"
            )
            time.sleep(0.3)

        self.assertEqual(link.baud, 115200)
        self.assertIsNone(link._pending_hbios_baud)

    def test_confirmation_without_prior_command_does_nothing(self):
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)
        fake.feed(b"  Change speed now.  Press a key to resume.\r\n")
        time.sleep(0.3)
        self.assertEqual(link.baud, 115200)

    def test_command_without_confirmation_does_not_change_baud_yet(self):
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)
        fake.feed(b"Boot [H=Help]: i 0 230400\r\n")
        time.sleep(0.3)
        self.assertEqual(link.baud, 115200)
        self.assertEqual(link._pending_hbios_baud, 230400)


if __name__ == "__main__":
    unittest.main()
