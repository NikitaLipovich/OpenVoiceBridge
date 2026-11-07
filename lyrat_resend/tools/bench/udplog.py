#!/usr/bin/env python3
"""Debug log of both boards over UDP: the S3's own lines and the LyraT's through the S3. Protocol: docs/OTA.md, "Debug log".

  udplog.py [--seconds N] [--host ADDR] [--port 5008] [--s3-level 3] [--lyrat-level 3]
            [--board s3|lyrat|all] [--level E|W|I|D|V] [--grep RE] [--echo] [--label NAME]

Asks the S3 with "LOG1 <s3_level> <lyrat_level>" every 2 s and stops it with "LOG0" at the end; without --seconds it runs
until Ctrl+C. The lines that pass the filter go to goals_harness/bench_runs/udplog/<stamp>/udplog.log, MAC prefixes hidden;
the counters of all lines go to summary.json: per board lines, restarts (a new boot id), lost on the board (its own
counter) and lost on the way (the rest of the gaps in the line numbers), duplicates. Levels: 0 none, 1 E, 2 W, 3 I, 4 D, 5 V.
Exit code: 0 lines came, 1 none came, 2 S3 not found.

As a module: parse_datagram(), parse_records(), Tracker, format_line(), LEVELS.
"""
import argparse
import json
import os
import re
import socket
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import redact, run_dir, write_json  # noqa: E402

LOG_PORT = 5008
REC_HDR = 18
UDP_VERSION = 1
BOARD_NAMES = {0: "s3", 1: "lyrat"}
LEVELS = {"N": 0, "E": 1, "W": 2, "I": 3, "D": 4, "V": 5}
LEVEL_CHARS = "-EWIDV"
LEASE_REPEAT_S = 2.0


def parse_records(data: bytes):
    """Records as in a UART frame of type 5: list of dicts; stops at the first one that does not parse."""
    out = []
    i = 0
    while i + REC_HDR <= len(data):
        board, level, boot, seq, t_ms, lost, n = struct.unpack_from("<BBHIIIH", data, i)
        if i + REC_HDR + n > len(data):
            break
        text = data[i + REC_HDR:i + REC_HDR + n].decode("utf-8", "replace")
        out.append({"board": board, "level": level, "boot": boot, "seq": seq, "t_ms": t_ms, "lost": lost, "text": text})
        i += REC_HDR + n
    return out


def parse_datagram(data: bytes):
    """Records of one UDP datagram, or None when it is not a log datagram."""
    if len(data) < 4 or data[:2] != b"UL" or data[2] != UDP_VERSION:
        return None
    recs = parse_records(data[4:])
    return recs if len(recs) == data[3] else recs or None


class Tracker:
    """Counts per board: where a gap in the line numbers comes from."""

    def __init__(self):
        self.boards = {}

    def add(self, rec):
        b = self.boards.setdefault(rec["board"], {"lines": 0, "restarts": 0, "lost_board": 0, "lost_way": 0,
                                                  "duplicates": 0, "boot": None, "seq": None, "lost": None})
        b["lines"] += 1
        if b["boot"] is not None and rec["boot"] != b["boot"]:
            b["restarts"] += 1
            b["seq"] = b["lost"] = None
        b["boot"] = rec["boot"]
        if b["seq"] is None:
            # Lines before the first one seen may have gone to an earlier listener: not counted, only noted.
            b.setdefault("first", []).append({"seq": rec["seq"], "lost": rec["lost"]})
        elif rec["seq"] <= b["seq"]:
            b["duplicates"] += 1
            return
        else:
            gap = rec["seq"] - b["seq"] - 1
            on_board = max(0, rec["lost"] - b["lost"])
            b["lost_board"] += on_board
            b["lost_way"] += max(0, gap - on_board)
        b["seq"], b["lost"] = rec["seq"], rec["lost"]

    def summary(self):
        keys = ("lines", "restarts", "lost_board", "lost_way", "duplicates", "first")
        return {BOARD_NAMES.get(k, str(k)): {x: v.get(x) for x in keys} for k, v in sorted(self.boards.items())}


