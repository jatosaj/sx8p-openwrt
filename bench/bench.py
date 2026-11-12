#!/usr/bin/env python3
# Description: Bench rig for the SX-8P on the bench PC - Tasmota cold cycles, serial boot capture, per-run PoE health, U-Boot RAM boot
"""Stdlib only (the bench PC has no pyserial). Run on the bench PC with the FTDI console and the Tasmota plug 1 that feeds the switch.

  bench.py power status|on|off|cycle [--yes]      relay + watt reading (off/cycle cut the switch's mains: --yes)
  bench.py cold --label L --runs N --yes [--via serial] [--warm]   N x (cycle, capture boot on serial, wait for ssh, run the check;
                                                  --via serial types the check into the console: no ssh/password needed)
  bench.py serial-check SCRIPT [--out F]          type a read-only check script into the console (logged-in/no-password shell)
  bench.py ramboot IMAGE --yes                    cycle, catch U-Boot, loady + bootm IMAGE (no flash writes)

Env: TASMOTA (plug URL, required), SX8P_TTY (default the FTDI by-id path),
SWITCH (ssh target host/IP, user root, your key authorised on the switch), BENCH_OUT (results dir).
Never touches flash, uci or the root password. Exit: 0 ok · 1 a run failed · 2 usage/refused · 3 rig problem.
"""

import argparse
import fcntl
import json
import os
import re
import select
import subprocess
import sys
import termios
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "tools" / "mac"
DEFAULT_TTY = "/dev/ttyUSB0"
UP = re.compile(rb"Please press Enter to activate this console")
UBOOT = re.compile(rb"U-Boot|autoboot|Hit any key")
BAD = re.compile(
    rb"(request-bad-checksum|Aborting request|pse.*(error|fail|timeout)|(?<![a-z])oe: *[1-9]|give up|giving up)",
    re.IGNORECASE,
)


def tasmota_base():
    base = os.environ.get("TASMOTA")
    if not base:
        raise ValueError(
            "set TASMOTA to the plug's URL (http://HOST), use your plug's address"
        )
    return base.rstrip("/")


def tas(cmnd, base=None, timeout=5.0):
    """One Tasmota command; returns the decoded JSON reply."""
    url = f"{base or tasmota_base()}/cm?cmnd={urllib.parse.quote(cmnd)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def watts(base=None):
    return float(tas("Status 10", base)["StatusSNS"]["ENERGY"]["Power"])


def power(action, base=None, off_secs=10.0, settle=20.0):
    """on/off/status/cycle. cycle = Off, wait for <1 W, hold off_secs, On. Returns watts after."""
    if action == "status":
        return watts(base)
    if action in ("on", "off"):
        tas(f"Power {action.capitalize()}", base)
    else:
        tas("Power Off", base)
        end = time.time() + settle
        while time.time() < end and watts(base) >= 1.0:
            time.sleep(0.5)
        time.sleep(off_secs)
        tas("Power On", base)
    time.sleep(1.0)
    return watts(base)


