"""Tests for the INI config file support in rc2014bridge/config.py."""

import os
import tempfile
import unittest

from rc2014bridge import config


class TestLoad(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(config.load("/no/such/file.ini"), {})

    def test_missing_path_returns_empty_dict(self):
        self.assertEqual(config.load(""), {})

    def test_flattens_sections(self):
        path = os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "cfg.ini")
        with open(path, "w") as f:
            f.write(
                "[serial]\n"
                "port = /dev/ttyUSB0\n"
                "baud = 230400\n"
                "\n"
                "[mcp]\n"
                "host = 127.0.0.1\n"
            )
        cfg = config.load(path)
        self.assertEqual(cfg["port"], "/dev/ttyUSB0")
        self.assertEqual(cfg["baud"], "230400")
        self.assertEqual(cfg["host"], "127.0.0.1")

    def test_malformed_file_returns_empty_dict_not_raises(self):
        path = os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "bad.ini")
        with open(path, "w") as f:
            f.write("not a valid = = ini [[[ file\n")
        self.assertEqual(config.load(path), {})


class TestUpdateValue(unittest.TestCase):
    def _path(self) -> str:
        return os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "cfg.ini")

    def test_creates_file_and_section_when_missing(self):
        path = self._path()
        config.update_value(path, "serial", "baud", "230400")
        self.assertEqual(config.load(path), {"baud": "230400"})

    def test_updates_existing_key_in_place(self):
        path = self._path()
        with open(path, "w") as f:
            f.write("[serial]\nport = /dev/ttyUSB0\nbaud = 115200\n")
        config.update_value(path, "serial", "baud", "230400")
        cfg = config.load(path)
        self.assertEqual(cfg["baud"], "230400")
        self.assertEqual(cfg["port"], "/dev/ttyUSB0")

    def test_preserves_comments_and_other_sections(self):
        path = self._path()
        with open(path, "w") as f:
            f.write(
                "# a helpful comment\n"
                "[serial]\n"
                "port = /dev/ttyUSB0\n"
                "baud = 115200\n"
                "\n"
                "[mcp]\n"
                "host = 0.0.0.0\n"
            )
        config.update_value(path, "serial", "baud", "230400")
        with open(path) as f:
            contents = f.read()
        self.assertIn("# a helpful comment", contents)
        self.assertIn("[mcp]", contents)
        self.assertIn("host = 0.0.0.0", contents)
        self.assertIn("baud = 230400", contents)
        self.assertNotIn("115200", contents)

    def test_adds_new_key_to_existing_section(self):
        path = self._path()
        with open(path, "w") as f:
            f.write("[serial]\nport = /dev/ttyUSB0\n")
        config.update_value(path, "serial", "baud", "230400")
        cfg = config.load(path)
        self.assertEqual(cfg["port"], "/dev/ttyUSB0")
        self.assertEqual(cfg["baud"], "230400")


if __name__ == "__main__":
    unittest.main()