def format_line(rec, pc_time=None):
    board = BOARD_NAMES.get(rec["board"], "b%d" % rec["board"])
    stamp = (pc_time or datetime.now()).strftime("%H:%M:%S.%f")[:-3]
    return "%s %-5s %9.3f %s" % (stamp, board, rec["t_ms"] / 1000.0, rec["text"])


def level_value(text):
    if text is None:
        return None
    t = str(text).upper()
    return int(t) if t.isdigit() else LEVELS[t[0]]


def make_filter(board=None, level=None, grep=None):
    want_board = {"s3": 0, "lyrat": 1}.get(board) if board and board != "all" else None
    max_level = level_value(level)
    rx = re.compile(grep) if grep else None

    def ok(rec):
        if want_board is not None and rec["board"] != want_board:
            return False
        if max_level is not None and rec["level"] and rec["level"] > max_level:
            return False
        return not rx or bool(rx.search(rec["text"]))
    return ok


def main(argv=None):
    # Board lines carry non-ASCII ("—"): a cp1252 console must not stop the tool.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seconds", type=float, default=0.0)
    ap.add_argument("--host")
    ap.add_argument("--port", type=int, default=LOG_PORT)
    ap.add_argument("--s3-level", default="3")
    ap.add_argument("--lyrat-level", default="3")
    ap.add_argument("--board", choices=["s3", "lyrat", "all"], default="all")
    ap.add_argument("--level", help="show lines up to this level: E, W, I, D, V")
    ap.add_argument("--grep")
    ap.add_argument("--echo", action="store_true", help="also print the lines")
    ap.add_argument("--label", default="")
    ap.add_argument("--out", help="directory instead of bench_runs/udplog/<stamp>")
    args = ap.parse_args(argv)

    host = args.host or os.environ.get("BENCH_S3_ADDR")
    if not host:
        from ota import find_host
        host = find_host(argparse.Namespace(host=None))
    if not host:
        print("S3 not found", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else run_dir("udplog", args.label)
    out.mkdir(parents=True, exist_ok=True)
    keep = make_filter(args.board, args.level, args.grep)
    tracker = Tracker()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    request = ("LOG1 %d %d" % (level_value(args.s3_level), level_value(args.lyrat_level))).encode()
    t0 = time.monotonic()
    next_ask = 0.0
    datagrams = written = 0
    s3_state = None
    try:
        with open(out / "udplog.log", "w", encoding="utf-8") as f:
            while not args.seconds or time.monotonic() - t0 < args.seconds:
                now = time.monotonic()
                if now >= next_ask:
                    try:
                        sock.sendto(request, (host, args.port))
                    except OSError:
                        pass
                    next_ask = now + LEASE_REPEAT_S
                sock.settimeout(0.2)
                try:
                    data, peer = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    # Windows reports an earlier ICMP "port unreachable" here: the S3 is restarting. Keep asking.
                    continue
                except KeyboardInterrupt:
                    break
                if peer[0] != host:
                    continue
                if data.startswith(b"LOG "):
                    try:
                        s3_state = json.loads(data.split(b" ", 2)[2])
                    except (ValueError, IndexError):
                        pass
                    continue
                recs = parse_datagram(data)
                if recs is None:
                    continue
                datagrams += 1
                for rec in recs:
                    tracker.add(rec)
                    if keep(rec):
                        line = redact(format_line(rec))
                        f.write(line + "\n")
                        written += 1
                        if args.echo:
                            print(line, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.sendto(b"LOG0", (host, args.port))
        except OSError:
            pass
        sock.close()
    summary = {"seconds": round(time.monotonic() - t0, 1), "datagrams": datagrams, "written": written,
               "filter": {"board": args.board, "level": args.level, "grep": args.grep},
               "boards": tracker.summary(), "s3": s3_state}
    write_json(out / "summary.json", summary)
    print(json.dumps({"out": str(out), "boards": summary["boards"]}))
    return 0 if datagrams else 1


if __name__ == "__main__":
    sys.exit(main())
