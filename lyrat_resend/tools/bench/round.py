#!/usr/bin/env python3
"""Bench round runner: one command, one JSON verdict based on measured values only.

  round.py power --power SOURCE [--seconds 900] [--no-open] [--label NAME]
  round.py link  --power SOURCE [--reboots 10] [--pairs 5] [--slc-timeout 30] [--label NAME]
  round.py sco   --power SOURCE [--n 5] [--active 20] [--label NAME]

power: watches the board, the phone ACL history and Windows USB removal events.
Writes <runs>/power/<stamp>-<label>/round.json and appends one line to <runs>/rounds.jsonl.
Later goals add stages here. Exit code: 0 PASS, 1 FAIL, 2 could not measure.
Holds off PC sleep during a round; a window the PC still slept through is INVALID_WINDOW.

PASS for power, port open: no port drop, no board reset, no USB removal event, no panic,
no SLC drop, and the board uptime grew over the whole window.
PASS for power, port closed: no USB removal event, and the uptime read right after the window
covers it; with adb available also no ACL teardown with the board.

link has two series. Reboot: pulse EN, SLC must come up within --slc-timeout with nothing done on the
phone, the reset must be seen in the log, the peer must be loaded at boot, and the phone must report
the headset connected. Pair: erase the board's NVS, forget on the phone, pair from the settings screen,
SLC up, passkey equal on both sides, bonds>=1, then one reboot to prove the peer was stored.
The board is erased before the phone forgets: a board that still holds the key would page the phone
and raise a pairing dialog of its own. The other bonds of the phone must end identical.
PASS only with every trial passed and the planned number of trials run.

sco drives the probe app (android/sco_probe, built and installed by android/build_apk.py --install).
One cycle: open, the board logs SCO OPEN and the app confirms the route, stays active for --active seconds,
close, the board logs SCO CLOSED and the app reports the route gone. The codec comes from the open line of
the board only: the board prints a meaningless codec with every other SCO state. No call may exist before or
after the round, checked through telecom and telephony; call indicators on the board are the phone's virtual
call that belongs to SCO without a call, not a call.
"""
import argparse
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phone  # noqa: E402
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


BONDS_LINE = re.compile(r"bonds=(\d+)")
# The board's own connect attempt names the peer it loaded; the plain boot line can be garbled by
# interleaved UART output, so it is only the fallback.
PEER_TRY = re.compile(r"HFP connect try peer\[\d+\] \S*?([0-9a-f]{2}:[0-9a-f]{2})\s*$")
PEER_BOOT = re.compile(r"BT: peer\[\d+\] = \S*?([0-9a-f]{2}:[0-9a-f]{2})")
PEER_STATUS = re.compile(r"CMD:\s+peer\[0\] = \S*?([0-9a-f]{2}:[0-9a-f]{2})\s*$")
SSP_LINE = re.compile(r"SSP confirm (\d{6})")
ACL_DOWN = re.compile(r"ACL DOWN reason=(0x[0-9a-f]+)")
SLC_SETTLE_S = 3.0      # SLC must survive this long to count
ERASE_RETRIES = 12


def lines_since(watch, t):
    return [line for at, line in watch.recent if at >= t]


def board_status(watch, timeout=4.0):
    """Ask the board for its status; a lost reply is retried once. Returns (bonds, peer0 suffix)."""
    for _ in range(2):
        t = watch.now()
        watch.send("s")
        watch.pump(time.monotonic() + timeout,
                   until=lambda: any("CMD:   heap" in line for line in lines_since(watch, t)))
        text = lines_since(watch, t)
        bonds = next((BONDS_LINE.search(line) for line in text if BONDS_LINE.search(line)), None)
        if bonds:
            peer = next((PEER_STATUS.search(line) for line in text if PEER_STATUS.search(line)), None)
            return int(bonds.group(1)), peer.group(1) if peer else None
    return None, None


def wait_slc(watch, slc_ups_before, timeout):
    """Seconds until a new SLC came up and stayed up; None if it did not within the timeout."""
    t0 = time.monotonic()
    watch.pump(t0 + timeout, until=lambda: watch.slc_ups > slc_ups_before)
    if watch.slc_ups <= slc_ups_before:
        return None
    took = time.monotonic() - t0
    watch.pump(time.monotonic() + SLC_SETTLE_S, until=lambda: not watch.slc_up)
    return round(took, 2) if watch.slc_up else None


