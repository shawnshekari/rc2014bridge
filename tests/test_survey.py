"""Tests for SURVEY parsing and for CP/M user-area prompts.

Both came out of the same observation: SURVEY reported 210 files on A: where a
DIR-based scan saw 50, because DIR only lists the current user area - and
stepping into another user area revealed that the prompt becomes "C2>", which
the original prompt pattern didn't match, so every command timed out.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from rc2014bridge.link import (CPM_PROMPT_RE, PROMPT_ONLY_RE, TRAILING_PROMPT_RE,
                               SerialLink, _parse_survey_output)

from fakes import FakeSerial

# Verbatim from an RC2014 Pro running RomWBW v3.7.0-dev.13.
SURVEY_OUTPUT = """        *** RomWBW System Survey (Mar 2023) ***

Drive A: 3764K bytes in 210 files with 4412K bytes remaining
Drive B: 28K bytes in 2 files with 228K bytes remaining
Drive C: 378K bytes in 50 files with 6K bytes remaining
Drive D: 36K bytes in 1 files with 8140K bytes remaining
Drive E: 32K bytes in 0 files with 8144K bytes remaining

Memory map:
0       8       16      24      32      40      48      56      64
|       |       |       |       |       |       |       |       |
TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTCCCBBBBBBB
T=TPA   C=CPM   B=BIOS or unassigned    R=ROM or bad
BIOS at E600    iobyte 94       drive 02        BDOS at D800

65535 Bytes RAM         0 Bytes ROM             55296 Bytes in TPA
0 Bytes Empty           65535 Total Active Bytes

Active I/O ports:
10 11 12 13 14 15 16 17
80 81 82 83 84 85 86 87
90 91 92 93 94 95 96 97
24 Ports active"""


class TestParseSurvey(unittest.TestCase):
    def setUp(self):
        self.info = _parse_survey_output(SURVEY_OUTPUT)

    def test_version(self):
        self.assertEqual(self.info["survey_version"], "RomWBW System Survey (Mar 2023)")

    def test_per_drive_totals_count_every_user_area(self):
        drives = self.info["drives"]
        self.assertEqual(set(drives), set("ABCDE"))
        self.assertEqual(drives["A"], {"used": "3764K", "files": 210, "free": "4412K"})
        self.assertEqual(drives["E"]["files"], 0)
        self.assertEqual(drives["B"]["free"], "228K")

    def test_memory_and_addresses(self):
        self.assertEqual(self.info["ram_bytes"], 65535)
        self.assertEqual(self.info["rom_bytes"], 0)
        self.assertEqual(self.info["tpa_bytes"], 55296)
        self.assertEqual(self.info["bios_addr"], "E600")
        self.assertEqual(self.info["bdos_addr"], "D800")
        self.assertEqual(self.info["iobyte"], "94")

    def test_memory_map_band(self):
        self.assertTrue(self.info["memory_map"].startswith("TTTT"))
        self.assertEqual(len(self.info["memory_map"]), 65)

    def test_io_ports(self):
        self.assertEqual(self.info["io_ports_active"], 24)
        self.assertEqual(len(self.info["io_ports"]), 24)
        self.assertIn("10", self.info["io_ports"])   # IDE
        self.assertIn("80", self.info["io_ports"])   # SIO0 console
        self.assertIn("97", self.info["io_ports"])

    def test_unparseable_text_yields_nothing_rather_than_guesses(self):
        self.assertEqual(_parse_survey_output("SURVEY?\nA>"), {})


class TestUserAreaPrompts(unittest.TestCase):
    """RomWBW/ZSDOS renders a non-zero user area as "C2>" - drive letter then
    user number. Matching only "2A>" made every command outside user 0 hang."""

    def test_recognises_both_orderings(self):
        for prompt in ("A>", "C2>", "2A>", "P15>"):
            self.assertTrue(CPM_PROMPT_RE.search(prompt), f"{prompt} should be a prompt")
            self.assertTrue(PROMPT_ONLY_RE.match(prompt), f"{prompt} should be prompt-only")
            self.assertTrue(TRAILING_PROMPT_RE.search(f"output\r\n{prompt}"),
                            f"{prompt} should end a command")

    def test_still_rejects_ordinary_output(self):
        for text in ("No File", "Bytes Remaining On B: 244k", "  |  TEST    .TXT"):
            self.assertFalse(PROMPT_ONLY_RE.match(text))


class TestSurveyCommand(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSerial()
        with patch("serial.Serial", return_value=self.fake):
            self.link = SerialLink(
                "/dev/fake",
                hw_info_file=os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hw.json"))
        self.addCleanup(self.link.close)
        self.link.hardware_info["drive_mappings"] = {"A": "IDE0:0", "C": "MD1:0"}
        self.commands = []

        def _responder(line: bytes):
            self.commands.append(line)
            if b"SURVEY" in line.upper():
                return b"\r\n" + SURVEY_OUTPUT.encode("latin-1").replace(b"\n", b"\r\n") + b"\r\nA>"
            return b"\r\nA>"

        self.fake.responder = _responder
        self._await_prompt()

    def _await_prompt(self, timeout: float = 2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            lines = [l.strip() for l in self.link.get_screen()["lines"] if l.strip()]
            if lines and lines[-1].startswith("A>"):
                return
            time.sleep(0.01)
        self.fail("fake board never printed a prompt")

    def test_runs_survey_from_the_rom_disk_and_records_it(self):
        res = self.link.survey()
        self.assertTrue(res["ok"], res)
        self.assertIn(b"C:SURVEY", b"".join(self.commands))
        self.assertEqual(res["survey"]["drives"]["A"]["files"], 210)
        self.assertEqual(self.link.hardware_info["survey"]["tpa_bytes"], 55296)
        self.assertIn("timestamp", self.link.hardware_info["survey"])
        self.assertTrue(os.path.exists(self.link.hw_info_file))

    def test_refuses_when_not_at_a_cpm_prompt(self):
        self.fake.responder = lambda line: b"\r\nBoot [H=Help]:"
        self.link.run_command("", timeout=2.0)
        res = self.link.survey()
        self.assertFalse(res["ok"])
        self.assertIn("not at a CP/M prompt", res["error"])

    def test_nudges_for_a_prompt_when_the_screen_catches_mid_output(self):
        """Seen on an SC700 whose boot profile prints more than the RC2014's: a
        snapshot right after boot lands mid-output, so a ready machine looked
        like it wasn't at a prompt. A bare CR reprints it."""
        # Leave the screen ending on profile output rather than a prompt.
        self.fake.responder = None
        self.fake.feed(b"\r\n- ZSDOS Path...\r\n   Symbolic : A0: --> D0:\r\n")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            lines = [l.strip() for l in self.link.get_screen()["lines"] if l.strip()]
            if lines and lines[-1].startswith("Symbolic"):
                break
            time.sleep(0.01)

        def _responder(line: bytes):
            self.commands.append(line)
            if b"SURVEY" in line.upper():
                return b"\r\n" + SURVEY_OUTPUT.encode("latin-1").replace(b"\n", b"\r\n") + b"\r\nA>"
            return b"\r\nA>"   # a bare CR reprints the prompt

        self.fake.responder = _responder
        res = self.link.survey()
        self.assertTrue(res["ok"], res)

    def test_reports_unparseable_output_instead_of_claiming_success(self):
        self.fake.responder = lambda line: b"\r\nSURVEY?\r\nA>"
        res = self.link.survey()
        self.assertFalse(res["ok"])
        self.assertIn("could not parse", res["error"])


if __name__ == "__main__":
    unittest.main()
