#!/usr/bin/env python3
"""Bench round runner: one command, one JSON verdict based on measured values only.

  round.py power --power SOURCE [--seconds 900] [--no-open] [--label NAME]
  round.py link  --power SOURCE [--reboots 10] [--pairs 5] [--slc-timeout 30] [--label NAME]
  round.py sco   --power SOURCE [--n 5] [--active 20] [--label NAME]
  round.py audio --power SOURCE --codec mSBC|CVSD [--n 10] [--seconds 7] [--label NAME]
  round.py tap   --power SOURCE [--n 3] [--long 300] [--s3-resets 3] [--label NAME]
  round.py btloss --power SOURCE [--seconds 1800] [--ab 3] [--phone-wifi asis|on|off] [--label NAME]
  round.py plc   --power SOURCE --codec mSBC|CVSD [--n 3] [--period 30] [--keep-wav] [--label NAME]
  round.py uart  --power SOURCE [--commands 1000] [--lyrat-resets 3] [--s3-resets 3] [--line-low 8] [--label NAME]

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

audio measures the content of the sound through the channel of sco, on the firmware without Wi-Fi. One trial: open,
then four phases. down: the app plays a tone sequence, the S3-ETH board captures DSDIN, audio_check judges call.wav.
up: "tone 1" on the board, the app records from the headset, audio_check judges the pulled file.
control: nothing plays and the tone is off; the same checks must FAIL here, and the mic peak of the board must leave
the tone's level. A trial whose control passes proves the test cannot fail, so it fails.
duplex: down and up at the same time. In the phases with a tone on DSDIN, the other line of the bus must not show it.
A boot check first proves the tone starts off, and the codec of every trial must be the one asked for: a phone that
picks another codec does not count. The rule is strict in both directions unless asked otherwise; both numbers are
reported whatever the rule. The LyraT stays at vol 0 during the round (R15): the volume acts behind the bus.

tap judges what the S3-ETH board captures from the I2S bus (udp_record.TapRecorder, tap_check). Plays nothing on the PC
(R15) and holds the LyraT at vol 0 during the round, which acts behind the bus, so the phone's tones stay out of the
room. The LyraT is reset at the end, which brings back its volume. One trial: the app plays tones through SCO, then a
stretch without a signal. DSDIN: the expected tones, SNR by blocks of one mSBC frame (a lost Bluetooth frame is an
impaired block). ASDOUT: firmware tones never reach this line, so the same check must fail on it, which is the control
against a swapped or shorted line. The control stretch must fail on both lines. The LyraT has no stream of its own since
goal 044, so there is no sample by sample reference any more. Mic liveness: room sound against stationary copies of
itself, never an SNR or a frequency, plus the level change caused by the codec's gain. Mic recordings are deleted once
judged. --long records with SCO open and measures the loss S3 to PC by sequence, apart from the Bluetooth counters.
--s3-resets restarts the S3 with SCO open: the LyraT must see nothing and the streams return with no action.

btloss measures the rare failures of the Bluetooth link and when they happen. One window: open, the app plays a steady
1800 Hz down, the board sends 1800 Hz up ("tone 2"), the app records it, and the S3-ETH board captures DSDIN; the SCO
packet counters are read every --pkt-period seconds. --board-cmd sends console commands before the windows. tone_blocks finds each failure with its time in both directions. The verdict says
only whether all was measured and the phone was left as found; the numbers themselves are the result. With --ab N the
phone's Wi-Fi alternates off, on over N pairs of windows (R4). What the phone's Wi-Fi was is saved to a file before it is
touched, it comes back in a finally block, a later round restores it if the process was killed, and it is verified.

plc checks the concealment of lost frames on a signal that a repeat cannot fake (voice_check.py), through the same path
as audio: sco_probe plays, the S3 captures DSDIN. It needs a test build (fw.py --variant ...+fdrop): the firmware replaces
frames with a marked loss on a plan and measures the concealment against the true samples it still has. Each trial runs
the plan with and without concealment. PASS only if the counters agree with the plan, the run without concealment is worse
against the reference, replaced frames are digital silence on the bus only without concealment, true silence stays
silent, a long loss fades out in the time the firmware promises, and the concealment beats a plain repeat of the last block.

uart checks the control channel PC, S3, UART, LyraT (s3_uart.py, docs/S3_UART.md). Commands of all kinds, each reply checked
against the state the earlier ones must have caused, and the LyraT's console, which is not part of the channel, confirms the
state and that each command ran once. Then the worst the line can carry, sent raw by the S3: bad CRC, bad header, a length
too big, frames that stop, noise, a repeat. A good frame by the same route is the control that must act. Then the LyraT
restarts, whose boot garbage reaches the S3's RX, and with SCO open the S3 restarts and its TX goes off so that the line is
held low; the sound up to the phone is judged by the strict rule during those, and the LyraT must see no reset, no SLC or SCO
drop. audio --uart-load HZ runs the audio stage with commands going through the channel at the same time.
"""
import argparse
import json
import math
import random
import re
import subprocess
import sys
import threading
import time
import wave
from array import array
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_check  # noqa: E402
import phone  # noqa: E402
from benchlib import DEFAULT_PORT, RUNS_ROOT, keep_awake, powershell, redact, run_dir, write_json  # noqa: E402
from serial_watch import SerialWatch  # noqa: E402
from udp_record import TapRecorder  # noqa: E402
import s3  # noqa: E402
import s3_uart  # noqa: E402
import tap_check  # noqa: E402
import tone_blocks  # noqa: E402
import voice_check  # noqa: E402

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
SCO_DIR = "/sdcard/Android/data/%s/files" % SCO_APP
SCO_STATUS = SCO_DIR + "/status.json"
SCO_OPEN_LINE = "*** SCO OPEN"
SCO_CLOSED_LINE = "*** SCO CLOSED"
# States 2 (CVSD) and 3 (mSBC) are the connected ones; any other state prints a meaningless codec.
SCO_CODEC = re.compile(r"SCO audio state=[23] codec=([A-Za-z]+)")
SCO_HEARTBEAT = "SCO=active"
SCO_POLL_S = 0.5


def app_ready():
    """Stop the app and give it what it needs. ZUI takes RECORD_AUDIO back after an install and also blocks the
    uid's app-op, which a granted permission does not lift: the recording then runs and holds only zeros."""
    adb("shell", "am", "force-stop", SCO_APP)
    adb("shell", "pm", "grant", SCO_APP, "android.permission.RECORD_AUDIO")
    adb("shell", "pm", "grant", SCO_APP, "android.permission.BLUETOOTH_CONNECT")
    adb("shell", "cmd", "appops", "set", "--uid", SCO_APP, "RECORD_AUDIO", "allow")
    adb("shell", "cmd", "appops", "set", SCO_APP, "RECORD_AUDIO", "allow")


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


def channel_open(watch, trial, open_id, timeout):
    """Open the route; True when both the app and the board confirm it. Fills trial with the evidence."""
    t_open = watch.now()
    app_command("open", open_id)
    seconds, status = wait_app(watch, open_id, lambda s: s.get("route_confirmed"), timeout)
    trial["app_confirm_s"] = seconds
    trial["app_open"] = status and {k: status.get(k) for k in (
        "ok", "error", "route_confirmed", "route_sco", "headset_audio", "mode", "sco_state", "open_latency_ms")}
    app_confirmed = bool(status and status.get("id") == open_id and status.get("ok") and status.get("route_confirmed"))
    trial["board_open_wait_s"] = wait_line(watch, t_open, SCO_OPEN_LINE, timeout)
    text = lines_since(watch, t_open)
    trial["codec"] = next((SCO_CODEC.search(line).group(1) for line in text if SCO_CODEC.search(line)), None)
    return app_confirmed and trial["board_open_wait_s"] is not None


def channel_close(watch, trial, close_id, timeout):
    """Release the route; a failed trial closes too, the next one must start from a released route."""
    t_close = watch.now()
    app_command("close", close_id)
    seconds, status = wait_app(watch, close_id, lambda s: not s.get("opened") and not s.get("route_sco")
                               and s.get("mode") == 0, timeout)
    trial["app_close_s"] = seconds
    trial["app_close"] = status and {k: status.get(k) for k in ("ok", "opened", "route_sco", "mode", "sco_state")}
    app_closed = bool(status and status.get("id") == close_id and status.get("ok") and seconds is not None)
    trial["board_close_wait_s"] = wait_line(watch, t_close, SCO_CLOSED_LINE, timeout)
    return app_closed and trial["board_close_wait_s"] is not None, t_close