def reboot_trial(watch, index, slc_timeout):
    """One EN pulse, then only watch: nothing is sent to the phone in this trial."""
    t = watch.now()
    resets = len(watch.resets)
    ups = watch.slc_ups
    watch.pulse_reset()
    latency = wait_slc(watch, ups, slc_timeout)
    text = lines_since(watch, t)
    bonds, peer = board_status(watch)
    connected = None
    if latency is not None:
        try:
            connected = phone.hfp_connected()
        except phone.PhoneError:
            connected = None
    trial = {
        "i": index, "slc_s": latency, "resets_seen": len(watch.resets) - resets,
        "reset_reason": next((r["reason"] for r in watch.resets[resets:] if r["reason"]), None),
        "peer_at_boot": next((rx.search(line).group(1) for rx in (PEER_TRY, PEER_BOOT) for line in text
                              if rx.search(line)), None),
        "bonds": bonds, "peer0": peer, "phone_hfp_connected": connected,
        "acl_down_reasons": [ACL_DOWN.search(line).group(1) for line in text if ACL_DOWN.search(line)],
        "panics_lines": sum(1 for line in text if "Guru Meditation" in line or "Backtrace:" in line),
    }
    trial["pass"] = bool(latency is not None and latency <= slc_timeout and trial["resets_seen"] == 1
                         and trial["peer_at_boot"] and bonds and bonds >= 1 and connected is True
                         and trial["panics_lines"] == 0)
    return trial


def erase_board(watch, port, log_dir, index):
    """esptool needs the port, so the watcher lets go of it for the erase."""
    watch.close_port()
    watch.event("erase_start", trial=index)
    res = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "fw.py"), "erase-bonds",
                          "--port", port, "--retries", str(ERASE_RETRIES)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    (log_dir / ("erase-%d.txt" % index)).write_text(redact(res.stdout + res.stderr), encoding="utf-8")
    watch.event("erase_end", trial=index, rc=res.returncode)
    return res.returncode


def pair_trial(watch, index, args, out):
    trial = {"i": index, "pass": False, "erase_rc": None, "forget": None, "pair": None, "slc_s": None}
    t_start = watch.now()
    trial["erase_rc"] = erase_board(watch, args.port, out, index)
    if trial["erase_rc"] != 0:
        trial["error"] = "erase failed"
        return trial
    # The erase ended in a reset we could not see; reopen so that all later lines are measured.
    t_open = watch.now()
    resets = len(watch.resets)
    watch.pump(time.monotonic() + 2.0)
    done = {}

    def phone_steps():
        try:
            done["forget"] = phone.forget()
            done["pair"] = phone.pair(args.pair_timeout)
        except phone.PhoneError as e:
            done["error"] = redact(str(e))
            done["code"] = e.code

    ups = watch.slc_ups
    worker = threading.Thread(target=phone_steps, daemon=True)
    t_pair = time.monotonic()
    worker.start()
    # Serial keeps being read while the phone is driven, so the SLC moment stays measured.
    watch.pump(t_pair + args.pair_timeout + 30, until=lambda: watch.slc_ups > ups or not worker.is_alive())
    while worker.is_alive():
        watch.pump(time.monotonic() + 0.5)
    trial.update(forget=done.get("forget"), pair=done.get("pair"))
    if "error" in done:
        trial["error"] = done["error"]
        trial["phone_error_code"] = done.get("code")
        return trial
    # The phone step ended; SLC may follow the bond by a few seconds.
    if watch.slc_ups <= ups:
        watch.pump(time.monotonic() + args.slc_timeout, until=lambda: watch.slc_ups > ups)
    if watch.slc_ups > ups:
        watch.pump(time.monotonic() + SLC_SETTLE_S, until=lambda: not watch.slc_up)
        trial["slc_up_after_settle"] = watch.slc_up
        trial["slc_s"] = round(time.monotonic() - t_pair, 2)
    text = lines_since(watch, t_start)
    board_key = next((SSP_LINE.search(line).group(1) for line in text if SSP_LINE.search(line)), None)
    phone_key = (trial["pair"] or {}).get("passkey")
    trial["passkey_match"] = bool(board_key and board_key == phone_key)
    trial["bonds"], trial["peer0"] = board_status(watch)
    trial["resets_after_erase"] = [round(r["t"] - t_open, 2) for r in watch.resets[resets:]]
    trial["reboot"] = reboot_trial(watch, index, args.slc_timeout)
    # One reset right at reopen is the erase's own; a second one or a later one is a real failure.
    early = [t for t in trial["resets_after_erase"] if t <= 5.0]
    late_resets = [t for t in trial["resets_after_erase"] if t > 5.0] + early[1:]
    trial["pass"] = bool(trial.get("slc_up_after_settle") and trial["passkey_match"] and trial["bonds"]
                         and trial["bonds"] >= 1 and trial["peer0"] and not late_resets
                         and trial["reboot"]["pass"] and trial["reboot"]["peer_at_boot"] == trial["peer0"])
    return trial


