#!/usr/bin/env python3
"""Follow the phone's Bluetooth log from the first moment of a series; addresses reduced, output outside git.

    python btlog.py --out FILE [--seconds N] [--no-clear]
Without --seconds it runs until killed. From Python: start_btlog(path) returns the process, stop_btlog(proc) ends it.
Only the Bluetooth stack's own tags are kept. Nothing from telephony is kept, so no caller number can land here.
"""
import argparse
import re
import subprocess
import sys
import time

# Stack tags of the phone, see tools/catalogs/zui_bt_ui.md.
KEEP = re.compile(r"\s[VDIWEF] (bt_\w+|HeadsetStateMachine)\s*:")
MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){4}([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})")
# What a series is judged by. Keep in step with the phone's library, see goal 055 DECISIONS D2.
ENTRY_ADDED = "blacklisting device"
ENTRY_KNOWN = "is already blacklisted for codec negotiation"
ENTRY_USED = "disable codec negotiation for phone"
NEGOTIATION_TIMEOUT = "bta_ag_codec_negotiation_timer_cback"


def follow(out, seconds=None, clear=True):
    if clear:
        subprocess.run(["adb", "logcat", "-c"], check=False)
    proc = subprocess.Popen(["adb", "logcat", "-b", "all", "-v", "threadtime"], stdout=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    end = time.time() + seconds if seconds else None
    with open(out, "a", encoding="utf-8") as fh:
        while end is None or time.time() < end:
            line = proc.stdout.readline()
            if not line:
                break
            if KEEP.search(line) and "ScoProbe" not in line:
                fh.write(MAC.sub(r"<m>:\1", line))
                fh.flush()
    proc.kill()


def start_btlog(path):
    return subprocess.Popen([sys.executable, __file__, "--out", str(path)])


def stop_btlog(proc):
    proc.kill()
    proc.wait()
    # The child "adb logcat" of a killed follower ends when its pipe closes.


def scan(path):
    """Counts of the lines a series is judged by, and the first timestamp of an added entry."""
    res = {"entry_added": 0, "entry_known": 0, "entry_used": 0, "first_added": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if ENTRY_ADDED in line:
                    res["entry_added"] += 1
                    res["first_added"] = res["first_added"] or line[:18]
                elif ENTRY_KNOWN in line:
                    res["entry_known"] += 1
                elif ENTRY_USED in line:
                    res["entry_used"] += 1
    except FileNotFoundError:
        pass
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--no-clear", action="store_true")
    args = ap.parse_args()
    follow(args.out, args.seconds, not args.no_clear)


if __name__ == "__main__":
    main()