def sco_cycle(watch, index, args, tag):
    trial = {"i": index, "pass": False}
    resets = len(watch.resets)
    slc_drops = watch.slc_drops

    opened = channel_open(watch, trial, "%s-%d-open" % (tag, index), args.open_timeout)
    active_ok = False
    if opened:
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

    closed, _ = channel_close(watch, trial, "%s-%d-close" % (tag, index), args.close_timeout)

    trial["board_resets"] = len(watch.resets) - resets
    trial["reset_reasons"] = [r["reason"] for r in watch.resets[resets:]]
    trial["slc_drops"] = watch.slc_drops - slc_drops
    trial["pass"] = bool(opened and trial["codec"] and active_ok and closed
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
    app_ready()
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


AUDIO_TONES = [400, 1000, 1800]
AUDIO_WAV = "audio_tones.wav"
AUDIO_WAV_SECONDS = 3 * 3       # three cycles, longer than any phase needs
AUDIO_LEVEL = 8000
AUDIO_RATE = 16000
TONE_PEAK = 8000                # the board's tone amplitude, seen in its MIC peak lines
TONE_PEAK_TOLERANCE = 0.05
PEAK_LINE_S = 0.75              # a MIC line reports the peak of the last 50 frames
TONE_LINE = re.compile(r"CMD:\s+tone=(\d)")
MIC_PEAK = re.compile(r"MIC f=\d+ pk=(\d+)")
SCO_PKT = re.compile(r"SCO pkt: rx_total=(\d+) rx_correct=(\d+) rx_err=(\d+) rx_none=(\d+) rx_lost=(\d+) "
                     r"tx_total=(\d+) tx_discarded=(\d+)")
SCO_PKT_KEYS = ("rx_total", "rx_correct", "rx_err", "rx_none", "rx_lost", "tx_total", "tx_discarded")
SCO_STATS = re.compile(r"SCO stats close: rate=(\d+) in=(\d+) in_block=(\d+) in_dropped=(\d+) "
                       r"out=(\d+) out_block=(\d+) out_short=(\d+)")
SCO_PLC = re.compile(r"SCO plc: on=(\d+) frames=(\d+) bad=(\d+) undecoded=(\d+) concealed=(\d+) out_fail=(\d+) zero=(\d+)")
SCO_CPU = re.compile(r"SCO cpu: frames=(\d+) avg_us=(\d+) max_us=(\d+) load_permille=(\d+)")
SCO_RING = re.compile(r"SCO ring: mic_dropped=(\d+) level_min=(\d+) level_max=(\d+) sn_jumps=(\d+)")


def make_tone_wav(path):
    """The sequence the board plays, at the same level; continuous phase, so no clicks between tones."""
    samples = array("h")
    phase = 0.0
    for i in range(AUDIO_WAV_SECONDS * AUDIO_RATE):
        tone = AUDIO_TONES[(i // AUDIO_RATE) % len(AUDIO_TONES)]
        phase += 2 * math.pi * tone / AUDIO_RATE
        samples.append(int(AUDIO_LEVEL * math.sin(phase)))
    if sys.byteorder == "big":
        samples.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes(samples.tobytes())


def set_tone(watch, on, timeout=3.0):  # 0, 1 or 2
    """A reply can get lost in the log traffic, so the board's own echo decides."""
    for _ in range(3):
        t = watch.now()
        watch.send("tone %d" % on)
        if wait_line(watch, t, "tone = %d" % on, timeout) is not None:
            return True
    return False


def board_tone(watch, timeout=4.0):
    """The tone flag as printed by the status command; None if no reply came."""
    for _ in range(3):
        t = watch.now()
        watch.send("s")
        watch.pump(time.monotonic() + timeout,
                   until=lambda: any(TONE_LINE.search(line) for line in lines_since(watch, t)))
        found = next((TONE_LINE.search(line) for line in lines_since(watch, t) if TONE_LINE.search(line)), None)
        if found:
            return int(found.group(1))
    return None


def judge_wav(path, args, criterion, exclude=()):
    """audio_check result without the bulk; a missing file is a failed check, not a crash."""
    try:
        samples, rate = audio_check.read_wav(path)
    except (OSError, ValueError, EOFError, wave.Error) as e:
        return {"pass": False, "error": str(e)}
    result = audio_check.check(samples, rate, AUDIO_TONES, args.min_snr, args.max_shift,
                               criterion=criterion, max_impaired=args.max_impaired, exclude=exclude)
    return {k: result.get(k) for k in ("pass", "error", "rate", "seconds", "peak_abs", "criterion", "excluded_frames",
                                       "tones")}


def mic_peaks(watch, since):
    """MIC peak lines after `since`; the first line may still hold the previous state, so it is skipped."""
    text = lines_since(watch, since + PEAK_LINE_S)
    return [int(MIC_PEAK.search(line).group(1)) for line in text if MIC_PEAK.search(line)]


def median(values):
    return sorted(values)[len(values) // 2] if values else None


def sco_packets(watch):
    """The counters of the open link; None if the reply did not come."""
    t = watch.now()
    watch.send("pkt")
    wait_line(watch, t, "SCO pkt:", 3.0)
    found = next((SCO_PKT.search(line) for line in lines_since(watch, t) if SCO_PKT.search(line)), None)
    return dict(zip(SCO_PKT_KEYS, (int(v) for v in found.groups()))) if found else None


def packet_delta(before, after):
    """What the counters did in between, with the two shares that matter."""
    if not before or not after:
        return None
    delta = {k: after[k] - before[k] for k in SCO_PKT_KEYS}
    delta["rx_none_pct"] = round(100.0 * delta["rx_none"] / delta["rx_total"], 3) if delta["rx_total"] else None
    delta["rx_err_pct"] = round(100.0 * delta["rx_err"] / delta["rx_total"], 3) if delta["rx_total"] else None
    return delta


def closing_counters(watch, since):
    """Frame status and cost of the channel, as the board prints them when it closes; None on an older firmware."""
    text = lines_since(watch, since)
    plc = next((SCO_PLC.search(line) for line in text if SCO_PLC.search(line)), None)
    cpu = next((SCO_CPU.search(line) for line in text if SCO_CPU.search(line)), None)
    ring = next((SCO_RING.search(line) for line in text if SCO_RING.search(line)), None)
    if not plc or not cpu:
        return None
    return {**(dict(zip(("mic_dropped", "level_min", "level_max", "sn_jumps"), (int(v) for v in ring.groups()))) if ring else {}),
            **dict(zip(("plc_on", "frames", "bad", "undecoded", "concealed", "out_fail", "zero"), (int(v) for v in plc.groups()))),
            **dict(zip(("cpu_frames", "avg_us", "max_us", "load_permille"), (int(v) for v in cpu.groups())))}


def tap_tones_quiet(path, gaps, criterion, args):
    """The tone check on each slot of a line, reduced to what the verdict and the report need."""
    result = {}
    for slot, samples in zip("LR", tap_check.read_channels(path)):
        r = audio_check.check(samples, AUDIO_RATE, AUDIO_TONES, args.min_snr, args.max_shift, criterion=criterion,
                              max_impaired=args.max_impaired, exclude=[tuple(g) for g in gaps])
        result[slot] = {"pass": r["pass"], "tones": {k: v.get("snr_db") for k, v in r["tones"].items()}}
    return result


def audio_phase(watch, args, tag, name, seconds, play, tone, out):
    """One measured stretch: what plays, whether the board's tone is on, and all the recordings.

    The phone always records and the S3 always captures, so that a control phase checks the same files as a real one.
    """
    phase = {"name": name, "play": play, "tone": tone, "errors": []}
    out.mkdir(parents=True, exist_ok=True)
    wav_name = "%s-%s.wav" % (tag, name)
    before = sco_packets(watch)
    recorder = TapRecorder(("L", "LR")).start()
    if tone and not set_tone(watch, 1):
        phase["errors"].append("tone 1 not confirmed by the board")
    t_measure = watch.now()
    play_id = None
    if play:
        play_id = "%s-%s-play" % (tag, name)
        app_command("play", play_id, "--es", "wav", AUDIO_WAV)
        _, status = wait_app(watch, play_id, lambda s: (s.get("play") or {}).get("id") == play_id, 10.0)
        if not status or status.get("id") != play_id or not status.get("ok"):
            phase["errors"].append("play not accepted: %s" % (status and status.get("error")))
    record_id = "%s-%s-rec" % (tag, name)
    app_command("record", record_id, "--ei", "seconds", str(seconds), "--es", "out", wav_name,
                "--es", "source", args.source)
    took, status = wait_app(watch, record_id, lambda s: (s.get("record") or {}).get("id") == record_id
                            and (s.get("record") or {}).get("state") != "recording", seconds + 15.0)
    record = (status or {}).get("record") or {}
    phase["record"] = {k: record.get(k) for k in ("state", "frames", "peak", "rms", "error")}
    if took is None or record.get("state") != "done":
        phase["errors"].append("record did not finish: %s" % (status and status.get("error")))
    phase["mic_peak_median"] = median(mic_peaks(watch, t_measure))
    if tone and not set_tone(watch, 0):
        phase["errors"].append("tone 0 not confirmed by the board")
    if play:
        # A track finishes on its own: stopping the phase before that would cut the next one.
        _, status = wait_app(watch, record_id, lambda s: (s.get("play") or {}).get("id") == play_id
                             and (s.get("play") or {}).get("state") == "done", AUDIO_WAV_SECONDS + 5.0)
        if not ((status or {}).get("play") or {}).get("state") == "done":
            phase["errors"].append("play did not finish")
    summary = recorder.stop().save(out)
    phase["sco_delta"] = packet_delta(before, sco_packets(watch))
    phase["tap"] = {"call": tap_stream_stats(summary, "call"), "mic": tap_stream_stats(summary, "mic"),
                    "pins_ok": (summary.get("board_status") or {}).get("pins_ok")}
    pulled = adb("pull", SCO_DIR + "/" + wav_name, str(out / "phone.wav"), timeout=30.0)
    if not (out / "phone.wav").exists():
        phase["errors"].append("pull failed: %s" % pulled.strip()[:120])
    adb("shell", "rm", "-f", SCO_DIR + "/" + wav_name)
    phase["up"] = judge_wav(out / "phone.wav", args, args.criterion_up)     # board to phone, as the app recorded it
    # S3 losses are known to the sample and belong to the recording channel, not to the sound.
    phase["down"] = judge_wav(out / "call.wav", args, args.criterion_down, [tuple(g) for g in summary["call"]["gaps"]])
    # The other line of the bus: a tone from the phone must not appear on it. Room sound goes as soon as it is judged.
    phase["mic_line"] = tap_tones_quiet(out / "mic.wav", summary["mic"]["gaps"], args.criterion_down, args)
    tap_delete_room(out)
    return phase


def audio_trial(watch, index, args, out, tag):
    trial = {"i": index, "pass": False, "phases": []}
    resets = len(watch.resets)
    slc_drops = watch.slc_drops
    t_start = watch.now()
    trial_dir = out / ("trial-%02d" % index)
    tag = "%s-%d" % (tag, index)

    opened = channel_open(watch, trial, tag + "-open", args.open_timeout)
    checks = {"opened": opened, "codec_expected": trial["codec"] == args.codec}
    if opened:
        watch.pump(time.monotonic() + args.settle)
        down = audio_phase(watch, args, tag, "down", args.seconds, True, False, trial_dir / "down")
        up = audio_phase(watch, args, tag, "up", args.seconds, False, True, trial_dir / "up")
        control = audio_phase(watch, args, tag, "control", args.control_seconds, False, False, trial_dir / "control")
        duplex = audio_phase(watch, args, tag, "duplex", args.seconds, True, True, trial_dir / "duplex")
        trial["phases"] += [down, up, control, duplex]
        checks.update({
            "down_tones": bool(down["down"]["pass"]),
            "up_tones": bool(up["up"]["pass"]),
            "duplex_down_tones": bool(duplex["down"]["pass"]),
            "duplex_up_tones": bool(duplex["up"]["pass"]),
            # The same files without a signal must fail, otherwise the checks above prove nothing.
            "control_down_fails": not control["down"]["pass"],
            "control_up_fails": not control["up"]["pass"],
            # A tone on DSDIN must not be found on ASDOUT: this catches a swapped or shorted line.
            "other_line_fails": not any(v["pass"] for p in (down, duplex) for v in p["mic_line"].values()),
            "tone_at_board_level": up["mic_peak_median"] is not None
                and abs(up["mic_peak_median"] - TONE_PEAK) <= TONE_PEAK * TONE_PEAK_TOLERANCE,
            "tone_off_returns_mic": control["mic_peak_median"] is not None
                and abs(control["mic_peak_median"] - TONE_PEAK) > TONE_PEAK * TONE_PEAK_TOLERANCE,
            "no_phase_errors": not any(p["errors"] for p in trial["phases"]),
            "s3_captured": all(p["tap"]["call"]["unique"] and p["tap"]["call"]["ovf"] == 0
                               and p["tap"]["pins_ok"] == 1 for p in trial["phases"]),
        })
        trial["sco_packets"] = sco_packets(watch)
    closed, t_close = channel_close(watch, trial, tag + "-close", args.close_timeout)
    checks["closed"] = closed
    watch.pump(time.monotonic() + 0.3)
    stats = next((SCO_STATS.search(line) for line in lines_since(watch, t_close) if SCO_STATS.search(line)), None)
    if stats:
        keys = ("bt_rate", "in_bytes", "in_block", "in_dropped", "out_bytes", "out_block", "out_short")
        trial["sco_stats"] = dict(zip(keys, (int(v) for v in stats.groups())))
    watch.pump(time.monotonic() + 0.3)     # the lines after the stats line
    trial["sco_frames"] = closing_counters(watch, t_close)
    trial["board_resets"] = len(watch.resets) - resets
    trial["reset_reasons"] = [r["reason"] for r in watch.resets[resets:]]
    trial["reset_at_s"] = [round(r["t"] - t_start, 1) for r in watch.resets[resets:]]
    trial["slc_drops"] = watch.slc_drops - slc_drops
    checks["board_resets_zero"] = trial["board_resets"] == 0
    checks["slc_drops_zero"] = trial["slc_drops"] == 0
    trial["checks"] = checks
    trial["pass"] = all(checks.values())
    return trial


def audio_worst(trials, phase_name, direction):
    """Worst value of each measure over all trials for one direction; a missing tone reads as None."""
    worst = {}
    for tone in AUDIO_TONES:
        entries = [((p.get(direction) or {}).get("tones") or {}).get(str(tone), {})
                   for t in trials for p in t["phases"] if p["name"] == phase_name]
        if not entries:
            continue
        snr = [e.get("snr_db") for e in entries]
        block = [e.get("block_snr_median_db") for e in entries]
        impaired = [e.get("impaired_pct") for e in entries]
        shift = [abs(e["shift_pct"]) for e in entries if e.get("shift_pct") is not None]
        worst[str(tone)] = {"snr_min_db": None if None in snr else min(snr),
                            "block_snr_min_db": None if None in block else min(block),
                            "impaired_max_pct": None if None in impaired else max(impaired),
                            "shift_max_pct": max(shift) if shift else None}
    return worst


def tap_losses(trials):
    """The S3 is the measuring channel of the down direction: its losses are reported next to the verdict."""
    totals = {"lost_packets": 0, "phases_with_loss": 0, "phases": 0, "max_burst": 0}
    for t in trials:
        for p in t["phases"]:
            stats = (p.get("tap") or {}).get("call")
            if stats:
                totals["phases"] += 1
                totals["lost_packets"] += stats["lost_packets"]
                totals["phases_with_loss"] += 1 if stats["lost_packets"] else 0
                totals["max_burst"] = max(totals["max_burst"], stats["max_burst"])
    return totals


def audio_no_data(args, out, error):
    result = {"stage": "audio", "label": args.label, "power": args.power, "verdict": "NO_DATA", "error": redact(error),
              "dir": str(out)}
    write_json(out / "round.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2


def stage_audio(args):
    keep_awake()
    out = run_dir("audio", args.label)
    start = datetime.now()
    t0 = time.monotonic()
    if SCO_APP not in adb("shell", "pm", "path", SCO_APP):
        return audio_no_data(args, out, "probe app is not installed, run android/build_apk.py --install")
    try:
        before = phone.status()
        bonds_before = phone.bond_list()
    except phone.PhoneError as e:
        return audio_no_data(args, out, str(e))
    if before["keyguard"]:
        return audio_no_data(args, out, "phone keyguard is showing")
    telecom_before = telecom_state()
    if telecom_before is None or telecom_before["calls_now"] or telecom_before["telephony_busy"]:
        return audio_no_data(args, out, "a call is in progress or telecom is unreadable: %s" % telecom_before)
    watch = SerialWatch(args.port, out, False, echo=args.echo)
    watch.pump(time.monotonic() + 15, until=lambda: watch.sp is not None)
    if watch.sp is None:
        watch.finish(0)
        return audio_no_data(args, out, "port not opened")
    app_ready()

    # Boot check: the tone must be off with nothing asked of the board, and all links must come back.
    ups = watch.slc_ups
    watch.pulse_reset()
    boot = {"slc_s": wait_slc(watch, ups, args.slc_timeout)}
    boot["tone_after_boot"] = board_tone(watch)
    boot["reset_seen"] = len(watch.resets) == 1
    print(json.dumps({"boot": boot}, ensure_ascii=False), flush=True)
    if boot["slc_s"] is None:
        watch.close_port()
        watch.finish(0)
        return audio_no_data(args, out, "no SLC after reset")
    boot_resets = len(watch.resets)
    # Nothing of the phone's tones may reach the room (R15); the volume acts behind the bus.
    boot["vol_0"] = board_cmd(watch, "vol 0", "volume = 0")
    if not boot["vol_0"]:
        watch.close_port()
        watch.finish(0)
        return audio_no_data(args, out, "the board did not confirm vol 0")

    wav = out / AUDIO_WAV
    make_tone_wav(wav)
    # The app must exist before its files directory is usable for the push.
    tag = datetime.now().strftime("%H%M%S")
    app_command("status", tag + "-start")
    wait_app(watch, tag + "-start", lambda s: True, 10.0)
    adb("push", str(wav), SCO_DIR + "/" + AUDIO_WAV, timeout=30.0)

    # The S3 needs a moment after the reset above to see the LyraT again and let its TX go.
    uart_load = UartLoad(args.uart_load) if args.uart_load else None
    if uart_load:
        watch.pump(time.monotonic() + 3.0)
        uart_load.start()
    trials = []
    for i in range(1, args.n + 1):
        trial = audio_trial(watch, i, args, out, tag)
        trials.append(trial)
        print(json.dumps({"trial": {k: trial.get(k) for k in ("i", "pass", "codec", "checks", "sco_stats", "sco_packets")},
                          "down": audio_worst([trial], "down", "down"), "up": audio_worst([trial], "up", "up")},
                         ensure_ascii=False), flush=True)
        write_json(out / "partial.json", {"trials": trials})
        if args.stop_on_fail and not trial["pass"]:
            break
        if i < args.n:
            watch.pump(time.monotonic() + args.gap)

    load_result = uart_load.result() if uart_load else None
    board_cmd(watch, "vol 100", "volume = 100")     # what the firmware sets at boot
    tone_end = board_tone(watch)
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
    checks = {
        "trials_all_pass": passed == args.n,
        # A summary with a value that was not measured cannot count, whatever the trials say.
        "worst_all_measured": bool(trials) and all(
            e[k] is not None
            for phase, direction in (("down", "down"), ("up", "up"), ("duplex", "down"), ("duplex", "up"))
            for e in audio_worst(trials, phase, direction).values()
            for k in ("snr_min_db", "block_snr_min_db", "impaired_max_pct", "shift_max_pct")),
        "tone_off_after_boot": boot["tone_after_boot"] == 0,
        "tone_off_at_end": tone_end == 0,
        "no_call_during_round": bool(telecom_after) and telecom_after["calls_now"] == 0
                                and telecom_after["telephony_busy"] == 0
                                and telecom_after["historical_lines"] == telecom_before["historical_lines"],
        "other_bonds_unchanged": bonds_after is not None and phone.others(bonds_before) == phone.others(bonds_after),
        "phone_hfp_connected_at_end": bool(after and after["hfp_connected"]),
        "board_resets_only_boot_check": summary["board_resets"] == boot_resets,
        "panics_zero": summary["panics"] == 0,
        "slc_drops_zero": summary["slc_drops"] == 0,
        "port_drops_zero": summary["port_drops"] == 0,
        "usb_removals_zero": removals == 0,
    }
    if load_result:
        checks["uart_load_all_answered"] = load_result["sent"] > 0 and load_result["failed_after_retry"] == 0
    verdict = "INVALID_WINDOW" if standby != 0 else ("PASS" if all(checks.values()) else "FAIL")
    result = {
        "stage": "audio", "label": args.label, "power": args.power, "verdict": verdict, "checks": checks,
        "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - t0, 1), "dir": str(out),
        "planned": {"trials": args.n, "codec": args.codec, "min_snr_db": args.min_snr, "max_shift_pct": args.max_shift,
                    "tones": AUDIO_TONES, "criterion_up": args.criterion_up, "criterion_down": args.criterion_down,
                    "max_impaired_pct": args.max_impaired, "source": args.source},
        "uart_load": load_result, "trials_passed": passed, "boot": boot, "tone_end": tone_end, "tap_losses": tap_losses(trials),
        "rx_none_pct_by_trial": [(t.get("sco_packets") or {}).get("rx_total") and round(
            100.0 * t["sco_packets"]["rx_none"] / t["sco_packets"]["rx_total"], 3) for t in trials],
        "worst": {"down": audio_worst(trials, "down", "down"), "up": audio_worst(trials, "up", "up"),
                  "duplex_down": audio_worst(trials, "duplex", "down"), "duplex_up": audio_worst(trials, "duplex", "up")},
        "telecom_before": telecom_before, "telecom_after": telecom_after, "phone_before": before, "phone_after": after,
        "pc_standby_entries": standby, "usb_removals": removals, "trials": trials, "serial": summary,
    }
    write_json(out / "round.json", result)
    with (RUNS_ROOT / "rounds.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps({k: result[k] for k in (
            "stage", "label", "power", "verdict", "start", "seconds", "trials_passed", "worst",
            "usb_removals", "dir")}, ensure_ascii=False)) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "trials"}, ensure_ascii=False, indent=2))
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