def stage_link(args):
    keep_awake()
    out = run_dir("link", args.label)
    start = datetime.now()
    t0 = time.monotonic()
    try:
        before = phone.status()
        bonds_before = phone.bond_list()
    except phone.PhoneError as e:
        result = {"stage": "link", "label": args.label, "power": args.power, "verdict": "NO_DATA",
                  "error": redact(str(e)), "dir": str(out)}
        write_json(out / "round.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    watch = SerialWatch(args.port, out, False, echo=args.echo)
    watch.pump(time.monotonic() + 15, until=lambda: watch.sp is not None)
    if watch.sp is None:
        watch.finish(0)
        result = {"stage": "link", "label": args.label, "power": args.power, "verdict": "NO_DATA",
                  "error": "port not opened", "dir": str(out)}
        write_json(out / "round.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    reboots, pairs = [], []
    for i in range(1, args.reboots + 1):
        trial = reboot_trial(watch, i, args.slc_timeout)
        reboots.append(trial)
        print(json.dumps({"reboot": trial}, ensure_ascii=False), flush=True)
        write_json(out / "partial.json", {"reboots": reboots, "pairs": pairs})
        if args.stop_on_fail and not trial["pass"]:
            break
    for i in range(1, args.pairs + 1):
        if args.stop_on_fail and any(not t["pass"] for t in reboots + pairs):
            break
        trial = pair_trial(watch, i, args, out)
        pairs.append(trial)
        print(json.dumps({"pair": trial}, ensure_ascii=False), flush=True)
        write_json(out / "partial.json", {"reboots": reboots, "pairs": pairs})

    watch.close_port()
    summary = watch.finish(args.reboots + args.pairs)
    end = datetime.now()
    try:
        after = phone.status()
        bonds_after = phone.bond_list()
    except phone.PhoneError:
        after, bonds_after = None, None
    others_same = None if bonds_after is None else phone.others(bonds_before) == phone.others(bonds_after)
    removals = usb_removals(start, end)
    standby = pc_standby_entries(start, end)
    window_valid = standby == 0
    reboot_pass = sum(1 for t in reboots if t["pass"])
    pair_pass = sum(1 for t in pairs if t["pass"])
    latencies = [t["slc_s"] for t in reboots if t["slc_s"] is not None]
    checks = {
        "reboots_all_pass": reboot_pass == args.reboots,
        "pairs_all_pass": pair_pass == args.pairs,
        "other_bonds_unchanged": others_same is True,
        "phone_bonded_at_end": bool(after and after["bonded"]),
        "usb_removals_zero": removals == 0,
        "port_drops_zero": summary["port_drops"] == 0,
        "panics_zero": summary["panics"] == 0,
    }
    verdict = "INVALID_WINDOW" if not window_valid else ("PASS" if all(checks.values()) else "FAIL")
    result = {
        "stage": "link", "label": args.label, "power": args.power, "verdict": verdict, "checks": checks,
        "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - t0, 1), "dir": str(out),
        "planned": {"reboots": args.reboots, "pairs": args.pairs, "slc_timeout_s": args.slc_timeout},
        "reboots_passed": reboot_pass, "pairs_passed": pair_pass,
        "slc_latency_s": {"min": min(latencies), "max": max(latencies),
                          "mean": round(sum(latencies) / len(latencies), 2)} if latencies else None,
        "phone_before": before, "phone_after": after, "pc_standby_entries": standby, "usb_removals": removals,
        "reboots": reboots, "pairs": pairs, "serial": summary,
    }
    write_json(out / "round.json", result)
    with (RUNS_ROOT / "rounds.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps({k: result[k] for k in (
            "stage", "label", "power", "verdict", "start", "seconds", "reboots_passed", "pairs_passed",
            "slc_latency_s", "usb_removals", "dir")}, ensure_ascii=False)) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("reboots", "pairs")}, ensure_ascii=False, indent=2))
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


