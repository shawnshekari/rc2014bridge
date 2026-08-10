"""Tests for send_keys - the control-character escape hatch.

This exists because of a concrete dead end found on hardware: XM armed itself on
the read-only ROM disk and sat poking "CKCKCK..." forever, and nothing in the
tool surface could deliver a bare Ctrl-X to stop it. send_text appends a CR, and
XM ignores 0x18 followed by 0x0D.
"""

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rc2014bridge.link import CAN, SerialLink, _parse_keys

from fakes import FakeSerial


def _flatten(keys: str) -> bytes:
    return b"".join(v for kind, v in _parse_keys(keys) if kind == "bytes")


class TestParseKeys(unittest.TestCase):
    def test_control_letters(self):
        self.assertEqual(_flatten("^A"), b"\x01")
        self.assertEqual(_flatten("^C"), b"\x03")
        self.assertEqual(_flatten("^X"), b"\x18")
        self.assertEqual(_flatten("^Z"), b"\x1a")
        self.assertEqual(_flatten("^x"), b"\x18", "lowercase means the same key")

    def test_control_symbols(self):
        self.assertEqual(_flatten("^["), b"\x1b")
        self.assertEqual(_flatten("^]"), b"\x1d")
        self.assertEqual(_flatten("^?"), b"\x7f")

    def test_literal_caret(self):
        self.assertEqual(_flatten("^^"), b"^")
        self.assertEqual(_flatten("a^^b"), b"a^b")

    def test_named_keys(self):
        self.assertEqual(_flatten("<ESC>"), b"\x1b")
        self.assertEqual(_flatten("<CR>"), b"\r")
        self.assertEqual(_flatten("<TAB>"), b"\t")
        self.assertEqual(_flatten("<DEL>"), b"\x7f")
        self.assertEqual(_flatten("<esc>"), b"\x1b", "names are case-insensitive")

    def test_backslash_escapes(self):
        self.assertEqual(_flatten("\\r"), b"\r")
        self.assertEqual(_flatten("\\n"), b"\n")
        self.assertEqual(_flatten("\\\\"), b"\\")

    def test_literal_text_passes_through(self):
        self.assertEqual(_flatten("DIR B:"), b"DIR B:")
        self.assertEqual(_flatten("Y<CR>"), b"Y\r")

    def test_nothing_is_appended(self):
        self.assertEqual(_flatten("DIR"), b"DIR", "send_keys must not add a CR")

    def test_pause_is_a_separate_step(self):
        parts = _parse_keys("^X<PAUSE>^X")
        self.assertEqual([kind for kind, _ in parts], ["bytes", "pause", "bytes"])
        self.assertEqual(parts[0][1], b"\x18")
        self.assertEqual(parts[2][1], b"\x18")
        self.assertGreater(parts[1][1], 0)

    def test_unknown_mnemonics_are_rejected(self):
        # Better a clear error than quietly sending the wrong bytes.
        with self.assertRaises(ValueError) as ctx:
            _parse_keys("<NOPE>")
        self.assertIn("NOPE", str(ctx.exception))
        with self.assertRaises(ValueError):
            _parse_keys("^!")

    def test_lone_angle_bracket_is_literal(self):
        self.assertEqual(_flatten("a < b"), b"a < b")


class TestSendKeys(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSerial()
        with patch("serial.Serial", return_value=self.fake):
            self.link = SerialLink(
                "/dev/fake",
                hw_info_file=os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hw.json"))
        self.addCleanup(self.link.close)
        time.sleep(0.3)  # let the startup CR probe land
        self.baseline = len(self.fake.written)

    def sent(self) -> bytes:
        return bytes(self.fake.written[self.baseline:])

    def test_sends_the_bytes_with_no_carriage_return(self):
        res = self.link.send_keys("^C")
        self.assertTrue(res["ok"])
        self.assertEqual(self.sent(), b"\x03")
        self.assertEqual(res["bytes_sent"], 1)
        self.assertEqual(res["hex"], "03")

    def test_xm_cancel_sequence(self):
        started = time.time()
        self.link.send_keys("^X<PAUSE>^X")
        self.assertEqual(self.sent(), bytes([CAN, CAN]))
        self.assertGreater(time.time() - started, 0.1, "the pause must actually pause")

    def test_reports_a_bad_mnemonic_without_sending_anything(self):
        with self.assertRaises(ValueError):
            self.link.send_keys("hello<BOGUS>")
        self.assertEqual(self.sent(), b"", "nothing goes out if the string won't parse")

    def test_allowed_while_another_operation_owns_the_wire(self):
        """The point of send_keys is interrupting a stuck operation, so unlike
        every other entry point it must not be refused when busy."""
        dst = os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "out.bin")
        transfer = threading.Thread(
            target=lambda: self.link.xmodem_receive(dst, handshake_timeout=2.0,
                                                    overall_timeout=2.5),
            daemon=True)
        transfer.start()
        time.sleep(0.5)
        self.assertIsNotNone(self.link.busy_reason(), "transfer should own the wire")

        res = self.link.send_keys("^X<PAUSE>^X")
        self.assertTrue(res["ok"])
        self.assertIn("XMODEM", res["during"])
        self.assertIn(bytes([CAN, CAN]), self.sent())
        transfer.join(timeout=8.0)


if __name__ == "__main__":
    unittest.main()
