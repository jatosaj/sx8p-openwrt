#!/bin/bash
# Description: Unattended test plan on the bench PC - R5 and R4 cold boots with a PoE camera, then OpenWrt main (RAM gate, flash, cold boots) and rollback
# usage: TASMOTA=http://PLUG SWITCH=ADDR run-plan.sh PHASE...   PHASE = r5 | r4 | main-ram | main-flash | main-flood | noquiet | rollback
# Stops at the first failed gate (exit 1) and leaves the unit as it is. main-flash/noquiet/rollback write flash: run them deliberately.
set -u -o pipefail
: "${TASMOTA:?}" "${SWITCH:?}"
# Writes to flash happen only in: r4 (install.sh swaps one binary on the overlay), main-flash, noquiet and rollback (sysupgrade).
# One controller at a time for the WHOLE plan, including the gaps between bench.py calls and the sysupgrade waits.
LOCKFILE=${BENCH_LOCK:-$HOME/.sx8p-rig.lock}
exec 9>"$LOCKFILE"
flock -n 9 || { echo "another bench run holds the rig ($LOCKFILE)" >&2; exit 3; }
export BENCH_LOCK_HELD=1
export SX8P_TTY=${SX8P_TTY:-/dev/ttyUSB0}
KIT=${KIT:-$HOME/sx8p-bench}
OUT=${BENCH_OUT:-$KIT/results/$(date +%Y%m%d)}
MAIN=${MAIN_DIR:-OPENWRT_MAIN/bin/targets/realtek/rtl838x}
export TASMOTA SWITCH BENCH_OUT=$OUT
mkdir -p "$OUT"
B=${B_CMD:-"python3 -I $KIT/bench/bench.py"}
SW=${SW_CMD:-"ssh -o BatchMode=yes -o ConnectTimeout=8 root@$SWITCH"}   # B_CMD/SW_CMD: test hooks
EXPECT='"lan7":\s*\{[^{}]*"status":\s*"Delivering power"'
EXPECT_MAIN='PSE attributes for lan7:.{0,120}?Power Detection Status: delivering power'
log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$OUT/plan.log"; }
gate() { # label: summary.json has exactly RUNS entries, all ok?
  python3 -I - "$OUT/$1/summary.json" "${RUNS:-10}" <<'PY' || { log "GATE FAIL $1"; exit 1; }
import json,sys
r=json.load(open(sys.argv[1])); sys.exit(0 if len(r)==int(sys.argv[2]) and all(x["ok"] for x in r) else 1)
PY
  log "gate ok $1"
}
wait_console() { # wait for the shell banner after a reboot/flash (serial), up to $1 s
  python3 -I - "$1" <<'PY'
import os,re,select,sys,termios,time
fd=os.open(os.environ.get("SX8P_TTY","/dev/ttyUSB0"),os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
a=termios.tcgetattr(fd);a[0]=a[1]=a[3]=0;a[2]=termios.CS8|termios.CREAD|termios.CLOCAL;a[4]=a[5]=termios.B115200;a[6][termios.VMIN]=0;termios.tcsetattr(fd,termios.TCSANOW,a)
end=time.time()+float(sys.argv[1]);buf=b""
while time.time()<end:
    if select.select([fd],[],[],0.2)[0]:buf+=os.read(fd,4096)
    if b"activate this console" in buf:sys.exit(0)
sys.exit(1)
PY
}
wait_ssh() { for _ in $(seq 1 60); do $SW true 2>/dev/null && return 0; sleep 5; done; return 1; }

phase_cold() { # label expect-regex-args...
  local label=$1; shift
  log "cold runs $label"
  rm -f "$OUT/$label/summary.json"   # a stale summary must never satisfy the gate
  # shellcheck disable=SC2086
  $B cold --label "$label" --runs "${RUNS:-10}" --settle 40 --check "$KIT/checks/01-readonly-health.sh" "$@" --yes | tee -a "$OUT/plan.log" \
    || { log "GATE FAIL $label: bench exited non-zero"; exit 1; }
  gate "$label"
  log "fault injection $label"
  $SW 'CONFIRM=yes sh -s' < "$KIT/checks/02-fault-injection.sh" > "$OUT/$label-fault.txt" 2>&1
  tail -3 "$OUT/$label-fault.txt" | tee -a "$OUT/plan.log"
  grep -q 'RESULT: PASS' "$OUT/$label-fault.txt" || { log "GATE FAIL $label fault injection"; exit 1; }
}

for ph in "$@"; do
  case $ph in
  r5)
    $SW 'sha256sum /usr/bin/realtek-poe' | tee -a "$OUT/plan.log"
    phase_cold r5 --expect-regex "$EXPECT" ;;
  r4)
    scp -O -i "$HOME/.ssh/id_sx8p_bench" "$KIT/releases/with-r4/realtek-poe" "root@$SWITCH:/tmp/realtek-poe.new" || exit 1
    $SW 'VARIANT=with-r4 CONFIRM=yes sh -s' < "$KIT/releases/install.sh" 2>&1 | tee -a "$OUT/plan.log" \
      || { log "GATE FAIL r4 install refused or failed"; exit 1; }
    want=$(sha256sum "$KIT/releases/with-r4/realtek-poe" | cut -d' ' -f1)
    $SW 'sha256sum /usr/bin/realtek-poe' | tee -a "$OUT/plan.log" | grep -q "^$want" \
      || { log "GATE FAIL r4: switch binary is not the with-r4 build"; exit 1; }
    phase_cold r4 --expect-regex "$EXPECT" ;;
  main-ram)
    log "main: RAM boot (no flash write)"
    $B ramboot "$MAIN/openwrt-realtek-rtl838x-pakedge_sx-8p-initramfs-kernel.bin" --yes 2>&1 | tail -n 60 > "$OUT/main-ram-boot.txt"
    sleep 20
    $B serial-check "$KIT/bench/checks/main-pse-health.sh" --out "$OUT/main-ram.txt" > /dev/null
    grep -E 'pse ports answering' "$OUT/main-ram.txt" | tee -a "$OUT/plan.log"
    grep -q 'pse ports answering: 8 of 8' "$OUT/main-ram.txt" || { log "GATE FAIL main-ram: PSE ports not all answering (see main-ram.txt)"; exit 1; }
    tr -d '\r' < "$OUT/main-ram.txt" | tr '\n' ' ' | grep -q -E 'PSE attributes for lan7:.{0,120}Power Detection Status: delivering power' \
      || { log "GATE FAIL main-ram: camera port not delivering"; exit 1; } ;;
  main-flash)
    grep -q 'pse ports answering: 8 of 8' "$OUT/main-ram.txt" 2>/dev/null \
      || { log "GATE FAIL main-flash: no passing RAM boot (main-ram) in $OUT"; exit 1; }
    log "main: back to the flash install, backup, sysupgrade (keeps config)"
    # leave a RAM image by a power cycle; a unit already running from flash (overlay root) needs none (no plug: SKIP_COLD=1)
    if ! $SW "grep -q '^overlayfs:/overlay / ' /proc/mounts" 2>/dev/null; then   # not booted from flash (RAM image or no ssh)
      $B power cycle --yes >/dev/null; wait_console 240 || exit 1; wait_ssh || { log "no ssh after RAM boot cycle"; exit 1; }
    fi
    umask 077; mkdir -p "$KIT/backup"
    $SW 'sysupgrade -b -' > "$KIT/backup/sx8p-config-$(date +%Y%m%d-%H%M).tar.gz" || exit 1
    scp -O -i "$HOME/.ssh/id_sx8p_bench" "$MAIN/openwrt-realtek-rtl838x-pakedge_sx-8p-squashfs-sysupgrade.bin" "root@$SWITCH:/tmp/main.bin" || exit 1
    $SW 'sysupgrade -T /tmp/main.bin' || { log "sysupgrade -T refused the image"; exit 1; }
    $SW 'sysupgrade /tmp/main.bin' >/dev/null 2>&1
    wait_console 400 || { log "GATE FAIL: no console after main flash - use rollback via U-Boot (README)"; exit 1; }
    log "main booted from flash"
    RUNS=${RUNS:-10}
    if [ "${SKIP_COLD:-}" = 1 ]; then log "main: cold runs skipped (SKIP_COLD=1, no plug)"; continue; fi
    rm -f "$OUT/main/summary.json"
    $B cold --label main --runs "$RUNS" --via serial --settle 40 --expect-regex "$EXPECT_MAIN" --yes | tee -a "$OUT/plan.log" \
      || { log "GATE FAIL main: bench exited non-zero"; exit 1; }
    gate main ;;
  noquiet)
    # OpenWrt main built WITHOUT loglevel=4 (default console level): does the kernel PSE driver start cleanly by itself?
    img="$KIT/images/main-noquiet/openwrt-realtek-rtl838x-pakedge_sx-8p-squashfs-sysupgrade.bin"
    [ -f "$img" ] || { log "GATE FAIL noquiet: $img missing"; exit 1; }
    $SW "grep -q '^overlayfs:/overlay / ' /proc/mounts" 2>/dev/null || { log "GATE FAIL noquiet: unit is not running from flash"; exit 1; }
    log "noquiet: backup, sysupgrade (keeps config)"
    umask 077; mkdir -p "$KIT/backup"
    $SW 'sysupgrade -b -' > "$KIT/backup/sx8p-config-$(date +%Y%m%d-%H%M).tar.gz" || exit 1
    scp -O -i "$HOME/.ssh/id_sx8p_bench" "$img" "root@$SWITCH:/tmp/noquiet.bin" || exit 1
    want=$(sha256sum "$img" | cut -d' ' -f1)
    $SW 'sha256sum /tmp/noquiet.bin' | grep -q "^$want" || { log "GATE FAIL noquiet: image changed in transfer"; exit 1; }
    $SW 'sysupgrade -T /tmp/noquiet.bin' || { log "GATE FAIL noquiet: sysupgrade -T refused the image"; exit 1; }
    $SW 'sysupgrade /tmp/noquiet.bin' >/dev/null 2>&1
    wait_console 400 || { log "GATE FAIL noquiet: no console after flash (U-Boot recovery, README)"; exit 1; }
    wait_ssh || { log "GATE FAIL noquiet: no ssh after flash"; exit 1; }
    log "noquiet booted from flash"
    # shellcheck disable=SC2016  # expanded on the switch
    $SW 'cat /proc/cmdline; echo printk: $(cat /proc/sys/kernel/printk)' | tee -a "$OUT/plan.log"
    # prove it is the unquieted kernel: no loglevel= on the command line and console level 15
    # shellcheck disable=SC2016
    $SW 'grep -q "loglevel=" /proc/cmdline && exit 1; [ "$(cut -f1 /proc/sys/kernel/printk)" = 15 ]' \
      || { log "GATE FAIL noquiet: the unit is not running the no-loglevel kernel"; exit 1; }
    # WARM=1 (no plug powering the switch): reboot over ssh instead of cutting mains; the label follows so the gate reads the right summary
    nq=noquiet; warm=""
    if [ "${WARM:-}" = 1 ]; then nq=noquiet-warm; warm="--warm"; fi
    rm -f "$OUT/$nq/summary.json"
    # report must show the driver probe and the camera port delivering; serial log must hold no PoE error text
    # shellcheck disable=SC2086
    $B cold --label "$nq" $warm --runs "${RUNS:-10}" --settle 40 --tail-secs 30 \
      --expect-regex '(?s)(?=.*8 ports across)(?=.*PSE attributes for lan7:.{0,120}Power Detection Status: delivering power)' --yes \
      | tee -a "$OUT/plan.log" || { log "GATE FAIL noquiet: bench exited non-zero"; exit 1; }
    gate "$nq" ;;
  main-flood)
    log "main: console flood test (over ssh)"
    $SW 'sh -s' < "$KIT/bench/checks/03-main-flood.sh" > "$OUT/main-flood.txt" 2>&1 \
      || { log "GATE FAIL main-flood: ssh run failed (see main-flood.txt)"; exit 1; }
    tr -d '\r' < "$OUT/main-flood.txt" | grep -E '^(control|flood|RESULT)' | tee -a "$OUT/plan.log"
    tr -d '\r' < "$OUT/main-flood.txt" | grep -q '^RESULT: PASS' || { log "GATE FAIL main-flood"; exit 1; } ;;
  rollback)
    log "rollback to 25.12 with-r4 (keeps config)"
    if ! $SW true 2>/dev/null; then log "main has no ssh: rollback needs the console route (README)"; exit 1; fi
    scp -O -i "$HOME/.ssh/id_sx8p_bench" "$KIT/images/25.12-with-r4/openwrt-realtek-rtl838x-pakedge_sx-8p-squashfs-sysupgrade.bin" "root@$SWITCH:/tmp/back.bin" || exit 1
    want=$(sha256sum "$KIT/images/25.12-with-r4/openwrt-realtek-rtl838x-pakedge_sx-8p-squashfs-sysupgrade.bin" | cut -d' ' -f1)
    $SW 'sha256sum /tmp/back.bin' | grep -q "^$want" || { log "GATE FAIL rollback: image changed in transfer"; exit 1; }
    $SW 'sysupgrade -T /tmp/back.bin' || { log "GATE FAIL rollback: sysupgrade -T refused the image"; exit 1; }
    $SW 'sysupgrade /tmp/back.bin' >/dev/null 2>&1
    if wait_console 400 && wait_ssh; then
      log "rolled back"
    else
      log "GATE FAIL rollback: no console/ssh after flash - U-Boot recovery (README)"; exit 1
    fi ;;
  *) echo "unknown phase $ph"; exit 2 ;;
  esac
done
log "plan finished: $*"
