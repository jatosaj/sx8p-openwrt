#!/usr/bin/env python3
# Description: Black-box tests for handoff/releases/install.sh (realtek-poe binary swap) with a fake switch on PATH
"""Run:  python3 -m unittest tests.test_releases -v
install.sh is fed through stdin exactly as `ssh root@switch 'sh -s'` does, under dash and BusyBox ash when
available. The binary, its backup and the variant marker live in a temp dir (test hooks BIN/NEW/BACKUP/MARK)."""

import hashlib
import os
import re
import shutil
import unittest

from tests.test_handoff_scripts import BAD, DISABLED, GOOD, SHELLS, FakeSwitch, run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL = os.path.join(ROOT, "handoff", "releases")
INSTALL = os.path.join(REL, "install.sh")
VARIANTS = ("with-r4", "plain-r5")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Box:
    """A FakeSwitch plus the files install.sh touches."""

    def __init__(self, before=GOOD, after=GOOD, new="with-r4"):
        self.fs = FakeSwitch(before=before, after=after)
        os.remove(os.path.join(self.fs.bin, "sha256sum"))  # the real one: hashes must be right
        d = self.fs.dir
        self.bin = os.path.join(d, "realtek-poe")
        self.new = os.path.join(d, "realtek-poe.new")
        self.backup = os.path.join(d, "realtek-poe.orig")
        self.mark = os.path.join(d, "realtek-poe.variant")
        with open(self.bin, "wb") as f:
            f.write(b"ORIGINAL r3 binary")
        if new:
            shutil.copy(os.path.join(REL, new, "realtek-poe"), self.new)

    def env(self, **extra):
        return self.fs.env(BIN=self.bin, NEW=self.new, BACKUP=self.backup, MARK=self.mark, **extra)

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def poe_calls(self):
        return [c for c in self.fs.calls() if c.startswith("poe ")]

    def cleanup(self):
        self.fs.cleanup()