SCO_APP = "com.openvoicebridge.scoprobe"
SCO_ACTIVITY = SCO_APP + "/.MainActivity"
SCO_STATUS = "/sdcard/Android/data/%s/files/status.json" % SCO_APP
SCO_OPEN_LINE = "*** SCO OPEN"
SCO_CLOSED_LINE = "*** SCO CLOSED"
# States 2 (CVSD) and 3 (mSBC) are the connected ones; any other state prints a meaningless codec.
SCO_CODEC = re.compile(r"SCO audio state=[23] codec=([A-Za-z]+)")
SCO_HEARTBEAT = "SCO=active"
SCO_POLL_S = 0.5


def app_command(cmd, cmd_id, *extra):
    return adb("shell", "am", "start", "-n", SCO_ACTIVITY, "--es", "cmd", cmd, "--es", "id", cmd_id, *extra)


def app_status():
    try:
        return json.loads(adb("exec-out", "cat", SCO_STATUS))
    except ValueError:
        return None


def telecom_state():
    """Counts only: the dump may hold a caller number, so nothing of it is ever saved."""
    dump = adb("shell", "dumpsys", "telecom")
    registry = adb("shell", "dumpsys", "telephony.registry")
    if not dump or not registry:
        return None
    lines = dump.splitlines()
    index = next((i for i, line in enumerate(lines) if line.strip() == "mCalls:"), None)
    history = next((i for i, line in enumerate(lines) if line.strip() == "Historical Events:"), None)
    if index is None or history is None:
        return None
    current = 0
    for line in lines[index + 1:]:
        if line.strip().startswith("mCallAudioManager"):
            break
        current += 1 if line.strip() else 0
    states = re.findall(r"mCallState=(\d+)", registry)
    return {"calls_now": current, "historical_lines": sum(1 for line in lines[history + 1:] if line.strip()),
            "telephony_busy": sum(1 for v in states if v != "0"), "telephony_states": len(states)}


def wait_app(watch, cmd_id, done, timeout):
    """Pump the serial log until the app reports this command done. Returns (seconds, last status)."""
    t0 = time.monotonic()
    status = None
    while time.monotonic() - t0 < timeout:
        status = app_status()
        if status and status.get("id") == cmd_id and (not status.get("ok") or done(status)):
            return round(time.monotonic() - t0, 2), status
        watch.pump(time.monotonic() + SCO_POLL_S)
    return None, status


def wait_line(watch, since, text, timeout):
    """Seconds until the line appears after `since`, counted from this call; None if it does not."""
    t0 = time.monotonic()
    seen = lambda: any(text in line for line in lines_since(watch, since))  # noqa: E731
    watch.pump(t0 + timeout, until=seen)
    return round(time.monotonic() - t0, 2) if seen() else None