def open_tty(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[4] = a[5] = termios.B115200
    a[6][termios.VMIN] = 0
    a[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def drain(fd):
    while select.select([fd], [], [], 0)[0]:
        try:
            if not os.read(fd, 65536):
                return
        except OSError:
            return


def capture(fd, until, timeout, poke=None):
    """Read serial until the regex `until` appears or timeout. Returns (bytes, seconds_to_match or None).
    poke: bytes sent every 50 ms once `until` is not yet matched and poke_when matches (U-Boot catch)."""
    buf = b""
    start = time.time()
    while time.time() - start < timeout:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                buf += os.read(fd, 4096)
            except OSError:
                break
        if poke and poke[0].search(buf[-4000:]):
            os.write(fd, poke[1])
        if until.search(buf):
            return buf, time.time() - start
    return buf, None


def ssh(host, args, stdin_file=None, timeout=60):
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"root@{host}",
        *args,
    ]
    f = open(stdin_file, "rb") if stdin_file else subprocess.DEVNULL  # noqa: SIM115
    try:
        return subprocess.run(
            cmd, stdin=f, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, -1, b"", b"ssh timed out")
    finally:
        if stdin_file:
            f.close()


def wait_ssh(host, secs):
    end = time.time() + secs
    while time.time() < end:
        if ssh(host, ["true"], timeout=15).returncode == 0:
            return True
        time.sleep(3)
    return False


def field(report, pattern):
    """First integer captured by `pattern` in a check report, or None when the check does not print it
    (01-readonly-health.sh prints the unknown count, main-pse-health.sh the answering count)."""
    m = re.search(pattern, report)
    return int(m.group(1)) if m else None


COUNTERS = re.compile(
    rb"^(?:request-bad-checksum\|bad checksum|MCU rejected|resynchronising|Aborting request|"
    rb"re-sending setup|giving up|No response from PoE)\s+(\d+)\s*$",
    re.MULTILINE,
)


def log_errors(report):
    """Sum of the daemon log counters 01-readonly-health.sh prints (all must be 0), plus BAD hits in the report."""
    rest = COUNTERS.sub(
        b"", report
    )  # the zero-valued counter lines themselves name the bad patterns
    return sum(int(n) for n in COUNTERS.findall(report)) + len(BAD.findall(rest))


def summarize(rec):
    """Pass = console reached, ssh answered, check ran clean, nothing on the bad-pattern list."""
    rec["ok"] = bool(
        rec["console_secs"] is not None
        and rec["ssh"]
        and rec["check_rc"] == 0
        and rec["bad_serial"] == 0
        and rec["expect_ok"] in (None, True)
        and rec["log_errors"] == 0
        and rec["daemon_alive"]
        and rec["unknown_ports"] in (None, 0)
        and rec["pse_answering"] in (None, 8)
    )
    return rec


def cold(args):
    out = Path(os.environ.get("BENCH_OUT", "bench-results")) / args.label
    out.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("SWITCH")
    if args.via == "ssh" and not host:
        print(
            "set SWITCH (ssh host of the switch), or use --via serial", file=sys.stderr
        )
        return 2
    check = args.check or str(HERE / "checks" / "main-pse-health.sh")
    fd = open_tty(os.environ.get("SX8P_TTY", DEFAULT_TTY))
    recs = []
    for i in range(1, args.runs + 1):
        drain(fd)
        if args.warm:
            # no plug: `reboot` over ssh - the same kernel start-up (driver probe vs console output), without the mains cut
            ssh(host, ["reboot"], timeout=20)
            w_on = None
        else:
            w_on = power("cycle", off_secs=args.off_secs)
        serial, secs = capture(fd, UP, args.boot_timeout)
        if secs is not None and args.via == "ssh":
            more, _ = capture(fd, re.compile(rb"\A\Z\Z"), args.tail_secs)
            serial += more  # late kernel/PSE errors after the prompt
        (out / f"run{i:02d}-serial.log").write_bytes(serial)
        rc, report = -1, b""
        if secs is not None and args.settle:
            time.sleep(
                args.settle
            )  # a PD (camera) needs time to be detected and powered
        if args.via == "serial":
            # a build with no root password has no ssh: ask the console shell instead
            up = secs is not None
            if up:
                tail = serial[UP.search(serial).end() :]
                report, ok = send_script(fd, check, 60, prior=tail)
                rc = 0 if ok else 1
        else:
            up = wait_ssh(host, args.ssh_timeout)
            if up:
                res = ssh(host, ["sh", "-s"], stdin_file=check, timeout=120)
                rc, report = res.returncode, res.stdout + res.stderr
        (out / f"run{i:02d}-check.txt").write_bytes(report)
        rec = summarize(
            {
                "run": i,
                "watts_after_on": w_on,
                "console_secs": secs,
                "ssh": up,
                "check_rc": rc,
                "bad_serial": len(BAD.findall(serial)),
                "expect_ok": (
                    bool(re.search(args.expect_regex.encode(), report, re.DOTALL))
                    if args.expect_regex
                    else None
                ),
                "log_errors": log_errors(report),
                # 01-readonly-health prints 'ports "unknown": 0' even when ubus returned nothing: need real poe info
                "daemon_alive": b'ports "unknown"' not in report
                or b"firmware" in report,
                "unknown_ports": field(report, rb'ports "unknown": (\d+)'),
                "pse_answering": field(report, rb"pse ports answering: (\d+) of 8"),
            }
        )
        recs.append(rec)
        print(json.dumps(rec), flush=True)
        (out / "summary.json").write_text(json.dumps(recs, indent=1))
    n_ok = sum(r["ok"] for r in recs)
    print(f"{args.label}: {n_ok}/{len(recs)} runs ok -> {out}")
    return 0 if n_ok == len(recs) else 1


END = re.compile(rb"=== end of report ===\r?\n")


def send_script(fd, script, timeout, prior=b""):
    """Type SCRIPT (comments and blank lines skipped) into an open console shell; return (output, ok).
    prior = console bytes already read after the prompt, so a report that arrived early still counts."""
    os.write(fd, b"\r")
    for line in Path(script).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            os.write(fd, line.encode() + b"\r")
            time.sleep(0.05)
    if END.search(prior):
        return prior, True
    buf, secs = capture(fd, END, timeout)
    return prior + buf, secs is not None


def serial_check(args):
    """For a RAM-booted image (no password, no ssh): send SCRIPT, save and print the report."""
    fd = open_tty(os.environ.get("SX8P_TTY", DEFAULT_TTY))
    drain(fd)
    buf, ok = send_script(fd, args.script, args.timeout)
    if args.out:
        Path(args.out).write_bytes(buf)
    sys.stdout.write(buf.decode("latin1").replace("\r", ""))
    return 0 if ok else 1


def ramboot(args):
    img = Path(args.image).resolve()
    if not img.is_file():
        print(f"no such image: {img}", file=sys.stderr)
        return 2
    tty = os.environ.get("SX8P_TTY", DEFAULT_TTY)
    fd = open_tty(tty)
    power("cycle", off_secs=args.off_secs)
    _, secs = capture(fd, re.compile(rb"Pakedge# "), 60, poke=(UBOOT, b" "))
    os.close(fd)
    if secs is None:
        print("no U-Boot prompt", file=sys.stderr)
        return 3
    size = img.stat().st_size

    def py(script, *a):
        return subprocess.run(
            [sys.executable, "-I", str(TOOLS / script), "--tty", tty, *a], check=False
        ).returncode

    if py("uboot_cmd.py", "printenv bootcmd"):
        return 3
    with open(tty, "wb", buffering=0) as t:
        t.write(b"loady 0x82000000\r")
    time.sleep(2)
    if py("ymodem_send.py", str(img)):
        return 3
    if py(
        "uboot_cmd.py",
        "--timeout",
        "30",
        f"crc32 0x82000000 0x{size:x}",
        "iminfo 0x82000000",
    ):
        return 4
    return py("term.py", "150", "bootm 0x82000000")


def rig_lock():
    """One controller at a time: two runs would cut each other's power and share the serial port."""
    if (
        os.environ.get("BENCH_LOCK_HELD") == "1"
    ):  # run-plan.sh holds it for the whole plan
        return open(os.devnull, encoding="utf-8")
    path = os.environ.get("BENCH_LOCK", str(Path.home() / ".sx8p-rig.lock"))
    fh = open(path, "w", encoding="utf-8")  # noqa: SIM115 (held for the process lifetime)
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise ValueError(f"another bench run holds the rig ({path})") from None
    return fh


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("power")
    p.add_argument("action", choices=["status", "on", "off", "cycle"])
    p.add_argument("--yes", action="store_true")
    p.add_argument("--off-secs", type=float, default=10.0)
    c = sub.add_parser("cold")
    c.add_argument("--label", required=True)
    c.add_argument("--runs", type=int, default=3)
    c.add_argument(
        "--check", help="script run on the switch via ssh (default main-pse-health.sh)"
    )
    c.add_argument(
        "--expect-regex",
        help="must match the check report, e.g. a port that has to be delivering power",
    )
    c.add_argument(
        "--settle",
        type=float,
        default=0.0,
        help="seconds to wait after the prompt before the check",
    )
    c.add_argument(
        "--warm",
        action="store_true",
        help="reboot over ssh instead of cutting the mains (needs SWITCH)",
    )
    c.add_argument("--via", choices=["ssh", "serial"], default="ssh")
    c.add_argument("--off-secs", type=float, default=10.0)
    c.add_argument("--boot-timeout", type=float, default=240.0)
    c.add_argument("--ssh-timeout", type=float, default=120.0)
    c.add_argument(
        "--tail-secs",
        type=float,
        default=20.0,
        help="keep reading the console this long after the prompt (ssh mode)",
    )
    c.add_argument("--yes", action="store_true")
    k = sub.add_parser("serial-check")
    k.add_argument("script")
    k.add_argument("--out")
    k.add_argument("--timeout", type=float, default=60.0)
    r = sub.add_parser("ramboot")
    r.add_argument("image")
    r.add_argument("--off-secs", type=float, default=10.0)
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "power" and a.action in ("off", "cycle") and not a.yes:
        print("refusing: off/cycle cut the switch's mains; pass --yes", file=sys.stderr)
        return 2
    if a.cmd in ("cold", "ramboot") and not a.yes:
        print("refusing: this power-cycles the switch; pass --yes", file=sys.stderr)
        return 2
    try:
        lock = (
            rig_lock()
            if (a.cmd != "power" or a.action in ("off", "cycle", "on"))
            else None
        )
        if a.cmd == "power":
            print(f"{power(a.action, off_secs=a.off_secs):.1f} W")
            return 0
        if a.cmd == "serial-check":
            return serial_check(a)
        try:
            return cold(a) if a.cmd == "cold" else ramboot(a)
        finally:
            if lock:
                lock.close()
    except (OSError, ValueError, KeyError) as e:
        print(f"rig problem: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
