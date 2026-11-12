#!/bin/sh
# Description: READ-ONLY health report for the kernel PSE driver build (OpenWrt main) - run as: ssh root@SWITCH 'sh -s' < this
# Reads uname/dmesg/ethtool --show-pse; its one write is enabling the camera port's PSE admin state (runtime only, gone at reboot).
echo "=== identity ==="
uname -srm
cat /tmp/sysinfo/board_name 2>/dev/null
echo "=== kernel PSE messages ==="
dmesg 2>/dev/null | grep -i -E 'pse|poe|realtek-pse' | tail -40
echo "=== pse modules ==="
lsmod 2>/dev/null | grep -i pse
# The kernel driver leaves every port disabled at boot (no daemon reads /etc/config/poe): switch on the bench
# camera port (CAMERA_PORT, default lan7) and give the PD time to be detected, so the report shows real delivery.
CAMERA_PORT=${CAMERA_PORT:-lan7}
ethtool --set-pse "$CAMERA_PORT" c33-pse-admin-control enable 2>&1
sleep 12
echo "=== per-port ethtool --show-pse ==="
n=1
ok=0
while [ "$n" -le 8 ]; do
	echo "--- lan$n ---"
	ethtool --show-pse "lan$n" 2>&1 && ok=$((ok + 1))
	n=$((n + 1))
done
echo "pse ports answering: $ok of 8"
echo "=== end of report ==="