TAP_SLOTS = ("LR", "LR")
TAP_ROOM_FILES = ("mic.wav",)
TAP_HW_GAIN = 30                # what the firmware sets at boot
TAP_LOW_GAIN = 0
TAP_GAIN_MIN_DB = 12.0          # the PGA moves by 24 dB; a smaller step means the level does not come through it
TAP_EXCERPT_S = 4.3
TAP_GAIN_SETTLE_S = 2.0
TAP_LIVE_TRIES = 5
TAP_LIVE_PAUSE_S = 60.0
TAP_STEADY_RATIO = 1.2          # short time level as steady as its stationary copy
TAP_RATE_TOLERANCE = 0.01


def board_cmd(watch, cmd, echo, timeout=3.0):
    """Send a console command; the board's own echo decides."""
    for _ in range(3):
        t = watch.now()
        watch.send(cmd)
        if wait_line(watch, t, echo, timeout) is not None:
            return True
    return False


def tap_stream_stats(summary, name):
    s = summary[name]
    return {k: s.get(k) for k in ("packets", "unique", "duplicates", "reordered", "lost_packets", "loss_pct", "max_burst",
                                  "ovf", "send_fail", "rate_hz", "frames", "boots", "channels")}


def tap_tones(path, gaps, args):
    """Tone check of every slot of a line, without the bulk."""
    result = {}
    for slot, samples in zip("LR", tap_check.read_channels(path)):
        r = audio_check.check(samples, AUDIO_RATE, AUDIO_TONES, args.min_snr, args.max_shift, criterion="blocks",
                              max_impaired=args.max_impaired, exclude=[tuple(g) for g in gaps])
        result[slot] = {"pass": r["pass"], "peak_abs": r.get("peak_abs"),
                        "tones": {k: {a: v.get(a) for a in ("pass", "snr_db", "block_snr_median_db", "impaired_pct",
                                                            "shift_pct", "seconds")} for k, v in r["tones"].items()}}
    return result


def tap_slots(path):
    """How the two slots of a line relate; rms of each."""
    left, right = tap_check.read_channels(path)
    rms = lambda c: round(math.sqrt(sum(v * v for v in c) / len(c)), 1) if c else None  # noqa: E731
    return {"rms": [rms(left), rms(right)],
            "identical_pct": round(100.0 * sum(a == b for a, b in zip(left, right)) / len(left), 3) if left else None}


def tap_delete_room(out):
    """Room recordings stay only as long as they are needed (R15); the numbers are already in the JSON."""
    for name in TAP_ROOM_FILES:
        (out / name).unlink(missing_ok=True)


def tap_phase(watch, out, seconds, play_id=None):
    out.mkdir(parents=True, exist_ok=True)
    rec = TapRecorder(TAP_SLOTS).start()
    errors = []
    if play_id:
        app_command("play", play_id, "--es", "wav", AUDIO_WAV)
    watch.pump(time.monotonic() + seconds)
    if play_id:
        _, status = wait_app(watch, play_id, lambda s: (s.get("play") or {}).get("id") == play_id
                             and (s.get("play") or {}).get("state") == "done", AUDIO_WAV_SECONDS + 5.0)
        if not status or status.get("id") != play_id or not status.get("ok"):
            errors.append("play failed: %s" % (status and status.get("error")))
    summary = rec.stop().save(out)
    return summary, errors


def tap_line_ok(stats):
    return bool(stats["unique"] and stats["ovf"] == 0 and stats["rate_hz"]
                and abs(stats["rate_hz"] - AUDIO_RATE) <= AUDIO_RATE * TAP_RATE_TOLERANCE)


def tap_sco_packets(watch):
    return sco_packets(watch)


def tap_trial(watch, index, args, out, tag):
    trial = {"i": index}
    resets, slc_drops = len(watch.resets), watch.slc_drops
    trial_dir = out / ("trial-%02d" % index)
    tag = "%s-%d" % (tag, index)
    checks = {"opened": channel_open(watch, trial, tag + "-open", args.open_timeout)}
    if checks["opened"]:
        watch.pump(time.monotonic() + args.settle)
        tone_dir, control_dir = trial_dir / "tone", trial_dir / "control"
        summary, errors = tap_phase(watch, tone_dir, args.seconds, tag + "-play")
        trial["tone"] = {
            "call": tap_stream_stats(summary, "call"), "mic": tap_stream_stats(summary, "mic"),
            "call_tones": tap_tones(tone_dir / "call.wav", summary["call"]["gaps"], args),
            "mic_tones": tap_tones(tone_dir / "mic.wav", summary["mic"]["gaps"], args),
            "call_slots": tap_slots(tone_dir / "call.wav"), "mic_slots": tap_slots(tone_dir / "mic.wav"),
            "pins_ok": (summary.get("board_status") or {}).get("pins_ok"), "errors": errors}
        control, control_errors = tap_phase(watch, control_dir, args.control_seconds)
        trial["control"] = {
            "call": tap_stream_stats(control, "call"), "mic": tap_stream_stats(control, "mic"),
            "call_tones": tap_tones(control_dir / "call.wav", control["call"]["gaps"], args),
            "mic_tones": tap_tones(control_dir / "mic.wav", control["mic"]["gaps"], args),
            "errors": control_errors}
        trial["sco_packets"] = tap_sco_packets(watch)
        t = trial["tone"]
        c = trial["control"]
        checks.update({
            "no_errors": not errors and not control_errors,
            "call_tones_some_slot": any(v["pass"] for v in t["call_tones"].values()),
            # The other line of the bus must not show the tone: this catches a swapped or shorted line.
            "other_line_tones_fail": not any(v["pass"] for v in t["mic_tones"].values()),
            "call_line_ok": tap_line_ok(t["call"]),
            "mic_line_ok": tap_line_ok(t["mic"]),
            "pins_inputs": t["pins_ok"] == 1,
            # The same checks without a signal must fail, otherwise the ones above prove nothing.
            "control_call_tones_fail": not any(v["pass"] for v in c["call_tones"].values()),
            "control_mic_tones_fail": not any(v["pass"] for v in c["mic_tones"].values()),
        })
        tap_delete_room(tone_dir)
        tap_delete_room(control_dir)
    closed, _ = channel_close(watch, trial, tag + "-close", args.close_timeout)
    checks["closed"] = closed
    trial["board_resets"] = len(watch.resets) - resets
    trial["slc_drops"] = watch.slc_drops - slc_drops
    checks["board_resets_zero"] = trial["board_resets"] == 0
    checks["slc_drops_zero"] = trial["slc_drops"] == 0
    trial["checks"] = checks
    trial["pass"] = all(checks.values())
    return trial


def tap_excerpt(watch, out):
    """A short recording of ASDOUT; only the measures stay."""
    summary, _ = tap_phase(watch, out, TAP_EXCERPT_S)
    channels = tap_check.read_channels(out / "mic.wav")
    result = {"slot_" + s: tap_check.liveness(c) for s, c in zip("LR", channels)}
    result["lost_packets"] = summary["mic"]["lost_packets"]
    tap_delete_room(out)
    (out / "call.wav").unlink(missing_ok=True)
    return result


def tap_liveness(watch, out):
    """Room sound on ASDOUT. A check that the mic is alive, not of its quality: no SNR and no frequency here."""
    live = {"excerpts": [], "tries": 0}
    # The room is not always playing something: then the line is steady, which is the noise baseline, and the
    # owner's rule is to look again later.
    for attempt in range(TAP_LIVE_TRIES):
        live["tries"] = attempt + 1
        for i in range(3):
            live["excerpts"].append(tap_excerpt(watch, out / ("excerpt-%d-%d" % (attempt, i))))
            print(json.dumps({"try": attempt, "excerpt": i, "L": live["excerpts"][-1]["slot_L"]}), flush=True)
        if any(e["slot_L"].get("pass") or e["slot_R"].get("pass") for e in live["excerpts"]):
            break
        if attempt + 1 < TAP_LIVE_TRIES:
            watch.pump(time.monotonic() + TAP_LIVE_PAUSE_S)
    live["gain_set"] = board_cmd(watch, "hw %d" % TAP_LOW_GAIN, "hw_gain = %d" % TAP_LOW_GAIN)
    watch.pump(time.monotonic() + TAP_GAIN_SETTLE_S)
    live["low_gain"] = tap_excerpt(watch, out / "excerpt-lowgain")
    live["gain_restored"] = board_cmd(watch, "hw %d" % TAP_HW_GAIN, "hw_gain = %d" % TAP_HW_GAIN)
    watch.pump(time.monotonic() + TAP_GAIN_SETTLE_S)
    live["high_gain"] = tap_excerpt(watch, out / "excerpt-highgain")
    # The median of the short time level: a step of the gain leaves a decaying offset that inflates the rms.
    high, low = live["high_gain"]["slot_L"].get("level_p50"), live["low_gain"]["slot_L"].get("level_p50")
    live["gain_step_db"] = round(20 * math.log10(high / low), 1) if high and low else None
    passed = [e for e in live["excerpts"] if e["slot_L"].get("pass") or e["slot_R"].get("pass")]
    live["excerpts_with_changing_sound"] = len(passed)
    live["room_sound_seen"] = bool(passed)
    live["level_follows_gain"] = bool(live["gain_step_db"] and live["gain_step_db"] >= TAP_GAIN_MIN_DB)
    live["quietest_level_LR"] = [min(e["slot_" + s].get("level_p10") or 0 for e in live["excerpts"]) for s in "LR"]
    steady = [e for e in live["excerpts"] if (e["slot_L"].get("modulation_ratio") or 9) < TAP_STEADY_RATIO]
    live["steady_excerpts"] = len(steady)
    live["steady_level_LR"] = [median([e["slot_" + s]["level_p50"] for e in steady]) for s in "LR"] if steady else None
    return live


def tap_long(watch, args, out, tag):
    """Loss S3 to PC by sequence, over --long seconds with SCO open, apart from the Bluetooth counters."""
    result = {}
    opened = channel_open(watch, result, tag + "-long-open", args.open_timeout)
    result["opened"] = opened
    if opened:
        watch.pump(time.monotonic() + args.settle)
        before = tap_sco_packets(watch)
        summary, _ = tap_phase(watch, out, args.long)
        after = tap_sco_packets(watch)
        result.update({"seconds": args.long, "call": tap_stream_stats(summary, "call"), "mic": tap_stream_stats(summary, "mic"),
                       "pins_ok": (summary.get("board_status") or {}).get("pins_ok"), "sco_before": before, "sco_after": after})
        if before and after:
            total = after["rx_total"] - before["rx_total"]
            none = after["rx_none"] - before["rx_none"]
            result["bluetooth_rx_none_pct"] = round(100.0 * none / total, 3) if total else None
        for name in ("call.wav", *TAP_ROOM_FILES):
            (out / name).unlink(missing_ok=True)
    channel_close(watch, {}, tag + "-long-close", args.close_timeout)
    return result


def tap_s3_reset(watch, index, args, out, tag):
    """Restart the S3 with SCO open: the LyraT must not notice and the streams must come back by themselves."""
    trial = {"i": index}
    resets, slc_drops = len(watch.resets), watch.slc_drops
    checks = {"opened": channel_open(watch, trial, "%s-s3r%d-open" % (tag, index), args.open_timeout)}
    if checks["opened"]:
        watch.pump(time.monotonic() + args.settle)
        here = out / ("s3-reset-%d" % index)
        here.mkdir(parents=True, exist_ok=True)
        rec = TapRecorder(TAP_SLOTS).start()
        watch.pump(time.monotonic() + 3.0)
        streams_before = rec.received("call"), rec.received("mic")
        w = s3.watch(here / "s3")
        w.pump(time.monotonic() + 8, until=lambda: w.sp is not None)
        t_since = watch.now()
        t0 = time.monotonic()
        s3.restart(w)
        back = None
        while time.monotonic() - t0 < args.restore_timeout:
            watch.pump(time.monotonic() + 0.1)
            w.pump(time.monotonic() + 0.1)
            if len(rec.streams["call"].boots) >= 2 and len(rec.streams["mic"].boots) >= 2:
                back = round(time.monotonic() - t0, 2)
                break
        watch.pump(time.monotonic() + 3.0)
        w.close_port()
        s3_serial = w.finish(0)
        summary = rec.stop().save(here)
        for name in ("call.wav", *TAP_ROOM_FILES):
            (here / name).unlink(missing_ok=True)
        text = lines_since(watch, t_since)
        trial.update({"packets_before": streams_before, "restored_s": back, "call_boots": summary["call"]["boots"],
                      "mic_boots": summary["mic"]["boots"], "s3_resets_seen": s3_serial["board_resets"],
                      "pins_ok": (summary.get("board_status") or {}).get("pins_ok"),
                      "sco_closed_lines": sum(1 for line in text if SCO_CLOSED_LINE in line)})
        checks.update({"streaming_before": min(streams_before) > 0, "streams_restored": back is not None,
                       "pins_inputs_after": trial["pins_ok"] == 1, "sco_stayed_open": trial["sco_closed_lines"] == 0})
    closed, _ = channel_close(watch, trial, "%s-s3r%d-close" % (tag, index), args.close_timeout)
    checks["closed"] = closed
    trial["lyrat_resets"] = len(watch.resets) - resets
    trial["slc_drops"] = watch.slc_drops - slc_drops
    checks["lyrat_resets_zero"] = trial["lyrat_resets"] == 0
    checks["slc_drops_zero"] = trial["slc_drops"] == 0
    trial["checks"] = checks
    trial["pass"] = all(checks.values())
    return trial


