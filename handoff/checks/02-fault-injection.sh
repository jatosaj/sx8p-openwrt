#!/bin/sh
# Description: DISRUPTIVE test - flood the serial console while restarting realtek-poe; prove the resync recovers
# Usage:  ssh root@SWITCH 'CONFIRM=yes sh -s' < handoff/checks/02-fault-injection.sh | tee handoff/results/fault-$(date +%Y%m%d-%H%M).txt
# Needs the owner's OK for a quiet window: PoE ports may drop power for a few seconds while the daemon restarts.
# Refuses (exit 3) unless the daemon answers and no port is Disabled/unknown beforehand: `poe restart` reloads the
# config and would power a port that was switched off with `ubus call poe manage`.
# Restores the original console level on every exit path, including Ctrl-C / kill / hangup / broken pipe.
# Writes only /dev/kmsg and the console level (dmesg -n). Test hooks (leave unset on the switch):
#   KMSG=/dev/kmsg  POE_INIT=/etc/init.d/poe  PRINTK=/proc/sys/kernel/printk
# Exit codes: 0 PASS, 1 FAIL, 2 refused (no CONFIRM=yes), 3 refused (unsafe start state), 130 aborted by signal

KMSG=${KMSG:-/dev/kmsg}
POE_INIT=${POE_INIT:-/etc/init.d/poe}
PRINTK=${PRINTK:-/proc/sys/kernel/printk}
[ "${CONFIRM:-}" = yes ] || { echo "refusing: run with CONFIRM=yes after the owner agrees (PoE may flicker)"; exit 2; }

nmatch() { grep -o -E "$1" | wc -l | tr -d ' '; }
count() { logread 2>/dev/null | grep -c -E "$1"; }
sig() { grep -o '"status": *"[^"]*"' | tr -d ' '; }   # per-port status, in output order
report() {
  printf '  bad-checksum/rejects: %s   resync: %s   aborts: %s   heal: %s   giving-up: %s\n' \
    "$(count 'request-bad-checksum|bad checksum|MCU rejected')" "$(count resynchronising)" \
    "$(count 'Aborting request')" "$(count 're-sending setup')" "$(count 'giving up')"
  printf '  ttyS1: %s\n' "$(grep -E '^1:' /proc/tty/driver/serial)"
}

main() {
  exec < /dev/null   # the script itself arrives on stdin (ssh sh -s): nothing may read it

  read -r CUR DEF MIN BDEF < "$PRINTK"
  case "$CUR" in
    [1-8]) ;;
    *) echo "refusing: cannot read the console level from $PRINTK ('$CUR')"; exit 3 ;;
  esac

  INFO_BEFORE=$(ubus call poe info 2>/dev/null)
  if ! printf '%s' "$INFO_BEFORE" | grep -q '"firmware"'; then
    echo "refusing: realtek-poe does not answer 'ubus call poe info' - run 01-readonly-health.sh and report"; exit 3
  fi
  if [ "$(printf '%s' "$INFO_BEFORE" | nmatch 'Disabled|"unknown"')" -ne 0 ]; then
    echo "refusing: a port is Disabled/unknown before the test (switched off on purpose, or already broken)"; exit 3
  fi

  FLOOD=""
  # shellcheck disable=SC2317  # called via trap
  restore() {   # idempotent; runs on every exit path. Console level first - printing can fail on a dead pipe
    [ -n "$FLOOD" ] && kill "$FLOOD" 2> /dev/null
    dmesg -n "$CUR"
  }
  trap restore EXIT
  trap 'exit 130' INT TERM HUP PIPE

  echo "=== before ==="; echo "printk was: $CUR $DEF $MIN $BDEF"
  SIG_BEFORE=$(printf '%s' "$INFO_BEFORE" | sig)
  GAVEUP_BEFORE=$(count 'giving up')
  printf 'ports delivering: %s\n' "$(printf '%s' "$INFO_BEFORE" | nmatch 'Delivering power')"; report

  echo "=== inject: console level 8, 300 kmsg lines in the background, restart poe daemon ==="
  dmesg -n 8
  (
    i=0
    while [ "$i" -lt 300 ]; do
      echo "<6>handoff-flood $i xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" >> "$KMSG"
      i=$((i + 1))
    done
  ) &
  FLOOD=$!
  "$POE_INIT" restart
  wait "$FLOOD"
  FLOOD=""
  dmesg -n "$CUR"
  echo "flood done, console level back to $CUR; waiting 60 s for the daemon to settle"
  sleep 60

  echo "=== after ==="
  INFO_AFTER=$(ubus call poe info 2>/dev/null)
  SIG_AFTER=$(printf '%s' "$INFO_AFTER" | sig)
  UNK=$(printf '%s' "$INFO_AFTER" | nmatch '"unknown"')
  FW0=$(printf '%s' "$INFO_AFTER" | nmatch 'v0.0')
  GAVEUP_AFTER=$(count 'giving up')
  printf 'ports delivering: %s   unknown: %s   fw v0.0: %s\n' \
    "$(printf '%s' "$INFO_AFTER" | nmatch 'Delivering power')" "$UNK" "$FW0"
  report
  logread 2> /dev/null | grep -i -E "resynchronising|Aborting|re-sending|bad checksum|rejected|giving up" | tail -n 20

  WHY=""
  printf '%s' "$INFO_AFTER" | grep -q '"firmware"' || WHY="$WHY daemon-not-answering"
  [ "$UNK" -eq 0 ] || WHY="$WHY ports-unknown"
  [ "$FW0" -eq 0 ] || WHY="$WHY firmware-v0.0"
  [ "$SIG_AFTER" = "$SIG_BEFORE" ] || WHY="$WHY port-status-changed (or a device was plugged/unplugged meanwhile)"
  [ "$GAVEUP_AFTER" -eq "$GAVEUP_BEFORE" ] || WHY="$WHY heal-gave-up"
  if [ -z "$WHY" ]; then
    echo "RESULT: PASS (daemon recovered; every port status unchanged; no 'giving up')"; exit 0
  fi
  echo "RESULT: FAIL -$WHY"
  echo "recover with: /etc/init.d/poe restart, then run 01-readonly-health.sh and report"; exit 1
}

main