def sco_cycle(watch, index, args, tag):
    trial = {"i": index, "pass": False}
    resets = len(watch.resets)
    slc_drops = watch.slc_drops
    t_open = watch.now()

    open_id = "%s-%d-open" % (tag, index)
    app_command("open", open_id)
    seconds, status = wait_app(watch, open_id, lambda s: s.get("route_confirmed"), args.open_timeout)
    trial["app_confirm_s"] = seconds
    trial["app_open"] = status and {k: status.get(k) for k in (
        "ok", "error", "route_confirmed", "route_sco", "headset_audio", "mode", "sco_state", "open_latency_ms")}
    app_confirmed = bool(status and status.get("id") == open_id and status.get("ok") and status.get("route_confirmed"))
    trial["board_open_wait_s"] = wait_line(watch, t_open, SCO_OPEN_LINE, args.open_timeout)
    text = lines_since(watch, t_open)
    trial["codec"] = next((SCO_CODEC.search(line).group(1) for line in text if SCO_CODEC.search(line)), None)

    active_ok = False
    if app_confirmed and trial["board_open_wait_s"] is not None:
        t_active = watch.now()
        watch.pump(time.monotonic() + args.active)
        text = lines_since(watch, t_active)
        # A new id makes the app answer now: a dead app leaves its last good status.json behind.
        active_id = "%s-%d-active" % (tag, index)
        app_command("status", active_id)
        _, status = wait_app(watch, active_id, lambda s: True, args.close_timeout)
        if not status or status.get("id") != active_id:
            status = None
        trial["closed_while_active"] = sum(1 for line in text if SCO_CLOSED_LINE in line)
        trial["board_heartbeats_active"] = sum(1 for line in text if SCO_HEARTBEAT in line)
        trial["app_active_end"] = status and {k: status.get(k) for k in (
            "route_confirmed", "headset_audio", "mode", "route_losses")}
        active_ok = bool(status and status.get("route_confirmed") and status.get("route_losses") == 0
                         and trial["closed_while_active"] == 0 and trial["board_heartbeats_active"] >= 1)
    trial["active_ok"] = active_ok

    # A failed cycle closes too: the next one must start from a released route.
    t_close = watch.now()
    close_id = "%s-%d-close" % (tag, index)
    app_command("close", close_id)
    seconds, status = wait_app(watch, close_id, lambda s: not s.get("opened") and not s.get("route_sco")
                               and s.get("mode") == 0, args.close_timeout)
    trial["app_close_s"] = seconds
    trial["app_close"] = status and {k: status.get(k) for k in ("ok", "opened", "route_sco", "mode", "sco_state")}
    app_closed = bool(status and status.get("id") == close_id and status.get("ok") and seconds is not None)
    trial["board_close_wait_s"] = wait_line(watch, t_close, SCO_CLOSED_LINE, args.close_timeout)

    trial["board_resets"] = len(watch.resets) - resets
    trial["reset_reasons"] = [r["reason"] for r in watch.resets[resets:]]
    trial["slc_drops"] = watch.slc_drops - slc_drops
    trial["pass"] = bool(app_confirmed and trial["board_open_wait_s"] is not None and trial["codec"] and active_ok
                         and app_closed and trial["board_close_wait_s"] is not None
                         and trial["board_resets"] == 0 and trial["slc_drops"] == 0)
    return trial


