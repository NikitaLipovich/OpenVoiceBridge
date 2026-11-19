#!/usr/bin/env python3
"""The LyraT's Bluetooth link from its log: known lines of the firmware -> states, and where it got stuck. Goal 102.

  python hfp_states.py LOG [LOG ...] [--json]     a console log (idf.py monitor, serial_watch) or a log of udplog.py
  python hfp_states.py -                           the same from stdin

Prints a timeline (one line per state change) and the verdict: the last state, the mode, who is connected, the codec,
counts (connect tries, SLC, drops, SCO opens), and a hint for the bench when it is stuck. Every pattern below is a
line of lyrat_resend/main/main.c; tests/test_dual_mode.py checks that each one is still in the source (goal 102).
Exit code: 0 the log ends with SLC up (or audio open), 1 it ends without a link, 2 no LyraT Bluetooth line at all.
As a module: parse(lines) -> events, summarize(events) -> dict, PATTERNS.
"""
import argparse
import json
import re
import sys

# name: (regex, literal text that must be in main.c). Times: the ESP log stamp "(12345)" in ms when the line has one.
PATTERNS = {
    "boot":        (re.compile(r"BOOT reset_reason=(\w+)"), "BOOT reset_reason="),
    "bt_ready":    (re.compile(r"BT ready: '([^']*)'"), "BT ready: '%s'"),
    "mode_boot":   (re.compile(r"mode boot: mode=(\w+) src=(\w+) phone=(\S+) pc=(\S+)( migrated)?"),
                    "mode boot: mode=%s src=%s phone=%s pc=%s%s"),
    "try":         (re.compile(r"mode connect try (\w+) (\S+) in (\w+) mode"), "mode connect try %s %s in %s mode"),
    "try_old":     (re.compile(r"HFP connect try peer\[(\d+)\]"), "HFP connect try peer[%u]"),
    "paired":      (re.compile(r"Paired: '([^']*)'"), "Paired: '%s'"),
    "pair_fail":   (re.compile(r"Pairing failed: (-?\d+)"), "Pairing failed: %d"),
    "acl_up":      (re.compile(r"ACL UP stat=(0x[0-9a-f]+)"), "ACL UP stat=0x%x"),
    "acl_down":    (re.compile(r"ACL DOWN reason=(0x[0-9a-f]+)"), "ACL DOWN reason=0x%x"),
    "hfp_state":   (re.compile(r"HFP conn state=(\d+)"), "HFP conn state=%d"),
    "slc_judge":   (re.compile(r"mode slc (\w+) (\S+) in (\w+) mode: (\w+)"), "mode slc %s %s in %s mode: %s"),
    "slc_up":      (re.compile(r"\*\*\* SLC CONNECTED \*\*\*"), "*** SLC CONNECTED ***"),
    "slc_down":    (re.compile(r"SLC DOWN after (-?\d+) s, last_acl_reason=(0x[0-9a-f]+)"),
                    "SLC DOWN after %\"PRId64\" s, last_acl_reason=0x%x"),
    "sco_state":   (re.compile(r"SCO audio state=(\d+) codec=([A-Za-z]+)"), "SCO audio state=%d codec=%s"),
    "sco_open":    (re.compile(r"\*\*\* SCO OPEN \*\*\*"), "*** SCO OPEN ***"),
    "sco_closed":  (re.compile(r"\*\*\* SCO CLOSED \*\*\*"), "*** SCO CLOSED ***"),
    "ring":        (re.compile(r"RING .{0,3}auto-answer"), "RING — auto-answer"),
    "mode_cmd":    (re.compile(r"mode cmd: (.*)$"), "mode cmd: %s"),
}
STAMP = re.compile(r"\b[IWEDV] \((\d+)\) ")
UDPLOG = re.compile(r"^\d\d:\d\d:\d\d\.\d{3} (\w+)\s+(\d+\.\d{3}) ")    # udplog.format_line: PC time, board, board s

