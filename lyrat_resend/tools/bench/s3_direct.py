#!/usr/bin/env python3
"""s3_direct.py: the bench without the PC's service, straight to the S3 (goal 105) - the reference of the phone's direct
mode (S3Direct.java) and a tool of its own.

  s3_direct.py status [--host ADDR] [--seconds 6]   the status the phone shows in direct mode, as JSON (UQ + log lines)
  s3_direct.py log [--host ADDR] [--seconds N]      the log as the phone writes it (format of udplog.format_line)

When benchd is not there the phone speaks to the S3 itself with the protocol of the PC's tools (docs/S3_UART.md):
UQ (the S3's counters, answered by the S3 alone, never through the UART), UC <seq> <command> repeated with the same seq,
UX <boot>, and the log by LOG1/LOG0 on the log port (udplog.py). DirectState turns what came into the same shape as the
"s3" and "lyrat" parts of benchd's /status (same keys, same rules for lost/ver/stat), so one panel of bench_ui_spec.json
shows both modes; test_bench_find.py holds S3Direct.java to it.

As a module: collect(host, seconds), DirectState (feed_uq, feed_line, status), DIRECT_OPS, S3_DIRECT_CMDS, direct_command(text) -> request.
Don't use it beside a running benchd: the S3 gives its log to one listener (this would take it).
"""
import argparse
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import udplog  # noqa: E402

# The ops an action of bench_ui_spec.json may take in direct mode ("direct": {"op": ...}); BenchActivity knows the same.
DIRECT_OPS = ("uc", "ux", "cmd")
# What the console action may ask of the S3 itself in direct mode (benchd also has power and tx: not without the PC).
S3_DIRECT_CMDS = ("query", "restart")
VER_LINE = re.compile(r"\bver=(\S+)")                                 # as benchd.VER_LINE
STAT_FIELDS = re.compile(r"\b(up|rssi|net|txp|selftest|ip)=(\S+)")    # as benchd.STAT_FIELDS
LOST_S = 10.0                                                         # benchd --lost-s default


def now_iso(dt=None):
    return (dt or datetime.now()).isoformat(timespec="milliseconds")


class DirectState:
    """What the phone knows of the S3 and the LyraT in direct mode; status() has the keys of benchd's /status."""

    def __init__(self, addr, lost_s=LOST_S):
        self.addr = addr
        self.lost_s = lost_s
        self.uq = None
        self.uq_t = None          # monotonic seconds of the last UQ answer
        self.uq_ms = None         # its round trip
        self.seen = None          # ISO time of the last UQ answer
        self.stat = {}
        self.ver = {"s3": None, "lyrat": None}
        self.last_line = {"s3": None, "lyrat": None}
        self.datagrams = 0

    def feed_uq(self, uq, t, ms=None, iso=None):
        self.uq, self.uq_t, self.uq_ms, self.seen = uq, t, ms, iso or now_iso()

    def feed_line(self, board, text, iso=None):
        """One whole log line (udplog.Joiner output) of board "s3" or "lyrat"."""
        if board not in self.ver:
            return
        self.last_line[board] = iso or now_iso()
        m = VER_LINE.search(text)
        if m:
            self.ver[board] = m.group(1)
        if board == "s3" and text.startswith("stat up="):
            self.stat = dict(STAT_FIELDS.findall(text))

    def status(self, t):
        """The status at monotonic time t, in the shape of benchd's /status (only the parts direct mode knows)."""
        uq = self.uq or {}
        quiet = self.uq_t is None or t - self.uq_t > self.lost_s
        lf = uq.get("last_frame_ms")
        flags = uq.get("lyrat_flags")
        return {
            "mode": "s3-direct",
            "s3": {"addr": self.addr, "found": bool(self.addr), "lost": quiet, "boot": uq.get("boot"),
                   "up_s": up_s(uq["up_ms"]) if uq.get("up_ms") is not None else None,
                   "rssi": self.stat.get("rssi"), "net": self.stat.get("net"), "txp": self.stat.get("txp"),
                   "selftest": self.stat.get("selftest"), "ver": self.ver["s3"], "tx_on": uq.get("tx_on"),
                   "last_answer": self.seen, "answer_ms": self.uq_ms, "last_line": self.last_line["s3"],
                   "datagrams": self.datagrams},
            "lyrat": {"lost": quiet or lf is None or lf < 0 or lf > max(3000, self.lost_s * 1000),
                      "up_s": uq.get("lyrat_up_s"), "last_frame_ms": lf, "ver": self.ver["lyrat"],
                      "last_line": self.last_line["lyrat"],
                      "sco_open": bool(flags & 2) if isinstance(flags, int) and flags >= 0 else None,
                      "mic_muted": bool(flags & 4) if isinstance(flags, int) and flags >= 0 else None},
        }


