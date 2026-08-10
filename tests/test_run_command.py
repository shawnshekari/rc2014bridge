"""Tests for the receive log, wait_for's delta semantics, run_command, and the
operation lock.

wait_for used to drain a buffer nothing else consumed, so its first call saw the
entire session's output and a pattern like "A>" matched instantly against
scrollback from minutes earlier. These tests pin down the replacement: a caller
only ever sees output that arrives after it starts waiting.
"""

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rc2014bridge.link import RX_LOG_MAX, SerialLink, _strip_echo_and_prompt

from fakes import FakeSerial


def _scratch_hw_info() -> str:
    return os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hardware_info.json")


class LinkTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSerial()
        with patch("serial.Serial", return_value=self.fake):
            self.link = SerialLink("/dev/fake", hw_info_file=_scratch_hw_info())
        self.addCleanup(self.link.close)

    def feed_later(self, data: bytes, after: float = 0.1):
        """Deliver console output shortly after the current call starts waiting."""
        def _worker():
            time.sleep(after)
            self.fake.feed(data)
        threading.Thread(target=_worker, daemon=True).start()

    def wait_for_rx(self, text: str, timeout: float = 2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if text in self.link.read_since(0)[0]:
                return
            time.sleep(0.01)
        self.fail(f"{text!r} never arrived")


class TestWaitFor(LinkTestCase):
    def test_ignores_output_from_before_the_call(self):
        self.fake.feed(b"A>DIR\r\nSOMEFILE COM\r\nA>")
        self.wait_for_rx("SOMEFILE")

        # The prompt is already on screen, but it is not news - a caller waiting
        # now is asking about what happens next.
        res = self.link.wait_for(r"A>", timeout=0.3)
        self.assertFalse(res["matched"], res)

    def test_matches_output_arriving_after_the_call(self):
        self.fake.feed(b"stale output\r\n")
        self.wait_for_rx("stale")

        self.feed_later(b"\r\nB>", after=0.1)
        res = self.link.wait_for(r"B>", timeout=2.0)
        self.assertTrue(res["matched"], res)
        self.assertEqual(res["match"], "B>")
        self.assertNotIn("stale", res["text"])

    def test_since_position_replays_from_a_marker(self):
        marker = self.link.rx_position()
        self.fake.feed(b"hello world")
        self.wait_for_rx("hello")

        # A caller that took a position first can still match output that
        # arrived before it got around to waiting.
        res = self.link.wait_for(r"hello", timeout=0.5, since=marker)
        self.assertTrue(res["matched"], res)

    def test_concurrent_waiters_both_see_the_output(self):
        results = {}

        def _wait(key, pattern):
            results[key] = self.link.wait_for(pattern, timeout=2.0)

        threads = [threading.Thread(target=_wait, args=("a", r"READY")),
                   threading.Thread(target=_wait, args=("b", r"READY"))]
        for t in threads:
            t.start()
        time.sleep(0.15)
        self.fake.feed(b"\r\nREADY\r\n")
        for t in threads:
            t.join(timeout=3.0)

        self.assertTrue(results["a"]["matched"], results)
        self.assertTrue(results["b"]["matched"], results)


class TestReceiveLog(LinkTestCase):
    def test_log_is_bounded_and_reports_truncation(self):
        chunk = "x" * 50_000
        for _ in range(8):  # 400 KB through a 256 KB log
            self.link._rx_append(chunk)

        text, pos, truncated = self.link.read_since(0)
        self.assertLessEqual(len(text), RX_LOG_MAX)
        self.assertEqual(pos, 400_000)
        self.assertTrue(truncated, "asking for output older than the log must say so")

        text, _pos, truncated = self.link.read_since(pos - 100)
        self.assertEqual(len(text), 100)
        self.assertFalse(truncated)


class TestRunCommand(LinkTestCase):
    def test_returns_only_the_command_output(self):
        # What the board actually sends back: our echo, the output, a new prompt.
        self.feed_later(b"DIR A:\r\nA: LEDSHOW  COM : TEST     TXT\r\nA>", after=0.1)
        res = self.link.run_command("DIR A:", timeout=3.0)

        self.assertTrue(res["ok"], res)
        self.assertFalse(res["timed_out"])
        self.assertEqual(res["prompt"], "A>")
        self.assertEqual(res["output"], "A: LEDSHOW  COM : TEST     TXT")
        self.assertEqual(res["command"], "DIR A:")

    def test_sends_the_command_with_a_carriage_return(self):
        self.feed_later(b"STAT\r\nA: R/W, Space: 4412k\r\nA>", after=0.1)
        self.link.run_command("STAT", timeout=3.0)
        self.assertIn(b"STAT\r", bytes(self.fake.written))

    def test_reports_timeout_with_partial_output(self):
        # An interactive program never returns to a shell prompt.
        self.feed_later(b"MBASIC\r\nBASIC-80 Rev 5.21\r\nOk\r\n", after=0.1)
        res = self.link.run_command("MBASIC", timeout=1.0)

        self.assertFalse(res["ok"])
        self.assertTrue(res["timed_out"])
        self.assertIn("BASIC-80", res["output"])

    def test_waits_out_a_prompt_shaped_string_mid_output(self):
        # "B>" appears inside the listing before the real prompt arrives; the
        # settle period is what stops run_command returning early.
        self.feed_later(b"TYPE X\r\nprompt is B> in this text\r\n", after=0.1)
        self.feed_later(b"more output after that\r\nA>", after=0.5)
        res = self.link.run_command("TYPE X", timeout=4.0)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["prompt"], "A>")
        self.assertIn("more output after that", res["output"])