def stage_tap(args):
    keep_awake()
    out = run_dir("tap", args.label)
    start = datetime.now()
    t0 = time.monotonic()
    if SCO_APP not in adb("shell", "pm", "path", SCO_APP):
        return audio_no_data(args, out, "probe app is not installed, run android/build_apk.py --install")
    try:
        before = phone.status()
        bonds_before = phone.bond_list()
    except phone.PhoneError as e:
        return audio_no_data(args, out, str(e))
    if before["keyguard"]:
        return audio_no_data(args, out, "phone keyguard is showing")
    telecom_before = telecom_state()
    if telecom_before is None or telecom_before["calls_now"] or telecom_before["telephony_busy"]:
        return audio_no_data(args, out, "a call is in progress or telecom is unreadable: %s" % telecom_before)
    watch = SerialWatch(args.port, out, False, echo=args.echo)
    watch.pump(time.monotonic() + 15, until=lambda: watch.sp is not None)
    if watch.sp is None:
        watch.finish(0)
        return audio_no_data(args, out, "port not opened")
    app_ready()

    setup = {}
    trials, resets_s3 = [], []
    live, loss = None, None
    try:
        # Volume first: nothing of the phone's tones may reach the room.
        setup = {"vol_0": board_cmd(watch, "vol 0", "volume = 0")}
        print(json.dumps({"setup": setup}), flush=True)
        if all(setup.values()):
            wav = out / AUDIO_WAV
            make_tone_wav(wav)
            tag = datetime.now().strftime("%H%M%S")
            app_command("status", tag + "-start")
            wait_app(watch, tag + "-start", lambda s: True, 10.0)
            adb("push", str(wav), SCO_DIR + "/" + AUDIO_WAV, timeout=30.0)
            for i in range(1, args.n + 1):
                trial = tap_trial(watch, i, args, out, tag)
                trials.append(trial)
                print(json.dumps({"trial": i, "pass": trial["pass"], "checks": trial["checks"],
                                  "tones": (trial.get("tone") or {}).get("call_tones", {}).get("L")}), flush=True)
                write_json(out / "partial.json", {"trials": trials})
                watch.pump(time.monotonic() + 2.0)
            live = tap_liveness(watch, out / "liveness")
            if args.long:
                loss = tap_long(watch, args, out / "long", tag)
                print(json.dumps({"long": loss}), flush=True)
            for i in range(1, args.s3_resets + 1):
                resets_s3.append(tap_s3_reset(watch, i, args, out, tag))
                print(json.dumps({"s3_reset": resets_s3[-1]}), flush=True)
                watch.pump(time.monotonic() + 2.0)
    finally:
        # A reset brings back the volume, gain and DSP of a normal boot.
        ups = watch.slc_ups
        resets_before = len(watch.resets)
        adb("shell", "am", "force-stop", SCO_APP)
        watch.pulse_reset()
        slc_after_reset = wait_slc(watch, ups, args.slc_timeout)
        tone_end = board_tone(watch)
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
    checks = {
        "setup": bool(setup) and all(setup.values()),
        "trials_all_pass": passed == args.n,
        "mic_room_sound_seen": bool(live and live["room_sound_seen"]),
        "mic_level_follows_gain": bool(live and live["level_follows_gain"]),
        "tone_off_at_end": tone_end == 0,
        "slc_back_after_final_reset": slc_after_reset is not None,
        "no_call_during_round": bool(telecom_after) and telecom_after["calls_now"] == 0
                                and telecom_after["telephony_busy"] == 0
                                and telecom_after["historical_lines"] == telecom_before["historical_lines"],
        "other_bonds_unchanged": bonds_after is not None and phone.others(bonds_before) == phone.others(bonds_after),
        "phone_hfp_connected_at_end": bool(after and after["hfp_connected"]),
        "lyrat_resets_only_final": summary["board_resets"] == resets_before + 1 and resets_before == 0,
        "panics_zero": summary["panics"] == 0,
        "slc_drops_zero": summary["slc_drops"] == 0,
        "port_drops_zero": summary["port_drops"] == 0,
        "usb_removals_zero": removals == 0,
    }
    if args.long:
        checks["long_loss_within_limit"] = bool(loss and loss.get("call") and loss.get("mic")
                                                and loss["call"]["loss_pct"] is not None
                                                and loss["mic"]["loss_pct"] is not None
                                                and loss["call"]["loss_pct"] <= args.max_loss
                                                and loss["mic"]["loss_pct"] <= args.max_loss
                                                and loss["call"]["ovf"] == 0 and loss["mic"]["ovf"] == 0)
    if args.s3_resets:
        checks["s3_resets_all_pass"] = len(resets_s3) == args.s3_resets and all(t["pass"] for t in resets_s3)
    verdict = "INVALID_WINDOW" if standby != 0 else ("PASS" if all(checks.values()) else "FAIL")
    result = {"stage": "tap", "label": args.label, "power": args.power, "verdict": verdict, "checks": checks,
              "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
              "seconds": round(time.monotonic() - t0, 1), "dir": str(out),
              "planned": {"trials": args.n, "min_snr_db": args.min_snr, "max_shift_pct": args.max_shift,
                          "max_impaired_pct": args.max_impaired, "max_loss_pct": args.max_loss, "long_s": args.long,
                          "s3_resets": args.s3_resets, "tones": AUDIO_TONES},
              "not_checked": "ASDOUT tones and SNR: no digital source reaches this line and no sound is played (R15). "
                             "Liveness by room sound is not a measure of quality.",
              "trials_passed": passed, "setup": setup, "liveness": live, "long": loss, "s3_resets": resets_s3,
              "tone_end": tone_end, "slc_after_final_reset_s": slc_after_reset,
              "telecom_before": telecom_before, "telecom_after": telecom_after, "phone_before": before,
              "phone_after": after, "pc_standby_entries": standby, "usb_removals": removals, "trials": trials,
              "serial": summary}
    write_json(out / "round.json", result)
    with (RUNS_ROOT / "rounds.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps({k: result[k] for k in ("stage", "label", "power", "verdict", "start", "seconds",
                                                           "trials_passed", "usb_removals", "dir")}, ensure_ascii=False)) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("trials", "phone_before", "phone_after")},
                     ensure_ascii=False, indent=2))
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


WIFI_SAVED = RUNS_ROOT / "phone_wifi_saved.json"
WIFI_WAIT_S = 45.0
# 1800 Hz is not a whole number of samples per period, so a concealed loss shows as a block out of phase; 400 and 1000 Hz
# survive a repeated pitch period almost perfectly and would hide the losses this stage is here to count.
LOSS_TONE = 1800
LOSS_PLAY_CHUNK_S = 300     # an app buffer of this length; whole seconds of the tone, so the next chunk continues the phase
LOSS_PLAY_LEAD_S = 5.0


def phone_wifi():
    """Whether the phone's Wi-Fi is on and connected, and on which band. Names and addresses are never kept."""
    text = adb("shell", "cmd", "wifi", "status")
    if not text:
        return None
    freq = re.search(r"Frequency: (\d+)MHz", text)
    rssi = re.search(r"RSSI: (-?\d+)", text)
    connected = "Wifi is connected" in text
    return {"enabled": "Wifi is enabled" in text, "connected": connected,
            "band_ghz": None if not (connected and freq) else (2.4 if int(freq.group(1)) < 3000 else 5.0),
            "rssi": int(rssi.group(1)) if connected and rssi else None}


def phone_wifi_set(on):
    """Switch and wait until the phone reports it; returns the state reached."""
    adb("shell", "svc", "wifi", "enable" if on else "disable")
    deadline = time.monotonic() + WIFI_WAIT_S
    state = phone_wifi()
    while time.monotonic() < deadline:
        state = phone_wifi()
        if state and state["enabled"] == on and (not on or state["connected"]):
            break
        time.sleep(1.0)
    return state


def phone_wifi_restore():
    """Bring back what the saved file says, if a file is there. True when none is left to restore."""
    if not WIFI_SAVED.exists():
        return True
    saved = json.loads(WIFI_SAVED.read_text(encoding="utf-8"))
    state = phone_wifi_set(saved["enabled"])
    ok = bool(state and state["enabled"] == saved["enabled"] and state["connected"] == saved["connected"])
    if ok:
        WIFI_SAVED.unlink()
    return ok


def loss_pkt_series(watch, seconds, period, on_tick=None):
    """Read the packet counters every period for the given time. Returns [[t, *SCO_PKT_KEYS]] and the 'live' stats."""
    series, stats = [], []
    t_start = time.monotonic()
    end = t_start + seconds
    t0 = since = watch.now()
    while time.monotonic() < end:
        tick = time.monotonic()
        watch.send("pkt")
        watch.pump(tick + period)
        now = watch.now()
        text = [(at, line) for at, line in watch.recent[-40:] if at >= since]
        for at, line in text:
            found = SCO_PKT.search(line)
            if found:
                series.append([round(at - t0, 3), *[int(v) for v in found.groups()]])
            if "SCO stats live" in line:
                stats.append(line[line.index("SCO stats"):])
        since = now
        if on_tick:
            on_tick(time.monotonic() - t_start)
    return series, stats


def loss_series_summary(series):
    """Share of lost frames and how the losses spread in time, from the periodic counter reads."""
    if len(series) < 2:
        return None
    index = {k: i + 1 for i, k in enumerate(SCO_PKT_KEYS)}
    delta = {k: series[-1][i] - series[0][i] for k, i in index.items()}
    per_read = [[b[0], b[index["rx_none"]] - a[index["rx_none"]], b[index["rx_err"]] - a[index["rx_err"]],
                 b[index["rx_total"]] - a[index["rx_total"]]] for a, b in zip(series, series[1:])]
    with_none = [p for p in per_read if p[1]]
    minutes = (series[-1][0] - series[0][0]) / 60.0
    total = delta["rx_total"]
    return {"minutes": round(minutes, 2), "rx_total": total, "rx_none": delta["rx_none"], "rx_err": delta["rx_err"],
            "rx_lost": delta["rx_lost"], "tx_total": delta["tx_total"], "tx_discarded": delta["tx_discarded"],
            "rx_none_pct": round(100.0 * delta["rx_none"] / total, 3) if total else None,
            "rx_err_pct": round(100.0 * delta["rx_err"] / total, 3) if total else None,
            "reads": len(per_read), "reads_with_rx_none": len(with_none),
            "reads_with_rx_none_pct": round(100.0 * len(with_none) / len(per_read), 1),
            "rx_none_per_min": round(delta["rx_none"] / minutes, 2) if minutes else None,
            "rx_none_max_per_read": max(p[1] for p in per_read)}


def loss_events_in_reads(events, series, offset):
    """How many tone failures fall into a read with rx_none: this ties the sound to the counter, or does not."""
    if not events or len(series) < 2:
        return None
    none = 1 + SCO_PKT_KEYS.index("rx_none")
    busy = [(a[0], b[0]) for a, b in zip(series, series[1:]) if b[none] - a[none] > 0]
    hit = 0
    for e in events:
        t = e["t"] + offset
        # One second of slack: the counter is read later than the loss it shows.
        hit += 1 if any(lo - 1.0 <= t <= hi + 1.0 for lo, hi in busy) else 0
    return {"events": len(events), "in_rx_none_reads": hit, "pct": round(100.0 * hit / len(events), 1)}


def loss_judge(path, tones, gaps=()):
    """Failures of a tone recording; the file goes once judged, the numbers are the result."""
    try:
        samples, rate = audio_check.read_wav(path)
    except (OSError, ValueError, EOFError, wave.Error) as e:
        return {"error": str(e)}
    return tone_blocks.analyse(samples, rate, tones, exclude=[tuple(g) for g in gaps])


def loss_window(watch, args, out, tag, seconds, wifi_expected=None):
    """One measured window: both directions carry a tone, each failure gets a time."""
    window = {"tag": tag, "seconds": seconds, "wifi_expected": wifi_expected, "errors": []}
    out.mkdir(parents=True, exist_ok=True)
    resets, slc_drops = len(watch.resets), watch.slc_drops
    window["phone_wifi"] = phone_wifi()
    if wifi_expected is not None and (not window["phone_wifi"] or window["phone_wifi"]["enabled"] != wifi_expected):
        window["errors"].append("phone Wi-Fi is not as planned")
    window["start"] = datetime.now().isoformat(timespec="seconds")
    opened = channel_open(watch, window, tag + "-open", args.open_timeout)
    if opened:
        watch.pump(time.monotonic() + args.settle)
        wav_name = tag + "-up.wav"
        t_rec = watch.now()
        recorder = TapRecorder(("L", "L")).start()
        if not set_tone(watch, 2):
            window["errors"].append("tone 2 not confirmed by the board")
        record_id = tag + "-rec"
        app_command("record", record_id, "--ei", "seconds", str(int(seconds)), "--es", "out", wav_name,
                    "--es", "source", args.source)
        chunk = {"n": 0, "next": 0.0}

        def play_more(elapsed):
            # The app takes the next buffer when the current one ends, with no gap in the track.
            if elapsed >= chunk["next"]:
                chunk["n"] += 1
                chunk["next"] += LOSS_PLAY_CHUNK_S - LOSS_PLAY_LEAD_S
                app_command("play", "%s-play%d" % (tag, chunk["n"]), "--es", "tone", str(LOSS_TONE),
                            "--ei", "seconds", str(LOSS_PLAY_CHUNK_S), "--ei", "level", str(AUDIO_LEVEL))

        play_more(0.0)
        t_series = watch.now()
        series, stats = loss_pkt_series(watch, seconds, args.pkt_period, play_more)
        if not set_tone(watch, 0):
            window["errors"].append("tone 0 not confirmed by the board")
        # Each play chunk replaced the last command id of the status file, so only the record field can tell.
        record = {}
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            record = (app_status() or {}).get("record") or {}
            if record.get("id") == record_id and record.get("state") != "recording":
                break
            watch.pump(time.monotonic() + SCO_POLL_S)
        window["record"] = {k: record.get(k) for k in ("state", "frames", "error")}
        if record.get("id") != record_id or record.get("state") != "done":
            window["errors"].append("record did not finish: %s" % record.get("error"))
        summary = recorder.stop().save(out)
        for name in TAP_ROOM_FILES:
            (out / name).unlink(missing_ok=True)
        window["tap"] = tap_stream_stats(summary, "call")
        window["pins_ok"] = (summary.get("board_status") or {}).get("pins_ok")
        window["sco"] = loss_series_summary(series)
        window["live_stats_last"] = stats[-1] if stats else None
        with (out / "pkt.csv").open("w", encoding="utf-8") as fh:
            fh.write("t,%s\n" % ",".join(SCO_PKT_KEYS))
            fh.writelines(",".join(str(v) for v in row) + "\n" for row in series)
        adb("pull", SCO_DIR + "/" + wav_name, str(out / "phone.wav"), timeout=300.0)
        adb("shell", "rm", "-f", SCO_DIR + "/" + wav_name)
        window["down"] = loss_judge(out / "call.wav", [LOSS_TONE], summary["call"]["gaps"])
        window["up"] = loss_judge(out / "phone.wav", [LOSS_TONE])
        # Time zero of the file is the first S3 packet, time zero of the series is its start.
        offset = t_rec + (summary["call"].get("first_packet_s") or 0.0) - t_series
        window["down_events_vs_counter"] = loss_events_in_reads(window["down"].get("event_list"), series, offset)
        write_json(out / "events.json", {"down_offset_to_pkt_csv_s": round(offset, 3),
                                         "down": window["down"].pop("event_list", None),
                                         "up": window["up"].pop("event_list", None)})
        if not args.keep_wav:
            for name in ("call.wav", "phone.wav"):
                (out / name).unlink(missing_ok=True)
    closed, _ = channel_close(watch, window, tag + "-close", args.close_timeout)
    window["closed"] = closed
    window["board_resets"] = len(watch.resets) - resets
    window["slc_drops"] = watch.slc_drops - slc_drops
    window["complete"] = bool(opened and closed and not window["errors"] and window["board_resets"] == 0
                              and window["slc_drops"] == 0 and window.get("sco") and window["down"].get("blocks")
                              and window["up"].get("blocks") and window["tap"]["ovf"] == 0)
    write_json(out / "window.json", window)
    return window


