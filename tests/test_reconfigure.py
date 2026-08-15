"""Tests for SerialLink.reconfigure() - the GUI Settings screen's "Apply &
Reconnect", which closes the current port and reopens it at a possibly
different port/baud/rtscts. The interesting risk is the background reader
thread: closing the old port out from under an in-flight read must not kill
that thread the way a real disconnect would (see _read_loop's epoch check).
"""

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rc2014bridge.link import SerialLink, list_serial_ports, probe_serial_connection

from fakes import FakeSerial


def _hw_path() -> str:
    return os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hw.json")


def _link(**kwargs) -> tuple[SerialLink, FakeSerial]:
    fake = FakeSerial()
    with patch("serial.Serial", return_value=fake):
        link = SerialLink("/dev/fake", hw_info_file=_hw_path(), **kwargs)
    return link, fake


def _constructing_serial():
    """A patch target whose calls actually build a FakeSerial from the args
    it was given, instead of always returning one fixed pre-built instance -
    needed here because reconfigure()'s whole job is choosing what those
    args are (new port/baud/rtscts)."""
    return patch("serial.Serial", side_effect=lambda *a, **kw: FakeSerial(*a, **kw))


class TestReconfigure(unittest.TestCase):
    def test_reconfigure_updates_port_baud_rtscts_and_swaps_ser(self):
        link, fake1 = _link(baud=115200)
        self.addCleanup(link.close)

        with _constructing_serial() as new_serial:
            res = link.reconfigure(port="/dev/fake2", baud=230400, rtscts=True)

        self.assertEqual(res, {"ok": True, "port": "/dev/fake2", "baud": 230400, "rtscts": True})
        self.assertEqual(link.port, "/dev/fake2")
        self.assertEqual(link.baud, 230400)
        self.assertIs(link.rtscts, True)
        self.assertIsNot(link._ser, fake1)
        self.assertFalse(fake1.is_open)
        self.assertTrue(link._ser.is_open)
        new_serial.assert_called_once()
        args, kwargs = new_serial.call_args
        self.assertEqual(args[0], "/dev/fake2")
        self.assertEqual(kwargs["baudrate"], 230400)
        self.assertEqual(kwargs["rtscts"], True)

    def test_reconfigure_defaults_to_current_values_when_unspecified(self):
        link, _ = _link(baud=115200, rtscts=False)
        self.addCleanup(link.close)

        with _constructing_serial():
            res = link.reconfigure(baud=57600)

        self.assertTrue(res["ok"])
        self.assertEqual(link.port, "/dev/fake")  # unchanged
        self.assertEqual(link.baud, 57600)
        self.assertFalse(link.rtscts)

    def test_reconfigure_failure_leaves_existing_connection_untouched(self):
        link, fake1 = _link(baud=115200)
        self.addCleanup(link.close)

        with patch("serial.Serial", side_effect=OSError("Permission denied")):
            res = link.reconfigure(port="/dev/does-not-exist", baud=9600)

        self.assertFalse(res["ok"])
        self.assertIn("Permission denied", res["error"])
        self.assertEqual(link.port, "/dev/fake")
        self.assertEqual(link.baud, 115200)
        self.assertIs(link._ser, fake1)
        self.assertTrue(fake1.is_open)

    def test_reader_thread_survives_reconfigure(self):
        """The reader thread must not exit just because reconfigure() closed
        the port it was reading - only a real (unintended) disconnect should
        stop it. Bytes fed to the new fake after reconfigure must still show
        up in the terminal screen."""
        link, _ = _link(baud=115200)
        self.addCleanup(link.close)

        with _constructing_serial():
            res = link.reconfigure(baud=230400)
        self.assertTrue(res["ok"])

        self.assertTrue(link._reader.is_alive())

        link._ser.feed(b"hello after reconfigure\r\n")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any("hello after reconfigure" in l for l in link.get_screen().get("lines", [])):
                break
            time.sleep(0.02)
        else:
            self.fail("reader thread did not pick back up after reconfigure()")

        self.assertTrue(link._reader.is_alive())

    def test_reader_thread_survives_non_serial_exception_on_close_race(self):
        """Regression test: real pyserial's read() isn't atomic about a
        concurrent close() - on POSIX it can raise a bare TypeError (from
        os.read() seeing fd=None mid-call) rather than SerialException/
        OSError. The reader loop must treat that as recoverable too, exactly
        like the other exception types, whenever it coincides with a
        reconfigure() (epoch bump) - not just serial-specific errors.

        Simulates the race deterministically: block the reader thread's
        in-flight read() on the *old* fake port until reconfigure() has
        actually swapped in and closed it, then have that blocked call raise
        a bare TypeError - exactly the shape of the real race, just with a
        controlled trigger instead of hoping for unlucky thread scheduling.
        """
        link, fake1 = _link(baud=115200)
        self.addCleanup(link.close)

        reconfigured = threading.Event()

        def flaky_read(size=1):
            reconfigured.wait(timeout=2.0)
            raise TypeError("'NoneType' object cannot be interpreted as an integer")

        fake1.read = flaky_read

        with _constructing_serial():
            res = link.reconfigure(baud=230400)
        self.assertTrue(res["ok"])
        reconfigured.set()

        link._ser.feed(b"survived typeerror\r\n")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any("survived typeerror" in l for l in link.get_screen().get("lines", [])):
                break
            time.sleep(0.02)
        else:
            self.fail("reader thread did not recover from a non-serial exception")

        self.assertTrue(link._reader.is_alive())


class TestConnectionHelpers(unittest.TestCase):
    def test_probe_serial_connection_success(self):
        fake = FakeSerial()
        with patch("serial.Serial", return_value=fake) as ctor:
            res = probe_serial_connection("/dev/fake", 115200, rtscts=True)
        self.assertEqual(res, {"ok": True})
        self.assertFalse(fake.is_open)  # closed again after the probe
        ctor.assert_called_once()

    def test_probe_serial_connection_failure(self):
        with patch("serial.Serial", side_effect=OSError("no such device")):
            res = probe_serial_connection("/dev/nope", 115200)
        self.assertFalse(res["ok"])
        self.assertIn("no such device", res["error"])

    def test_list_serial_ports_returns_sorted_device_paths(self):
        class FakePortInfo:
            def __init__(self, device):
                self.device = device

        with patch("serial.tools.list_ports.comports",
                   return_value=[FakePortInfo("/dev/ttyUSB1"), FakePortInfo("/dev/ttyUSB0")]):
            self.assertEqual(list_serial_ports(), ["/dev/ttyUSB0", "/dev/ttyUSB1"])

    def test_list_serial_ports_swallows_errors(self):
        with patch("serial.tools.list_ports.comports", side_effect=Exception("boom")):
            self.assertEqual(list_serial_ports(), [])


if __name__ == "__main__":
    unittest.main()