class TestWaitUntilReady(LinkTestCase):
    """A boot profile keeps running programs after the OS banner. A command sent
    during that window is lost - observed for real as 'DIR B:' arriving as
    'IR B:', its first character swallowed by the profile's own output."""

    def test_waits_for_the_profile_to_finish(self):
        # Output still arriving, prompt only afterwards.
        self.feed_later(b"\r\nA$LDTIM\r\n", after=0.1)
        self.feed_later(b"- ZSDOS Path...\r\n", after=0.5)
        self.feed_later(b"A>", after=1.0)

        res = self.link.wait_until_ready(settle=0.4)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["state"], "cpm")

    def test_reports_not_ready_when_no_prompt_appears(self):
        self.fake.responder = lambda line: None      # nothing ever answers
        res = self.link.wait_until_ready(settle=0.2)
        self.assertFalse(res["ok"])
        self.assertIn("no prompt", res["error"])


class TestBootLoaderWarning(LinkTestCase):
    """The HBIOS loader acts on single keys, so a CP/M command sent there does
    something else and still returns a prompt - "DIR B:" ran D (device
    inventory), and "REN A=B" would run R (reboot). Observed for real."""

    def test_warns_about_a_multi_character_command_at_the_loader(self):
        self.link._system_state = "hbios"
        self.feed_later(b"DIR B:\r\nUnit        Device\r\nBoot [H=Help]:", after=0.1)
        res = self.link.run_command("DIR B:", timeout=3.0)

        self.assertIn("warning", res)
        self.assertIn("single keys", res["warning"])
        self.assertIn("'D'", res["warning"])
        self.assertEqual(res["state"], "hbios")

    def test_no_warning_for_a_single_key(self):
        self.link._system_state = "hbios"
        self.feed_later(b"4\r\nBooting Disk Unit 4\r\nBoot [H=Help]:", after=0.1)
        res = self.link.run_command("4", timeout=3.0)
        self.assertNotIn("warning", res)

    def test_no_warning_at_a_cpm_prompt(self):
        self.link._system_state = "cpm"
        self.feed_later(b"DIR B:\r\nNO FILE\r\nA>", after=0.1)
        res = self.link.run_command("DIR B:", timeout=3.0)
        self.assertNotIn("warning", res)


class TestStalePromptRace(LinkTestCase):
    def test_a_prompt_still_in_flight_does_not_satisfy_the_next_command(self):
        """Observed as a partial STAT capture during a drive scan.

        If the previous command's trailing prompt is still arriving when
        run_command starts, that prompt lands inside the new call's window and
        looks like "this command finished" - so it returns with only the echo,
        and the real output shows up as nobody's.
        """
        # STAT probes every drive before printing, so real output can be a
        # second or more away - long enough for the stale prompt to get a full
        # quiet window to itself.
        def _responder(line: bytes):
            if line.strip().upper() == b"STAT":
                self.feed_later(
                    b"A: R/W, Space: 4412k\r\nB: R/W, Space: 244k\r\n"
                    b"C: R/W, Space: 22k\r\nA>", after=0.8)
            return None

        self.fake.responder = _responder
        # The previous command's prompt is in flight right as we call.
        self.feed_later(b"\r\nA>", after=0.05)

        res = self.link.run_command("STAT", timeout=5.0)

        self.assertTrue(res["ok"], res)
        self.assertIn("A: R/W, Space: 4412k", res["output"])
        self.assertIn("C: R/W, Space: 22k", res["output"],
                      "the whole listing must be captured, not just the first lines")


class TestStripEchoAndPrompt(unittest.TestCase):
    def test_strips_echo_prompt_and_blank_lines(self):
        raw = "DIR B:\r\n\r\nB: HELLO    TXT\r\n\r\nB>"
        self.assertEqual(_strip_echo_and_prompt(raw, "DIR B:"), "B: HELLO    TXT")

    def test_strips_echo_when_prompt_precedes_it(self):
        raw = "A>STAT\r\nA: R/W, Space: 10k\r\nA>"
        self.assertEqual(_strip_echo_and_prompt(raw, "STAT"), "A: R/W, Space: 10k")

    def test_keeps_output_that_merely_resembles_the_command(self):
        raw = "TYPE README\r\nTYPE README to read this\r\nA>"
        self.assertEqual(_strip_echo_and_prompt(raw, "TYPE README"),
                         "TYPE README to read this")


class TestOperationLock(LinkTestCase):
    def test_second_operation_is_told_to_wait(self):
        dst = os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "out.bin")
        thread = threading.Thread(
            target=lambda: self.link.xmodem_receive(dst, handshake_timeout=1.5,
                                                    overall_timeout=2.0),
            daemon=True)
        thread.start()
        time.sleep(0.5)  # transfer now owns the wire

        self.assertIn("XMODEM", self.link.busy_reason() or "")
        res = self.link.run_command("DIR", timeout=1.0)
        self.assertTrue(res.get("busy"), res)
        self.assertIn("XMODEM", res["error"])

        thread.join(timeout=6.0)
        # ...and the wire is free again afterwards
        self.assertIsNone(self.link.busy_reason())

    def test_composites_may_nest_within_one_thread(self):
        # upload() calls run_command() internally; the RLock must not deadlock.
        with self.link._operation("upload"):
            self.feed_later(b"DIR\r\nNO FILE\r\nA>", after=0.1)
            res = self.link.run_command("DIR", timeout=3.0)
        self.assertFalse(res.get("busy"), res)


if __name__ == "__main__":
    unittest.main()
