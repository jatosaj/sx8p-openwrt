#!/usr/bin/env python3
"""Send lines slowly to the SX-8P console and print everything received.
usage: term.py [--tty DEV] WAIT_AFTER_SECONDS "line1" "line2" ...   (WAIT applies after the last line)
DEV defaults to $SX8P_TTY, else /dev/cu.usbserial."""

import argparse
import os
import select
import sys
import termios
import time

ap = argparse.ArgumentParser(description="send lines to the SX-8P console")
ap.add_argument(
    "--tty",
    default=os.environ.get("SX8P_TTY", "/dev/cu.usbserial"),
    help="serial device (default: $SX8P_TTY, else /dev/cu.usbserial)",
)
ap.add_argument("wait", type=float)
ap.add_argument("lines", nargs="*")
args = ap.parse_args()
fd = os.open(args.tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
a = termios.tcgetattr(fd)
a[0] = 0
a[1] = 0
a[3] = 0
a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
a[4] = a[5] = termios.B115200
a[6][termios.VMIN] = 0
a[6][termios.VTIME] = 0
termios.tcsetattr(fd, termios.TCSANOW, a)
out = bytearray()


def pump(t):
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                d = os.read(fd, 4096)
            except BlockingIOError:
                d = b""
            if d:
                out.extend(d)
                sys.stdout.write(d.decode("latin1").replace("\r", ""))
                sys.stdout.flush()


wait = args.wait
for line in args.lines:
    for ch in (line + "\r").encode():
        os.write(fd, bytes([ch]))
        time.sleep(0.02)
    pump(2)
pump(wait)
os.close(fd)
