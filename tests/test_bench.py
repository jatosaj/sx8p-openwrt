#!/usr/bin/env python3
# Description: Tests for bench/bench.py with a fake Tasmota plug (HTTP), a pty as the serial console and a fake ssh
"""Run:  python3 -m unittest tests.test_bench -v   (no hardware, no network beyond 127.0.0.1)"""

import http.server
import json
import os
import pty
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bench"))
import bench

CHECK = os.path.join(ROOT, "bench", "checks", "main-pse-health.sh")
HEALTH_OK = '"firmware":"v17.1"\nports "unknown": 0\nAborting request   0\npse ports answering: 8 of 8\n'


class FakePlug:
    """Tasmota stand-in: Power On/Off flips the watts; on 'Power On' it writes `boot` to the pty master."""

    def __init__(self, boot=b"", master=None):
        self.requests = []
        self.watts = 10.0
        plug = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                cmnd = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)[
                    "cmnd"
                ][0]
                plug.requests.append(cmnd)
                if cmnd == "Power Off":
                    plug.watts = 0.0
                elif cmnd == "Power On":
                    plug.watts = 10.0
                    if master is not None and boot:
                        os.write(master, boot)
                body = json.dumps(
                    {"StatusSNS": {"ENERGY": {"Power": plug.watts}}, "POWER": "x"}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Power(unittest.TestCase):
    def test_cycle_goes_off_then_on_and_reads_watts(self):
        plug = FakePlug()
        self.addCleanup(plug.close)
        w = bench.power("cycle", base=plug.url, off_secs=0.0, settle=2.0)
        self.assertEqual(w, 10.0)
        self.assertEqual(
            [r for r in plug.requests if r.startswith("Power")],
            ["Power Off", "Power On"],
        )
        self.assertLess(
            plug.requests.index("Power Off"), plug.requests.index("Power On")
        )

    def test_off_and_cycle_refuse_without_yes(self):
        for argv in (["power", "off"], ["power", "cycle"], ["cold", "--label", "x"]):
            self.assertEqual(bench.main(argv), 2, argv)

    def test_status_needs_no_yes_and_does_not_switch(self):
        plug = FakePlug()
        self.addCleanup(plug.close)
        os.environ["TASMOTA"] = plug.url
        self.addCleanup(os.environ.pop, "TASMOTA")
        self.assertEqual(bench.main(["power", "status"]), 0)
        self.assertFalse([r for r in plug.requests if r.startswith("Power")])


class Lock(unittest.TestCase):
    def test_second_run_is_refused_while_the_rig_is_held(self):
        d = tempfile.mkdtemp(prefix="bench-lock-")
        self.addCleanup(shutil.rmtree, d, True)
        os.environ["BENCH_LOCK"] = os.path.join(d, "lock")
        self.addCleanup(os.environ.pop, "BENCH_LOCK")
        held = bench.rig_lock()
        self.addCleanup(held.close)
        plug = FakePlug()
        self.addCleanup(plug.close)
        os.environ["TASMOTA"] = plug.url
        self.addCleanup(os.environ.pop, "TASMOTA")
        self.assertEqual(bench.main(["power", "cycle", "--yes", "--off-secs", "0"]), 3)
        self.assertFalse([r for r in plug.requests if r.startswith("Power")])


class Parse(unittest.TestCase):
    def test_field(self):
        self.assertEqual(bench.field(HEALTH_OK.encode(), rb'ports "unknown": (\d+)'), 0)
        self.assertIsNone(bench.field(b"nothing", rb'ports "unknown": (\d+)'))

    def rec(self, **kw):
        base = {
            "console_secs": 30.0,
            "ssh": True,
            "check_rc": 0,
            "bad_serial": 0,
            "unknown_ports": 0,
            "pse_answering": None,
            "log_errors": 0,
            "daemon_alive": True,
            "expect_ok": None,
        }
        base.update(kw)
        return bench.summarize(base)["ok"]

    def test_summarize_fails_on_each_symptom(self):
        self.assertTrue(self.rec())
        self.assertFalse(self.rec(console_secs=None))
        self.assertFalse(self.rec(ssh=False))
        self.assertFalse(self.rec(check_rc=1))
        self.assertFalse(self.rec(bad_serial=1))
        self.assertFalse(self.rec(unknown_ports=2))
        self.assertFalse(self.rec(pse_answering=5))
        self.assertFalse(self.rec(log_errors=1))
        self.assertFalse(self.rec(daemon_alive=False))
        self.assertFalse(self.rec(expect_ok=False))
        self.assertTrue(self.rec(expect_ok=True))
        self.assertTrue(self.rec(pse_answering=8))

    def test_log_counters_and_dead_daemon(self):
        rep = b'poe info\nports "unknown": 0\nAborting request    2\nMCU rejected   0\n'
        self.assertEqual(bench.log_errors(rep), 2)
        self.assertEqual(
            bench.log_errors(b"giving up   0\nNo response from PoE   0\n"), 0
        )

    def test_bad_pattern_matches_poe_trouble_only(self):
        self.assertTrue(bench.BAD.search(b"poe: Aborting request 3"))
        self.assertTrue(bench.BAD.search(b"1: uart:16550A tx:5 rx:9 oe:8"))
        self.assertFalse(bench.BAD.search(b"1: uart:16550A tx:5 rx:9 oe:0"))
        self.assertFalse(bench.BAD.search(b"realtek-poe: Power over Ethernet"))
        self.assertFalse(bench.BAD.search(b"eth0: link up"))


class Cold(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bench-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.master, slave = pty.openpty()
        self.tty = os.ttyname(slave)
        self.slave = slave
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, slave)
        fakebin = os.path.join(self.dir, "bin")
        os.mkdir(fakebin)
        self.fakebin = fakebin

    def fake_ssh(self, body):
        p = os.path.join(self.fakebin, "ssh")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"#!/bin/sh\n{body}\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)

    def run_cold(self, boot, ssh_body, runs=2, extra=()):
        plug = FakePlug(boot=boot, master=self.master)
        self.addCleanup(plug.close)
        self.fake_ssh(ssh_body)
        env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ['PATH']}",
            "TASMOTA": plug.url,
            "SX8P_TTY": self.tty,
            "SWITCH": "192.0.2.1",
            "BENCH_OUT": self.dir,
        }
        r = subprocess.run(
            [
                sys.executable,
                "-I",
                os.path.join(ROOT, "bench", "bench.py"),
                "cold",
                "--label",
                "t",
                "--runs",
                str(runs),
                "--yes",
                "--off-secs",
                "0",
                "--boot-timeout",
                "5",
                "--ssh-timeout",
                "5",
                "--tail-secs",
                "0.2",
                *extra,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        return r, plug

    def test_clean_boots_pass_and_write_logs(self):
        ssh_body = f'case "$*" in *true*) exit 0;; esac\nprintf %b {HEALTH_OK!r}'
        r, plug = self.run_cold(
            b"boot...\r\nPlease press Enter to activate this console.\r\n", ssh_body
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2/2 runs ok", r.stdout)
        self.assertEqual(plug.requests.count("Power Off"), 2)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "t", "run02-serial.log")))
        with open(os.path.join(self.dir, "t", "summary.json"), encoding="utf-8") as f:
            summary = json.load(f)
        self.assertTrue(all(x["ok"] for x in summary))

    def test_via_serial_needs_no_ssh(self):
        boot = (
            b"Please press Enter to activate this console.\r\n"
            b"pse ports answering: 8 of 8\r\n=== end of report ===\r\n"
        )
        r, _ = self.run_cold(boot, "exit 255", runs=1, extra=["--via", "serial"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('"pse_answering": 8', r.stdout)
        self.assertIn('"ssh": true', r.stdout)

    def test_via_serial_fails_when_pse_ports_do_not_answer(self):
        boot = (
            b"Please press Enter to activate this console.\r\n"
            b"pse ports answering: 3 of 8\r\n=== end of report ===\r\n"
        )
        r, _ = self.run_cold(boot, "exit 255", runs=1, extra=["--via", "serial"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_warm_reboots_over_ssh_and_never_touches_the_plug(self):
        log = os.path.join(self.dir, "ssh.log")
        self.fake_ssh(
            f'echo "$*" >> {log}\ncase "$*" in *true*|*reboot*) exit 0;; esac\nprintf %b {HEALTH_OK!r}'
        )
        env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ['PATH']}",
            "TASMOTA": "http://127.0.0.1:9",  # nothing listens: any plug call would fail the run
            "SX8P_TTY": self.tty,
            "SWITCH": "192.0.2.1",
            "BENCH_OUT": self.dir,
            "BENCH_LOCK": os.path.join(self.dir, "lock"),
        }
        t = threading.Timer(
            1.5,
            os.write,
            (self.master, b"Please press Enter to activate this console.\r\n"),
        )
        t.start()
        self.addCleanup(t.cancel)
        r = subprocess.run(
            [
                sys.executable,
                "-I",
                os.path.join(ROOT, "bench", "bench.py"),
                "cold",
                "--label",
                "w",
                "--runs",
                "1",
                "--warm",
                "--yes",
                "--boot-timeout",
                "10",
                "--ssh-timeout",
                "5",
                "--tail-secs",
                "0.2",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(log, encoding="utf-8") as f:
            self.assertIn("reboot", f.read())

    def test_poe_reject_on_console_fails_the_run(self):
        ssh_body = f'case "$*" in *true*) exit 0;; esac\nprintf %b {HEALTH_OK!r}'
        boot = b"poe: request-bad-checksum\r\nPlease press Enter to activate this console.\r\n"
        r, _ = self.run_cold(boot, ssh_body, runs=1)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_silent_console_fails_the_run(self):
        ssh_body = f'case "$*" in *true*) exit 0;; esac\nprintf %b {HEALTH_OK!r}'
        r, _ = self.run_cold(b"", ssh_body, runs=1)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('"console_secs": null', r.stdout)

    def test_dead_daemon_with_zero_unknown_fails_the_run(self):
        # 01-readonly-health prints unknown 0 when ubus returned nothing: no firmware = not alive
        body = 'case "$*" in *true*) exit 0;; esac\nprintf \'ports "unknown": 0\\n\''
        r, _ = self.run_cold(
            b"Please press Enter to activate this console.\r\n", body, runs=1
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('"daemon_alive": false', r.stdout)

    def test_expected_port_must_be_delivering(self):
        up = b"Please press Enter to activate this console.\r\n"
        good = '"lan7": {"status": "Delivering power"}'
        bad = '"lan7": {"status": "Searching"}'
        for report, code in ((good, 0), (bad, 1)):
            body = f"case \"$*\" in *true*) exit 0;; esac\nprintf %b {HEALTH_OK!r}\nprintf '%s\\n' '{report}'"
            r, _ = self.run_cold(
                up,
                body,
                runs=1,
                extra=[
                    "--expect-regex",
                    r'"lan7":\s*\{[^{}]*"status":\s*"Delivering power"',
                ],
            )
            self.assertEqual(r.returncode, code, r.stdout + r.stderr)

    def test_ssh_down_fails_the_run(self):
        r, _ = self.run_cold(
            b"Please press Enter to activate this console.\r\n", "exit 255", runs=1
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('"ssh": false', r.stdout)


class SerialCheck(unittest.TestCase):
    def test_sends_lines_and_stops_at_end_marker(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        d = tempfile.mkdtemp(prefix="bench-sc-")
        self.addCleanup(shutil.rmtree, d, True)
        script = os.path.join(d, "c.sh")
        with open(script, "w", encoding="utf-8") as f:
            f.write("# comment\necho a\n\necho b\n")
        # the report arrives after the script started: stale input is drained on purpose
        t = threading.Timer(
            1.0,
            os.write,
            (master, b"pse ports answering: 8 of 8\r\n=== end of report ===\r\n"),
        )
        t.start()
        self.addCleanup(t.cancel)
        env = {**os.environ, "SX8P_TTY": os.ttyname(slave)}
        r = subprocess.run(
            [
                sys.executable,
                "-I",
                os.path.join(ROOT, "bench", "bench.py"),
                "serial-check",
                script,
                "--timeout",
                "5",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("answering: 8 of 8", r.stdout)
        sent = os.read(master, 4096)
        self.assertIn(b"echo a\r", sent)
        self.assertIn(b"echo b\r", sent)
        self.assertNotIn(b"comment", sent)

    def test_times_out_without_marker(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        env = {**os.environ, "SX8P_TTY": os.ttyname(slave)}
        r = subprocess.run(
            [
                sys.executable,
                "-I",
                os.path.join(ROOT, "bench", "bench.py"),
                "serial-check",
                CHECK,
                "--timeout",
                "1",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(r.returncode, 1)


class RunPlan(unittest.TestCase):
    PLAN = os.path.join(ROOT, "bench", "run-plan.sh")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="plan-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, body in (
            ("bstub", "exit 3"),
            ("sw", "echo e11ca9a3  /usr/bin/realtek-poe"),
        ):
            p = os.path.join(self.dir, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"#!/bin/sh\n{body}\n")
            os.chmod(p, 0o755)
        os.symlink(os.path.join(ROOT, "bench"), os.path.join(self.dir, "bench"))

    def plan(self, *phases, lock=None):
        env = {
            **os.environ,
            "TASMOTA": "http://127.0.0.1:9",
            "SWITCH": "192.0.2.1",
            "KIT": self.dir,
            "BENCH_OUT": os.path.join(self.dir, "out"),
            "BENCH_LOCK": lock or os.path.join(self.dir, "lock"),
            "B_CMD": os.path.join(self.dir, "bstub"),
            "SW_CMD": os.path.join(self.dir, "sw"),
            "RUNS": "3",
        }
        return subprocess.run(
            ["bash", self.PLAN, *phases],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_failing_bench_stops_the_plan_even_with_a_stale_passing_summary(self):
        stale = os.path.join(self.dir, "out", "r5")
        os.makedirs(stale)
        with open(os.path.join(stale, "summary.json"), "w", encoding="utf-8") as f:
            json.dump([{"ok": True}] * 3, f)
        r = self.plan("r5")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("GATE FAIL r5", r.stdout)
        self.assertNotIn("gate ok", r.stdout)

    def test_main_flash_refuses_without_a_passing_ram_boot(self):
        r = self.plan("main-flash")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no passing RAM boot", r.stdout)

    def test_second_plan_is_refused_while_the_first_holds_the_rig(self):
        import fcntl

        lock = os.path.join(self.dir, "held")
        with open(lock, "w", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            r = self.plan("r5", lock=lock)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("holds the rig", r.stderr)

    def test_flood_phase_gates_on_the_result_line(self):
        sw = os.path.join(self.dir, "sw")
        for result, code in (("RESULT: PASS", 0), ("RESULT: FAIL (x)", 1)):
            with open(sw, "w", encoding="utf-8") as f:
                f.write(
                    f"#!/bin/sh\ncat >/dev/null\necho 'control: set failed 0 of 60'\necho '{result}'\n"
                )
            os.chmod(sw, 0o755)
            r = self.plan("main-flood")
            self.assertEqual(r.returncode, code, r.stdout + r.stderr)

    def test_noquiet_refuses_without_its_image(self):
        r = self.plan("noquiet")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("noquiet", r.stdout)
        self.assertIn("missing", r.stdout)

    def test_bench_skips_its_own_lock_when_the_plan_holds_it(self):
        os.environ["BENCH_LOCK_HELD"] = "1"
        self.addCleanup(os.environ.pop, "BENCH_LOCK_HELD")
        a, b = bench.rig_lock(), bench.rig_lock()
        a.close()
        b.close()


@unittest.skipUnless(shutil.which("dash"), "needs dash")
class FloodScript(unittest.TestCase):
    SCRIPT = os.path.join(ROOT, "bench", "checks", "03-main-flood.sh")

    def run_flood(
        self, fail_while_flooding=False, always_fail=False, kmsg_unwritable=False
    ):
        d = tempfile.mkdtemp(prefix="bench-flood-")
        self.addCleanup(shutil.rmtree, d, True)
        kmsg = os.path.join(d, "kmsg")
        if kmsg_unwritable:
            kmsg = os.path.join(d, "no-such-dir", "kmsg")
        # the fake MCU fails every call once the flood has started writing to the kmsg file
        ethtool = f'echo "$@" >> {d}/ethtool.log; if [ {always_fail!r} = True ] || ( [ {fail_while_flooding!r} = True ] && [ -s {kmsg} ] ); then echo "netlink error: Bad message" >&2; exit 1; fi; echo "Clause 33 PSE Power Detection Status: delivering power"; exit 0'
        fakes = {
            "ethtool": ethtool,
            "dmesg": f'echo "x$*" >> {d}/dmesg.log; [ "$1" = -n ] && printf "%s\\t4\\t1\\t7\\n" "$2" > {d}/printk; exit 0',
            "sleep": "exit 0",
        }
        for name, body in fakes.items():
            p = os.path.join(d, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"#!/bin/sh\n{body}\n")
            os.chmod(p, 0o755)
        with open(os.path.join(d, "printk"), "w", encoding="utf-8") as f:
            f.write("4\t4\t1\t7\n")
        with open(self.SCRIPT, "rb") as f:
            r = subprocess.run(
                ["dash", "-s"],
                stdin=f,
                capture_output=True,
                text=True,
                env={
                    "PATH": f"{d}:/usr/bin:/bin",
                    "KMSG": kmsg,
                    "PRINTK": os.path.join(d, "printk"),
                    "N": "5",
                    "STOP": os.path.join(d, "stop"),
                    "COUNT": os.path.join(d, "count"),
                },
                check=False,
            )
        with open(os.path.join(d, "dmesg.log"), encoding="utf-8") as f:
            r.dmesg = [ln for ln in f.read().splitlines() if ln.startswith("x-n")]
        try:
            with open(kmsg, encoding="utf-8") as f:
                r.flooded = len(f.read().splitlines())
        except OSError:
            r.flooded = 0
        return r

    def test_pass_floods_and_restores_the_level(self):
        r = self.run_flood()
        self.assertIn("RESULT: PASS", r.stdout, r.stdout + r.stderr)
        self.assertGreater(r.flooded, 0)
        self.assertRegex(r.stdout, r"\d+ messages written")
        self.assertEqual(r.dmesg[0], "x-n 8")
        self.assertEqual(r.dmesg[-1], "x-n 4")

    def test_inconclusive_when_the_control_round_fails(self):
        r = self.run_flood(always_fail=True)
        self.assertIn("RESULT: INCONCLUSIVE", r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.flooded, 0)
        self.assertNotIn("x-n 8", r.dmesg)  # never raised the console level
        self.assertEqual(r.dmesg[-1], "x-n 4")

    def test_inconclusive_when_the_flood_does_not_run(self):
        r = self.run_flood(kmsg_unwritable=True)
        self.assertIn("RESULT: INCONCLUSIVE", r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.dmesg[-1], "x-n 4")

    def test_fail_when_the_driver_loses_round_trips_under_flood(self):
        r = self.run_flood(fail_while_flooding=True)
        self.assertIn("RESULT: FAIL", r.stdout, r.stdout + r.stderr)
        self.assertIn("control: set failed 0 of 10", r.stdout)
        self.assertEqual(r.dmesg[-1], "x-n 4")


@unittest.skipUnless(shutil.which("dash"), "needs dash")
class CheckScript(unittest.TestCase):
    def run_check(self, ethtool_body):
        d = tempfile.mkdtemp(prefix="bench-chk-")
        self.addCleanup(shutil.rmtree, d, True)
        for name, body in (
            ("ethtool", ethtool_body),
            ("dmesg", "echo 'pse: ok'"),
            ("lsmod", "echo pse_realtek_mcu"),
            ("uname", "echo Linux"),
        ):
            p = os.path.join(d, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"#!/bin/sh\n{body}\n")
            os.chmod(p, 0o755)
        with open(CHECK, "rb") as f:
            return subprocess.run(
                ["dash", "-s"],
                stdin=f,
                capture_output=True,
                text=True,
                env={"PATH": f"{d}:/usr/bin:/bin"},
                check=False,
            )

    def test_counts_answering_ports(self):
        r = self.run_check("echo PSE ok")
        self.assertEqual(r.returncode, 0)
        self.assertIn("pse ports answering: 8 of 8", r.stdout)

    def test_counts_failing_ports(self):
        r = self.run_check('[ "$2" = lan3 ] && { echo no; exit 1; }; echo ok')
        self.assertIn("pse ports answering: 7 of 8", r.stdout)

    def test_only_write_is_enabling_the_camera_port(self):
        with open(CHECK, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("--set-pse"), 1)
        self.assertIn('--set-pse "$CAMERA_PORT" c33-pse-admin-control enable', text)

    def test_check_is_read_only(self):
        with open(CHECK, encoding="utf-8") as f:
            text = "\n".join(
                ln for ln in f.read().splitlines() if not ln.lstrip().startswith("#")
            )
        for bad in (
            "uci ",
            "reboot",
            "sysupgrade",
            "mtd",
            " rm ",
            "dmesg -n",
        ):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
