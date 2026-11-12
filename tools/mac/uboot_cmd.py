#!/usr/bin/env python3
# Description: Run U-Boot commands one by one on the SX-8P console; stop on the first error
"""Send each command, wait for the 'Pakedge#' prompt (per-command timeout), print the
output, and stop with exit 3 if the output matches an error pattern or the prompt never
comes back. Use for long sf erase/write: the timeout is per command.

usage: uboot_cmd.py [--tty DEV] [--timeout SEC] "cmd1" "cmd2" ...
exit: 0 all commands ok · 2 no prompt at start · 3 error text or timeout (stops there)
"""

import argparse
import os
import re
import select
import sys
import termios
import time

PROMPT = b"Pakedge# "
ERRORS = re.compile(
    rb"(error|fail|unknown command|bad |invalid|not found|"
    rb"usage:|timeout|!= |differ|abort|protected)",
    re.IGNORECASE,
)


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
    return fd


def read_until_prompt(fd, timeout):
    buf = bytearray()
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                d = os.read(fd, 4096)
            except BlockingIOError:
                d = b""
            if d:
                buf.extend(d)
                sys.stdout.write(d.decode("latin1").replace("\r", ""))
                sys.stdout.flush()
                if buf.rstrip(b"# \r\n").endswith(b"Pakedge") or buf.endswith(PROMPT):
                    return bytes(buf), True
    return bytes(buf), False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tty", default=os.environ.get("SX8P_TTY", "/dev/cu.usbserial")
    )
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("cmds", nargs="+")
    a = ap.parse_args()
    fd = open_tty(a.tty)
    termios.tcflush(fd, termios.TCIFLUSH)
    # Ctrl-C, never Enter: an empty Enter at the U-Boot prompt REPEATS the last command
    # (after `sf erase` that would erase again).
    os.write(fd, b"\x03")
    _, ok = read_until_prompt(fd, 5)
    if not ok:
        print("\n[uboot_cmd] no U-Boot prompt", flush=True)
        sys.exit(2)
    for cmd in a.cmds:
        for ch in (cmd + "\r").encode():
            os.write(fd, bytes([ch]))
            time.sleep(0.02)
        t0 = time.time()
        out, ok = read_until_prompt(fd, a.timeout)
        body = (
            out.split(b"\n", 1)[1] if b"\n" in out else b""
        )  # drop the echoed command line
        if not ok:
            print(f"\n[uboot_cmd] TIMEOUT after {a.timeout:.0f}s on: {cmd}", flush=True)
            sys.exit(3)
        if not cmd.startswith(("help", "?")) and ERRORS.search(body):
            print(f"\n[uboot_cmd] ERROR text after: {cmd}", flush=True)
            sys.exit(3)
        print(f"\n[uboot_cmd] ok ({time.time() - t0:.1f}s): {cmd}", flush=True)
    termios.tcdrain(fd)
    os.close(fd)


if __name__ == "__main__":
    main()
