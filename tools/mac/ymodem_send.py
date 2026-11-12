#!/usr/bin/env python3
"""YMODEM-1K sender for U-Boot `loady`, stdlib only. Runs on the Mac next to the FTDI adapter.
usage: ymodem_send.py [--tty DEV] FILE   (U-Boot must already be waiting in `loady ADDR`)
       DEV defaults to $SX8P_TTY, else /dev/cu.usbserial
Exit: 0 sent, EOT and end-of-batch acknowledged · 1 usage · 2 receiver never asked ·
3 a block was refused 10 times · 4 EOT never acknowledged · 5 end-of-batch block refused."""

import argparse
import fcntl
import os
import select
import sys
import termios
import time

DEFAULT_TTY = "/dev/cu.usbserial"
SOH, STX, EOT, ACK, NAK, CAN, CRC = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18, ord("C")


def crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
            )
    return crc


def header_payload(name, size):
    """Block-0 payload: name NUL size NUL, the name truncated so it fits 128 bytes."""
    tail = b"\0" + str(size).encode() + b"\0"
    return name[: 128 - len(tail)] + tail


def open_tty(dev):
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = 0
    a[1] = 0
    a[3] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[4] = a[5] = termios.B115200
    a[6][termios.VMIN] = 0
    a[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    termios.tcflush(
        fd, termios.TCIFLUSH
    )  # drop stale console bytes before the handshake
    return fd


def close_tty(fd):
    """Wait until every byte left the UART, then close (blocking mode for tcdrain)."""
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    termios.tcdrain(fd)
    os.close(fd)


class Sender:
    def __init__(self, fd):
        self.fd = fd

    def getc(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if r:
                try:
                    d = os.read(self.fd, 1)
                except BlockingIOError:
                    continue
                if d:
                    return d[0]
        return None

    def write_all(self, b):
        v = memoryview(b)
        while v:
            try:
                n = os.write(self.fd, v)
                v = v[n:]
            except BlockingIOError:
                select.select([], [self.fd], [], 1)

    def wait_c(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if self.getc(1) == CRC:
                return True
        return False

    def send_block(self, seq, payload, size):
        hdr = STX if size == 1024 else SOH
        if len(payload) > size:
            raise ValueError(f"block {seq}: {len(payload)} B payload > {size}")
        data = payload.ljust(size, b"\x1a" if seq else b"\x00")
        pkt = (
            bytes([hdr, seq & 0xFF, 0xFF - (seq & 0xFF)])
            + data
            + crc16(data).to_bytes(2, "big")
        )
        for _ in range(10):
            self.write_all(pkt)
            c = self.getc(10)
            if c == ACK:
                return True
            if c == CAN:
                return False
        return False

    def send_eot(self):
        """EOT until ACKed; the receiver normally NAKs the first one."""
        for _ in range(5):
            self.write_all(bytes([EOT]))
            if self.getc(5) == ACK:
                return True
        return False


def send_file(fd, path):
    with open(path, "rb") as f:
        blob = f.read()
    s = Sender(fd)
    if not s.wait_c(60):
        print("receiver never sent 'C'")
        return 2
    if not s.send_block(
        0, header_payload(os.path.basename(path).encode(), len(blob)), 128
    ):
        print("header refused")
        return 3
    s.wait_c(10)
    t0 = time.time()
    seq = 1
    for off in range(0, len(blob), 1024):
        if not s.send_block(seq, blob[off : off + 1024], 1024):
            print(f"block {seq} refused")
            return 3
        seq += 1
        if seq % 512 == 0:
            print(
                f"{off + 1024}/{len(blob)} bytes, {time.time() - t0:.0f}s", flush=True
            )
    if not s.send_eot():
        print("EOT never acknowledged")
        return 4
    s.wait_c(5)
    if not s.send_block(0, b"", 128):  # empty header = end of batch
        print("end-of-batch block refused")
        return 5
    print(f"sent {len(blob)} bytes in {time.time() - t0:.0f}s", flush=True)
    return 0


class Args(argparse.ArgumentParser):
    def error(self, message):  # exit 1, not argparse's 2 (2 = receiver never asked)
        self.print_usage(sys.stderr)
        print(f"ymodem_send.py: error: {message}", file=sys.stderr)
        sys.exit(1)


def main():
    ap = Args(description="YMODEM-1K sender for U-Boot loady")
    ap.add_argument("--tty", default=os.environ.get("SX8P_TTY", DEFAULT_TTY))
    ap.add_argument("file")
    a = ap.parse_args()
    fd = open_tty(a.tty)
    try:
        rc = send_file(fd, a.file)
    finally:
        close_tty(fd)
    sys.exit(rc)


if __name__ == "__main__":
    main()
