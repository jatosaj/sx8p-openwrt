#!/bin/sh
# Description: Console-flood test for the kernel PSE driver (OpenWrt main) - writes only PSE priority 1/0 on one port, restores the console level
# Run: ssh root@SWITCH 'sh -s' < this   (over ssh: typing it on the console garbles it while the system is still booting)
# Control pass first (console level unchanged), then the same operations while kernel messages are printed at console level 8.
# Each operation is `ethtool --set-pse PORT prio 1|0` plus `--show-pse`: real MCU round trips on the UART the flood starves.
# Measured 2026-11-12 on the SX-8P: control 0 failures; flood about 80% "Bad message" (bad MCU reply) on set and show, state intact after.
# The flood runs for the whole measured round. Verdicts: PASS, FAIL, or INCONCLUSIVE (control failed / flood did not run).
# Test hooks: KMSG (default /dev/kmsg), PRINTK (default /proc/sys/kernel/printk), CAMERA_PORT (default lan7), N (default 30), STOP, COUNT.
P=${CAMERA_PORT:-lan7}
KMSG=${KMSG:-/dev/kmsg}
PRINTK=${PRINTK:-/proc/sys/kernel/printk}
N=${N:-30}
orig=$(cut -f1 "$PRINTK")
restore() { dmesg -n "$orig"; }
trap restore EXIT INT TERM
ethtool --set-pse "$P" c33-pse-admin-control enable 2>&1
sleep 12
round() { # prints "set_failed show_failed"
  k=0; s=0; h=0
  while [ "$k" -lt "$N" ]; do
    ethtool --set-pse "$P" prio 1 >/dev/null 2>&1 || s=$((s+1))
    ethtool --set-pse "$P" prio 0 >/dev/null 2>&1 || s=$((s+1))
    ethtool --show-pse "$P" >/dev/null 2>&1 || h=$((h+1))
    k=$((k+1))
  done
  echo "$s $h"
}
STOP=${STOP:-/tmp/flood-stop}
COUNT=${COUNT:-/tmp/flood-count}
echo "=== flood test: console level was $orig, $N rounds of 2 writes + 1 read ==="
# shellcheck disable=SC2046  # round prints two numbers, split on purpose
set -- $(round)
c1=$1; c2=$2
echo "control: set failed $c1 of $((N*2)), show failed $c2 of $N"
if [ "$c1" -ne 0 ] || [ "$c2" -ne 0 ]; then
  echo "RESULT: INCONCLUSIVE (the control round failed, not a flood effect)"
  echo "=== end of report ==="
  exit 0
fi
rm -f "$STOP" "$COUNT"
dmesg -n 8
lvl=$(cut -f1 "$PRINTK")
# the flood keeps running until the measured round is over; it counts the writes that really succeeded
( i=0; while [ ! -e "$STOP" ]; do echo "<6>flood $i xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" >> "$KMSG" && i=$((i+1)); done; echo "$i" > "$COUNT" ) &
# shellcheck disable=SC2046
set -- $(round)
touch "$STOP"
wait
rm -f "$STOP"
restore
w=$(cat "$COUNT" 2>/dev/null || echo 0)
rm -f "$COUNT"
echo "flood:   set failed $1 of $((N*2)), show failed $2 of $N (console level $lvl, $w messages written)"
sleep 12
ethtool --show-pse "$P" 2>&1 | grep 'Detection Status'
echo "console level now: $(cut -f1 "$PRINTK")"
if [ "$lvl" != 8 ] || [ "$w" -lt 100 ]; then
  echo "RESULT: INCONCLUSIVE (the flood did not run: level $lvl, $w messages)"
elif [ "$1" -eq 0 ] && [ "$2" -eq 0 ]; then
  echo "RESULT: PASS"
else
  echo "RESULT: FAIL (driver loses MCU round trips while the console is flooded)"
fi
echo "=== end of report ==="
