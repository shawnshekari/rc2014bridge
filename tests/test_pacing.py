"""Tests for serial write pacing and its calibration.

The shipped defaults were derived on a 7.4MHz Z80 behind an external SIO and run
a 115200 line at roughly 5% of its rate - fine for a DIR, painful for a 512KB ROM
image. calibrate_pacing() finds what a given board actually accepts, and must
never leave a machine configured with pacing that failed.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from rc2014bridge.link import (DEFAULT_TEXT_CHUNK, DEFAULT_TEXT_DELAY,
                               DEFAULT_XMODEM_CHUNK, DEFAULT_XMODEM_DELAY,
                               PACING_CANDIDATES, SerialLink)

from fakes import FakeSerial


def _hw_path() -> str:
    return os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hw.json")


def _link(**kwargs) -> tuple[SerialLink, FakeSerial]:
    fake = FakeSerial()
    with patch("serial.Serial", return_value=fake):
        link = SerialLink("/dev/fake", hw_info_file=kwargs.pop("hw_info_file", _hw_path()), **kwargs)
    return link, fake


class TestPacingConfiguration(unittest.TestCase):
    def test_defaults_are_the_conservative_values(self):
        link, _ = _link()
        self.addCleanup(link.close)
        self.assertEqual(link.text_pacing, (DEFAULT_TEXT_CHUNK, DEFAULT_TEXT_DELAY))
        self.assertEqual(link.xmodem_pacing, (DEFAULT_XMODEM_CHUNK, DEFAULT_XMODEM_DELAY))

    def test_explicit_pacing_wins(self):
        link, _ = _link(text_pacing=(4, 0.001), xmodem_pacing=(128, 0.002))
        self.addCleanup(link.close)
        self.assertEqual(link.text_pacing, (4, 0.001))
        self.assertEqual(link.xmodem_pacing, (128, 0.002))

    def test_calibrated_pacing_is_reused_on_the_next_run(self):
        import json
        path = _hw_path()
        with open(path, "w") as f:
            json.dump({"pacing": {"xmodem": [128, 0.002], "text": [8, 0.005]}}, f)

        link, _ = _link(hw_info_file=path)
        self.addCleanup(link.close)
        self.assertEqual(link.xmodem_pacing, (128, 0.002))
        self.assertEqual(link.text_pacing, (8, 0.005))

    def test_explicit_pacing_overrides_the_stored_value(self):
        import json
        path = _hw_path()
        with open(path, "w") as f:
            json.dump({"pacing": {"xmodem": [128, 0.002]}}, f)

        link, _ = _link(hw_info_file=path, xmodem_pacing=(8, 0.010))
        self.addCleanup(link.close)
        self.assertEqual(link.xmodem_pacing, (8, 0.010))

    def test_write_paced_uses_xmodem_pacing_by_default(self):
        link, fake = _link(xmodem_pacing=(64, 0.0))
        self.addCleanup(link.close)
        before = len(fake.written)
        link._write_paced(b"x" * 200)
        self.assertEqual(len(fake.written) - before, 200)

    def test_zero_delay_does_not_sleep(self):
        import time
        link, _ = _link(xmodem_pacing=(16, 0.0))
        self.addCleanup(link.close)
        started = time.time()
        link._write_paced(b"y" * 4096)   # 256 chunks; would be slow if it slept
        self.assertLess(time.time() - started, 0.5)


class TestCalibration(unittest.TestCase):
    """The calibration loop, with the transfer layer stubbed - what matters here
    is which setting it settles on and what it leaves behind."""

    def setUp(self):
        self.link, self.fake = _link()
        self.addCleanup(self.link.close)
        self.link.hardware_info["drive_mappings"] = {"A": "IDE0:0", "B": "MD0:0", "C": "MD1:0"}
        self.link._ensure_cpm_prompt = lambda: True
        self.link.run_command = lambda *a, **kw: {"ok": True, "output": "", "timed_out": False}
        self.tried = []

    def _stub_transfers(self, fails_at=None):
        """Accept every pacing up to `fails_at` (a (chunk, delay) tuple)."""
        def _upload(local_path, **kwargs):
            self.tried.append(self.link.xmodem_pacing)
            if self.link.xmodem_pacing == fails_at:
                return {"ok": False, "error": "block 3 failed after 10 retries"}
            with open(local_path, "rb") as f:
                self._payload = f.read()
            return {"ok": True, "blocks": 32}

        def _download(cpm_path, local_path=None, **kwargs):
            import hashlib
            with open(local_path, "wb") as f:
                f.write(self._payload)
            return {"ok": True, "bytes": len(self._payload),
                    "sha256": hashlib.sha256(self._payload).hexdigest()}

        self.link.upload = _upload
        self.link.download = _download

    def test_settles_on_the_fastest_setting_that_verifies(self):
        self._stub_transfers()
        res = self.link.calibrate_pacing(test_bytes=1024)

        self.assertTrue(res["ok"], res)
        self.assertEqual(tuple(res["xmodem_pacing"]), PACING_CANDIDATES[-1])
        self.assertEqual(self.link.xmodem_pacing, PACING_CANDIDATES[-1])
        self.assertEqual(self.tried, PACING_CANDIDATES)

    def test_stops_at_the_first_failure_and_keeps_the_last_good_setting(self):
        self._stub_transfers(fails_at=PACING_CANDIDATES[2])
        res = self.link.calibrate_pacing(test_bytes=1024)

        self.assertTrue(res["ok"], res)
        self.assertEqual(tuple(res["xmodem_pacing"]), PACING_CANDIDATES[1],
                         "must fall back to the last setting that verified")
        self.assertEqual(self.link.xmodem_pacing, PACING_CANDIDATES[1])
        self.assertEqual(self.tried, PACING_CANDIDATES[:3], "no point trying faster after a failure")
        self.assertFalse(res["attempts"][-1]["ok"])
        self.assertIn("send_seconds", res["attempts"][0])

    def test_records_the_result_for_future_runs(self):
        self._stub_transfers(fails_at=PACING_CANDIDATES[3])
        self.link.calibrate_pacing(test_bytes=1024)

        stored = self.link.hardware_info["pacing"]
        self.assertEqual(tuple(stored["xmodem"]), PACING_CANDIDATES[2])
        self.assertIn("calibrated", stored)
        self.assertTrue(os.path.exists(self.link.hw_info_file))

    def test_a_board_that_fails_even_the_safest_setting_keeps_its_original(self):
        self._stub_transfers(fails_at=PACING_CANDIDATES[0])
        original = self.link.xmodem_pacing
        res = self.link.calibrate_pacing(test_bytes=1024)

        self.assertFalse(res["ok"])
        self.assertIn("no pacing setting passed", res["error"])
        self.assertEqual(self.link.xmodem_pacing, original,
                         "a failed calibration must not change the live setting")
        self.assertNotIn("pacing", self.link.hardware_info)

    def test_defaults_to_the_ram_disk_as_scratch(self):
        captured = {}
        self._stub_transfers()
        real_upload = self.link.upload

        def _upload(local_path, **kwargs):
            captured.update(kwargs)
            return real_upload(local_path, **kwargs)

        self.link.upload = _upload
        self.link.calibrate_pacing(test_bytes=1024)
        self.assertEqual(captured["dest_drive"], "B:", "MD0 is the volatile RAM disk here")

    def test_small_samples_do_not_get_a_throughput_projection(self):
        """A 4KB test understated the real rate 5x, because ~15s of fixed
        per-transfer cost swamped it. Better to say so than to project."""
        self._stub_transfers()
        res = self.link.calibrate_pacing(test_bytes=1024)
        self.assertNotIn("projected_512kb_minutes", res)
        self.assertIn("too small to project", res["note"])

    def test_large_samples_do_get_a_projection(self):
        self._stub_transfers()
        res = self.link.calibrate_pacing(test_bytes=16384)
        self.assertIn("projected_512kb_minutes", res)
        self.assertNotIn("note", res)

    def test_refuses_without_a_cpm_prompt(self):
        self.link._ensure_cpm_prompt = lambda: False
        res = self.link.calibrate_pacing(test_bytes=1024)
        self.assertFalse(res["ok"])
        self.assertIn("not at a CP/M prompt", res["error"])


if __name__ == "__main__":
    unittest.main()
