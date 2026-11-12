#!/usr/bin/env python3
# Description: Loopback tests for tools/mac/ymodem_send.py against a stub receiver on a pty
"""Run:  python3 -m unittest discover -s tests -v   (POSIX only: needs pty)."""

import os
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import tty
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENDER = os.path.join(ROOT, "tools", "mac", "ymodem_send.py")
sys.path.insert(0, os.path.join(ROOT, "tools", "mac"))
import ymodem_send

SOH, STX, EOT, ACK, NAK, CAN, C = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18, ord("C")


class StubReceiver(threading.Thread):
    """Minimal U-Boot `loady` stand-in on the pty master.

    eot: "ack" = NAK the first EOT, ACK the second (normal); "nak" = NAK every EOT.
    end: "ack" = ACK the end-of-batch block; "can" = cancel it."""

    def __init__(self, master, eot="ack", end="ack"):
        super().__init__(daemon=True)
        self.m, self.eot, self.end = master, eot, end
        self.header = None
        self.data = bytearray()
        self.end_block_seen = False
        self.error = None
        self.stop = threading.Event()

    def read(self, n, timeout=10.0):
        out = bytearray()
        deadline = time.time() + timeout
        while len(out) < n:
            if self.stop.is_set() or time.time() > deadline:
                raise TimeoutError(f"wanted {n} B, got {len(out)}")
            r, _, _ = select.select([self.m], [], [], 0.05)
            if r:
                try:
                    out += os.read(self.m, n - len(out))
                except OSError:  # sender closed the slave
                    raise TimeoutError("pty closed") from None
        return bytes(out)

    def put(self, b):
        os.write(self.m, bytes([b]))

    def packet(self, first):
        size = 128 if first == SOH else 1024
        rest = self.read(2 + size + 2)
        seq, inv, data = rest[0], rest[1], rest[2 : 2 + size]
        if seq != 0xFF - inv:
            raise AssertionError("bad sequence complement")
        if ymodem_send.crc16(data) != int.from_bytes(rest[-2:], "big"):
            raise AssertionError(f"bad CRC in block {seq}")
        return seq, data

    def run(self):
        try:
            self.session()
        except (AssertionError, OSError, TimeoutError) as e:  # reported by the test
            self.error = e
        while not self.stop.is_set():  # keep draining so the sender's tcdrain returns
            r, _, _ = select.select([self.m], [], [], 0.05)
            if r:
                try:
                    os.read(self.m, 4096)
                except OSError:
                    return

    def first_byte_while_poking(self):
        while True:
            self.put(C)
            r, _, _ = select.select([self.m], [], [], 1.0)
            if r:
                return self.read(1)[0]
            if self.stop.is_set():
                raise TimeoutError("stopped")

    def session(self):
        b = self.first_byte_while_poking()
        while b not in (SOH, STX):  # stale bytes
            b = self.read(1)[0]
        seq, data = self.packet(b)
        assert seq == 0, seq
        self.header = data
        self.put(ACK)
        self.put(C)
        want, eots = 1, 0
        while True:
            b = self.read(1)[0]
            if b == EOT:
                eots += 1
                if self.eot == "ack" and eots >= 2:
                    self.put(ACK)
                    break
                self.put(NAK)
                continue
            seq, data = self.packet(b)
            if seq == 0 and want == 1:  # header resent (stale 'C' read as no-ACK)
                self.put(ACK)
                continue
            if seq == (want - 1) & 0xFF:  # duplicate
                self.put(ACK)
                continue
            assert seq == want & 0xFF, (seq, want)
            self.data += data
            want += 1
            self.put(ACK)
        self.put(C)
        b = self.read(1)[0]
        seq, data = self.packet(b)
        assert seq == 0 and data == b"\0" * 128, (seq, data[:16])
        self.end_block_seen = True
        self.put(ACK if self.end == "ack" else CAN)


class YmodemLoopback(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.dev = os.ttyname(self.slave)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        os.close(self.master)
        os.close(self.slave)
        for f in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    def send(self, blob, name="sx8p-flash.bin", use_env=False, **recv):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(blob)
        rx = StubReceiver(self.master, **recv)
        rx.start()
        env = dict(os.environ)
        args = [sys.executable, SENDER]
        if use_env:
            env["SX8P_TTY"] = self.dev
        else:
            args += ["--tty", self.dev]
        r = subprocess.run(
            [*args, path],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        rx.stop.set()
        rx.join(5)
        return r, rx

    def test_happy_path(self):
        blob = os.urandom(5000)
        r, rx = self.send(blob)
        self.assertIsNone(rx.error)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        name, size = rx.header.split(b"\0")[:2]
        self.assertEqual((name, int(size)), (b"sx8p-flash.bin", len(blob)))
        self.assertEqual(bytes(rx.data[: len(blob)]), blob)
        self.assertEqual(bytes(rx.data[len(blob) :]), b"\x1a" * (-len(blob) % 1024))
        self.assertTrue(rx.end_block_seen)

    def test_tty_from_env(self):
        r, rx = self.send(b"abc", use_env=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(rx.error)

    def test_long_name_truncated_into_128_byte_header(self):
        blob = b"x" * 3000
        r, rx = self.send(blob, name="n" * 200)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(rx.error)
        name, size = rx.header.split(b"\0")[:2]
        self.assertEqual(int(size), len(blob))
        self.assertEqual(name, b"n" * len(name))
        self.assertLessEqual(len(name), 128 - 2 - len(size))

    def test_eot_never_acked_fails(self):
        r, _rx = self.send(
            os.urandom(2000), eot="nak"
        )  # rx then times out: sender gave up
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("EOT never acknowledged", r.stdout)

    def test_end_of_batch_refused_fails(self):
        r, rx = self.send(os.urandom(2000), end="can")
        self.assertIsNone(rx.error)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertTrue(rx.end_block_seen)

    def test_header_payload_never_exceeds_block(self):
        for n in (0, 1, 100, 125, 126, 127, 128, 1000):
            p = ymodem_send.header_payload(b"a" * n, 12345678)
            self.assertLessEqual(len(p), 128, n)
            self.assertTrue(p.endswith(b"\x0012345678\x00"), n)


if __name__ == "__main__":
    unittest.main()
