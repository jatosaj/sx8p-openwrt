#!/bin/sh
# Description: Read-only health report for the Pakedge SX-8P (OpenWrt 25.12) - writes nothing, changes nothing
# Usage:  ssh root@SWITCH 'sh -s' < handoff/checks/01-readonly-health.sh | tee handoff/results/health-$(date +%Y%m%d-%H%M).txt
#         SHOW_NET=1 ssh root@SWITCH 'SHOW_NET=1 sh -s' < ...   (adds addresses/VLANs; leave off if the output is committed)
# Exit codes: 0 report printed (read it - this script does not judge), never non-zero for findings.
# Runs under BusyBox ash. No file writes, no service restarts.

hdr() { printf '\n=== %s ===\n' "$1"; }
nmatch() { grep -o -E "$1" | wc -l | tr -d ' '; }
count() { logread 2>/dev/null | grep -c -E "$1"; }

main() {
exec < /dev/null   # the script itself arrives on stdin (ssh sh -s): nothing may read it
hdr "identity"
date -u
grep -E 'DISTRIB_(RELEASE|TARGET|REVISION)' /etc/openwrt_release
printf 'board: %s\n' "$(cat /tmp/sysinfo/board_name 2>/dev/null)"
uname -srvm   # no hostname: the report gets committed
uptime

hdr "kernel command line and console level (expect loglevel=4 and printk starting with 4)"
cat /proc/cmdline
printf 'printk: %s\n' "$(cat /proc/sys/kernel/printk)"
printf 'system.conloglevel: %s\n' "$(uci -q get system.@system[0].conloglevel || echo unset)"

hdr "realtek-poe package and binary"
(apk info -v realtek-poe 2>/dev/null || opkg list-installed realtek-poe 2>/dev/null) | head -2
sha256sum /usr/bin/realtek-poe 2>/dev/null
grep -E "budget|port|enable" /etc/config/poe 2>/dev/null | head -20

hdr "poe info (raw)"
ubus call poe info 2>&1
INFO=$(ubus call poe info 2>/dev/null)
printf '\nports "unknown": %s   "Delivering power": %s   "Searching": %s   "Disabled": %s\n' \
  "$(printf '%s' "$INFO" | nmatch '"unknown"')" \
  "$(printf '%s' "$INFO" | nmatch 'Delivering power')" \
  "$(printf '%s' "$INFO" | nmatch 'Searching')" \
  "$(printf '%s' "$INFO" | nmatch 'Disabled')"

hdr "log counters since boot (expect all 0 after the loglevel=4 fix)"
for pat in "request-bad-checksum|bad checksum" "MCU rejected" "resynchronising" "Aborting request" \
           "re-sending setup" "giving up" "No response from PoE"; do
  printf '%-34s %s\n' "$pat" "$(count "$pat")"
done

hdr "ttyS1 (PoE MCU UART) error counters - oe = RX overruns, should not grow"
grep -E '^1:' /proc/tty/driver/serial 2>/dev/null

hdr "last poe/realtek log lines"
logread 2>/dev/null | grep -i -E "poe|realtek" | tail -n 30

hdr "storage"
df -h /overlay 2>/dev/null

if [ "${SHOW_NET:-}" = 1 ]; then
  hdr "network (SHOW_NET=1)"
  ip -br addr 2>/dev/null || ip addr
  bridge vlan show 2>/dev/null
fi
printf '\n=== end of report ===\n'
}

main
