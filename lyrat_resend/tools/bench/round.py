#!/usr/bin/env python3
"""Bench round runner: one command, one JSON verdict based on measured values only.

  round.py power --power SOURCE [--seconds 900] [--no-open] [--label NAME]

power: watches the board, the phone ACL history and Windows USB removal events.
Writes <runs>/power/<stamp>-<label>/round.json and appends one line to <runs>/rounds.jsonl.
Later goals add stages here. Exit code: 0 PASS, 1 FAIL, 2 could not measure.
Holds off PC sleep during a round; a window the PC still slept through is INVALID_WINDOW.

PASS for power, port open: no port drop, no board reset, no USB removal event, no panic,
no SLC drop, and the board uptime grew over the whole window.
PASS for power, port closed: no USB removal event, and the uptime read right after the window
covers it; with adb available also no ACL teardown with the board.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import DEFAULT_PORT, RUNS_ROOT, keep_awake, powershell, redact, run_dir, write_json  # noqa: E402
from serial_watch import SerialWatch  # noqa: E402

BOARD_NAME = "LyraT_Phone"
USB_ID = "VID_10C4"
CLOSED_SAMPLE_S = 15.0
ACL_LINE = re.compile(
    r"shim::acl peer:\S*?([0-9a-f]{2}:[0-9a-f]{2}) handle:\S+ is_locally_initiated:(\w+) "
    r"creation_time:(\S+ \S+)(?: teardown_time:(\S+ \S+) disconnect_reason:(\S+))?")
RECORD_LINE = re.compile(r"([0-9a-fA-F]{2}:[0-9a-fA-F]{2})\s+BR_EDR\b[^\n]*name:\"" + BOARD_NAME + '"')
# The security record list empties after a phone reboot; the bonded list stays.
BONDED_LINE = re.compile(r"([0-9a-fA-F]{2}:[0-9a-fA-F]{2})\s+\[[^\]\n]*\]\[[^\]\n]*\]\s+" + BOARD_NAME)


def adb(*args, timeout=60.0):
    try:
        res = subprocess.run(["adb", *args], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
        return res.stdout if res.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def phone_acl(start: datetime, end: datetime):
    """Closed ACL sessions with the board inside the window; None if unmeasured.

    The phone records a session only at its teardown, so a live link is not visible here.
    """
    dump = adb("shell", "dumpsys", "bluetooth_manager")
    if not dump:
        return None
    m = RECORD_LINE.search(dump) or BONDED_LINE.search(dump)
    if not m:
        return None   # board unknown to the phone: this is "not measured", not "zero sessions"
    suffix = m.group(1).lower()
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    created = teardowns = 0
    reasons = {}
    for m in ACL_LINE.finditer(dump):
        if suffix is None or m.group(1) != suffix:
            continue
        born = datetime.strptime(m.group(3), fmt)
        created += 1 if start <= born <= end else 0
        if m.group(4):
            gone = datetime.strptime(m.group(4), fmt)
            if start <= gone <= end:
                teardowns += 1
                reasons[m.group(5)] = reasons.get(m.group(5), 0) + 1
    return {"board_suffix": suffix, "acl_created": created, "acl_teardowns": teardowns,
            "teardown_reasons": reasons}


def usb_removals(start: datetime, end: datetime):
    """Kernel-PnP event 1010: the bridge vanished from the bus. Independent of the port being open."""
    fmt = "%Y-%m-%dT%H:%M:%S"
    script = (
        "$a=[datetime]'%s'; $b=[datetime]'%s'; "
        "$e=Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Kernel-PnP/Device Management';"
        "Id=1010;StartTime=$a;EndTime=$b} -ErrorAction SilentlyContinue | Where-Object {$_.Message -match '%s'}; "
        "@($e).Count" % (start.strftime(fmt), end.strftime(fmt), USB_ID))
    res = powershell(script, timeout=60)
    text = res.stdout.strip().splitlines()
    return int(text[-1]) if text and text[-1].isdigit() else None


def pc_standby_entries(start: datetime, end: datetime):
    """Kernel-Power event 506: the PC entered Modern Standby, so the window is not a real measurement."""
    fmt = "%Y-%m-%dT%H:%M:%S"
    script = (
        "$a=[datetime]'%s'; $b=[datetime]'%s'; "
        "@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Kernel-Power';"
        "Id=506;StartTime=$a;EndTime=$b} -ErrorAction SilentlyContinue).Count" % (start.strftime(fmt), end.strftime(fmt)))
    text = powershell(script, timeout=60).stdout.strip().splitlines()
    return int(text[-1]) if text and text[-1].isdigit() else None


def stage_power(args):
    keep_awake()
    out = run_dir("power", args.label)
    start = datetime.now()
    phone_before = adb("get-state").strip() or "gone"
    watch = SerialWatch(args.port, out, args.no_open, echo=args.echo).run(args.seconds)
    end = datetime.now()
    closed_sample = None
    if args.no_open:
        # Liveness without the phone: the first uptime read after the closed window must cover it.
        closed_sample = SerialWatch(args.port, out / "after_close", False, echo=args.echo).run(CLOSED_SAMPLE_S)
    acl = phone_acl(start, end)
    removals = usb_removals(start, end)
    standby = pc_standby_entries(start, end)
    # The monotonic clock keeps running through standby, so an overrun also exposes a sleeping PC.
    window_valid = standby == 0 and watch["elapsed_s"] <= args.seconds + 30

    checks = {"usb_removals_zero": removals == 0, "port_drops_zero": watch["port_drops"] == 0}
    if args.no_open:
        if acl is not None:
            checks["acl_teardowns_zero"] = acl["acl_teardowns"] == 0
        checks["uptime_covers_closed_window"] = (closed_sample["max_uptime_ms"] or 0) >= args.seconds * 1000
    else:
        checks["board_resets_zero"] = watch["board_resets"] == 0
        checks["panics_zero"] = watch["panics"] == 0
        checks["slc_drops_zero"] = watch["slc_drops"] == 0
        # Guards against a silently missed reset: the uptime must have grown over the window.
        checks["uptime_covers_window"] = (watch["last_uptime_ms"] or 0) >= (args.seconds - 30) * 1000
    measured = watch["lines"] > 0 or args.no_open
    if not window_valid:
        verdict = "INVALID_WINDOW"
    elif not measured:
        verdict = "NO_DATA"
    else:
        verdict = "PASS" if all(checks.values()) else "FAIL"

    result = {"stage": "power", "label": args.label, "power": args.power, "verdict": verdict, "checks": checks,
              "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
              "dir": str(out), "phone_before": phone_before, "pc_standby_entries": standby, "window_valid": window_valid,
              "usb_removals": removals, "acl": acl, "closed_sample": closed_sample}
    result.update(watch)
    write_json(out / "round.json", result)
    with (RUNS_ROOT / "rounds.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps({k: result[k] for k in (
            "stage", "label", "power", "verdict", "start", "seconds", "port_drops", "board_resets",
            "usb_removals", "slc_drops", "dir")}, ensure_ascii=False)) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="stage", required=True)
    p = sub.add_parser("power", help="board stability")
    p.add_argument("--seconds", type=float, default=900.0)
    p.add_argument("--no-open", action="store_true", help="keep the serial port closed")
    p.add_argument("--label", default="")
    p.add_argument("--power", required=True, help="what feeds the board, e.g. adapter or battery (R11)")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--echo", action="store_true", help="print events live")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    return stage_power(args)


if __name__ == "__main__":
    sys.exit(main())
