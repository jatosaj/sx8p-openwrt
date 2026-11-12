#!/usr/bin/env python3
"""SX-8P: log in on the stock console, reboot, stop U-Boot autoboot, read U-Boot env.
Runs on the Mac next to the FTDI adapter so keypresses are not delayed by the network.
Writes nothing to flash: only printenv/help/version are sent at the U-Boot prompt
(unless other commands are given).
usage: uboot_catch.py [--tty DEV] USER PASS [UBOOT_CMD...]
DEV defaults to $SX8P_TTY, else /dev/cu.usbserial."""

import argparse
import os
import select
import sys
import tempfile
import termios
import time

ap = argparse.ArgumentParser(description="catch the SX-8P U-Boot prompt")
ap.add_argument(
    "--tty",
    default=os.environ.get("SX8P_TTY", "/dev/cu.usbserial"),
    help="serial device (default: $SX8P_TTY, else /dev/cu.usbserial)",
)
ap.add_argument("user")
ap.add_argument("password")
ap.add_argument("cmds", nargs="*")
args = ap.parse_args()
LOG = os.environ.get(
    "SX8P_LOG", os.path.join(tempfile.gettempdir(), "sx8p-uboot.log")
)  # per-user dir on macOS
USER, PASS = args.user, args.password

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
log = open(LOG, "wb")  # noqa: SIM115 — module-lifetime log, closed at process exit
buf = bytearray()


def pump(t):
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.02)
        if r:
            try:
                d = os.read(fd, 4096)
            except BlockingIOError:
                d = b""
            if d:
                buf.extend(d)
                log.write(d)
                log.flush()


def send(s):
    os.write(fd, s.encode() if isinstance(s, str) else s)


def wait_for(pat, t, poke=None):
    start = len(buf)
    end = time.time() + t
    while time.time() < end:
        pump(0.1)
        if pat.encode() in buf[start:]:
            return True
        if poke:
            send(poke)
    return False


def step(msg):
    print(f"[{time.strftime('%T')}] {msg}", flush=True)


# 1. get to a CLI prompt
send("\r")
pump(1.5)
tail = bytes(buf[-200:])
if b"#" not in tail.split(b"\n")[-1]:
    if not wait_for("Username:", 8, poke=b"\r"):
        step("no Username prompt")
        sys.exit(2)
    send(USER + "\r")
    if not wait_for("Password:", 5):
        step("no Password prompt")
        sys.exit(2)
    send(PASS + "\r")
    if not wait_for("#", 5):
        step("login failed")
        sys.exit(2)
step("CLI prompt")

# 2. reboot, answer a confirmation if asked
send("reboot\r")
pump(2)
if b"y/n" in bytes(buf[-200:]).lower() or b"(y" in bytes(buf[-200:]).lower():
    send("y\r")
step("reboot sent")

# 3. stop autoboot: hammer spaces from the U-Boot banner until the prompt appears
if not wait_for("U-Boot Version", 40):
    step("no U-Boot banner")
    sys.exit(3)
step("U-Boot banner seen, sending keys")
caught = False
end = time.time() + 25
while time.time() < end:
    send(b" ")
    pump(0.05)
    tail = bytes(buf[-400:])
    if b"Starting kernel" in tail or b"Booting kernel" in tail:
        break
    if tail.rstrip().endswith(b"Pakedge#") and b"Hit any key" in bytes(buf):
        caught = True
        break
pump(1)
if not caught and b"Booting kernel" in bytes(buf[-3000:]):
    step("MISSED: kernel is booting")
    sys.exit(4)
send("\r")
pump(1)
step("at U-Boot prompt")

# 4. commands at the U-Boot prompt (argv[3:]); default is read-only inspection
for cmd in args.cmds or ["printenv", "version", "help"]:
    send(cmd + "\r")
    pump(3)
pump(float(os.environ.get("AFTER", "0")))
step("done")
os.close(fd)
