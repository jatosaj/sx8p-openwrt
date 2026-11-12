#!/bin/sh
# Description: DISRUPTIVE - swap the SX-8P's realtek-poe binary for a test variant, or put the original back
# Usage (from a clone of this repo; SWITCH = the switch's address):
#   scp -O handoff/releases/VARIANT/realtek-poe root@SWITCH:/tmp/realtek-poe.new
#   ssh root@SWITCH 'VARIANT=with-r4 CONFIRM=yes sh -s' < handoff/releases/install.sh
#   ssh root@SWITCH 'ROLLBACK=yes CONFIRM=yes sh -s' < handoff/releases/install.sh
# VARIANT: with-r4 (retry/resync/self-heal, backlog 16 fixed) | plain-r5 (stock realtek-poe, loglevel=4 alone).
# Needs the owner's OK for a quiet window: PoE ports drop power for a few seconds while the daemon restarts.
# The first install keeps the binary that was there (the unit's r3) in BACKUP for good; ROLLBACK puts it back.
# Refuses unless the daemon answers and no port is Disabled/unknown: the restart reloads the config and would
# power a port that was switched off with `ubus call poe manage`. ROLLBACK=yes, and FORCE=yes on an install,
# go ahead anyway (warning first): a broken unit is exactly when the way back must work (review 2026-11-09).
# If the daemon does not answer after an install, the previous binary is put back at once; after a rollback the
# original stays and a reboot is advised. Writes only BIN (via BIN.staging, BIN.prev), BACKUP, MARK; removes NEW.
# Test hooks (leave unset on the switch): BIN NEW BACKUP MARK POE_INIT
# Exit codes: 0 done, 1 swap failed (previous binary put back), 2 refused (no CONFIRM / bad VARIANT),
#             3 refused (unsafe start state, wrong file, no backup, no space)

BIN=${BIN:-/usr/bin/realtek-poe}
NEW=${NEW:-/tmp/realtek-poe.new}
BACKUP=${BACKUP:-/root/realtek-poe.orig}
MARK=${MARK:-/root/realtek-poe.variant}
POE_INIT=${POE_INIT:-/etc/init.d/poe}

[ "${CONFIRM:-}" = yes ] || { echo "refusing: run with CONFIRM=yes after the owner agrees (PoE drops for seconds)"; exit 2; }
if [ "${ROLLBACK:-}" = yes ]; then
  LABEL=original
else
  # sha256 of handoff/releases/<variant>/realtek-poe (tests/test_releases.py keeps these in step)
  case "${VARIANT:-}" in
    with-r4) WANT=ca4a119cab15bd8ebbdf15d5b923b5d09af6754f4a0e114baae485c623d5ada7 ;;
    plain-r5) WANT=e11ca9a30f9ee640dd603b49584e460d0147894d01a5b289254c8a3093ba7340 ;;
    *) echo "refusing: VARIANT must be with-r4 or plain-r5 (or ROLLBACK=yes)"; exit 2 ;;
  esac
  LABEL=$VARIANT
fi

nmatch() { grep -o -E "$1" | wc -l | tr -d ' '; }
healthy() {   # $1 = ubus poe info output
  printf '%s' "$1" | grep -q '"firmware"' && [ "$(printf '%s' "$1" | nmatch '"unknown"')" -eq 0 ]
}

main() {
  exec < /dev/null   # the script itself arrives on stdin (ssh sh -s): nothing may read it

  INFO=$(ubus call poe info 2>/dev/null)
  UNSAFE=""
  printf '%s' "$INFO" | grep -q '"firmware"' || UNSAFE="realtek-poe does not answer 'ubus call poe info'"
  [ -z "$UNSAFE" ] && [ "$(printf '%s' "$INFO" | nmatch 'Disabled|"unknown"')" -ne 0 ] &&
    UNSAFE="a port is Disabled/unknown (switched off on purpose, or already broken)"
  if [ -n "$UNSAFE" ]; then
    if [ "$LABEL" = original ] || [ "${FORCE:-}" = yes ]; then
      echo "WARNING: $UNSAFE - going ahead; a port switched off with 'ubus call poe manage' comes back on"
      printf '%s' "$INFO" | grep -o '"status": *"[^"]*"' | tr -d ' ' | tr '\n' ' '; echo
    else
      echo "refusing: $UNSAFE - run 01-readonly-health.sh and report (FORCE=yes only on the owner's word)"; exit 3
    fi
  fi

  if [ "$LABEL" = original ]; then
    [ -f "$BACKUP" ] || { echo "refusing: no $BACKUP - nothing was installed by this script"; exit 3; }
    SRC=$BACKUP
  else
    [ -f "$NEW" ] || { echo "refusing: $NEW missing - scp the variant's realtek-poe there first"; exit 3; }
    GOT=$(sha256sum "$NEW" | cut -d' ' -f1)
    [ "$GOT" = "$WANT" ] || { echo "refusing: checksum of $NEW is $GOT, not the $VARIANT binary"; exit 3; }
    SRC=$NEW
  fi

  NEED=$(( $(wc -c < "$SRC") * 3 / 1024 + 64 ))   # new copy + backup + spare, in KiB
  FREE=$(df -Pk "$(dirname "$BIN")" | awk 'NR == 2 { print $4 }')
  [ "${FREE:-0}" -ge "$NEED" ] || { echo "refusing: ${FREE:-?} KiB free next to $BIN, need $NEED"; exit 3; }

  echo "before: $(sha256sum "$BIN" | cut -d' ' -f1)  $BIN  (variant: $(cat "$MARK" 2>/dev/null || echo as-built))"
  [ -f "$BACKUP" ] || cp -p "$BIN" "$BACKUP" || { echo "FAIL: could not keep the original in $BACKUP"; exit 1; }
  PREV="$BIN.prev"
  cp -p "$BIN" "$PREV" || { echo "FAIL: could not keep the running binary"; exit 1; }
  STAGE="$BIN.staging"
  cp "$SRC" "$STAGE" && chmod 755 "$STAGE" && mv -f "$STAGE" "$BIN" || {
    rm -f "$STAGE"; mv -f "$PREV" "$BIN"; echo "FAIL: copy failed, nothing changed"; exit 1; }

  "$POE_INIT" restart
  sleep 30
  AFTER=$(ubus call poe info 2>/dev/null)
  if ! healthy "$AFTER" && [ "$LABEL" = original ]; then
    rm -f "$PREV" "$MARK"
    echo "FAIL: the original binary is back but the daemon is not healthy yet - reboot the switch, then run A"
    exit 1
  fi
  if ! healthy "$AFTER"; then
    mv -f "$PREV" "$BIN"
    "$POE_INIT" restart
    echo "FAIL: the daemon did not come back with $LABEL - previous binary put back and restarted"
    echo "run 01-readonly-health.sh and report"; exit 1
  fi
  rm -f "$PREV"
  if [ "$LABEL" = original ]; then rm -f "$MARK"; else echo "$LABEL" > "$MARK"; rm -f "$NEW"; fi
  echo "after:  $(sha256sum "$BIN" | cut -d' ' -f1)  $BIN"
  printf 'ports delivering: %s   searching: %s\n' \
    "$(printf '%s' "$AFTER" | nmatch 'Delivering power')" "$(printf '%s' "$AFTER" | nmatch 'Searching')"
  if [ "$LABEL" = original ]; then echo "RESULT: rolled back to the original binary"; else echo "RESULT: installed $LABEL"; fi
  exit 0
}

main
