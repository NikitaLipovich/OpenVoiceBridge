#!/usr/bin/env python3
"""Build and flash the S3-ETH firmware, read its console, restart it. Over the network by default (goal 093).

  s3.py port
  s3.py build
  s3.py flash [--usb] [--retries N]
  s3.py console --seconds N [--out DIR] [--echo] [--usb]
  s3.py reset [--usb]

Over the network: flash is ota.py s3 with the image just built, console the S3's own lines of the UDP log (udplog.py,
its stat line every 2 s among them), reset the word "UX <boot>" on UDP 5006. Over USB (--usb or BENCH_USB=1): the port is
found by USB VID, not by number, as before.
As a module: find_port(), watch(out) (a NetWatch, or a SerialWatch on USB), restart(watch), parse_stat(line).
Environment: BENCH_S3_PORT overrides the search of the USB port. Logs stay outside git (bench_runs).
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from serial.tools import list_ports

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import REPO_DIR, RUNS_ROOT, redact  # noqa: E402
from fw import idf  # noqa: E402
from serial_watch import SerialWatch  # noqa: E402

S3_VID = 0x303A
S3_DIR = REPO_DIR / "s3_eth_bridge"
FLASH_IMAGES = 3
STAT = re.compile(r"stat up=(\d+) eth=(\w+) link=(\d) ip=(\S+)")
# The fields after the head, each found on its own: a line from an older firmware lacks the newer ones, and an older S3
# cut the line at 159 characters in its UDP log (goal 092 #002). Pairs are call/mic.
STAT_FIELDS = (
    ("dsdin_rate", r" dsdin rate=(\d+)", int), ("dsdin_peak", r" dsdin rate=\d+ peak=(\d+)/(\d+)", int),
    ("asdout_rate", r" asdout rate=(\d+)", int), ("asdout_peak", r" asdout rate=\d+ peak=(\d+)/(\d+)", int),
    ("timeouts", r" timeouts=(\d+)/(\d+)", int), ("ovf", r" ovf=(\d+)/(\d+)", int),
    ("send_fail", r" send_fail=(\d+)/(\d+)", int), ("sent", r" sent=(\d+)/(\d+)", int),
    ("streaming", r" streaming=(\d)\b", int), ("pins_ok", r" pins_ok=(\d)\b", int),
    ("uart_tx", r" uart tx=(\d)\b", int), ("uart_rx", r" uart tx=\d rx=(\d+)", int), ("frames", r" frames=(\d+)", int),
    ("bad", r" bad=(\d+)/(\d+)", int), ("lrelay_lost", r" lrelay_lost=(\d+)", int), ("selftest", r" selftest=(\w+)", str),
    ("net", r" net=(\w+)", str), ("rssi", r" rssi=(-?\d+)", int), ("txp", r" txp=(-?\d+)", int))


def find_port():
    env = os.environ.get("BENCH_S3_PORT")
    if env:
        return env
    found = [p.device for p in list_ports.comports() if p.vid == S3_VID]
    return found[0] if found else None


def usb_mode():
    return os.environ.get("BENCH_USB") == "1"


def watch(out, usb=None):
    """The S3's console: its lines over the network (console.NetWatch, board s3), or on USB a SerialWatch whose DTR and
    RTS stay released until restart(). usb: None follows BENCH_USB."""
    if not (usb_mode() if usb is None else usb):
        import console
        return console.NetWatch(out, board="s3")
    port = find_port()
    if port is None:
        raise SystemExit("S3 console not found")
    return SerialWatch(port, out)


def restart(w):
    """Restart of the S3. Over the network: "UX <boot>" (NetWatch.pulse_reset). On USB: RTS pulse with DTR released;
    the USB port drops and returns, the watch reopens it.

    usbser.sys sends a change of RTS only together with a set of DTR, so DTR is written again after each change.
    """
    if getattr(w, "transport", "usb") == "net":
        return w.pulse_reset()
    w.sp.dtr = False
    w.sp.rts = True
    w.sp.dtr = False
    time.sleep(0.2)
    w.sp.rts = False
    w.sp.dtr = False
    w.event("reset_pulse")


def parse_stat(line):
    """The S3's stat line (console, or udplog.py with its parts joined) as a dict; None when it is not one. Fields the line
    does not carry are left out; ip is not kept. Since goal 092 also uart_tx, uart_rx, frames, bad, lrelay_lost, selftest,
    net (wifi or eth) and rssi (dBm, 0 without Wi-Fi)."""
    m = STAT.search(line)
    if not m:
        return None
    out = {"up": int(m.group(1)), "eth": m.group(2), "link": int(m.group(3))}
    rest = line[m.end():]
    for name, rx, kind in STAT_FIELDS:
        f = re.search(rx, rest)
        if f:
            values = [kind(v) for v in f.groups()]
            out[name] = values if len(values) > 1 else values[0]
    return out


def build():
    res = idf("idf.py -C '%s' build" % S3_DIR, "s3-build")
    if res.returncode != 0:
        print((res.stdout + res.stderr)[-3000:])
    print("s3 build rc=%d" % res.returncode, flush=True)
    return res.returncode


def flash(retries, usb=None):
    """Build, then flash: over the network with ota.py (fw.py flash --board s3), or over USB."""
    if not (usb_mode() if usb is None else usb):
        import subprocess
        return subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "fw.py"), "flash", "--board", "s3"]
                              ).returncode
    return flash_usb(retries)


def flash_usb(retries):
    rc = build()
    if rc != 0:
        return rc
    for attempt in range(1, retries + 1):
        port = find_port()
        if port is None:
            print("s3 flash attempt %d: no port" % attempt, flush=True)
            time.sleep(3)
            continue
        res = idf("idf.py -C '%s' -p %s flash" % (S3_DIR, port), "s3-flash-try%d" % attempt)
        text = res.stdout + res.stderr
        rc = res.returncode
        if rc == 0 and text.count("Hash of data verified") < FLASH_IMAGES:
            rc = 3
        print("s3 flash attempt %d/%d rc=%d" % (attempt, retries, rc), flush=True)
        if rc == 0:
            return 0
        time.sleep(3)
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["port", "build", "flash", "console", "reset"])
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--out", default=str(RUNS_ROOT / "s3_eth" / "console"))
    ap.add_argument("--echo", action="store_true", help="print the console lines")
    ap.add_argument("--usb", action="store_true", help="the S3's own USB port instead of the network")
    args = ap.parse_args()
    if args.usb:
        os.environ["BENCH_USB"] = "1"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.action == "port":
        print(find_port())
        return 0 if find_port() else 2
    if args.action == "build":
        return build()
    if args.action == "flash":
        return flash(args.retries)
    w = watch(args.out)
    if args.action == "reset":
        w.pump(time.monotonic() + 8, until=lambda: w.sp is not None)
        ok = restart(w) is not False
        w.close_port()
        w.finish(0)
        return 0 if ok else 1
    w.pump(time.monotonic() + args.seconds)
    w.close_port()
    summary = w.finish(args.seconds)
    if args.echo:
        for _, line in w.recent:
            print(redact(line))
    print(json.dumps({k: summary[k] for k in ("port", "port_opens", "port_drops", "lines", "board_resets", "panics")}))
    return 0 if summary["port_opens"] else 2


if __name__ == "__main__":
    sys.exit(main())
