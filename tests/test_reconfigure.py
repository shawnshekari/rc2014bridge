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

    def test_same_port_reconfigure_closes_before_reopening(self):
        # Windows refuses a second open of a port we already hold
        # (PermissionError 13, "Access is denied"), so an in-place
        # reconfigure - e.g. just toggling RTS/CTS - must close the old
        # handle BEFORE opening the new one.
        link, fake1 = _link(baud=115200)
        self.addCleanup(link.close)

        events = []
        orig_close = fake1.close
        def _close():
            events.append("close")
            orig_close()
        fake1.close = _close

        with patch("serial.Serial",
                   side_effect=lambda *a, **kw: (events.append("open"), FakeSerial(*a, **kw))[1]):
            res = link.reconfigure(rtscts=True)  # same port, same baud

        self.assertTrue(res["ok"], res)
        self.assertEqual(events, ["close", "open"])
        self.assertFalse(fake1.is_open)
        self.assertTrue(link._ser.is_open)
        self.assertTrue(link.rtscts)

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

    def test_probe_connection_short_circuits_the_port_we_hold(self):
        # The Settings screen probing our own port must not try to open it a
        # second time - Windows denies that outright.
        link, fake = _link(baud=115200)
        self.addCleanup(link.close)
        with patch("serial.Serial") as ctor:
            res = link.probe_connection("/dev/fake", 115200)
        self.assertTrue(res["ok"])
        self.assertTrue(res["already_connected"])
        ctor.assert_not_called()

    def test_probe_connection_delegates_for_other_ports(self):
        link, _ = _link(baud=115200)
        self.addCleanup(link.close)
        with patch("serial.Serial", return_value=FakeSerial()) as ctor:
            res = link.probe_connection("/dev/other", 9600)
        self.assertTrue(res["ok"])
        self.assertNotIn("already_connected", res)
        ctor.assert_called_once()


class TestStartsDisconnected(unittest.TestCase):
    """A configured --port that no longer exists (e.g. a re-plugged FTDI
    adapter renumbering ttyUSB0 -> ttyUSB1) must not be fatal at startup -
    SerialLink should come up disconnected so the GUI's Connection Settings
    screen is reachable to pick the right port."""

    def test_construction_survives_a_missing_port(self):
        with patch("serial.Serial", side_effect=FileNotFoundError(
                "[Errno 2] No such file or directory: '/dev/ttyUSB0'")):
            link = SerialLink("/dev/ttyUSB0", baud=230400, hw_info_file=_hw_path())
        self.addCleanup(link.close)

        self.assertFalse(link.is_connected)
        self.assertEqual(link.port, "/dev/ttyUSB0")
        self.assertEqual(link.baud, 230400)
        # get_screen()'s state feeds the GUI status bar's connected/disconnected badge.
        self.assertFalse(link.get_screen()["connected"])

    def test_reader_thread_stays_alive_while_disconnected(self):
        with patch("serial.Serial", side_effect=OSError("no such device")):
            link = SerialLink("/dev/ttyUSB0", hw_info_file=_hw_path())
        self.addCleanup(link.close)

        time.sleep(0.05)
        self.assertTrue(link._reader.is_alive())

    def test_disconnected_writes_are_a_safe_no_op(self):
        with patch("serial.Serial", side_effect=OSError("no such device")):
            link = SerialLink("/dev/ttyUSB0", hw_info_file=_hw_path())
        self.addCleanup(link.close)

        link.send_text("DIR")  # must not raise

    def test_reconfigure_recovers_a_working_port(self):
        with patch("serial.Serial", side_effect=OSError("no such device")):
            link = SerialLink("/dev/ttyUSB0", hw_info_file=_hw_path())
        self.addCleanup(link.close)

        with _constructing_serial():
            res = link.reconfigure(port="/dev/ttyUSB1")

        self.assertTrue(res["ok"])
        self.assertTrue(link.is_connected)
        self.assertEqual(link.port, "/dev/ttyUSB1")


class TestListSerialPorts(unittest.TestCase):
    def test_list_serial_ports_returns_sorted_device_paths(self):
        class FakePortInfo:
            def __init__(self, device, vid=0x0403):
                self.device = device
                self.vid = vid

        with patch("serial.tools.list_ports.comports",
                   return_value=[FakePortInfo("/dev/ttyUSB1"), FakePortInfo("/dev/ttyUSB0")]):
            self.assertEqual(list_serial_ports(), ["/dev/ttyUSB0", "/dev/ttyUSB1"])

    def test_list_serial_ports_excludes_non_usb_ports(self):
        class FakePortInfo:
            def __init__(self, device, vid):
                self.device = device
                self.vid = vid

        with patch("serial.tools.list_ports.comports",
                   return_value=[FakePortInfo("/dev/ttyUSB0", 0x0403),
                                 FakePortInfo("/dev/ttyS0", None)]):
            self.assertEqual(list_serial_ports(), ["/dev/ttyUSB0"])

    def test_list_serial_ports_swallows_errors(self):
        with patch("serial.tools.list_ports.comports", side_effect=Exception("boom")):
            self.assertEqual(list_serial_ports(), [])


if __name__ == "__main__":
    unittest.main()
