#!/usr/bin/env python3
# Description: Black-box tests for handoff/checks/*.sh with fake ubus/logread/dmesg on PATH (no hardware)
"""Run:  python3 -m unittest tests.test_handoff_scripts -v
The scripts are fed through stdin (`sh -s < script`) exactly as `ssh root@switch 'sh -s'` does,
under dash and BusyBox ash when available. Every fake command logs its full command line to
calls.log, so tests assert on exactly what a script tried to do."""

import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = os.path.join(ROOT, "handoff", "checks")
HEALTH = os.path.join(CHECKS, "01-readonly-health.sh")
FAULT = os.path.join(CHECKS, "02-fault-injection.sh")
SHELLS = [s for s in (shutil.which("dash"), shutil.which("busybox")) if s]

GOOD = '{"ports":[{"status":"Delivering power"},{"status":"Delivering power"},{"status":"Searching"}],"firmware":"v17.1"}'
IDLE = '{"ports":[{"status":"Searching"}],"firmware":"v17.1"}'
BAD = '{"ports":[{"status":"unknown"},{"status":"unknown"}],"firmware":"v0.0"}'
DISABLED = (
    '{"ports":[{"status":"Disabled"},{"status":"Delivering power"}],"firmware":"v17.1"}'
)
FEWER = '{"ports":[{"status":"Delivering power"},{"status":"Searching"},{"status":"Searching"}],"firmware":"v17.1"}'

HEALTH_ALLOWED = (
    "ubus call poe info",
    "uci -q get ",
    "logread",
    "apk info",
    "opkg list-installed",
    "sha256sum /usr/bin/realtek-poe",
)


def sh_cmd(shell):
    return [shell, "ash", "-s"] if shell.endswith("busybox") else [shell, "-s"]


class FakeSwitch:
    """Temp dir with fake switch commands; every call is logged to calls.log."""

    def __init__(self, before=GOOD, after=GOOD, printk="4 4 1 7\n", poe_extra=""):
        self.dir = tempfile.mkdtemp(prefix="sx8p-fake-")
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.log = os.path.join(self.dir, "calls.log")
        self.kmsg = os.path.join(self.dir, "kmsg")
        self.printk = os.path.join(self.dir, "printk")
        for name, text in (("before", before), ("after", after), ("printk", printk)):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        self.script(
            "ubus",
            'echo "ubus $*" >> "$D/calls.log"\n'
            'n=$(cat "$D/n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$D/n"\n'
            '[ "$n" -le 1 ] && cat "$D/before" || cat "$D/after"',
        )
        self.script(
            "logread",
            'echo "logread $*" >> "$D/calls.log"\n'
            '[ -e "$D/gaveup" ] && echo "poe: giving up until reload"\nexit 0',
        )
        for name in ("dmesg", "sleep"):
            self.script(name, f'echo "{name} $*" >> "$D/calls.log"')
        for name in ("uci", "apk", "opkg", "sha256sum"):
            self.script(name, f'echo "{name} $*" >> "$D/calls.log"\nexit 1')
        self.poe = os.path.join(self.dir, "poe-init")
        self.script_at(self.poe, f'echo "poe $*" >> "$D/calls.log"\n{poe_extra}')

    def script_at(self, path, body):
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nD="{self.dir}"\n{body}\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def script(self, name, body):
        self.script_at(os.path.join(self.bin, name), body)

    def env(self, **extra):
        e = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "KMSG": self.kmsg,
            "POE_INIT": self.poe,
            "PRINTK": self.printk,
        }
        e.update(extra)
        return e

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return f.read().splitlines()

    def dmesg_calls(self):
        return [c for c in self.calls() if c.startswith("dmesg ")]

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def run(shell, script, env):
    with open(script, "rb") as f:
        return subprocess.run(
            sh_cmd(shell),
            stdin=f,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )


def strip_comments(path):
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(re.sub(r"\s#.*$", "", ln) for ln in lines)