def loss_brief(window):
    """One line of numbers for the log and for rounds.jsonl."""
    sco, down, up = window.get("sco") or {}, window.get("down") or {}, window.get("up") or {}
    return {"tag": window["tag"], "wifi": (window.get("phone_wifi") or {}).get("enabled"),
            "band": (window.get("phone_wifi") or {}).get("band_ghz"), "complete": window.get("complete"),
            "codec": window.get("codec"), "rx_none_pct": sco.get("rx_none_pct"), "rx_err_pct": sco.get("rx_err_pct"),
            "rx_none_per_min": sco.get("rx_none_per_min"), "tx_discarded": sco.get("tx_discarded"),
            "down_events_per_min": down.get("events_per_min"), "down_impaired_pct": down.get("impaired_pct"),
            "down_worst_s_db": (down.get("snr_per_second") or {}).get("worst_db"),
            "down_under_20": (down.get("snr_per_second") or {}).get("under_min"),
            "up_events_per_min": up.get("events_per_min"), "up_impaired_pct": up.get("impaired_pct"),
            "up_worst_s_db": (up.get("snr_per_second") or {}).get("worst_db"),
            "up_under_20": (up.get("snr_per_second") or {}).get("under_min")}


def stage_btloss(args):
    keep_awake()
    out = run_dir("btloss", args.label)
    start = datetime.now()
    t0 = time.monotonic()
    if SCO_APP not in adb("shell", "pm", "path", SCO_APP):
        return audio_no_data(args, out, "probe app is not installed, run android/build_apk.py --install")
    # A round killed earlier may have left the phone's Wi-Fi off.
    leftover = WIFI_SAVED.exists()
    if not phone_wifi_restore():
        return audio_no_data(args, out, "the phone's Wi-Fi could not be restored from the saved state")
    try:
        before = phone.status()
        bonds_before = phone.bond_list()
    except phone.PhoneError as e:
        return audio_no_data(args, out, str(e))
    if before["keyguard"]:
        return audio_no_data(args, out, "phone keyguard is showing")
    telecom_before = telecom_state()
    if telecom_before is None or telecom_before["calls_now"] or telecom_before["telephony_busy"]:
        return audio_no_data(args, out, "a call is in progress or telecom is unreadable: %s" % telecom_before)
    wifi_before = phone_wifi()
    if wifi_before is None:
        return audio_no_data(args, out, "the phone's Wi-Fi state is unreadable")
    watch = SerialWatch(args.port, out, False, echo=args.echo)
    watch.pump(time.monotonic() + 15, until=lambda: watch.sp is not None)
    if watch.sp is None:
        watch.finish(0)
        return audio_no_data(args, out, "port not opened")
    app_ready()
    tag = datetime.now().strftime("%H%M%S")
    windows = []
    plan = []
    if args.ab:
        for i in range(args.ab):
            plan += [(False, "off%d" % (i + 1)), (True, "on%d" % (i + 1))]
    elif args.phone_wifi != "asis":
        plan = [(args.phone_wifi == "on", args.phone_wifi)]
    else:
        plan = [(None, "asis")]
    wifi_touched = any(want is not None for want, _ in plan)
    vol_0 = False
    wifi_after = None
    restored = None
    tone_end = None
    try:
        if wifi_touched:
            write_json(WIFI_SAVED, {"enabled": wifi_before["enabled"], "connected": wifi_before["connected"]})
        vol_0 = board_cmd(watch, "vol 0", "volume = 0")     # R15; the volume acts behind the bus
        app_command("status", tag + "-start")
        wait_app(watch, tag + "-start", lambda s: True, 10.0)
        for cmd in args.board_cmd or []:
            watch.send(cmd)
            watch.pump(time.monotonic() + 1.0)
        if vol_0:
            for want, name in plan:
                if want is not None:
                    reached = phone_wifi_set(want)
                    print(json.dumps({"phone_wifi": reached, "for": name}), flush=True)
                    watch.pump(time.monotonic() + args.wifi_settle)
                window = loss_window(watch, args, out / name, "%s-%s" % (tag, name), args.seconds, want)
                windows.append(window)
                print(json.dumps(loss_brief(window), ensure_ascii=False), flush=True)
                write_json(out / "partial.json", {"windows": [loss_brief(w) for w in windows]})
                watch.pump(time.monotonic() + args.gap)
    finally:
        if wifi_touched:
            restored = phone_wifi_restore()
        wifi_after = phone_wifi()
        board_cmd(watch, "vol 100", "volume = 100")
        tone_end = board_tone(watch)
        adb("shell", "am", "force-stop", SCO_APP)
    watch.close_port()
    summary = watch.finish(args.seconds)
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
    briefs = [loss_brief(w) for w in windows]

    def side(on):
        chosen = [b for b, w in zip(briefs, windows) if w.get("wifi_expected") == on]
        keys = ("rx_none_pct", "rx_none_per_min", "down_events_per_min", "up_events_per_min", "down_impaired_pct",
                "up_impaired_pct")
        return {"windows": len(chosen), **{k: [b.get(k) for b in chosen] for k in keys}} if chosen else None

    checks = {
        "all_windows_complete": len(windows) == len(plan) and all(w.get("complete") for w in windows),
        "vol_0": vol_0,
        "tone_off_at_end": tone_end == 0,
        "no_call_during_round": bool(telecom_after) and telecom_after["calls_now"] == 0
                                and telecom_after["telephony_busy"] == 0
                                and telecom_after["historical_lines"] == telecom_before["historical_lines"],
        "other_bonds_unchanged": bonds_after is not None and phone.others(bonds_before) == phone.others(bonds_after),
        "phone_hfp_connected_at_end": bool(after and after["hfp_connected"]),
        "phone_wifi_as_found": bool(wifi_after) and wifi_after["enabled"] == wifi_before["enabled"]
                               and wifi_after["connected"] == wifi_before["connected"] and restored is not False,
        "board_resets_zero": summary["board_resets"] == 0,
        "panics_zero": summary["panics"] == 0,
        "slc_drops_zero": summary["slc_drops"] == 0,
        "port_drops_zero": summary["port_drops"] == 0,
        "usb_removals_zero": removals == 0,
    }
    verdict = "INVALID_WINDOW" if standby != 0 else ("PASS" if all(checks.values()) else "FAIL")
    result = {"stage": "btloss", "label": args.label, "power": args.power, "verdict": verdict, "checks": checks,
              "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
              "seconds": round(time.monotonic() - t0, 1), "dir": str(out),
              "planned": {"window_s": args.seconds, "ab_pairs": args.ab, "phone_wifi": args.phone_wifi,
                          "pkt_period_s": args.pkt_period, "tone_hz": LOSS_TONE, "board_cmd": args.board_cmd},
              "leftover_wifi_state_restored": leftover, "phone_wifi_before": wifi_before, "phone_wifi_after": wifi_after,
              "windows": briefs, "wifi_off": side(False), "wifi_on": side(True),
              "telecom_before": telecom_before, "telecom_after": telecom_after, "phone_before": before, "phone_after": after,
              "pc_standby_entries": standby, "usb_removals": removals, "serial": summary}
    write_json(out / "round.json", result)
    with (RUNS_ROOT / "rounds.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps({k: result[k] for k in ("stage", "label", "power", "verdict", "start", "seconds",
                                                           "windows", "usb_removals", "dir")}, ensure_ascii=False)) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("phone_before", "phone_after", "serial")},
                     ensure_ascii=False, indent=2))
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


PLC_WAV = "plc_voice.wav"
PLC_WAV_SECONDS = 9.0
PLC_PLAY_LEAD_S = 1.0           # the recorder starts before the phone's track
PLC_SILENCE_S = 4
FDSTAT = re.compile(r"fdstat injected=(\d+) concealed=(\d+) bad=(\d+) plc=(\d) sig=(\d+) err=(\d+) "
                    r"err_repeat=(\d+) err_next=(\d+)")
FDSTAT_KEYS = ("injected", "concealed", "bad", "plc", "sig", "err", "err_repeat", "err_next")
PLC_BLOCK_S = 0.0075
PLC_TONE_HZ = 1000
PLC_LONG_BURST = 40             # frames: 300 ms, past the point where the concealment has faded out
PLC_LONG_PERIOD = 400
PLC_FADE_END_S = 0.0675         # hold and fade of main/plc.c
PLC_FADE_SLACK_S = 0.03
PLC_QUIET_FRACTION = 0.05       # of the tone's level: what counts as gone
PLC_SILENCE_MAX_RMS = 10.0      # the bus carries a signal at about 10000 rms


def plc_fdstat(watch):
    for _ in range(3):
        t = watch.now()
        watch.send("fdstat")
        if wait_line(watch, t, "fdstat injected=", 3.0) is not None:
            found = next((FDSTAT.search(line) for line in lines_since(watch, t) if FDSTAT.search(line)), None)
            if found:
                return dict(zip(FDSTAT_KEYS, (int(v) for v in found.groups())))
    return None


def plc_quiet_runs(path, level):
    """Lengths in seconds of the stretches below a share of the level, in blocks of one frame."""
    samples, rate = audio_check.read_wav(path)
    size = int(PLC_BLOCK_S * rate)
    runs, run = [], 0
    limit = (PLC_QUIET_FRACTION * level) ** 2
    seen_signal = False
    for at in range(0, len(samples) - size + 1, size):
        block = samples[at:at + size]
        if sum(v * v for v in block) / size < limit:
            run += 1
        else:
            if seen_signal and run:
                runs.append(round(run * PLC_BLOCK_S, 4))
            seen_signal = True
            run = 0
    return runs


def plc_level(path):
    samples, _ = audio_check.read_wav(path)
    return {"rms": round(math.sqrt(sum(v * v for v in samples) / max(len(samples), 1)), 2),
            "peak": max((abs(v) for v in samples), default=0), "samples": len(samples)}


def plc_case(watch, args, out, tag, name, plc, period, burst, signal, ref, zeros=0):
    """One recording of the bus with the plan of frame replacement; signal: voice, tone or None."""
    case = {"name": name, "plc": plc, "period": period, "burst": burst, "signal": signal, "zeros": zeros, "errors": []}
    case_dir = out / name
    case_dir.mkdir(parents=True, exist_ok=True)
    if not board_cmd(watch, "plc %d" % plc, "plc = %d" % plc):
        case["errors"].append("plc %d not confirmed" % plc)
    if not board_cmd(watch, "fdrop %d %d %d" % (period, burst, zeros), "fdrop = %d" % period):
        case["errors"].append("fdrop not confirmed")
    before = plc_fdstat(watch)
    pkt_before = sco_packets(watch)
    recorder = TapRecorder(("L", "L")).start()
    watch.pump(time.monotonic() + PLC_PLAY_LEAD_S)
    seconds = PLC_SILENCE_S
    if signal == "voice":
        seconds = int(PLC_WAV_SECONDS)
        app_command("play", tag + "-play", "--es", "wav", PLC_WAV)
    elif signal == "tone":
        seconds = int(PLC_WAV_SECONDS)
        app_command("play", tag + "-play", "--es", "tone", str(PLC_TONE_HZ), "--ei", "seconds", str(seconds),
                    "--ei", "level", str(AUDIO_LEVEL))
    if signal:
        _, status = wait_app(watch, tag + "-play", lambda s: (s.get("play") or {}).get("id") == tag + "-play", 10.0)
        if not status or not status.get("ok"):
            case["errors"].append("play not accepted: %s" % (status and status.get("error")))
    watch.pump(time.monotonic() + seconds + PLC_PLAY_LEAD_S)
    summary = recorder.stop().save(case_dir)
    (case_dir / "mic.wav").unlink(missing_ok=True)
    after = plc_fdstat(watch)
    pkt_after = sco_packets(watch)
    board_cmd(watch, "fdrop 0", "fdrop = 0")
    case["tap"] = tap_stream_stats(summary, "call")
    case["packets"] = packet_delta(pkt_before, pkt_after)
    if before and after:
        # The injection counters restart with fdrop; the concealment counters run from the opening of the channel.
        stat = {"injected": after["injected"], "concealed": after["concealed"] - before["concealed"],
                "bad": after["bad"] - before["bad"], "sig": after["sig"], "err": after["err"],
                "err_repeat": after["err_repeat"], "err_next": after["err_next"]}
        if stat["sig"] and stat["err"]:
            stat["conceal_snr_db"] = round(10 * math.log10(stat["sig"] / stat["err"]), 2)
        if stat["sig"] and stat["err_repeat"]:
            stat["repeat_snr_db"] = round(10 * math.log10(stat["sig"] / stat["err_repeat"]), 2)
        case["frames"] = stat
    else:
        case["errors"].append("fdstat did not answer")
    wav = case_dir / "call.wav"
    if signal == "voice":
        rec, _ = audio_check.read_wav(wav)
        case["voice"] = voice_check.compare(ref, rec)
    elif signal == "tone":
        case["quiet_runs_s"] = plc_quiet_runs(wav, AUDIO_LEVEL)
    else:
        case["level"] = plc_level(wav)
    if not args.keep_wav:
        wav.unlink(missing_ok=True)
    return case