HINTS = {
    "no_bt": "no Bluetooth line: the firmware did not start Bluetooth (look at the BOOT lines; a no_bt variant?)",
    "no_target": "nobody to call: no bond in this mode. Pair: the {mode} adds LyraT_Phone (phone: Bluetooth settings; "
                 "Windows: Settings > Bluetooth > Add device), with LyraT in the {mode} mode",
    "no_answer": "LyraT calls the {role} {addr} and it does not answer: its Bluetooth is off, it is away, or it forgot "
                 "the pairing (then pair anew). Windows never calls the headset itself: LyraT has to reach it",
    "acl_no_slc": "the radio link comes up but HFP does not: on Windows the Hands-Free service of LyraT_Phone may be off "
                  "(Devices and Printers > LyraT_Phone > Services); on a phone, the 'Calls' switch of the device",
    "drop_other": "the {role} keeps connecting while LyraT is in the {mode} mode; it is dropped on purpose. Switch the "
                  "mode (s3_uart.py mode {role}) or turn that one's Bluetooth off",
    "drop_unknown": "an AG in neither slot ({addr}) connected and was dropped: 'mode forget {mode}' lets the next one "
                    "take the {mode} slot, or pair it anew in this mode",
    "slc_pc_no_sco": "connected to the PC; Windows opens audio only while an app uses 'LyraT_Phone Hands-Free' "
                     "(Zoom: microphone and speaker; Sound Recorder: start a recording)",
    "slc_phone_no_sco": "connected to the phone; audio opens on a call or with the probe app (round.py sco)",
    "sco": "audio open, codec {codec}",
}


def parse(lines):
    """Events: dicts with t (ms or None), n (line index), kind, groups."""
    events = []
    for n, line in enumerate(lines):
        stamp, net = STAMP.search(line), UDPLOG.match(line)
        if net and net.group(1) != "lyrat":
            continue                     # the S3's own lines
        t = int(stamp.group(1)) if stamp else (int(float(net.group(2)) * 1000) if net else None)
        for kind, (rx, _) in PATTERNS.items():
            m = rx.search(line)
            if m:
                events.append({"t": t, "n": n, "kind": kind, "g": [g for g in m.groups()]})
                break
    return events


def summarize(events):
    s = {"state": "no_bt", "mode": None, "src": None, "slots": {}, "link": None, "codec": None, "sco": False,
         "counts": {k: 0 for k in ("boots", "tries", "acl_up", "slc_up", "slc_down", "drops", "sco_open", "sco_close",
                                   "mode_switch", "mode_refused", "paired")},
         "last_try": None, "last_drop": None, "timeline": [], "tries_since": 0}
    c = s["counts"]

    def go(state, ev, note=""):
        if s["state"] != state or note:
            s["timeline"].append({"t": ev["t"], "n": ev["n"], "state": state, "note": note})
        s["state"] = state

    for ev in events:
        k, g = ev["kind"], ev["g"]
        if k == "boot":
            c["boots"] += 1
            s.update(link=None, sco=False)
            go("boot", ev, g[0])
        elif k == "bt_ready":
            go("bt_ready", ev)
        elif k == "mode_boot":
            s.update(mode=g[0], src=g[1], slots={"phone": g[2], "pc": g[3]}, tries_since=0, last_try=None)
            go("idle", ev, "mode=%s src=%s%s" % (g[0], g[1], " migrated" if g[4] else ""))
        elif k == "try":
            c["tries"] += 1
            s["tries_since"] += 1
            s["last_try"] = {"role": g[0], "addr": g[1]}
            s["mode"] = g[2]
            if s["state"] not in ("connecting",):
                go("connecting", ev, "%s %s" % (g[0], g[1]))
        elif k == "try_old":
            if s["mode"] is None:          # a log of before goal 102: count its tries the same
                c["tries"] += 1
                if s["state"] != "connecting":
                    go("connecting", ev, "peer[%s]" % g[0])
        elif k == "paired":
            c["paired"] += 1
            go(s["state"], ev, "paired '%s'" % g[0])
        elif k == "acl_up":
            if g[0] == "0x0":
                c["acl_up"] += 1
                if s["state"] in ("connecting", "idle", "bt_ready"):
                    go("acl", ev)
        elif k == "slc_judge":
            if g[3].startswith("drop"):
                c["drops"] += 1
                s["last_drop"] = {"role": g[0], "addr": g[1], "why": g[3]}
                go("dropped", ev, "%s %s: %s" % (g[0], g[1], g[3]))
            else:
                s["link"] = {"role": g[0] if g[0] != "unknown" else s["mode"], "addr": g[1]}
                s["mode"] = g[2]
        elif k == "slc_up":
            c["slc_up"] += 1
            go("slc", ev, (s["link"] or {}).get("role", ""))
        elif k == "slc_down":
            c["slc_down"] += 1
            s.update(link=None, sco=False, tries_since=0, last_try=None)
            go("idle", ev, "after %s s, acl reason %s" % (g[0], g[1]))
        elif k == "sco_state" and g[0] in ("2", "3"):
            s["codec"] = g[1]
        elif k == "sco_open":
            c["sco_open"] += 1
            s["sco"] = True
            go("sco", ev, s["codec"] or "")
        elif k == "sco_closed":
            c["sco_close"] += 1
            s["sco"] = False
            if s["state"] == "sco":
                go("slc", ev, "audio closed")
        elif k == "mode_cmd":
            text = g[0]
            if "refused" in text:
                c["mode_refused"] += 1
            elif "saved" in text:
                c["mode_switch"] += 1
                m = re.match(r"mode (\w+) saved", text)
                if m:
                    s.update(mode=m.group(1), tries_since=0, last_try=None)
            s["timeline"].append({"t": ev["t"], "n": ev["n"], "state": s["state"], "note": text})
    s["hint"] = hint(s)
    return s