@unittest.skipUnless(SHELLS, "needs dash or busybox")
class FaultInjection(unittest.TestCase):
    def go(self, shell=None, confirm=True, **kw):
        fs = FakeSwitch(**kw)
        self.addCleanup(fs.cleanup)
        env = fs.env(CONFIRM="yes") if confirm else fs.env()
        return fs, run(shell or SHELLS[0], FAULT, env)

    def assertRestored(self, fs):
        """Console level restored to the original (4) as the very last dmesg call."""
        d = fs.dmesg_calls()
        self.assertEqual(d[0], "dmesg -n 8", d)
        self.assertEqual(d[-1], "dmesg -n 4", d)

    def test_refuses_without_confirm(self):
        for shell in SHELLS:
            fs, r = self.go(shell, confirm=False)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertEqual(fs.calls(), [], "must not touch anything when refused")
            self.assertFalse(os.path.exists(fs.kmsg))

    def test_pass_restores_exact_level_and_floods(self):
        for shell in SHELLS:
            fs, r = self.go(shell)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("RESULT: PASS", r.stdout)
            self.assertRestored(fs)
            self.assertEqual(fs.calls().count("poe restart"), 1)
            with open(fs.kmsg, encoding="utf-8") as f:
                self.assertEqual(len(f.read().splitlines()), 300)

    def test_other_original_level_is_restored(self):
        fs, r = self.go(printk="6 4 1 7\n")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(fs.dmesg_calls()[-1], "dmesg -n 6")

    def test_fail_when_ports_unknown_still_restores(self):
        for shell in SHELLS:
            fs, r = self.go(shell, after=BAD)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("RESULT: FAIL", r.stdout)
            self.assertRestored(fs)

    def test_fail_when_a_port_status_changes(self):
        _, r = self.go(after=FEWER)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("port-status-changed", r.stdout)

    def test_fail_when_daemon_is_dead_even_if_nothing_was_delivering(self):
        fs, r = self.go(before=IDLE, after="")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("daemon-not-answering", r.stdout)
        self.assertNotIn("RESULT: PASS", r.stdout)
        self.assertRestored(fs)

    def test_fail_when_heal_gave_up(self):
        _, r = self.go(poe_extra='touch "$D/gaveup"')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("heal-gave-up", r.stdout)

    def test_refuses_when_a_port_is_disabled_beforehand(self):
        fs, r = self.go(before=DISABLED)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(fs.dmesg_calls(), [], "must not touch the console level")
        self.assertNotIn("poe restart", fs.calls())

    def test_refuses_when_daemon_silent_beforehand(self):
        fs, r = self.go(before="")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(fs.dmesg_calls(), [])

    def test_refuses_when_console_level_unreadable(self):
        fs, r = self.go(printk="garbage\n")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(fs.dmesg_calls(), [])
        self.assertNotIn("poe restart", fs.calls())

    def test_signal_aborts_and_restores(self):
        for shell in SHELLS:
            fs, r = self.go(shell, poe_extra="kill -TERM $PPID")
            self.assertEqual(r.returncode, 130, r.stdout + r.stderr)
            self.assertNotIn("RESULT:", r.stdout)
            self.assertRestored(fs)
            self.assertNotIn(
                "sleep 60", fs.calls(), "must stop, not carry on after the signal"
            )


@unittest.skipUnless(SHELLS, "needs dash or busybox")
class ReadOnlyHealth(unittest.TestCase):
    def test_runs_via_stdin_and_reports_sections(self):
        for shell in SHELLS:
            fs = FakeSwitch()
            self.addCleanup(fs.cleanup)
            r = run(shell, HEALTH, fs.env())
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            for section in (
                "=== identity ===",
                "=== poe info (raw) ===",
                "=== end of report ===",
            ):
                self.assertIn(section, r.stdout)
            self.assertIn('ports "unknown": 0', r.stdout)
            self.assertIn('"Delivering power": 2', r.stdout)

    def test_only_allowlisted_commands_run(self):
        fs = FakeSwitch()
        self.addCleanup(fs.cleanup)
        run(SHELLS[0], HEALTH, fs.env())
        calls = fs.calls()
        self.assertTrue(calls, "fakes should have been called")
        for c in calls:
            self.assertTrue(c.startswith(HEALTH_ALLOWED), f"unexpected command: {c}")
        self.assertFalse(os.path.exists(fs.kmsg))

    def test_script_text_has_no_write_or_control_verbs(self):
        text = strip_comments(HEALTH)
        for redirect in (
            "2>/dev/null",
            "2> /dev/null",
            "> /dev/null",
            ">/dev/null",
            "2>&1",
            "< /dev/null",
        ):
            text = text.replace(redirect, "")
        self.assertNotIn(">", text, "no output redirection to files")
        verbs = r"\b(set|commit|manage|reload|sendframe|restart|tee|reboot|sysupgrade|mtd|dmesg|kill|rm|mv|cp)\b"
        self.assertIsNone(re.search(verbs, text), re.search(verbs, text))

    def test_report_has_no_hostname(self):
        fs = FakeSwitch()
        self.addCleanup(fs.cleanup)
        r = run(SHELLS[0], HEALTH, fs.env())
        self.assertNotIn(socket.gethostname(), r.stdout)


if __name__ == "__main__":
    unittest.main()