def up_s(up_ms):
    """Seconds with one decimal, half up (the same in S3Direct.upS; round() would round half to even)."""
    return math.floor(up_ms / 100.0 + 0.5) / 10.0


def is_uq_answer(data):
    """The same test as s3_uart.Channel.query()."""
    return data.startswith(b'{"fw":"s3tap","boot"') and b'"tx_mode"' in data


def direct_command(target, text):
    """(kind, payload) of the console action in direct mode: ("uc", text) for the LyraT, ("uq"|"ux", None) for the S3;
    ValueError for what direct mode does not do."""
    words = text.split()
    if not words:
        raise ValueError("a command is needed")
    if any(not 0x20 <= ord(c) <= 0x7e for c in text.strip()):
        raise ValueError("the console takes printable ASCII only")
    if target == "lyrat":
        return "uc", text
    if target == "s3" and words[0] in S3_DIRECT_CMDS and len(words) == 1:
        return ("uq" if words[0] == "query" else "ux"), None
    raise ValueError("directly the S3 takes only: %s" % ", ".join(S3_DIRECT_CMDS))


def collect(host=None, seconds=6.0, on_line=None):
    """Direct mode for this long: UQ every 2 s and the log by LOG1 (LOG0 at the end). The DirectState, or None when no
    S3 answers. on_line(rec) gets every whole log line."""
    import socket
    import s3_uart
    ch = s3_uart.Channel(host)
    try:
        if not ch.find(timeout=4.0):
            return None
        st = DirectState(ch.board)
        log = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log.bind(("", 0))
        joiner = udplog.Joiner()
        t0 = time.monotonic()
        next_ask = next_uq = 0.0
        try:
            while time.monotonic() - t0 < seconds:
                now = time.monotonic()
                if now >= next_ask:
                    log.sendto(b"LOG1 3 3", (ch.board, udplog.LOG_PORT))
                    next_ask = now + udplog.LEASE_REPEAT_S
                if now >= next_uq:
                    q0 = time.monotonic()
                    q = ch.query()
                    if q:
                        st.feed_uq(q, time.monotonic(), round((time.monotonic() - q0) * 1000.0, 1))
                    next_uq = now + 2.0
                log.settimeout(0.2)
                try:
                    data, peer = log.recvfrom(2048)
                except (socket.timeout, ConnectionResetError):
                    continue
                recs = udplog.parse_datagram(data) if peer[0] == ch.board else None
                if not recs:
                    continue
                st.datagrams += 1
                for rec in recs:
                    for whole in joiner.feed(rec):
                        st.feed_line(udplog.BOARD_NAMES.get(whole["board"], "b%d" % whole["board"]), whole["text"])
                        if on_line:
                            on_line(whole)
        finally:
            try:
                log.sendto(b"LOG0", (ch.board, udplog.LOG_PORT))
            except OSError:
                pass
            log.close()
        return st
    finally:
        ch.close()


def run(args):
    on_line = (lambda rec: print(udplog.format_line(rec), flush=True)) if args.action == "log" else None
    st = collect(args.host, args.seconds, on_line)
    if st is None:
        print("S3 not found", file=sys.stderr)
        return 2
    if args.action == "status":
        print(json.dumps(st.status(time.monotonic()), ensure_ascii=False, indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["status", "log"])
    ap.add_argument("--host", help="the S3's address (else BENCH_S3_ADDR, else found by broadcast)")
    ap.add_argument("--seconds", type=float, default=6.0)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
