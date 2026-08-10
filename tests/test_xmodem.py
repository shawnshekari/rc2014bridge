"""XMODEM protocol tests.

test_block_numbers_advance is the regression test for the bug where every
block was transmitted with sequence number 1: a conforming receiver treats
blocks 2+ as duplicate retransmissions, ACKs them, and discards the payload,
so any upload over 128 bytes silently arrived truncated.
"""

import hashlib
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rc2014bridge.link import ACK, BLOCK_SIZE, EOT, NAK, SOH, SUB, SerialLink, _crc16

from fakes import FakeSerial


def _scratch_hw_info() -> str:
    """A hardware-info path that doesn't exist, so tests never read or clobber
    the real hardware_info.json."""
    return os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hardware_info.json")


class ScriptedReceiver(threading.Thread):
    """Stands in for RomWBW's XM receiving a file: pokes 'C', then ACKs each
    well-formed block, recording the sequence number it saw."""

    def __init__(self, port: FakeSerial, use_crc: bool = True):
        super().__init__(daemon=True)
        self.port = port
        self.use_crc = use_crc
        self.headers: list[int] = []
        self.payloads: list[bytes] = []
        self.saw_eot = False
        self._stop = threading.Event()

    def run(self):
        trailer = 2 if self.use_crc else 1
        packet_len = 3 + BLOCK_SIZE + trailer
        poke = b"C" if self.use_crc else bytes([NAK])
        next_poke = 0.0
        cursor = 0
        deadline = time.time() + 20.0
        while not self._stop.is_set() and time.time() < deadline:
            buf = self.port.written_since(cursor)
            if not buf:
                # Keep poking until the first block lands, exactly as a real XM
                # receiver does - the sender may not be listening yet.
                if not self.headers and time.time() >= next_poke:
                    self.port.feed(poke)
                    next_poke = time.time() + 0.25
                time.sleep(0.005)
                continue
            if buf[0] == EOT:
                self.saw_eot = True
                cursor += 1
                self.port.feed(bytes([ACK]))
                return
            if buf[0] != SOH:
                cursor += 1  # stray byte (the startup CR probe, for instance)
                continue
            if len(buf) < packet_len:
                time.sleep(0.005)
                continue
            self.headers.append(buf[1])
            self.payloads.append(bytes(buf[3:3 + BLOCK_SIZE]))
            cursor += packet_len
            self.port.feed(bytes([ACK]))

    def stop(self):
        self._stop.set()


def _unpaced(link):
    """Pacing is a UART workaround, irrelevant to protocol correctness in a
    loopback - skipping it keeps these tests fast."""
    return patch.object(type(link), "_write_paced",
                        lambda self, data, chunk=8, delay=0.010: self._write_raw(data))