@unittest.skipUnless(SHELLS, "needs dash or busybox")
class InstallScript(unittest.TestCase):
    def box(self, **kw):
        b = Box(**kw)
        self.addCleanup(b.cleanup)
        return b

    def test_checksums_in_script_match_the_shipped_binaries(self):
        with open(INSTALL, encoding="utf-8") as f:
            text = f.read()
        for v in VARIANTS:
            m = re.search(rf"^\s*{v}\) WANT=([0-9a-f]{{64}})", text, re.MULTILINE)
            self.assertIsNotNone(m, v)
            assert m is not None
            self.assertEqual(m.group(1), sha(os.path.join(REL, v, "realtek-poe")), v)

    def test_refuses_without_confirm_and_touches_nothing(self):
        for shell in SHELLS:
            b = self.box()
            r = run(shell, INSTALL, b.env(VARIANT="with-r4"))
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertEqual(b.fs.calls(), [])
            self.assertEqual(b.read(b.bin), b"ORIGINAL r3 binary")

    def test_refuses_an_unknown_variant(self):
        b = self.box()
        r = run(SHELLS[0], INSTALL, b.env(VARIANT="r9", CONFIRM="yes"))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(b.read(b.bin), b"ORIGINAL r3 binary")

    def test_refuses_a_file_whose_hash_is_not_the_variant(self):
        b = self.box(new="plain-r5")  # the r5 file, installed as r4
        r = run(SHELLS[0], INSTALL, b.env(VARIANT="with-r4", CONFIRM="yes"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("checksum", r.stdout)
        self.assertEqual(b.read(b.bin), b"ORIGINAL r3 binary")
        self.assertEqual(b.poe_calls(), [])

    def test_refuses_when_a_port_is_disabled_beforehand(self):
        # a restart reloads the config: a port switched off with ubus would come back on
        b = self.box(before=DISABLED)
        r = run(SHELLS[0], INSTALL, b.env(VARIANT="with-r4", CONFIRM="yes"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(b.read(b.bin), b"ORIGINAL r3 binary")

    def test_installs_keeps_the_original_and_restarts(self):
        for shell in SHELLS:
            b = self.box()
            r = run(shell, INSTALL, b.env(VARIANT="with-r4", CONFIRM="yes"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(sha(b.bin), sha(os.path.join(REL, "with-r4", "realtek-poe")))
            self.assertEqual(b.read(b.backup), b"ORIGINAL r3 binary")
            self.assertEqual(b.read(b.mark).decode().strip(), "with-r4")
            self.assertEqual(b.poe_calls(), ["poe restart"])
            self.assertTrue(os.access(b.bin, os.X_OK))
            self.assertIn("RESULT: installed with-r4", r.stdout)

    def test_second_install_keeps_the_first_original(self):
        b = self.box()
        run(SHELLS[0], INSTALL, b.env(VARIANT="with-r4", CONFIRM="yes"))
        shutil.copy(os.path.join(REL, "plain-r5", "realtek-poe"), b.new)
        os.remove(os.path.join(b.fs.dir, "n"))  # fresh ubus answers for the second run
        r = run(SHELLS[0], INSTALL, b.env(VARIANT="plain-r5", CONFIRM="yes"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(b.read(b.backup), b"ORIGINAL r3 binary")
        self.assertEqual(b.read(b.mark).decode().strip(), "plain-r5")

    def test_rollback_puts_the_original_back(self):
        b = self.box()
        run(SHELLS[0], INSTALL, b.env(VARIANT="with-r4", CONFIRM="yes"))
        os.remove(os.path.join(b.fs.dir, "n"))
        r = run(SHELLS[0], INSTALL, b.env(ROLLBACK="yes", CONFIRM="yes"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(b.read(b.bin), b"ORIGINAL r3 binary")
        self.assertFalse(os.path.exists(b.mark))
        self.assertIn("RESULT: rolled back", r.stdout)

    def test_rollback_works_on_a_broken_unit(self):
        # review 2026-11-09: plain-r5 leaving ports unknown is exactly when the way back must work
        b = self.box(new="plain-r5")
        self.assertEqual(run(SHELLS[0], INSTALL, b.env(VARIANT="plain-r5", CONFIRM="yes")).returncode, 0)
        with open(os.path.join(b.fs.dir, "before"), "w", encoding="utf-8") as f:
            f.write(BAD)
        os.remove(os.path.join(b.fs.dir, "n"))
        r = run(SHELLS[0], INSTALL, b.env(ROLLBACK="yes", CONFIRM="yes"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WARNING", r.stdout)
        self.assertEqual(b.read(b.bin), b"ORIGINAL r3 binary")

    def test_force_installs_over_a_broken_unit_but_not_without_it(self):
        b = self.box(before=BAD)
        r = run(SHELLS[0], INSTALL, b.env(VARIANT="with-r4", CONFIRM="yes"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        os.remove(os.path.join(b.fs.dir, "n"))
        r = run(SHELLS[0], INSTALL, b.env(VARIANT="with-r4", CONFIRM="yes", FORCE="yes"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sha(b.bin), sha(os.path.join(REL, "with-r4", "realtek-poe")))

    def test_rollback_keeps_the_original_even_if_the_daemon_stays_down(self):
        b = self.box(new="plain-r5")
        self.assertEqual(run(SHELLS[0], INSTALL, b.env(VARIANT="plain-r5", CONFIRM="yes")).returncode, 0)
        for name, text in (("before", BAD), ("after", BAD)):
            with open(os.path.join(b.fs.dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        os.remove(os.path.join(b.fs.dir, "n"))
        r = run(SHELLS[0], INSTALL, b.env(ROLLBACK="yes", CONFIRM="yes"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(b.read(b.bin), b"ORIGINAL r3 binary")  # never the broken variant again
        self.assertIn("reboot", r.stdout)

    def test_rollback_without_a_backup_is_refused(self):
        b = self.box()
        r = run(SHELLS[0], INSTALL, b.env(ROLLBACK="yes", CONFIRM="yes"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(b.read(b.bin), b"ORIGINAL r3 binary")

    def test_daemon_dead_after_the_swap_puts_the_previous_binary_back(self):
        b = self.box(after='{"error":"no daemon"}')
        r = run(SHELLS[0], INSTALL, b.env(VARIANT="with-r4", CONFIRM="yes"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(b.read(b.bin), b"ORIGINAL r3 binary")
        self.assertEqual(b.poe_calls(), ["poe restart", "poe restart"])
        self.assertIn("put back", r.stdout)


if __name__ == "__main__":
    unittest.main()
