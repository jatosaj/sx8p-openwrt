#!/usr/bin/env bash
# Description: Build OpenWrt main for the Pakedge SX-8P with our local additions (PoE-enable init script, ethtool-full)
#
# usage: build-main.sh OPENWRT_MAIN_DIR [JOBS]
#   OPENWRT_MAIN_DIR  checkout of openwrt/openwrt main WITH the device commit (openwrt/main/0001-...patch) on top;
#                     if the commit is missing the patch is applied (not committed)
# What is added on top of the upstream-clean device commit (kept out of it on purpose):
#   openwrt/main/local/files/  -> OPENWRT_MAIN_DIR/files/   (init script pse-enable, runs at S95)
#   CONFIG_PACKAGE_ethtool-full=y                            (ethtool --show-pse/--set-pse need the netlink build)
# exit: 0 image built · 1 bad args/patch · 2 build failed · 3 disk below the guard
# Output: OPENWRT_MAIN_DIR/bin/targets/realtek/rtl838x/*pakedge_sx-8p*; log OPENWRT_MAIN_DIR/build-sx8p-main.log
set -euo pipefail
MIN_FREE_GB=${MIN_FREE_GB:-4}
dir=${1:?OPENWRT_MAIN_DIR}
jobs=${2:-8}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
patch="$here/openwrt/main/0001-realtek-add-Pakedge-SX-8P.patch"
cd "$dir"

# Apply the device patch unless the tree already carries exactly its content (as a commit or as a plain apply):
# a tree with an older device commit must not be built silently.
if git apply --check "$patch" 2>/dev/null; then
	git apply "$patch"
elif git apply --reverse --check "$patch" 2>/dev/null; then
	echo "device patch already in the tree: $dir"
else
	echo "the tree's device support differs from $patch (older commit?): $dir" >&2
	exit 1
fi
free_gb=$(df -BG --output=avail . | tail -1 | tr -dc 0-9)
[ "$free_gb" -ge "$MIN_FREE_GB" ] || { echo "only ${free_gb} GB free (< $MIN_FREE_GB)" >&2; exit 3; }

mkdir -p files
rsync -a "$here/openwrt/main/local/files/" files/
cat >.config <<'CFG'
CONFIG_TARGET_realtek=y
CONFIG_TARGET_realtek_rtl838x=y
CONFIG_TARGET_realtek_rtl838x_DEVICE_pakedge_sx-8p=y
CONFIG_PACKAGE_ethtool-full=y
CFG
make defconfig >/dev/null
{ make -j"$jobs" download && make -j"$jobs"; } >build-sx8p-main.log 2>&1 || {
	echo "build failed, see $dir/build-sx8p-main.log" >&2
	exit 2
}
cd bin/targets/realtek/rtl838x
sha256sum ./*pakedge_sx-8p*.bin