def hint(s):
    st, mode = s["state"], s["mode"] or "phone"
    if st == "no_bt":
        return HINTS["no_bt"]
    if st == "sco":
        return HINTS["sco"].format(codec=s["codec"] or "?")
    if st == "slc":
        role = (s["link"] or {}).get("role") or mode
        return HINTS["slc_pc_no_sco"] if role == "pc" else HINTS["slc_phone_no_sco"]
    if st == "dropped" or (s["last_drop"] and st in ("connecting", "idle") and s["counts"]["drops"] >= 2):
        d = s["last_drop"]
        if d and d["why"] == "drop_other":
            return HINTS["drop_other"].format(role=d["role"], mode=mode)
        return HINTS["drop_unknown"].format(addr=d["addr"] if d else "?", mode=mode)
    if st == "acl" or (st == "connecting" and s["counts"]["acl_up"] > s["counts"]["slc_up"] + s["counts"]["drops"]):
        return HINTS["acl_no_slc"]
    if st == "connecting" and s["last_try"]:
        return HINTS["no_answer"].format(role=s["last_try"]["role"], addr=s["last_try"]["addr"])
    if st in ("idle", "bt_ready", "dropped") and s["tries_since"] == 0:
        return HINTS["no_target"].format(mode=mode)
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", help="log files, or - for stdin")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    lines = []
    for path in args.logs:
        if path == "-":
            lines += sys.stdin.read().splitlines()
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines += f.read().splitlines()
    s = summarize(parse(lines))
    if args.json:
        print(json.dumps(s, indent=1))
    else:
        for row in s["timeline"]:
            print("%8s  %-10s %s" % ("" if row["t"] is None else "%d ms" % row["t"], row["state"], row["note"]))
        print("state=%s mode=%s link=%s sco=%s codec=%s counts=%s" % (
            s["state"], s["mode"], (s["link"] or {}).get("role"), s["sco"], s["codec"],
            " ".join("%s=%d" % kv for kv in s["counts"].items() if kv[1])))
        if s["hint"]:
            print("hint: " + s["hint"])
    return 2 if s["state"] == "no_bt" else (0 if s["state"] in ("slc", "sco") else 1)


if __name__ == "__main__":
    sys.exit(main())
