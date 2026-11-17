#!/usr/bin/env python3
"""Build and flash the bench firmwares, and erase the bond memory only. Over the network by default (goal 093).

  fw.py build [--variant NAME] [--board lyrat|s3|both]
  fw.py flash [--variant NAME] [--board lyrat|s3|both]   builds first, then ota.py over Wi-Fi through the S3
  fw.py flash --image FILE [--board lyrat|s3]           a ready image over the network, no build
  fw.py erase-bonds                                      the LyraT forgets its bonds and peer list, then restarts
  ... --usb [--port COM15] [--retries N]                 the same over USB as before: idf.py flash, esptool erase_region

Over the network: flash runs `ota.py lyrat|s3 build/<image>` (the LyraT with --force: the update is taken during a link
to the phone, its restart still waits for the end of a call), result and exit code are those of ota.py (0 done, 1 refused
or failed, 2 S3 not found, 3 rolled back, 4 timeout); erase-bonds sends the console commands "unpair" and "reboot" through
the S3 and waits for the LyraT to come back. Over USB (--usb or --port, or BENCH_USB=1): idf.py flash with retries,
esptool erase_region of the nvs partition, as before goal 093.

Variants compile bench switches through the ignored main/bench_variant.h:
  base     normal firmware
  no_pa    PA (GPIO21) off and codec volume 0
  no_bt    Bluetooth never started
  bod      normal firmware plus the hardware brownout reset at level 0, about 2.43 V (diagnostic)
  bod7     the same at level 7, about 2.80 V
  mac_b5, mac_b3, mac_b2, mac_b1   one byte of the Bluetooth address plus 1: the phone sees a new device, see goal 044 DECISIONS D7
  mac_b<I>p<N>                     byte I of the address plus N, for bench experiments that may burn an address (goal 055)
  no_ctl_uart                      the control UART task is not started (goal 057 #004); commands through the S3 stop working
  bredr_p9, bredr_p6, bredr_p3, bredr_n12   fixed BR/EDR transmit power, dBm, the boot log prints it, goals 062, 063: more current in transmit
  afh_N, afhassess_N, esco_pkt_xNNN, esco_lat_N  goal 063, only in its sandbox build: Wi-Fi channels the host marks bad (bit 0 ch 1, bit 1 ch 6, bit 2 ch 11),
                                   the controller's own assessment of channels on or off,
                                   packet types and latency the board allows when the phone opens the channel
  no_first_sniff                   the board does not ask for sniff before the phone, as before goal 055: burns addresses, comparison only
  no_wbs   CONFIG_BT_HFP_WBS_ENABLE=n in sdkconfig: the phone can only choose CVSD; any other variant restores =y
  fdrop    test build: console commands fdrop P B and fdstat replace frames with a marked loss, see goal 045
  nsinj    test build: command inj N puts a known signal in front of the microphone chain, the bus carries input and output, goal 057
  afe      test build: noise suppression modes afe (whole ESP-SR AFE) and webo (older esp_ns call), only to compare, goal 057
Combine with "+": no_pa+no_wbs.

Over USB a flash counts as done only when esptool confirmed the hash of all three images (four with otadata, see
first_flash.py); over the network only when ota.py saw the new image running and valid (ELF SHA-256).

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
          "nsinj": ("BENCH_NS_INJECT", 1), "afe": ("BENCH_NS_AFE", 1), "no_ctl_uart": ("BENCH_NO_CTL_UART", 1),
          "bredr_p9": ("BENCH_BREDR_POWER", 7), "bredr_p6": ("BENCH_BREDR_POWER", 6), "bredr_p3": ("BENCH_BREDR_POWER", 5),
          "bredr_n12": ("BENCH_BREDR_POWER", 0)}
MAC_SHIFT = re.compile(r"^mac_b([0-5])p([1-9][0-9]?)$")
BENCH_NUMBER = re.compile(r"^(afhassess|afh|esco_pkt|esco_lat)_(x[0-9a-f]+|[0-9]+)$")   # hex only with x
BENCH_NUMBER_MACROS = {"afhassess": "BENCH_AFH_ASSESS", "afh": "BENCH_AFH_BLOCK", "esco_pkt": "BENCH_ESCO_PKT", "esco_lat": "BENCH_ESCO_LAT"}


def macro_of(name: str):
    number = BENCH_NUMBER.match(name)
    if number:
        value = number.group(2)
        return (BENCH_NUMBER_MACROS[number.group(1)], int(value[1:], 16) if value[0] == "x" else int(value))
    shift = MAC_SHIFT.match(name)
    return ("BENCH_MAC_PATCH", int(shift.group(1)) * 256 + int(shift.group(2))) if shift else MACROS.get(name)


def current_variant(header: Path = None, sdkconfig: Path = None) -> str:
    """The variant the working tree is built with, from bench_variant.h (and sdkconfig for no_wbs): what a new build
    must keep, the Bluetooth address of mac_b1 above all, or the phone loses its pairing. "base" when nothing is set."""
    header = header or VARIANT_H
    sdkconfig = sdkconfig or SDKCONFIG
    names = []
    text = header.read_text(encoding="utf-8") if header.exists() else ""
    for macro, value in re.findall(r"^#define\s+(\w+)\s+(-?\d+)", text, re.M):
        value = int(value)
        named = [n for n, mv in MACROS.items() if mv == (macro, value)]
        if named:
            names.append(named[0])
        elif macro == "BENCH_MAC_PATCH":
            names.append("mac_b%dp%d" % (value // 256, value % 256))
        else:
            kind = next((k for k, m in BENCH_NUMBER_MACROS.items() if m == macro), None)
            if kind is None:
                raise SystemExit("bench_variant.h: unknown macro %s" % macro)
            names.append("%s_%d" % (kind, value))
    if sdkconfig.exists():
        conf = sdkconfig.read_text(encoding="utf-8")
        names += [v for v, opt in KCONFIG_OFF.items() if "# %s is not set\n" % opt in conf]
    return "+".join(names) or "base"


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


IMAGES = {"lyrat": FW_DIR / "build" / "lyrat_resend.bin",
          "s3": FW_DIR.parent / "s3_eth_bridge" / "build" / "s3_eth_bridge.bin"}
UNPAIR_REPLY = re.compile(r"unpair removed=(-?\d+) bonds_before=(\d+)")
BACK_TIMEOUT_S = 30.0


def build_board(board: str, variant: str) -> int:
    if board == "lyrat":
        return build(variant)
    import s3
    return s3.build()


def ota_flash(board: str, variant: str, image=None) -> int:
    """ota.py with the image just built (or the one given); its JSON line is printed, its whole output kept in
    bench_runs/fw/."""
    import subprocess
    args = [sys.executable, str(Path(__file__).resolve().parent / "ota.py"), board, str(image or IMAGES[board]),
            "--label", "fw-" + (variant.replace("+", "_") if board == "lyrat" else "s3")]
    if board == "lyrat":
        args.append("--force")
    res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    log_dir = RUNS_ROOT / "fw"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / ("%s-ota-%s.txt" % (stamp(), board))).write_text(res.stdout + res.stderr, encoding="utf-8")
    last = (res.stdout.strip().splitlines() or [""])[-1]
    print("flash %s over the network: %s rc=%d" % (board, last, res.returncode), flush=True)
    return res.returncode


def lyrat_up(channel):
    """(seconds the LyraT has been up, from its heartbeat through the S3) or None when no fresh frame came."""
    q = channel.query()
    if q and 0 <= q.get("last_frame_ms", -1) < 3000 and q.get("lyrat_up_s", -1) >= 0:
        return q["lyrat_up_s"]
    return None


def erase_bonds_net() -> int:
    """unpair, then reboot, through the S3; done when the LyraT is back with a new start. 2: S3 not found."""
    import s3_uart
    ch = s3_uart.Channel()
    lines = []
    try:
        if not ch.find():
            print("erase-bonds: S3 not found on the network", flush=True)
            return 2
        got = ch.command("unpair", timeout=1.0)
        lines.append("unpair: status=%s %s" % (got["status"], (got["text"] or "").strip()))
        m = UNPAIR_REPLY.search(got["text"] or "")
        if got["status"] != s3_uart.ST_OK or not m or int(m.group(1)) < 0:
            print("\n".join(lines + ["erase-bonds: not done"]), flush=True)
            return 1
        up_before = lyrat_up(ch)
        # The heartbeat counts whole seconds: a board up for less than 2 s could not show that it started again.
        settle = time.monotonic() + 3.0
        while (up_before is None or up_before < 2) and time.monotonic() < settle:
            time.sleep(0.5)
            up_before = lyrat_up(ch)
        time.sleep(0.5)        # the stack writes the removal to NVS before the restart
        got = ch.command("reboot", retries=2)
        lines.append("reboot: status=%s %s" % (got["status"], (got["text"] or "").strip()))
        if got["status"] != s3_uart.ST_OK or "reboot in" not in (got["text"] or ""):
            print("\n".join(lines + ["erase-bonds: reboot refused"]), flush=True)
            return 1
        end = time.monotonic() + BACK_TIMEOUT_S
        time.sleep(1.5)
        back = None
        while time.monotonic() < end:
            up = lyrat_up(ch)
            if up is not None and (up_before is None or up < up_before):
                back = up
                break
            time.sleep(0.5)
        lines.append("lyrat back: %s" % ("up %d s" % back if back is not None else "no"))
        print("\n".join(lines), flush=True)
        return 0 if back is not None else 4
    finally:
        ch.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["build", "flash", "erase-bonds"])
    ap.add_argument("--variant", default="base")
    ap.add_argument("--board", choices=["lyrat", "s3", "both"], default="lyrat")
    ap.add_argument("--usb", action="store_true", help="over USB as before goal 093 (idf.py flash, esptool)")
    ap.add_argument("--port", default=None, help="LyraT USB port (implies --usb); default BENCH_PORT or COM15")
    ap.add_argument("--retries", type=int, default=12)   # "Wrong boot mode" came 8 times in a row
    ap.add_argument("--image", help="flash: this app image over the network, no build (one --board)")
    args = ap.parse_args()
    usb = args.usb or bool(args.port) or os.environ.get("BENCH_USB") == "1"
    port = args.port or DEFAULT_PORT
    boards = ["s3", "lyrat"] if args.board == "both" else [args.board]
    if args.action == "build":
        for board in boards:
            rc = build_board(board, args.variant)
            if rc != 0:
                return rc
        return 0
    if args.action == "flash" and args.image:
        if usb or len(boards) != 1:
            ap.error("--image goes over the network to one board")
        return ota_flash(boards[0], args.variant, args.image)
    if args.action == "flash":
        for board in boards:
            rc = build_board(board, args.variant)
            if rc != 0:
                return rc
            if not usb:
                rc = ota_flash(board, args.variant)
            elif board == "lyrat":
                rc = with_retries("flash", "idf.py -C '%s' -p %s flash" % (FW_DIR, port), args.retries,
                                  verify_images=FLASH_IMAGES)
            else:
                import s3
                rc = s3.flash_usb(args.retries)
            if rc != 0:
                return rc
        return 0
    if not usb:
        return erase_bonds_net()
    offset, size = nvs_region()
    return with_retries("erase-bonds",
                        "python -m esptool --chip esp32 -p %s erase_region %s %s" % (port, offset, size),
                        args.retries)


if __name__ == "__main__":
    sys.exit(main())
