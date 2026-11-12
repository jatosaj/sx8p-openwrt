#!/usr/bin/env python3
"""Run commands on the SX-8P stock CLI from the Mac (local timing, slow typing).
usage: cli.py [--tty DEV] USER PASS "cmd1" "cmd2" ...   -> prints the transcript
DEV defaults to $SX8P_TTY, else /dev/cu.usbserial.
Logs in only if not already at a 'SX-8P' prompt."""

import argparse
import os
import select
import sys
import termios
import time

ap = argparse.ArgumentParser(description="run commands on the SX-8P stock CLI")
ap.add_argument(
    "--tty",
    default=os.environ.get("SX8P_TTY", "/dev/cu.usbserial"),
    help="serial device (default: $SX8P_TTY, else /dev/cu.usbserial)",
)
ap.add_argument("user")
ap.add_argument("password")
ap.add_argument("cmds", nargs="*")
args = ap.parse_args()
USER, PASS, CMDS = args.user, args.password, args.cmds
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
buf = bytearray()


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
                buf.extend(d)


def typ(s):  # type slowly: this CLI drops characters in bursts
    for ch in s.encode():
        os.write(fd, bytes([ch]))
        time.sleep(0.02)


def wait_for(pats, t):
    start = len(buf)
    end = time.time() + t
    while time.time() < end:
        pump(0.1)
        seg = bytes(buf[start:])
        for p in pats:
            if p in seg:
                return p
    return None


typ("\r")
pump(1.5)
last = bytes(buf[-300:])
if b"SX-8P" not in last.split(b"\n")[-1]:
    if b"Press any key" in last:
        typ(" ")
        pump(1)
    if wait_for([b"Username:"], 3) is None and b"Username:" not in bytes(buf[-100:]):
        typ("\r")
        if wait_for([b"Username:"], 5) is None:
            print("no Username prompt")
            print(bytes(buf).decode("latin1"))
            sys.exit(2)
    typ(USER + "\r")
    if wait_for([b"Password:"], 5) is None:
        print("no Password prompt")
        sys.exit(2)
    typ(PASS + "\r")
    if wait_for([b"SX-8P#", b"SX-8P>"], 6) is None:
        print("login failed")
        sys.exit(2)
mark = len(buf)
for c in CMDS:
    typ(c + "\r")
    wait_for([b"SX-8P#", b"SX-8P(config", b"SX-8P>"], 15)
    pump(0.3)
sys.stdout.write(bytes(buf[mark:]).decode("latin1").replace("\r", ""))
os.close(fd)
