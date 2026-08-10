"""Tests for the GUI keystroke guard during XMODEM transfers.

Deliberately a guard, not a block: ordinary typing is held back while a transfer
owns the wire, but Ctrl-C and Ctrl-X always go through. A human who cannot
interrupt a stuck transfer has no way to rescue their own machine - and we have
watched XM arm itself and poke forever.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from rc2014bridge.display import (INTERRUPT_BYTES, _allowed_during_transfer,
                                  _key_to_bytes)
from rc2014bridge.link import SerialLink

from fakes import FakeSerial


class FakeKeyEvent:
    def __init__(self, key, unicode_="", mod=0):
        self.key = key
        self.unicode = unicode_
        self.mod = mod


class TestAllowedDuringTransfer(unittest.TestCase):
    def test_interrupts_always_pass(self):
        self.assertTrue(_allowed_during_transfer(b"\x03"), "Ctrl-C must pass")
        self.assertTrue(_allowed_during_transfer(b"\x18"), "Ctrl-X must pass")
        self.assertEqual(INTERRUPT_BYTES, {0x03, 0x18})

    def test_ordinary_typing_is_held_back(self):
        for data in (b"A", b"\r", b"\x08", b"\x1b", b"\x1b[A", b" "):
            self.assertFalse(_allowed_during_transfer(data), f"{data!r} should be held")

    def test_other_control_keys_are_held_back(self):
        # Only the two documented interrupts get through, not every control char.
        self.assertFalse(_allowed_during_transfer(b"\x1a"))  # Ctrl-Z
        self.assertFalse(_allowed_during_transfer(b"\x04"))  # Ctrl-D


class TestKeyToBytes(unittest.TestCase):
    def test_ctrl_x_maps_to_can(self):
        import pygame
        event = FakeKeyEvent(pygame.K_x, "\x18", pygame.KMOD_CTRL)
        self.assertEqual(_key_to_bytes(event), b"\x18")

    def test_ctrl_c_maps_to_etx(self):
        import pygame
        event = FakeKeyEvent(pygame.K_c, "\x03", pygame.KMOD_CTRL)
        self.assertEqual(_key_to_bytes(event), b"\x03")


class TestIsTransferring(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSerial()
        with patch("serial.Serial", return_value=self.fake):
            self.link = SerialLink(
                "/dev/fake",
                hw_info_file=os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hw.json"))
        self.addCleanup(self.link.close)

    def test_false_when_idle_true_during_a_transfer(self):
        self.assertFalse(self.link.is_transferring())
        with self.link._mode_lock:
            self.link._mode = "xmodem"
        self.assertTrue(self.link.is_transferring())
        with self.link._mode_lock:
            self.link._mode = "terminal"
        self.assertFalse(self.link.is_transferring())

    def test_send_text_itself_is_never_blocked(self):
        """The guard lives in the GUI, not the link: send_keys and the keystroke
        path must still be able to write mid-transfer."""
        with self.link._mode_lock:
            self.link._mode = "xmodem"
        before = len(self.fake.written)
        self.link.send_keys("^X")
        self.assertEqual(bytes(self.fake.written[before:]), b"\x18")


if __name__ == "__main__":
    unittest.main()
