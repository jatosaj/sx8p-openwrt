#!/usr/bin/env python3
"""Copy files off a busybox shell with no stty/base64: gzip | hexdump on the switch, decode here.
usage: pull_hex.py [--tty DEV] OUTDIR REMOTE_PATH...   (DEV: $SX8P_TTY, else
/dev/cu.usbserial)   Markers are split in the typed command so the echo never matches."""

import argparse
import gzip
import hashlib
import os
import re
import select
import termios
import time

ap = argparse.ArgumentParser(description="copy files off the switch as hex")
ap.add_argument(
    "--tty",
    default=os.environ.get("SX8P_TTY", "/dev/cu.usbserial"),
    help="serial device (default: $SX8P_TTY, else /dev/cu.usbserial)",
)
ap.add_argument("outdir")
ap.add_argument("paths", nargs="+")
args = ap.parse_args()
out, paths = args.outdir, args.paths
os.makedirs(out, exist_ok=True)
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


def typ(s):
    for ch in s.encode():
        os.write(fd, bytes([ch]))
        time.sleep(0.02)


def read_until(marker, timeout):
    buf = bytearray()
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                d = os.read(fd, 65536)
            except BlockingIOError:
                d = b""
            if d:
                buf.extend(d)
                if marker in buf:
                    return bytes(buf)
    return bytes(buf)


typ("\r")
read_until(b"# ", 3)
for p in paths:
    t0 = time.time()
    typ(
        'echo; echo @@BE""GIN@@; gzip -c '
        + p
        + ' | hexdump -v -e \'64/1 "%02x" "\\n"\'; echo @@EN""D@@\r'
    )
    data = read_until(b"@@END@@", 3600).replace(b"\r", b"")
    try:
        body = data.split(b"@@BEGIN@@\n", 1)[1].rsplit(b"@@END@@", 1)[0]
    except IndexError:
        print(f"FAIL {p}: markers not found ({len(data)} B)", flush=True)
        continue
    hexs = re.sub(rb"[^0-9a-f]", b"", body)
    raw = gzip.decompress(bytes.fromhex(hexs.decode()))
    name = os.path.join(out, os.path.basename(p))
    with open(name, "wb") as f:
        f.write(raw)
    print(
        f"{p}: {len(raw)} B (gz {len(hexs) // 2} B) in {time.time() - t0:.0f}s sha256 {hashlib.sha256(raw).hexdigest()}",
        flush=True,
    )
    read_until(b"# ", 3)
os.close(fd)