def plc_case_ok(case):
    """A counter that agrees with the plan: every replaced frame is concealed when concealment is on, none when off."""
    stat = case.get("frames")
    if not stat or case["errors"]:
        return False
    if case["period"] and not stat["injected"]:
        return False
    natural = 0
    if case.get("packets"):
        natural = case["packets"]["rx_err"] + case["packets"]["rx_none"] + case["packets"]["rx_lost"]
    if not case["plc"]:
        return stat["concealed"] == 0
    # Concealed frames are the replaced ones plus the real losses; both fell in the window, a real loss may fall on a
    # replaced frame, and a frame of the counter window may lie just outside the replacement window: two frames of slack.
    if case.get("zeros"):
        # Zeros count as a loss only inside a loud signal: pauses and the starts of syllables stay out by design.
        return stat["injected"] * 0.25 <= stat["concealed"] <= stat["injected"] + natural + 2
    return stat["injected"] - 2 <= stat["concealed"] <= stat["injected"] + natural + 2


def plc_summary(cases):
    by_name = {}
    for c in cases:
        by_name.setdefault(c["name"], []).append(c)

    def values(name, key, sub):
        return [(c.get(sub) or {}).get(key) for c in by_name.get(name, []) if (c.get(sub) or {}).get(key) is not None]

    return {name: {"conceal_snr_db": values(name, "conceal_snr_db", "frames"),
                   "repeat_snr_db": values(name, "repeat_snr_db", "frames"),
                   "injected": values(name, "injected", "frames"), "concealed": values(name, "concealed", "frames"),
                   "voice_snr_db": values(name, "snr_db", "voice"),
                   "voice_p02_db": values(name, "block_snr_p02_db", "voice"),
                   "voice_under_0db_pct": values(name, "blocks_under_0db_pct", "voice"),
                   "zero_runs": values(name, "zero_runs", "voice"),
                   "level_rms": values(name, "rms", "level"), "level_peak": values(name, "peak", "level"),
                   "quiet_runs_s": [c.get("quiet_runs_s") for c in by_name[name] if "quiet_runs_s" in c]}
            for name in by_name}


def stage_plc(args):
    keep_awake()
    out = run_dir("plc", args.label)
    start = datetime.now()
    t0 = time.monotonic()
    if SCO_APP not in adb("shell", "pm", "path", SCO_APP):
        return audio_no_data(args, out, "probe app is not installed, run android/build_apk.py --install")
    try:
        before = phone.status()
        bonds_before = phone.bond_list()
    except phone.PhoneError as e:
        return audio_no_data(args, out, str(e))
    telecom_before = telecom_state()
    if before["keyguard"] or telecom_before is None or telecom_before["calls_now"] or telecom_before["telephony_busy"]:
        return audio_no_data(args, out, "phone not usable: %s %s" % (before["keyguard"], telecom_before))
    watch = SerialWatch(args.port, out, False, echo=args.echo)
    watch.pump(time.monotonic() + 15, until=lambda: watch.sp is not None)
    if watch.sp is None:
        watch.finish(0)
        return audio_no_data(args, out, "port not opened")
    app_ready()
    # The same start as audio: a known state and a live link before anything is asked of the channel.
    ups = watch.slc_ups
    watch.pulse_reset()
    if wait_slc(watch, ups, args.slc_timeout) is None:
        watch.close_port()
        watch.finish(0)
        return audio_no_data(args, out, "no SLC after reset")
    boot_resets = len(watch.resets)
    if plc_fdstat(watch) is None:
        watch.close_port()
        watch.finish(0)
        return audio_no_data(args, out, "the board has no fdrop command: flash a build with the variant fdrop")
    if not board_cmd(watch, "vol 0", "volume = 0"):     # R15; the volume acts behind the bus
        watch.close_port()
        watch.finish(0)
        return audio_no_data(args, out, "the board did not confirm vol 0")

    ref = voice_check.make(PLC_WAV_SECONDS, AUDIO_LEVEL)
    voice_check.write_wav(out / PLC_WAV, ref)
    tag = datetime.now().strftime("%H%M%S")
    app_command("status", tag + "-start")
    wait_app(watch, tag + "-start", lambda s: True, 10.0)
    adb("push", str(out / PLC_WAV), SCO_DIR + "/" + PLC_WAV, timeout=30.0)

    # name, plc, period, burst, signal. The order alternates so that a slow change of the link falls on both sides.
    plan = [("voice_clean", 1, 0, 1, "voice", 0),
            ("voice_on", 1, args.period, 1, "voice", 0), ("voice_off", 0, args.period, 1, "voice", 0),
            ("burst_on", 1, args.period * 3, 3, "voice", 0), ("burst_off", 0, args.period * 3, 3, "voice", 0),
            ("silence_on", 1, args.period, 1, None, 0), ("silence_off", 0, args.period, 1, None, 0),
            ("fade_on", 1, PLC_LONG_PERIOD, PLC_LONG_BURST, "tone", 0)]
    if args.codec == "CVSD":    # zeros with a good status: a loss only the content shows
        plan += [("zeros_on", 1, args.period, 1, "voice", 1), ("zeros_off", 0, args.period, 1, "voice", 1)]
    cases = []
    codec = None
    opened = False
    for i in range(1, args.n + 1):
        trial = {}
        opened = channel_open(watch, trial, "%s-%d-open" % (tag, i), args.open_timeout)
        codec = trial.get("codec")
        if opened and codec == args.codec:
            watch.pump(time.monotonic() + args.settle)
            for name, plc, period, burst, signal, zeros in plan:
                case = plc_case(watch, args, out / ("trial-%02d" % i), "%s-%d-%s" % (tag, i, name), name, plc, period,
                                burst, signal, ref, zeros)
                case["trial"] = i
                cases.append(case)
                print(json.dumps({k: case.get(k) for k in ("trial", "name", "frames", "voice", "level", "quiet_runs_s",
                                                           "errors")}, ensure_ascii=False), flush=True)
        board_cmd(watch, "plc 1", "plc = 1")
        channel_close(watch, trial, "%s-%d-close" % (tag, i), args.close_timeout)
        watch.pump(time.monotonic() + args.gap)
        if not opened or codec != args.codec:
            break
    board_cmd(watch, "vol 100", "volume = 100")
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
    table = plc_summary(cases)

    def side(name, key):
        return table.get(name, {}).get(key) or []

    def pairs_better(on, off, key):
        a, b = side(on, key), side(off, key)
        return bool(a) and len(a) == len(b) and all(x > y for x, y in zip(a, b))

    fade = [r for runs in side("fade_on", "quiet_runs_s") for r in runs if r > 0.1]
    fade_expected = PLC_LONG_BURST * PLC_BLOCK_S - PLC_FADE_END_S
    checks = {
        "codec_expected": codec == args.codec,
        "all_cases_run": len(cases) == len(plan) * args.n,
        "counters_match_plan": bool(cases) and all(plc_case_ok(c) for c in cases),
        # The same plan without concealment must be worse, on the bus and against the reference.
        "voice_off_worse": pairs_better("voice_on", "voice_off", "voice_snr_db"),
        "burst_off_worse": pairs_better("burst_on", "burst_off", "voice_snr_db"),
        "voice_p02_off_worse": pairs_better("voice_on", "voice_off", "voice_p02_db"),
        # Without concealment each replaced frame is digital silence on the bus; with it none is left.
        "zero_runs_seen_off": bool(side("voice_off", "zero_runs")) and all(v > 0 for v in side("voice_off", "zero_runs")),
        "zero_runs_gone_on": bool(side("voice_on", "zero_runs")) and all(v == 0 for v in side("voice_on", "zero_runs")),
        **({"zeros_off_worse": pairs_better("zeros_on", "zeros_off", "voice_snr_db"),
            # A zero frame after a quiet one is left alone by design, so a rare one remains on a fall of the signal.
            "zero_frames_gone_on": bool(side("zeros_on", "zero_runs")) and all(v <= 1 for v in side("zeros_on", "zero_runs")),
            "zero_frames_seen_off": bool(side("zeros_off", "zero_runs")) and all(v > 0 for v in side("zeros_off", "zero_runs"))}
           if args.codec == "CVSD" else {}),
        # On the frames it replaced, the concealment must beat the plain repeat of the last block that goal 044 refuted.
        "better_than_repeat": bool(side("voice_on", "conceal_snr_db")) and all(
            a > b for a, b in zip(side("voice_on", "conceal_snr_db"), side("voice_on", "repeat_snr_db"))),
        # True silence stays silent: the phone's own rare clicks differ from one recording to the next, so two levels
        # cannot be compared; the level with concealment stays 60 dB under the signal, whichever frames it replaced.
        "silence_untouched": bool(side("silence_on", "level_rms")) and all(
            a <= PLC_SILENCE_MAX_RMS for a in side("silence_on", "level_rms")),
        # A long loss ends in silence, in the time the firmware says.
        "long_loss_fades": bool(fade) and abs(sorted(fade)[len(fade) // 2] - fade_expected) <= PLC_FADE_SLACK_S,
        "no_call_during_round": bool(telecom_after) and telecom_after["calls_now"] == 0
                                and telecom_after["telephony_busy"] == 0
                                and telecom_after["historical_lines"] == telecom_before["historical_lines"],
        "other_bonds_unchanged": bonds_after is not None and phone.others(bonds_before) == phone.others(bonds_after),
        "board_resets_only_start": summary["board_resets"] == boot_resets,
        "panics_zero": summary["panics"] == 0,
        "slc_drops_zero": summary["slc_drops"] == 0,
        "port_drops_zero": summary["port_drops"] == 0,
        "usb_removals_zero": removals == 0,
    }
    verdict = "INVALID_WINDOW" if standby != 0 else ("PASS" if all(checks.values()) else "FAIL")
    result = {"stage": "plc", "label": args.label, "power": args.power, "verdict": verdict, "checks": checks,
              "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
              "seconds": round(time.monotonic() - t0, 1), "dir": str(out),
              "planned": {"n": args.n, "codec": args.codec, "period": args.period, "long_burst": PLC_LONG_BURST},
              "codec": codec, "table": table, "fade_expected_s": round(fade_expected, 4), "cases": cases,
              "telecom_before": telecom_before, "telecom_after": telecom_after,
              "pc_standby_entries": standby, "usb_removals": removals, "serial": summary}
    write_json(out / "round.json", result)
    with (RUNS_ROOT / "rounds.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps({k: result[k] for k in ("stage", "label", "power", "verdict", "start", "seconds", "table",
                                                           "usb_removals", "dir")}, ensure_ascii=False)) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("cases", "serial")}, ensure_ascii=False, indent=2))
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


UART_RECORD_S = 12
UART_RESET_AT_S = 3.0           # into the recording, so the sound is measured before, during and after
UART_BACK_S = 40.0
UART_STAT_TAIL = ("bad_hdr", "bad_crc", "trunc", "noise")
UART_BURST_BYTES = 256


def uart_delta(before, after, keys=s3_uart.USTAT_KEYS):
    return {k: after[k] - before[k] for k in keys if k not in ("baud", "rx_level")} if before and after else None


