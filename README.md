# OpenWrt for the Pakedge SX-8P

OpenWrt support for the **Pakedge SX-8P**: an 8-port gigabit PoE+ switch with one SFP slot, built on a Realtek RTL8380M
and closely based on the Zyxel GS1900-10HP. The vendor firmware (v1.03, 2017) is abandoned. This repo has everything
needed to build and run current OpenWrt on it, plus the test rig and the results that back each claim.

> **Never share the vendor firmware.** Dump your own flash before you install anything; that dump is your only way back
> to stock. This repo contains no vendor images and no flash dumps, and pull requests adding them will be refused.

## Status

| Build | PoE | State |
|---|---|---|
| **OpenWrt 25.12.6 + realtek-poe r4** (`openwrt/v25.12/`) | daemon with retry, serial resync and self-heal | **Recommended.** Tested at home and on the bench with real PoE devices. |
| OpenWrt 25.12.6 + stock realtek-poe 1.3.2 (`openwrt/v25.12/variants/`) | stock daemon | Works; drops requests under a console flood (see results) |
| OpenWrt `main` (`openwrt/main/`) | kernel PSE driver (`realtek,pse-mcu-gen1`) | Experimental. Ports start disabled; `local/files` adds a boot script that enables them |

## Testing

One unit, tested over ten days at home and on a bench, with real PoE devices. Cold boot = mains cut by a smart plug
(10 s off); warm boot = `reboot`.

| Date | Build | PoE load | Boots | Result |
|---|---|---|---|---|
| 2026-11-02/03 | 24.10.4, stock realtek-poe | Wi-Fi access point | 3 cold | PoE back after each boot, but after warm reboots ports stayed `unknown` until `poe restart`. That led to the retry/self-heal fix |
| 2026-11-04 | 25.12 + realtek-poe r3 | Wi-Fi access point, 4.3 W | 10 warm + 10 cold | Pass: 0 ports `unknown`, no aborts |
| 2026-11-06 | 25.12, wire capture on the PoE UART | | 5 warm + 5 cold | Found the split frames (see below) |
| up to 2026-11-11 | 25.12 + realtek-poe r3 | Router powered from lan1 | warm reboots | Router came back with PoE each time |
| 2026-11-11 | 25.12 + r4 and stock r5 | none (router moved to USB-C) | 3 warm + 3 cold each, plus console flood | Both pass boots; under the flood r5 dropped 3 requests, r4 retried them all |
| 2026-11-12 | 25.12 + r4, 25.12 + r5, `main` | PoE camera, 1.6 W | 10 cold each | All pass, 0 PoE errors. Console flood: r4 and r5 pass |
| 2026-11-12 | `main` without `loglevel=4` | PoE camera, 1.6 W | 10 warm + 10 cold | All pass |

Full bench report for 2026-11-12: [`bench/results/RESULT-20261112.md`](bench/results/RESULT-20261112.md).
Not tested: high loads (the most drawn was 4.3 W of the 125 W budget), several powered ports at once, a second unit.

## Hardware

- SoC Realtek RTL8380M, 128 MiB RAM, 16 MiB SPI NOR
- 8x 10/100/1000 copper (lan1-lan8), all PoE+, 125 W budget; 1x SFP (lan9, 1000base-x)
- PoE controller: ST32F100 MCU (firmware v17.1) with a BCM59111, on the SoC's second UART at 19200 baud
- Serial console 115200 8N1; U-Boot prompt `Pakedge#`

## Build (OpenWrt 25.12.6)

```sh
git clone https://github.com/jatosaj/sx8p-openwrt.git
git clone https://git.openwrt.org/openwrt/openwrt.git -b v25.12.6 && cd openwrt
./scripts/feeds update -a && ./scripts/feeds install -a
git apply ../sx8p-openwrt/openwrt/v25.12/0001-realtek-add-Pakedge-SX-8P.patch
git -C feeds/packages apply ../../../sx8p-openwrt/openwrt/v25.12/0002-packages-realtek-poe-retry-self-heal.patch
printf 'CONFIG_TARGET_realtek=y\nCONFIG_TARGET_realtek_rtl838x=y\nCONFIG_TARGET_realtek_rtl838x_DEVICE_pakedge_sx-8p=y\n' > .config
make defconfig && make -j"$(nproc)"
```

Images land in `bin/targets/realtek/rtl838x/`: `...pakedge_sx-8p-initramfs-kernel.bin` (RAM boot) and
`...pakedge_sx-8p-squashfs-sysupgrade.bin` (flash). OpenWrt `main`: `../sx8p-openwrt/tools/build-main.sh .`
from an OpenWrt `main` checkout with `openwrt/main/0001-realtek-add-Pakedge-SX-8P.patch` applied.

## Install

1. **Dump your flash first** (16 MiB SPI NOR).
2. Connect the serial console and stop U-Boot at `Pakedge#`.
3. `loady 0x82000000`, send the initramfs image over YMODEM (`tools/mac/ymodem_send.py`), then `bootm 0x82000000`.
   This boots OpenWrt from RAM and writes nothing. `tools/mac/uboot_catch.py`, `uboot_cmd.py` and `ymodem_send.py` automate it.
4. In that system: copy the sysupgrade image to `/tmp` and run `sysupgrade -n /tmp/<image>`.

The U-Boot environment and the `u-boot` / `sysinfo` partitions are never written. To go back to stock, load your dumped
firmware partition with `loady` and write it at `0x2a0000` with `sf erase` / `sf write` (planned, not rehearsed here).

## Why the console log level is 4

On the 25.12 builds the kernel prints its boot messages on ttyS0 at 115200. That delays the PoE controller's UART
(ttyS1) enough that commands reach the MCU in pieces; it answers each piece with a bad-checksum reject, and the stock
daemon pairs the extra rejects with later commands (ports stuck as `unknown`). A wire capture showed the split frames;
`loglevel=4` in the DTS bootargs removed them. Details and a repro:
[Hurricos/realtek-poe issue 10](https://github.com/Hurricos/realtek-poe/issues/10).

On `main` with the kernel PSE driver, boots were clean without `loglevel=4` (10/10 cold, 10/10 warm).

## Repo layout

| Path | What |
|---|---|
| `openwrt/v25.12/` | device patch (DTS, image recipe, PoE LEDs) and the realtek-poe r4 patch; `variants/` = stock daemon |
| `openwrt/main/` | device patch for OpenWrt `main` and `local/files` (PoE-enable boot script) |
| `handoff/releases/` | prebuilt realtek-poe r4 and r5 binaries (built from these patches) and `install.sh` to swap them on a running unit |
| `handoff/checks/` | read-only health report and a gated console-flood test for the 25.12 builds |
| `bench/` | test rig: smart-plug cold cycles, serial capture, per-boot PoE checks, U-Boot RAM boot (`bench.py`, `run-plan.sh`) |
| `tools/` | serial helpers (U-Boot commands, YMODEM sender, console), build script for `main` |
| `tests/` | `python3 -m unittest discover -s tests` (no hardware needed) |

## Known issues (`main`, kernel PSE driver)

- All PoE ports are disabled after boot until enabled (`ethtool --set-pse lanN c33-pse-admin-control enable`, needs `ethtool-full`).
- `ethtool --set-pse lanN c33-pse-admin-control disable` fails with an I/O error and a kernel warning ("unbalanced disables").
- The front PoE LEDs do not follow the PoE state.

## License

GPL-2.0 (see `LICENSE`), like the OpenWrt code these patches apply to.
