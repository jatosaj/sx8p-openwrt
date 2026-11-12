#!/usr/bin/env python3
# Description: Regression tests for the v25.12 OpenWrt patches (PoE start-up fixes)
"""Run:  python3 -m unittest discover -s tests -v
Black-box text checks on openwrt/v25.12/*.patch. PATCH_DIR overrides the directory (used to prove
the tests FAIL on the pre-fix commit). No OpenWrt tree is needed: `git apply --stat` only parses."""

import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH_DIR = os.environ.get("PATCH_DIR", os.path.join(ROOT, "openwrt", "v25.12"))
DEVICE = os.path.join(PATCH_DIR, "0001-realtek-add-Pakedge-SX-8P.patch")
POE = os.path.join(PATCH_DIR, "0002-packages-realtek-poe-retry-self-heal.patch")
# The comparison build (2026-11-09): stock realtek-poe 1.3.2, loglevel=4 only
POE_PLAIN = os.path.join(PATCH_DIR, "variants", "0002-packages-realtek-poe-plain.patch")
GIT = shutil.which("git") or "git"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class ConsoleLoglevel(unittest.TestCase):
    """Backlog 15 (2026-11-06): kernel printk at 115200 on ttyS0 blocked ttyS1, so the PoE MCU got
    split frames and answered 'bad checksum' for each piece. loglevel=4 removes the cause."""

    def test_bootargs_keep_console_quiet(self):
        m = re.search(r'bootargs = "([^"]*)"', read(DEVICE))
        self.assertIsNotNone(m, "SX-8P DTS must set bootargs")
        assert m is not None
        args = m.group(1).split()
        levels = [a for a in args if a.startswith("loglevel=")]
        self.assertEqual(len(levels), 1, f"exactly one loglevel= expected, got {args}")
        self.assertLessEqual(int(levels[0].split("=")[1]), 4)


class PoeDaemonPatch(unittest.TestCase):
    """Backlog 11/12: retry back-off, serial resync, self-heal in realtek-poe 1.3.2."""

    text = ""

    @classmethod
    def setUpClass(cls):
        cls.text = read(POE)

    def test_retry_ceiling_and_backoff_bounded(self):
        # 100 ms << num_retries; 7 retries = 12.8 s max. A much larger MAX_RETRIES would stall
        # the command queue for minutes.
        m = re.search(r"^\++#define MAX_RETRIES\s+(\d+)", self.text, re.MULTILINE)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertLessEqual(int(m.group(1)), 10)
        self.assertIn("100 << cmd->num_retries", self.text)

    def test_resync_drops_input_on_bad_checksum(self):
        self.assertIn("tcflush(mcu->stream.fd.fd, TCIFLUSH)", self.text)
        self.assertIn("mcu_resync(mcu, cmd);", self.text)

    def test_replies_ignored_during_resync_window(self):
        self.assertRegex(self.text, r"if \(mcu->resyncing\)\s*\n\++\s*return;")

    def test_manual_off_survives_heal(self):
        # self-heal must not switch a port back on that an operator turned off via ubus
        self.assertIn("poe->admin_off[i] = !blobmsg_get_bool(tb[1]);", self.text)

    def test_heal_never_powers_a_port_that_is_off(self):
        # Backlog 16 (1): the heal re-sent the full setup (every configured port ON), then switched admin-off
        # ports off again - a moment of power on a port the operator turned off. It must send a copy of the
        # config with those ports off instead, and send no "off" afterwards.
        body = self.text[self.text.index("static void poe_heal_resend") :]
        body = body[: body.index("static void poe_self_heal")]
        self.assertIn("struct config cfg = poe->config;", body)
        self.assertRegex(body, r"if \(poe->admin_off\[i\]\)\s*\n\++\s*cfg\.ports\[i\]\.enable = 0;")
        self.assertIn("poe_initial_setup(&poe->mcu, &cfg);", body)
        self.assertNotIn("poe_cmd_port_enable", body)

    def test_heal_also_repairs_a_port_that_should_be_off(self):
        # Backlog 16 (2): a lost "port off" left the port powered and the heal only looked at ports that should be on.
        self.assertIn("const bool want = cfg->ports[i].enable && !poe->admin_off[i];", self.text)
        self.assertIn('(status && strcmp(status, "Disabled"))', self.text)

    def test_abort_drops_only_the_failing_command(self):
        # Backlog 16 (2): dropping the whole queue on one abort lost queued "port off" commands. Upstream frees
        # only the command that failed; the queue carries on.
        self.assertNotIn("dropping queue", self.text)
        abort = self.text[self.text.index("after %d attempts") :]
        abort = abort[: abort.index("100 << cmd->num_retries")]
        self.assertNotIn("list_empty(&mcu->pending_cmds)", abort)

    def test_flush_drains_every_buffered_chunk(self):
        # ustream hands the read buffer back one chunk at a time: one consume can leave a stale frame behind
        self.assertRegex(self.text, r"while \(ustream_get_read_buf\(s, &len\) && len > 0\)")

    def test_heal_gives_up(self):
        self.assertRegex(self.text, r"#define HEAL_MAX\s+\d+")
        self.assertIn("giving up until reload", self.text)

    def test_uci_defaults_hardened(self):
        self.assertIn("+umask 077", self.text)  # config must not be world-readable
        self.assertIn("+uci commit poe", self.text)  # not the whole uci tree
        self.assertIn("-umask 055", self.text)

    def test_package_release_bumped(self):
        m = re.search(r"^\+PKG_RELEASE:=(\d+)", self.text, re.MULTILINE)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(int(m.group(1)), 4)  # r4 = backlog 16 fixed; r3 is on the unit today


class PlainVariant(unittest.TestCase):
    """The comparison build: no retry/resync/self-heal patch at all; the 30-poe hardening stays."""

    text = ""

    @classmethod
    def setUpClass(cls):
        cls.text = read(POE_PLAIN)

    def test_no_daemon_patch(self):
        self.assertNotIn("100-retry-backoff-and-self-heal.patch", self.text)
        self.assertNotIn("src/main.c", self.text)

    def test_own_release_number(self):
        m = re.search(r"^\+PKG_RELEASE:=(\d+)", self.text, re.MULTILINE)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(int(m.group(1)), 5)  # never confused with r3 (unit today) or r4 (with)

    def test_uci_defaults_still_hardened(self):
        self.assertIn("+umask 077", self.text)
        self.assertIn("+uci commit poe", self.text)


class PatchesWellFormed(unittest.TestCase):
    def test_git_apply_stat_parses(self):
        for patch in (DEVICE, POE, POE_PLAIN):
            r = subprocess.run(
                [GIT, "apply", "--stat", patch],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r.returncode, 0, f"{patch}: {r.stderr}")
            self.assertIn("changed", r.stdout)


if __name__ == "__main__":
    unittest.main()