class TestXmodemSender(unittest.TestCase):
    def _link(self) -> tuple[SerialLink, FakeSerial]:
        fake = FakeSerial()
        with patch("serial.Serial", return_value=fake):
            link = SerialLink("/dev/fake", hw_info_file=_scratch_hw_info())
        self.addCleanup(link.close)
        return link, fake

    def test_block_numbers_advance(self):
        link, fake = self._link()
        receiver = ScriptedReceiver(fake)

        payload = bytes(range(256)) * 2  # 512 bytes -> exactly 4 blocks
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(payload)
            path = tf.name
        self.addCleanup(os.unlink, path)

        receiver.start()
        with _unpaced(link):
            res = link.xmodem_send(path)
        receiver.stop()

        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["blocks"], 4)
        self.assertEqual(receiver.headers, [1, 2, 3, 4],
                         "each block must carry its own sequence number")
        self.assertEqual(b"".join(receiver.payloads), payload,
                         "the receiver must see every byte of the file, in order")
        self.assertTrue(receiver.saw_eot)

    def test_block_header_complement_and_crc(self):
        link, fake = self._link()
        receiver = ScriptedReceiver(fake)

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"A" * 200)  # 2 blocks, second one padded
            path = tf.name
        self.addCleanup(os.unlink, path)

        receiver.start()
        with _unpaced(link):
            res = link.xmodem_send(path)
        receiver.stop()

        self.assertTrue(res.get("ok"), res)
        raw = bytes(fake.written)
        first = raw.index(bytes([SOH]))
        packet = raw[first:first + 3 + BLOCK_SIZE + 2]
        self.assertEqual(packet[1] ^ packet[2], 0xFF, "header must be blocknum/~blocknum")
        block = packet[3:3 + BLOCK_SIZE]
        crc = _crc16(block)
        self.assertEqual(packet[-2:], bytes([crc >> 8, crc & 0xFF]))
        # Final short block is padded to a full 128 bytes with 0x1A
        self.assertEqual(receiver.payloads[-1][-1], SUB)

    def test_no_prompt_nudge_when_no_os_is_running(self):
        """After a ROM write the documented recovery is to retry the transfer and
        touch nothing else, so we must not fire an unsolicited CR into the flash
        updater's menu. Only nudge when an OS is there to redraw a prompt."""
        link, fake = self._link()
        link._system_state = "hbios"          # e.g. sitting in the flash updater
        receiver = ScriptedReceiver(fake)

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"R" * 256)
            path = tf.name
        self.addCleanup(os.unlink, path)

        receiver.start()
        with _unpaced(link):
            res = link.xmodem_send(path)
        receiver.stop()

        self.assertTrue(res.get("ok"), res)
        self.assertFalse(res["prompt_nudged"])
        # the last thing we sent must be the EOT, not a stray carriage return
        self.assertEqual(bytes(fake.written)[-1], EOT)

    def test_prompt_nudge_happens_under_cpm(self):
        link, fake = self._link()
        link._system_state = "cpm"
        receiver = ScriptedReceiver(fake)

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"C" * 256)
            path = tf.name
        self.addCleanup(os.unlink, path)

        receiver.start()
        with _unpaced(link):
            res = link.xmodem_send(path)
        receiver.stop()

        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res["prompt_nudged"])
        self.assertEqual(bytes(fake.written)[-1], ord("\r"))

    def test_handshake_timeout_cancels(self):
        link, fake = self._link()
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"x" * 64)
            path = tf.name
        self.addCleanup(os.unlink, path)

        # Nobody ever pokes, so the handshake must give up - and must not leave
        # the receiver hanging: a cancel goes out on the way back.
        with _unpaced(link):
            res = link.xmodem_send(path, handshake_timeout=1.0)
        self.assertFalse(res["ok"])
        self.assertIn("handshake timeout", res["error"])
        self.assertIn(b"\x18\x18", bytes(fake.written))


class TestXmodemRoundTrip(unittest.TestCase):
    def test_binary_round_trip(self):
        """Send a multi-block binary between two cross-wired links and require
        the bytes to survive exactly - including embedded 0x1A padding bytes."""
        port_a, port_b = FakeSerial.pair()
        with patch("serial.Serial", side_effect=[port_a, port_b]):
            sender = SerialLink("/dev/fake-a", hw_info_file=_scratch_hw_info())
            receiver = SerialLink("/dev/fake-b", hw_info_file=_scratch_hw_info())
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)

        payload = bytes((i * 7 + (0x1A if i % 41 == 0 else 0)) & 0xFF for i in range(5000))
        payload = payload[:-1] + b"\x99"  # must not end in 0x1A, which is padding
        digest = hashlib.sha256(payload).hexdigest()

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(payload)
            src = tf.name
        dst = src + ".received"
        self.addCleanup(os.unlink, src)
        self.addCleanup(lambda: os.path.exists(dst) and os.unlink(dst))

        recv_result = {}

        def _receive():
            recv_result.update(receiver.xmodem_receive(dst, handshake_timeout=15.0,
                                                       overall_timeout=40.0))

        # One patch on the class covers both links.
        with _unpaced(sender):
            thread = threading.Thread(target=_receive, daemon=True)
            thread.start()
            time.sleep(0.4)  # let the receiver start poking
            send_result = sender.xmodem_send(src, handshake_timeout=15.0)
            thread.join(timeout=25.0)

        self.assertTrue(send_result.get("ok"), send_result)
        self.assertTrue(recv_result.get("ok"), recv_result)
        with open(dst, "rb") as f:
            received = f.read()
        self.assertEqual(len(received), len(payload))
        self.assertEqual(hashlib.sha256(received).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
