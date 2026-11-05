#!/usr/bin/env python3
"""Build and flash the LyraT firmware, and erase the bond memory only.

  fw.py build [--variant NAME]
  fw.py flash [--variant NAME] [--retries N]   builds first, retries when the port drops
  fw.py erase-bonds [--retries N]              erases the nvs partition, nothing else

Variants compile bench switches through the ignored main/bench_variant.h:
  base     normal firmware
  no_pa    PA (GPIO21) off and codec volume 0
  no_bt    Bluetooth never started
  bod      normal firmware plus the hardware brownout reset at level 0, about 2.43 V (diagnostic)
  bod7     the same at level 7, about 2.80 V
  mac_b5, mac_b3, mac_b2, mac_b1   one byte of the Bluetooth address plus 1: the phone sees a new device, see goal 044 DECISIONS D7
  mac_b<I>p<N>                     byte I of the address plus N, for bench experiments that may burn an address (goal 055)
  no_ctl_uart                      the control UART task is not started (goal 057 #004); commands through the S3 stop working
  no_first_sniff                   the board does not ask for sniff before the phone, as before goal 055: burns addresses, comparison only
  no_wbs   CONFIG_BT_HFP_WBS_ENABLE=n in sdkconfig: the phone can only choose CVSD; any other variant restores =y
  fdrop    test build: console commands fdrop P B and fdstat replace frames with a marked loss, see goal 045
  nsinj    test build: command inj N puts a known signal in front of the microphone chain, the bus carries input and output, goal 057
  afe      test build: noise suppression modes afe (whole ESP-SR AFE) and webo (older esp_ns call), only to compare, goal 057
Combine with "+": no_pa+no_wbs.

A flash counts as done only when esptool confirmed the hash of all three images.

Environment: IDF_PATH (default C:\\esp\\v5.5.2\\esp-idf), IDF_TOOLS_PATH (default C:\\Espressif),
BENCH_PORT (default COM15). Exit code other than 0 means a real failure. erase_flash is never used.
"""
import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import DEFAULT_PORT, FW_DIR, RUNS_ROOT, powershell, stamp  # noqa: E402

IDF_PATH = os.environ.get("IDF_PATH", r"C:\esp\v5.5.2\esp-idf")
IDF_TOOLS = os.environ.get("IDF_TOOLS_PATH", r"C:\Espressif")
VARIANT_H = FW_DIR / "main" / "bench_variant.h"
# Macro and value; BENCH_BROWNOUT_RESET carries the threshold level plus one.
MACROS = {"no_pa": ("BENCH_NO_PA", 1), "no_bt": ("BENCH_NO_BT", 1),
          "bod": ("BENCH_BROWNOUT_RESET", 1), "bod7": ("BENCH_BROWNOUT_RESET", 8),
          # BENCH_MAC_PATCH: byte index * 256 + what to add to that byte of the address.
          "mac_b5": ("BENCH_MAC_PATCH", 5 * 256 + 1), "mac_b3": ("BENCH_MAC_PATCH", 3 * 256 + 1),
          "mac_b2": ("BENCH_MAC_PATCH", 2 * 256 + 1), "mac_b1": ("BENCH_MAC_PATCH", 1 * 256 + 1),
          "fdrop": ("BENCH_FRAME_DROP", 1), "no_first_sniff": ("BENCH_NO_FIRST_SNIFF", 1),
          "nsinj": ("BENCH_NS_INJECT", 1), "afe": ("BENCH_NS_AFE", 1), "no_ctl_uart": ("BENCH_NO_CTL_UART", 1)}
MAC_SHIFT = re.compile(r"^mac_b([0-5])p([1-9][0-9]?)$")


def macro_of(name: str):
    shift = MAC_SHIFT.match(name)
    return ("BENCH_MAC_PATCH", int(shift.group(1)) * 256 + int(shift.group(2))) if shift else MACROS.get(name)


PORT_ERRORS = ("could not open port", "serial port", "Failed to connect", "Write timeout",
               "The device does not recognize", "PermissionError", "Lost connection", "Serial data stream stopped",
               "No serial data", "FileNotFoundError", "ClearCommError", "chip stopped responding", "Wrong boot mode")
# Variants that need a Kconfig option off; the option goes back on as soon as they are not asked for.
KCONFIG_OFF = {"no_wbs": "CONFIG_BT_HFP_WBS_ENABLE"}
SDKCONFIG = FW_DIR / "sdkconfig"
FLASH_IMAGES = 3   # bootloader, partition table, app