def sco_no_data(args, out, error):
    result = {"stage": "sco", "label": args.label, "power": args.power, "verdict": "NO_DATA", "error": redact(error),
              "dir": str(out)}
    write_json(out / "round.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2


def stage_sco(args):
    keep_awake()
    out = run_dir("sco", args.label)
    start = datetime.now()
    t0 = time.monotonic()
    if SCO_APP not in adb("shell", "pm", "path", SCO_APP):
        return sco_no_data(args, out, "probe app is not installed, run android/build_apk.py --install")
    try:
        before = phone.status()
        bonds_before = phone.bond_list()
    except phone.PhoneError as e:
        return sco_no_data(args, out, str(e))
    if before["keyguard"] or not before["hfp_connected"]:
        return sco_no_data(args, out, "phone not usable: keyguard=%s hfp_connected=%s"
                           % (before["keyguard"], before["hfp_connected"]))
    telecom_before = telecom_state()
    if telecom_before is None or telecom_before["calls_now"] or telecom_before["telephony_busy"]:
        return sco_no_data(args, out, "a call is in progress or telecom is unreadable: %s" % telecom_before)
    watch = SerialWatch(args.port, out, False, echo=args.echo)
    watch.pump(time.monotonic() + 15, until=lambda: watch.sp is not None)
    if watch.sp is None:
        watch.finish(0)
        return sco_no_data(args, out, "port not opened")
    watch.slc_up = True     # the phone just reported the headset connected, so a drop must count
    adb("shell", "am", "force-stop", SCO_APP)
    tag = datetime.now().strftime("%H%M%S")

    trials = []
    for i in range(1, args.n + 1):
        trial = sco_cycle(watch, i, args, tag)
        trials.append(trial)
        print(json.dumps({"cycle": trial}, ensure_ascii=False), flush=True)
        write_json(out / "partial.json", {"cycles": trials})
        if args.stop_on_fail and not trial["pass"]:
            break
        if i < args.n:
            watch.pump(time.monotonic() + args.gap)

    app_command("status", tag + "-end")
    watch.pump(time.monotonic() + 1)
    app_final = app_status()
    adb("shell", "am", "force-stop", SCO_APP)
    watch.close_port()
    summary = watch.finish(args.n)
    end = datetime.now()
    telecom_after = telecom_state()
    try:
        phone.go_home()
        after = phone.status()
        bonds_after = phone.bond_list()
    except phone.PhoneError:
        after, bonds_after = None, None
    removals = usb_removals(start, end)
    standby = pc_standby_entries(start, end)
    passed = sum(1 for t in trials if t["pass"])
    codecs = sorted({t["codec"] for t in trials if t.get("codec")})
    checks = {
        "cycles_all_pass": passed == args.n,
        "codec_recorded": bool(codecs),
        "no_call_during_round": bool(telecom_after) and telecom_after["calls_now"] == 0
                                and telecom_after["telephony_busy"] == 0
                                and telecom_after["historical_lines"] == telecom_before["historical_lines"],
        "other_bonds_unchanged": bonds_after is not None and phone.others(bonds_before) == phone.others(bonds_after),
        "phone_hfp_connected_at_end": bool(after and after["hfp_connected"]),
        "board_resets_zero": summary["board_resets"] == 0,
        "panics_zero": summary["panics"] == 0,
        "slc_drops_zero": summary["slc_drops"] == 0,
        "port_drops_zero": summary["port_drops"] == 0,
        "usb_removals_zero": removals == 0,
    }
    verdict = "INVALID_WINDOW" if standby != 0 else ("PASS" if all(checks.values()) else "FAIL")
    result = {
        "stage": "sco", "label": args.label, "power": args.power, "verdict": verdict, "checks": checks,
        "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - t0, 1), "dir": str(out),
        "planned": {"cycles": args.n, "active_s": args.active}, "cycles_passed": passed, "codecs": codecs,
        "telecom_before": telecom_before, "telecom_after": telecom_after, "phone_before": before, "phone_after": after,
        "app_final": app_final, "pc_standby_entries": standby, "usb_removals": removals,
        "cycles": trials, "serial": summary,
    }
    write_json(out / "round.json", result)
    with (RUNS_ROOT / "rounds.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps({k: result[k] for k in (
            "stage", "label", "power", "verdict", "start", "seconds", "cycles_passed", "codecs",
            "usb_removals", "dir")}, ensure_ascii=False)) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "cycles"}, ensure_ascii=False, indent=2))
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
    p = sub.add_parser("link", help="reboot to SLC and full re-pair series")
    p.add_argument("--power", required=True, help="what feeds the board, e.g. adapter or battery (R11)")
    p.add_argument("--reboots", type=int, default=10)
    p.add_argument("--pairs", type=int, default=5)
    p.add_argument("--slc-timeout", type=float, default=30.0, help="seconds from reset to SLC")
    p.add_argument("--pair-timeout", type=float, default=90.0, help="seconds for the pairing on the phone")
    p.add_argument("--stop-on-fail", action="store_true", help="stop at the first failed trial")
    p.add_argument("--label", default="")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--echo", action="store_true", help="print events live")
    p = sub.add_parser("sco", help="open and close the headset channel without a call, through the probe app")
    p.add_argument("--power", required=True, help="what feeds the board, e.g. adapter or battery (R11)")
    p.add_argument("--n", type=int, default=5, help="number of cycles")
    p.add_argument("--active", type=float, default=20.0, help="seconds to hold the channel open")
    p.add_argument("--open-timeout", type=float, default=15.0)
    p.add_argument("--close-timeout", type=float, default=10.0)
    p.add_argument("--gap", type=float, default=3.0, help="seconds between cycles")
    p.add_argument("--stop-on-fail", action="store_true", help="stop at the first failed cycle")
    p.add_argument("--label", default="")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--echo", action="store_true", help="print events live")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    return {"link": stage_link, "sco": stage_sco, "power": stage_power}[args.stage](args)


if __name__ == "__main__":
    sys.exit(main())