def uart_wait_command(channel, text, timeout):
    """Seconds until the command is answered; a board in its restart answers nothing or says its TX is off."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        got = channel.command(text, timeout=0.3, retries=1)
        if got["status"] == s3_uart.ST_OK:
            return round(time.monotonic() - t0, 2), got
        if channel.board is None or got["status"] is None:
            channel.find(2.0)
        time.sleep(0.2)
    return None, None


def uart_check_status(text, want):
    return ("tone=%d" % want["tone"] in text and "plc=%d" % want["plc"] in text and "hpf     = %d" % want["hpf"] in text
            and "SLC     = up" in text and "peers=" in text and not re.search(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", text))


def uart_percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, int(len(values) * fraction))] if values else None


def uart_commands(watch, channel, n):
    """n commands of all kinds, each reply checked against the state the earlier replies must have caused."""
    first = channel.command("status")
    hpf = re.search(r"hpf     = (\d)", first["text"] or "")
    if not hpf:
        return {"error": "no status at the start", "first": first}
    want = {"tone": 0, "plc": 1, "hpf": int(hpf.group(1))}
    start_hpf = want["hpf"]
    cycle = ("status", "tone 1", "status", "tone 0", "plc 0", "status", "plc 1", "hpf", "status", "nosuch", "ustat",
             "tone 2", "status", "tone 0", "pkt")
    usable = n - (n % 2)    # hpf returns to where it was only after an even number of cycles at most; repaired below
    witness_before = s3_uart.console_ustat(watch)
    s3_before = channel.query()
    retries_before, attempts_before = channel.retries, channel.attempts
    wrong, failed, millis, histogram, witness = [], 0, [], {}, []
    for i in range(usable):
        text = cycle[i % len(cycle)]
        got = channel.command(text)
        histogram[got["attempts"]] = histogram.get(got["attempts"], 0) + 1
        if got["status"] is None:
            failed += 1
            continue
        millis.append(got["ms"])
        reply = got["text"]
        if text.startswith("tone "):
            want["tone"] = int(text[5:])
            ok = got["status"] == s3_uart.ST_OK and "tone = %d" % want["tone"] in reply
        elif text.startswith("plc "):
            want["plc"] = int(text[4:])
            ok = got["status"] == s3_uart.ST_OK and "plc = %d" % want["plc"] in reply
        elif text == "hpf":
            want["hpf"] = 1 - want["hpf"]
            ok = got["status"] == s3_uart.ST_OK and "hpf = %d" % want["hpf"] in reply
        elif text == "status":
            ok = got["status"] == s3_uart.ST_OK and uart_check_status(reply, want)
        elif text == "nosuch":
            ok = got["status"] == s3_uart.ST_UNKNOWN and "Unknown" in reply
        elif text == "ustat":
            ok = got["status"] == s3_uart.ST_OK and s3_uart.parse_ustat(reply) is not None
        else:
            ok = got["status"] == s3_uart.ST_OK
        if not ok:
            wrong.append({"i": i, "cmd": text, "status": got["status"], "reply": redact(reply)[:160]})
        if i % 200 == 199:
            # The board's own console has the last word on the state, not the channel being tested.
            seen = board_tone(watch)
            witness.append({"i": i, "tone_console": seen, "tone_channel": want["tone"]})
    if want["hpf"] != start_hpf:
        channel.command("hpf")
        want["hpf"] = start_hpf
    if want["tone"]:
        channel.command("tone 0")
    if want["plc"] != 1:
        channel.command("plc 1")
    witness_after = s3_uart.console_ustat(watch)
    s3_after = channel.query()
    delta = uart_delta(witness_before, witness_after)
    return {"planned": usable, "failed_after_retry": failed, "wrong_replies": len(wrong), "wrong": wrong[:10],
            "attempts_histogram": histogram, "retries": channel.retries - retries_before,
            "retry_pct": round(100.0 * (channel.retries - retries_before) / max(1, channel.attempts - attempts_before), 3),
            "ms": {"p50": uart_percentile(millis, 0.5), "p95": uart_percentile(millis, 0.95),
                   "max": max(millis) if millis else None},
            "witness": witness, "witness_ok": all(w["tone_console"] == w["tone_channel"] for w in witness),
            "lyrat": delta, "lyrat_cmds_extra": (delta["cmds"] - usable) if delta else None,
            "s3": {k: s3_after[k] - s3_before[k] for k in UART_STAT_TAIL + ("frames", "sent", "replies")}
            if s3_before and s3_after else None}


def uart_raw_case(watch, channel, name, data, expect, then=None, settle=0.15):
    """Raw bytes onto the line toward the LyraT. expect: the change of its counters that must be seen, others stay at 0."""
    before = s3_uart.console_ustat(watch)
    took = channel.raw(data)
    time.sleep(settle)
    if then:
        then()
    after = s3_uart.console_ustat(watch)
    delta = uart_delta(before, after)
    case = {"name": name, "took": took, "delta": delta, "expect": expect}
    # A frame that stops may end by the gap, or swallow the next heartbeat ack and end by a check: either way it is gone.
    if delta:
        delta["dropped"] = delta["trunc"] + delta["bad_crc"] + delta["bad_hdr"]
    case["pass"] = bool(delta) and took == len(data) and all(
        (delta[k] >= v[0] if isinstance(v, tuple) else delta[k] == v) for k, v in expect.items())
    return case


def uart_parser(watch, channel):
    """What the line can carry at worst. Each case has a control: a good frame by the same route must act."""
    rng = random.Random()
    cases = []
    tone_start = board_tone(watch)
    seq = lambda: rng.randrange(1, 60000)  # noqa: E731
    command = s3_uart.frame(s3_uart.T_CMD, seq(), b"tone 1", flip_crc=True)
    cases.append(uart_raw_case(watch, channel, "bad_crc", command, {"bad_crc": 1, "cmds": 0, "replies": 0}))
    tone_after_bad_crc = board_tone(watch)
    cases.append(uart_raw_case(watch, channel, "good_frame_control", s3_uart.frame(s3_uart.T_CMD, seq(), b"tone 1"),
                               {"cmds": 1, "replies": 1, "bad_crc": 0}))
    tone_after_good = board_tone(watch)
    channel.command("tone 0")
    cases.append(uart_raw_case(watch, channel, "bad_header", s3_uart.frame(s3_uart.T_CMD, seq(), b"tone 1", flip_header=True),
                               {"bad_hdr": 1, "cmds": 0}))
    head = bytes([s3_uart.T_CMD, 1, 0, 0xFF, 0x7F])
    oversize = bytes([0xA5, 0x5A]) + head + bytes([head[0] ^ head[1] ^ head[2] ^ head[3] ^ head[4]])
    cases.append(uart_raw_case(watch, channel, "length_too_big", oversize, {"bad_hdr": 1, "cmds": 0}))
    # A frame that stops: the next good command must still get through, at worst with a repeat.
    full = s3_uart.frame(s3_uart.T_CMD, seq(), b"status")
    cases.append(uart_raw_case(watch, channel, "truncated", full[:len(full) // 2], {"dropped": (1,), "cmds": 1},
                               then=lambda: (time.sleep(0.25), channel.command("pkt"))))
    head = bytes([s3_uart.T_CMD, 2, 0, 0x00, 0x04])
    longer = bytes([0xA5, 0x5A]) + head + bytes([head[0] ^ head[1] ^ head[2] ^ head[3] ^ head[4]])
    cases.append(uart_raw_case(watch, channel, "announced_1024_nothing_came", longer, {"dropped": (1,), "cmds": 1},
                               then=lambda: (time.sleep(0.25), channel.command("pkt"))))
    cases.append(uart_raw_case(watch, channel, "wrong_type", s3_uart.frame(s3_uart.T_REPLY, seq(), b"x"),
                               {"bad_type": 1, "cmds": 0}))
    cases.append(uart_raw_case(watch, channel, "not_text", s3_uart.frame(s3_uart.T_CMD, seq(), bytes([1, 2, 200])),
                               {"cmds": 0, "replies": 1}))
    # The same seq twice must run once: without this a repeat after a lost reply would flip a toggle back.
    again = s3_uart.frame(s3_uart.T_CMD, seq(), b"hpf")
    cases.append(uart_raw_case(watch, channel, "repeat_runs_once", again + again, {"cmds": 1, "dups": 1}))
    hpf_after_repeat = channel.command("status")
    channel.command("hpf")
    for burst in range(5):
        noise = bytearray(rng.randrange(256) for _ in range(UART_BURST_BYTES))
        for at in range(0, UART_BURST_BYTES - 2, 37):    # many false starts of a frame
            noise[at:at + 2] = b"\xa5\x5a"
        case = uart_raw_case(watch, channel, "noise_%d" % burst, bytes(noise), {"noise": (1,), "cmds": 1},
                             then=lambda: (time.sleep(0.25), channel.command("pkt")))
        cases.append(case)
    after_noise = channel.command("status")
    end_tone = board_tone(watch)
    # The other direction, as at the start of the LyraT: noise toward the S3, which must count it and stay up.
    s3_before = channel.query()
    garbage_sent = board_cmd(watch, "ugarb %d" % (UART_BURST_BYTES * 4), "ugarb = %d" % (UART_BURST_BYTES * 4))
    watch.pump(time.monotonic() + 0.5)
    after_garbage = channel.command("status")
    s3_after = channel.query()
    s3_case = {"sent": garbage_sent, "same_boot": bool(s3_before and s3_after and s3_before["boot"] == s3_after["boot"]),
               "delta": {k: s3_after[k] - s3_before[k] for k in UART_STAT_TAIL + ("rx_bytes", "frames")} if s3_before and s3_after else None,
               "answered": after_garbage["status"] == s3_uart.ST_OK}
    s3_case["pass"] = bool(garbage_sent and s3_case["same_boot"] and s3_case["answered"] and s3_case["delta"]
                           and s3_case["delta"]["noise"] > 0)
    return {"cases": cases, "s3_noise": s3_case, "tone_start": tone_start, "tone_after_bad_crc": tone_after_bad_crc,
            "tone_after_good": tone_after_good, "tone_end": end_tone, "hpf_reply_ok": hpf_after_repeat["status"] == 0,
            "alive_after_noise": after_noise["status"] == s3_uart.ST_OK,
            "pass": all(c["pass"] for c in cases) and tone_start == 0 and tone_after_bad_crc == 0
            and tone_after_good == 1 and end_tone == 0 and after_noise["status"] == s3_uart.ST_OK and s3_case["pass"]}


def uart_lyrat_reset(watch, channel, index, args):
    """LyraT restarts: its boot garbage reaches the S3's RX. The S3 must stay up and the channel must come back alone."""
    trial = {"i": index}
    before = channel.query()
    resets = len(watch.resets)
    ups = watch.slc_ups
    watch.pulse_reset()
    slc = wait_slc(watch, ups, args.slc_timeout)
    back, reply = uart_wait_command(channel, "status", UART_BACK_S)
    after = channel.query()
    trial.update({"slc_s": slc, "command_back_s": back, "s3_boot_same": bool(before and after and before["boot"] == after["boot"]),
                  "s3_garbage": {k: after[k] - before[k] for k in UART_STAT_TAIL} if before and after else None,
                  "pins_ok": after and after["pins_ok"], "lyrat_up_s": after and after["lyrat_up_s"],
                  "lyrat_resets": len(watch.resets) - resets})
    trial["pass"] = bool(slc is not None and back is not None and trial["s3_boot_same"] and trial["pins_ok"] == 1
                         and trial["lyrat_resets"] == 1 and trial["lyrat_up_s"] is not None
                         and "tone=0" in (reply or {}).get("text", ""))
    return trial


def uart_up_record(watch, args, tag, name, seconds, out, during=None):
    """The board's tone to the phone: a measure of the sound that does not depend on the S3 at all."""
    out.mkdir(parents=True, exist_ok=True)
    wav_name = "%s-%s.wav" % (tag, name)
    result = {"errors": []}
    before = sco_packets(watch)
    if not set_tone(watch, 1):
        result["errors"].append("tone 1 not confirmed")
    record_id = "%s-%s-rec" % (tag, name)
    app_command("record", record_id, "--ei", "seconds", str(seconds), "--es", "out", wav_name, "--es", "source", args.source)
    if during:
        during(result)
    took, status = wait_app(watch, record_id, lambda s: (s.get("record") or {}).get("id") == record_id
                            and (s.get("record") or {}).get("state") != "recording", seconds + 25.0)
    record = (status or {}).get("record") or {}
    if took is None or record.get("state") != "done":
        result["errors"].append("record did not finish: %s" % (status and status.get("error")))
    if not set_tone(watch, 0):
        result["errors"].append("tone 0 not confirmed")
    adb("pull", SCO_DIR + "/" + wav_name, str(out / "phone.wav"), timeout=30.0)
    adb("shell", "rm", "-f", SCO_DIR + "/" + wav_name)
    result["sound"] = judge_wav(out / "phone.wav", args, "strict")
    result["sco_delta"] = packet_delta(before, sco_packets(watch))
    return result


def uart_sound_brief(sound):
    return {str(k): {a: v.get(a) for a in ("pass", "snr_db", "shift_pct")} for k, v in (sound.get("tones") or {}).items()}


def uart_sco_trial(watch, channel, args, out, tag, name, action):
    """SCO open and the sound measured while the action runs. The LyraT must not notice: no reset, no SLC or SCO drop."""
    trial = {"name": name}
    resets, slc_drops, t_start = len(watch.resets), watch.slc_drops, watch.now()
    checks = {"opened": channel_open(watch, trial, "%s-%s-open" % (tag, name), args.open_timeout),
              "codec_expected": trial.get("codec") == args.codec}
    if checks["opened"]:
        watch.pump(time.monotonic() + args.settle)
        before = s3_uart.console_ustat(watch)
        record = uart_up_record(watch, args, tag, name, UART_RECORD_S, out / name, lambda r: action(r, trial, checks))
        after = s3_uart.console_ustat(watch)
        trial["lyrat_uart"] = uart_delta(before, after)
        trial["sound"] = uart_sound_brief(record["sound"])
        trial["sco_delta"] = record["sco_delta"]
        trial["sound_errors"] = record["errors"]
        checks["sound_strict"] = bool(record["sound"].get("pass")) and not record["errors"]
        # Line low must cost a break or a few bytes, not a storm of interrupts on the audio core.
        rate = trial["lyrat_uart"]["rx_bytes"] / max(1.0, UART_RECORD_S) if trial["lyrat_uart"] else None
        trial["rx_bytes_per_s"] = round(rate, 1) if rate is not None else None
        checks["no_rx_storm"] = rate is not None and rate <= args.max_rx_rate
    closed, _ = channel_close(watch, trial, "%s-%s-close" % (tag, name), args.close_timeout)
    checks["closed"] = closed
    trial["sco_closed_lines_during"] = sum(1 for line in lines_since(watch, t_start) if SCO_CLOSED_LINE in line) - 1
    checks["sco_stayed_open"] = trial["sco_closed_lines_during"] == 0
    trial["lyrat_resets"] = len(watch.resets) - resets
    trial["slc_drops"] = watch.slc_drops - slc_drops
    checks["lyrat_resets_zero"] = trial["lyrat_resets"] == 0
    checks["slc_drops_zero"] = trial["slc_drops"] == 0
    trial["checks"] = checks
    trial["pass"] = all(checks.values())
    return trial


def uart_s3_restart_action(watch, channel, out):
    """Restart the S3 in the middle of a recording; the channel must come back with nothing asked of it."""
    def run(result, trial, checks):
        before = channel.query()
        w = s3.watch(out / "s3-console")
        w.pump(time.monotonic() + 3, until=lambda: w.sp is not None)
        watch.pump(time.monotonic() + UART_RESET_AT_S)
        t0 = time.monotonic()
        s3.restart(w)
        w.pump(time.monotonic() + 0.3)
        watch.pump(time.monotonic() + 0.5)
        w.close_port()
        s3_serial = w.finish(0)
        channel.board = None
        back = None
        while time.monotonic() - t0 < UART_BACK_S:
            watch.pump(time.monotonic() + 0.3)
            if channel.find(0.6) and channel.query() and channel.query()["boot"] != (before or {}).get("boot"):
                got = channel.command("status", timeout=0.3, retries=1)
                if got["status"] == s3_uart.ST_OK:
                    back = round(time.monotonic() - t0, 2)
                    break
        after = channel.query()
        trial.update({"s3_back_s": back, "s3_boot_changed": bool(before and after and before["boot"] != after["boot"]),
                      "s3_pins_ok": after and after["pins_ok"], "s3_tx_on_after": after and after["tx_on"],
                      "s3_console_resets": s3_serial["board_resets"]})
        checks["s3_restarted"] = trial["s3_boot_changed"]
        checks["channel_back_alone"] = back is not None
        checks["s3_pins_ok"] = trial["s3_pins_ok"] == 1
    return run


def uart_line_low_action(watch, channel, args):
    """The S3 stops driving its TX and the LyraT's pull-down holds the line low, as with a dead S3, for the whole recording."""
    def run(result, trial, checks):
        channel.set_tx(0)
        time.sleep(0.1)
        state = channel.query()
        refused = channel.command("tone 1", timeout=0.3, retries=1)
        witness = s3_uart.console_ustat(watch)
        trial.update({"s3_tx_on": state and state["tx_on"], "refused_status": refused["status"],
                      "lyrat_rx_level_low": witness and witness["rx_level"]})
        checks["tx_really_off"] = trial["s3_tx_on"] == 0
        # The control: with the TX off the command cannot arrive, so the channel is really this wire.
        checks["command_refused_with_tx_off"] = refused["status"] == s3_uart.ST_NO_TX
        checks["lyrat_rx_held_low"] = trial["lyrat_rx_level_low"] == 0
        watch.pump(time.monotonic() + args.line_low)
        answered = s3_uart.console_ustat(watch)
        checks["console_alive_with_line_low"] = answered is not None
        channel.set_tx("A")
        back, _ = uart_wait_command(channel, "status", 10.0)
        trial["command_back_s"] = back
        checks["channel_back"] = back is not None
    return run