def idf(args: str, log_name: str, timeout: float = 900.0):
    # idf.py is a PowerShell function: without the explicit exit its failure status is lost.
    script = "$env:IDF_TOOLS_PATH='%s'; . '%s\\export.ps1' | Out-Null; %s; exit $LASTEXITCODE" % (
        IDF_TOOLS, IDF_PATH, args)
    res = powershell(script, timeout=timeout)
    log_dir = RUNS_ROOT / "fw"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / ("%s-%s.txt" % (stamp(), log_name))).write_text(res.stdout + res.stderr, encoding="utf-8")
    return res


def set_kconfig(names) -> None:
    """Switch the options of KCONFIG_OFF in sdkconfig; the build notices the change and reconfigures."""
    if not SDKCONFIG.exists():      # generated and ignored by git: sdkconfig.defaults is the source
        res = idf("idf.py -C '%s' reconfigure" % FW_DIR, "reconfigure")
        if res.returncode != 0:
            raise SystemExit((res.stdout + res.stderr)[-3000:])
    text = original = SDKCONFIG.read_text(encoding="utf-8")
    for variant, option in KCONFIG_OFF.items():
        on, off = "%s=y\n" % option, "# %s is not set\n" % option
        want, other = (off, on) if variant in names else (on, off)
        if want not in text and other not in text:
            raise SystemExit("%s is not in sdkconfig" % option)
        text = text.replace(other, want)
    if text != original:
        SDKCONFIG.write_text(text, encoding="utf-8")


def set_variant(variant: str) -> None:
    """Rewrite bench_variant.h only on change so an unchanged variant stays an incremental build."""
    names = [] if variant == "base" else variant.split("+")
    unknown = [n for n in names if macro_of(n) is None and n not in KCONFIG_OFF]
    if unknown:
        raise SystemExit("unknown variant: %s (known: base, %s)" % (",".join(unknown), ", ".join([*MACROS, *KCONFIG_OFF])))
    set_kconfig(names)
    body = "// Generated by tools/bench/fw.py, ignored by git.\n#pragma once\n" + "".join(
        "#define %s %d\n" % macro_of(n) for n in names if macro_of(n))
    if not VARIANT_H.exists() or VARIANT_H.read_text(encoding="utf-8") != body:
        VARIANT_H.write_text(body, encoding="utf-8")
        os.utime(FW_DIR / "main" / "main.c")   # main.c may not depend on the header yet


def build(variant: str) -> int:
    set_variant(variant)
    res = idf("idf.py -C '%s' build" % FW_DIR, "build-" + variant.replace("+", "_"))
    if res.returncode != 0:
        print((res.stdout + res.stderr)[-3000:])
    print("build variant=%s rc=%d" % (variant, res.returncode), flush=True)
    return res.returncode


def with_retries(name: str, command: str, retries: int, verify_images: int = 0) -> int:
    rc = 1
    for attempt in range(1, retries + 1):
        res = idf(command, "%s-try%d" % (name, attempt))
        rc = res.returncode
        text = res.stdout + res.stderr
        if rc == 0 and text.count("Hash of data verified") < verify_images:
            rc = 3
        print("%s attempt %d/%d rc=%d" % (name, attempt, retries, rc), flush=True)
        if rc == 0:
            return 0
        if rc != 3 and not any(p in text for p in PORT_ERRORS):
            print(text[-3000:])
            return rc
        time.sleep(4)
    return rc


def nvs_region():
    with (FW_DIR / "partitions_bt_sink_example.csv").open(encoding="utf-8") as fh:
        for row in csv.reader(line for line in fh if not line.startswith("#")):
            if row and row[0].strip() == "nvs":
                return row[3].strip(), row[4].strip()
    raise SystemExit("nvs partition not found")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["build", "flash", "erase-bonds"])
    ap.add_argument("--variant", default="base")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--retries", type=int, default=12)   # "Wrong boot mode" came 8 times in a row
    args = ap.parse_args()
    if args.action == "build":
        return build(args.variant)
    if args.action == "flash":
        rc = build(args.variant)
        if rc != 0:
            return rc
        return with_retries("flash", "idf.py -C '%s' -p %s flash" % (FW_DIR, args.port), args.retries,
                            verify_images=FLASH_IMAGES)
    offset, size = nvs_region()
    return with_retries("erase-bonds",
                        "python -m esptool --chip esp32 -p %s erase_region %s %s" % (args.port, offset, size),
                        args.retries)


if __name__ == "__main__":
    sys.exit(main())
