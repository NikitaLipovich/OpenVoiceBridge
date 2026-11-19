#!/usr/bin/env python3
"""Events of the bench from the boards' log lines: starts, Bluetooth link, headset channel, updates, supply dips. Goal 103.

  bench_events.py FILE...        the events of log files (benchd's log-*.txt, udplog.log, serial.log) as JSON lines

The patterns are the lines of the firmwares on the branch phone-bringup (lyrat_resend/main/main.c, lyrat_ota.c,
s3_eth_bridge/main/main.c, s3_ota.c), not of the documents. As a module: EventIndex(clock).feed(rec) -> [event], where
rec is a record of udplog.parse_records() (board, boot, seq, text; joined by udplog.Joiner) and an event a dict
{"type", "board", ...}; the caller adds the time. RULES is the table of line patterns.

Types: boot (a new boot id of a board; reason from the LyraT's first line "BOOT reset_reason=X code=N"), slc_up,
slc_down (after_s, acl_reason), hfp_down, acl_down (reason), sco_open (codec), sco_close, ota_start, ota_done, ota_fail,
ota_rollback, panic, bod (supply dips: the counter of "bod ... trips=N" grew), net_down, net_up (the S3's Wi-Fi),
mic_mute (state on|off, src key|cmd|boot: the LyraT's mic, goal 111).
benchd adds board_lost/board_back, log_gap, cmd, lock, job and benchd from its own state.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from serial_watch import PANIC  # noqa: E402

BOARD_NAMES = {0: "s3", 1: "lyrat"}
BOOT_LINE = re.compile(r"BOOT reset_reason=([A-Z0-9_]+) code=(\d+)")
BOD_LINE = re.compile(r"bod on=(\d) level=(\d+) trips=(\d+) last_ms=(-?\d+)")
SCO_LINE = re.compile(r"SCO audio state=(\d+) codec=(\S+)")

# (type, board or None for any, pattern, fields from the groups)
RULES = [
    ("slc_up", "lyrat", re.compile(r"SLC CONNECTED"), ()),
    ("slc_down", "lyrat", re.compile(r"SLC DOWN after (-?\d+) s, last_acl_reason=(0x[0-9a-fA-F]+)"),
     (("after_s", int), ("acl_reason", str))),
    ("hfp_down", "lyrat", re.compile(r"HFP conn state=0\b"), ()),
    ("acl_down", "lyrat", re.compile(r"ACL DOWN reason=(0x[0-9a-fA-F]+)"), (("reason", str),)),
    # Updates of the S3 (s3_ota.c) and of the LyraT through the S3 or by itself (lyrat_ota.c, the updater).
    ("ota_start", None, re.compile(r"receiving (\d+) bytes into (\S+)"), (("bytes", int), ("slot", str))),
    ("ota_start", "lyrat", re.compile(r"erasing (\d+) bytes of (\S+)"), (("bytes", int), ("slot", str))),
    ("ota_start", "s3", re.compile(r"sending the image to the lyrat"), ()),
    ("ota_done", None, re.compile(r"new image in (\S+?),? restart"), (("slot", str),)),
    ("ota_done", None, re.compile(r"self-test passed: image confirmed"), ()),
    ("ota_done", "s3", re.compile(r"lyrat runs the new image, confirmed"), ()),
    ("ota_done", None, re.compile(r"dry run: image good, not activated"), ()),
    ("ota_fail", None, re.compile(r"self-test failed\b"), ()),
    ("ota_fail", None, re.compile(r"\bimage check: (\S+)"), (("err", str),)),
    ("ota_fail", None, re.compile(r"\bota begin: (\S+)"), (("err", str),)),
    ("ota_fail", "s3", re.compile(r"lyrat update failed: (.*)$"), (("why", str),)),
    ("ota_rollback", "s3", re.compile(r"lyrat rolled back to (\S+)"), (("elf", str),)),
    ("ota_rollback", None, re.compile(r"self-test fails on purpose \(failtest\), back to the old image"), ()),
    ("net_down", "s3", re.compile(r"wifi lost \(reason (-?\d+)\)"), (("reason", int),)),
    ("net_down", "s3", re.compile(r"wifi off for (\d+) s"), (("seconds", int),)),
    ("net_up", "s3", re.compile(r"wifi ip \S+"), ()),
    ("mic_mute", "lyrat", re.compile(r"MIC MUTE (on|off) src=(\w+)"), (("state", str), ("src", str))),
]


class EventIndex:
    """Turns records into events; keeps per board the boot id and the supply-dip counter."""

    def __init__(self):
        self.boot = {}          # board name -> boot id seen last
        self.bod = None         # trips of the LyraT seen last, None before the first "bod" line
        self.sco_open = False

    def _boot(self, board, rec, text):
        """A boot event when the boot id changed, or when the first record seen of a board is its boot line."""
        boot = rec.get("boot")
        if boot is None:            # a line without its record (a command's reply, a log file): no start to see
            return []
        last = self.boot.get(board)
        m = BOOT_LINE.search(text) if rec.get("seq") == 0 else None
        if boot is not None:
            self.boot[board] = boot
        if not ((last is not None and boot != last) or (last is None and m)):
            return []
        ev = {"type": "boot", "board": board, "boot": boot, "first_seq": rec.get("seq"),
              "reason": m.group(1) if m else "UNKNOWN"}
        if m:
            ev["code"] = int(m.group(2))
        out = [ev]
        if board == "lyrat":
            self.bod = None              # the counter starts again with the board
            if self.sco_open:
                self.sco_open = False
                out.append({"type": "sco_close", "board": board, "why": "restart"})
        return out

    def feed(self, rec):
        board = BOARD_NAMES.get(rec.get("board"), "b%s" % rec.get("board"))
        text = rec.get("text", "")
        out = self._boot(board, rec, text)
        m = SCO_LINE.search(text)
        if m:
            state = int(m.group(1))
            if state in (2, 3) and not self.sco_open:
                self.sco_open = True
                out.append({"type": "sco_open", "board": board, "codec": m.group(2)})
            elif state == 0 and self.sco_open:
                self.sco_open = False
                out.append({"type": "sco_close", "board": board, "codec": m.group(2)})
        m = BOD_LINE.search(text)
        if m:
            trips = int(m.group(3))
            if self.bod is not None and trips > self.bod:
                out.append({"type": "bod", "board": board, "trips": trips, "new": trips - self.bod,
                            "last_ms": int(m.group(4))})
            elif self.bod is None and trips > 0:
                out.append({"type": "bod", "board": board, "trips": trips, "new": None, "last_ms": int(m.group(4)),
                            "note": "first reading since the start of benchd or of the board"})
            self.bod = trips
        if PANIC.search(text):
            out.append({"type": "panic", "board": board, "text": text[:160]})
        for kind, only, rx, fields in RULES:
            if only and only != board:
                continue
            m = rx.search(text)
            if m:
                ev = {"type": kind, "board": board}
                for (name, conv), value in zip(fields, m.groups()):
                    ev[name] = conv(value)
                if kind.startswith("ota_"):
                    ev["text"] = text[:160]
                out.append(ev)
        return out


LINE = re.compile(r"^(\S+) (s3|lyrat)\s+(-?[\d.]+) (.*)$")


def events_of_file(path):
    """Events of a log file written by benchd or udplog.py (the boot id is not in the line: no boot events)."""
    idx = EventIndex()
    out = []
    ids = {"s3": 0, "lyrat": 1}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE.match(line.rstrip("\n"))
            if not m:
                continue
            for ev in idx.feed({"board": ids[m.group(2)], "text": m.group(4)}):
                ev["t"] = m.group(1)
                out.append(ev)
    return out


def main(argv=None):
    for path in (argv if argv is not None else sys.argv[1:]):
        for ev in events_of_file(path):
            print(json.dumps(ev, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