def uart_no_data(args, out, error):
    result = {"stage": "uart", "label": args.label, "power": args.power, "verdict": "NO_DATA", "error": redact(error), "dir": str(out)}
    write_json(out / "round.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2


def stage_uart(args):
    keep_awake()
    out = run_dir("uart", args.label)
    start = datetime.now()
    t0 = time.monotonic()
    if SCO_APP not in adb("shell", "pm", "path", SCO_APP):
        return uart_no_data(args, out, "probe app is not installed, run android/build_apk.py --install")
    try:
        before = phone.status()
        bonds_before = phone.bond_list()
    except phone.PhoneError as e:
        return uart_no_data(args, out, str(e))
    if before["keyguard"]:
        return uart_no_data(args, out, "phone keyguard is showing")
    telecom_before = telecom_state()
    if telecom_before is None or telecom_before["calls_now"] or telecom_before["telephony_busy"]:
        return uart_no_data(args, out, "a call is in progress or telecom is unreadable: %s" % telecom_before)
    channel = s3_uart.Channel()
    if not channel.find():
        return uart_no_data(args, out, "S3 not found")
    watch = SerialWatch(args.port, out, False, echo=args.echo)
    watch.pump(time.monotonic() + 15, until=lambda: watch.sp is not None)
    if watch.sp is None:
        watch.finish(0)
        return uart_no_data(args, out, "port not opened")
    app_ready()

    s3_start = channel.query()
    setup = {"s3_tx_auto": s3_start and s3_start["tx_mode"] == 2}
    back, _ = uart_wait_command(channel, "status", 15.0)
    setup["channel_up"] = back is not None
    commands, parser, lyrat_resets, s3_restarts, line_low = None, None, [], [], None
    tone_end = None
    try:
        if all(setup.values()):
            commands = uart_commands(watch, channel, args.commands)
            print(json.dumps({"commands": {k: v for k, v in commands.items() if k != "witness"}}), flush=True)
            parser = uart_parser(watch, channel)
            print(json.dumps({"parser": [{c["name"]: c["pass"]} for c in parser["cases"]], "pass": parser["pass"]}), flush=True)
            for i in range(1, args.lyrat_resets + 1):
                lyrat_resets.append(uart_lyrat_reset(watch, channel, i, args))
                print(json.dumps({"lyrat_reset": lyrat_resets[-1]}), flush=True)
            # Volume last: a restart of the LyraT brings it back. Nothing of the phone may reach the room (R15).
            setup["vol_0"] = board_cmd(watch, "vol 0", "volume = 0")
            tag = datetime.now().strftime("%H%M%S")
            app_command("status", tag + "-start")
            wait_app(watch, tag + "-start", lambda s: True, 10.0)
            for i in range(1, args.s3_resets + 1):
                name = "s3r%d" % i
                action = uart_s3_restart_action(watch, channel, out / name)
                s3_restarts.append(uart_sco_trial(watch, channel, args, out, tag, name, action))
                print(json.dumps({"s3_restart": s3_restarts[-1]}), flush=True)
                watch.pump(time.monotonic() + 2.0)
            line_low = uart_sco_trial(watch, channel, args, out, tag, "linelow", uart_line_low_action(watch, channel, args))
            print(json.dumps({"line_low": line_low}), flush=True)
    finally:
        channel.set_tx("A")
        adb("shell", "am", "force-stop", SCO_APP)
        tone_end = board_tone(watch)
        board_cmd(watch, "vol 100", "volume = 100")    # what the firmware sets at boot
    final_back, _ = uart_wait_command(channel, "status", 10.0)
    witness_end = s3_uart.console_ustat(watch)
    s3_end = channel.query()
    channel.close()
    watch.close_port()
    summary = watch.finish(args.commands)
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
    c = commands or {}
    checks = {
        "setup": all(setup.values()),
        "commands_all_run": c.get("planned") == args.commands - (args.commands % 2),
        "failed_after_retry_zero": c.get("failed_after_retry") == 0,
        "wrong_replies_zero": c.get("wrong_replies") == 0,
        "retries_within_limit": c.get("retry_pct") is not None and c["retry_pct"] <= args.max_retry_pct,
        # The console of the LyraT is not part of the channel: each command ran once, at the state the channel reported.
        "witness_console_state": bool(c.get("witness")) and c.get("witness_ok") is True,
        "witness_each_ran_once": bool(c.get("lyrat")) and 0 <= c["lyrat_cmds_extra"] <= 3,
        "parser_all_cases": bool(parser and parser["pass"]),
        "lyrat_resets_all_pass": len(lyrat_resets) == args.lyrat_resets and all(t["pass"] for t in lyrat_resets),
        "s3_restarts_all_pass": len(s3_restarts) == args.s3_resets and all(t["pass"] for t in s3_restarts),
        "line_low_pass": bool(line_low and line_low["pass"]),
        "channel_back_at_end": final_back is not None,
        "s3_pins_ok_at_end": bool(s3_end and s3_end["pins_ok"] == 1),
        "tone_off_at_end": tone_end == 0,
        "no_call_during_round": bool(telecom_after) and telecom_after["calls_now"] == 0
                                and telecom_after["telephony_busy"] == 0
                                and telecom_after["historical_lines"] == telecom_before["historical_lines"],
        "other_bonds_unchanged": bonds_after is not None and phone.others(bonds_before) == phone.others(bonds_after),
        "phone_hfp_connected_at_end": bool(after and after["hfp_connected"]),
        "lyrat_resets_only_planned": summary["board_resets"] == args.lyrat_resets,
        "panics_zero": summary["panics"] == 0,
        "port_drops_zero": summary["port_drops"] == 0,
        "usb_removals_zero": removals == 0,
    }
    verdict = "INVALID_WINDOW" if standby != 0 else ("PASS" if all(checks.values()) else "FAIL")
    result = {"stage": "uart", "label": args.label, "power": args.power, "verdict": verdict, "checks": checks,
              "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
              "seconds": round(time.monotonic() - t0, 1), "dir": str(out),
              "planned": {"commands": args.commands, "lyrat_resets": args.lyrat_resets, "s3_resets": args.s3_resets,
                          "line_low_s": args.line_low, "max_retry_pct": args.max_retry_pct, "min_snr_db": args.min_snr,
                          "max_rx_bytes_per_s": args.max_rx_rate, "codec": args.codec},
              "not_checked": "A real power loss of the S3 is not tried: the wiring forbids it while the LyraT is on. "
                             "A restart and a line held low are what is measured.",
              "setup": setup, "commands": commands, "parser": parser, "lyrat_resets": lyrat_resets, "s3_restarts": s3_restarts,
              "line_low": line_low, "tone_end": tone_end, "s3_end": s3_end, "lyrat_uart_end": witness_end,
              "telecom_before": telecom_before, "telecom_after": telecom_after, "phone_before": before, "phone_after": after,
              "pc_standby_entries": standby, "usb_removals": removals, "serial": summary}
    write_json(out / "round.json", result)
    with (RUNS_ROOT / "rounds.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(redact(json.dumps({"stage": "uart", "label": args.label, "power": args.power, "verdict": verdict,
                                    "start": result["start"], "seconds": result["seconds"],
                                    "commands": c.get("planned"), "failed_after_retry": c.get("failed_after_retry"),
                                    "retry_pct": c.get("retry_pct"), "usb_removals": removals, "dir": str(out)},
                                   ensure_ascii=False)) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("phone_before", "phone_after", "commands", "parser",
                                                                   "s3_restarts", "line_low", "lyrat_resets")},
                     ensure_ascii=False, indent=2))
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


class UartLoad(threading.Thread):
    """Commands through the S3 during another stage, to see whether the UART task reaches the sound."""

    def __init__(self, rate):
        super().__init__(daemon=True)
        self.rate = rate
        self.halt = threading.Event()
        self.sent = self.failed = self.retries = self.not_on = 0

    def run(self):
        channel = s3_uart.Channel()
        channel.find()
        while not self.halt.wait(1.0 / self.rate):
            got = channel.command("status", timeout=0.25, retries=4)
            self.sent += 1
            self.retries += got["attempts"] - 1
            if got["status"] is None:
                self.failed += 1
            elif got["status"] != s3_uart.ST_OK:
                self.not_on += 1
        channel.close()

    def result(self):
        self.halt.set()
        self.join(5.0)
        return {"rate_hz": self.rate, "sent": self.sent, "failed_after_retry": self.failed, "retries": self.retries,
                "other_status": self.not_on}


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
    p = sub.add_parser("audio", help="control tones through Bluetooth both ways, judged by the content of the sound")
    p.add_argument("--power", required=True, help="what feeds the board, e.g. adapter or battery (R11)")
    p.add_argument("--codec", required=True, choices=["mSBC", "CVSD"], help="the codec this firmware must negotiate")
    p.add_argument("--n", type=int, default=10, help="number of trials")
    p.add_argument("--seconds", type=int, default=7, help="length of each tone phase")
    p.add_argument("--control-seconds", type=int, default=3, help="length of the phase without a signal")
    p.add_argument("--min-snr", type=float, default=20.0)
    p.add_argument("--max-shift", type=float, default=1.0, help="percent")
    p.add_argument("--criterion-up", choices=["strict", "blocks"], default="strict")
    p.add_argument("--criterion-down", choices=["strict", "blocks"], default="strict")
    p.add_argument("--max-impaired", type=float, default=30.0, help="percent of blocks, criterion blocks")
    p.add_argument("--source", default="raw", choices=["vc", "mic", "recog", "raw"],
                   help="Android record source; raw has no platform processing")
    p.add_argument("--settle", type=float, default=1.0, help="seconds after the route is confirmed")
    p.add_argument("--open-timeout", type=float, default=15.0)
    p.add_argument("--close-timeout", type=float, default=10.0)
    p.add_argument("--slc-timeout", type=float, default=30.0)
    p.add_argument("--gap", type=float, default=3.0, help="seconds between trials")
    p.add_argument("--stop-on-fail", action="store_true", help="stop at the first failed trial")
    p.add_argument("--label", default="")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--echo", action="store_true", help="print events live")
    p.add_argument("--uart-load", type=float, default=0.0, help="commands per second through the S3 during the trials")
    p = sub.add_parser("tap", help="content of both I2S lines captured by the S3-ETH board, no sound on the PC")
    p.add_argument("--power", required=True, help="what feeds the board, e.g. adapter or battery (R11)")
    p.add_argument("--n", type=int, default=3, help="number of trials")
    p.add_argument("--seconds", type=int, default=8, help="length of the tone phase")
    p.add_argument("--control-seconds", type=int, default=4, help="length of the phase without a signal")
    p.add_argument("--min-snr", type=float, default=30.0)
    p.add_argument("--max-shift", type=float, default=1.0, help="percent")
    p.add_argument("--max-impaired", type=float, default=30.0, help="percent of blocks, Bluetooth losses with Wi-Fi on")
    p.add_argument("--max-loss", type=float, default=0.5, help="percent of packets S3 to PC")
    p.add_argument("--long", type=float, default=0.0, help="seconds of the loss measurement, 0 skips it")
    p.add_argument("--s3-resets", type=int, default=0, help="restarts of the S3 with SCO open")
    p.add_argument("--restore-timeout", type=float, default=30.0, help="seconds for the streams to return")
    p.add_argument("--settle", type=float, default=1.0)
    p.add_argument("--open-timeout", type=float, default=15.0)
    p.add_argument("--close-timeout", type=float, default=10.0)
    p.add_argument("--slc-timeout", type=float, default=30.0)
    p.add_argument("--label", default="")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--echo", action="store_true", help="print events live")
    p = sub.add_parser("btloss", help="rate and time of the failures of the Bluetooth link, phone Wi-Fi A/B")
    p.add_argument("--power", required=True, help="what feeds the board, e.g. adapter or battery (R11)")
    p.add_argument("--seconds", type=float, default=1800.0, help="length of one window")
    p.add_argument("--ab", type=int, default=0, help="pairs of windows with the phone's Wi-Fi off, then on")
    p.add_argument("--phone-wifi", choices=["asis", "on", "off"], default="asis", help="for a single window")
    p.add_argument("--pkt-period", type=float, default=1.0, help="seconds between reads of the packet counters")
    p.add_argument("--wifi-settle", type=float, default=10.0, help="seconds after the phone's Wi-Fi changed")
    p.add_argument("--keep-wav", action="store_true", help="keep the recordings of the two tones")
    p.add_argument("--board-cmd", action="append", help="console command before the windows, may repeat")
    p.add_argument("--source", default="raw", choices=["vc", "mic", "recog", "raw"])
    p.add_argument("--settle", type=float, default=1.0)
    p.add_argument("--gap", type=float, default=3.0)
    p.add_argument("--open-timeout", type=float, default=15.0)
    p.add_argument("--close-timeout", type=float, default=10.0)
    p.add_argument("--label", default="")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--echo", action="store_true", help="print events live")
    p = sub.add_parser("plc", help="the concealment on a voice-like signal, with controls; needs the variant fdrop")
    p.add_argument("--power", required=True, help="what feeds the board, e.g. adapter or battery (R11)")
    p.add_argument("--codec", required=True, choices=["mSBC", "CVSD"], help="the codec this firmware must negotiate")
    p.add_argument("--n", type=int, default=3, help="number of trials")
    p.add_argument("--period", type=int, default=30, help="one replaced frame out of this many")
    p.add_argument("--keep-wav", action="store_true")
    p.add_argument("--settle", type=float, default=1.0)
    p.add_argument("--open-timeout", type=float, default=15.0)
    p.add_argument("--close-timeout", type=float, default=10.0)
    p.add_argument("--slc-timeout", type=float, default=60.0)
    p.add_argument("--gap", type=float, default=3.0)
    p.add_argument("--label", default="")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--echo", action="store_true", help="print events live")
    p = sub.add_parser("uart", help="commands through the S3 and the UART, a bad line, restarts of both boards")
    p.add_argument("--power", required=True, help="what feeds the board, e.g. adapter or battery (R11)")
    p.add_argument("--codec", default="mSBC", choices=["mSBC", "CVSD"], help="the codec of the SCO phases")
    p.add_argument("--commands", type=int, default=1000)
    p.add_argument("--lyrat-resets", type=int, default=3)
    p.add_argument("--s3-resets", type=int, default=3)
    p.add_argument("--line-low", type=float, default=8.0, help="seconds of the line low within the recording")
    p.add_argument("--max-retry-pct", type=float, default=1.0, help="share of the sends that were repeats")
    p.add_argument("--max-rx-rate", type=float, default=200.0, help="bytes per second the LyraT may read during SCO")
    p.add_argument("--min-snr", type=float, default=20.0)
    p.add_argument("--max-shift", type=float, default=1.0, help="percent")
    p.add_argument("--max-impaired", type=float, default=30.0)
    p.add_argument("--source", default="raw", choices=["vc", "mic", "recog", "raw"])
    p.add_argument("--settle", type=float, default=1.0)
    p.add_argument("--open-timeout", type=float, default=15.0)
    p.add_argument("--close-timeout", type=float, default=10.0)
    p.add_argument("--slc-timeout", type=float, default=30.0)
    p.add_argument("--label", default="")
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--echo", action="store_true", help="print events live")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    return {"link": stage_link, "sco": stage_sco, "power": stage_power, "audio": stage_audio, "tap": stage_tap,
            "btloss": stage_btloss, "plc": stage_plc, "uart": stage_uart}[args.stage](args)


if __name__ == "__main__":
    sys.exit(main())
