#!/usr/bin/env python3
"""Control channel of the bench: PC, UDP 5006, S3, UART, LyraT and back. Protocol: docs/S3_UART.md.

  s3_uart.py query                 counters of the S3 end, JSON
  s3_uart.py cmd "tone 1"          one console command on the LyraT through the S3
  s3_uart.py tx 0|1|A              the S3's TX: off, on, follow the LyraT
  s3_uart.py linecheck [--baud N]  low speed check of both directions, GPIO1 stays an input until the first one passed

As a module: Channel (query, command, raw, set_tx, set_baud), frame(), and line_check(watch, channel, baud).
The S3 is found by broadcast, its address is not stored. Exit code: 0 done, 1 failed, 2 board not found.
"""
import argparse
import json
import os
import random
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import DEFAULT_PORT, RUNS_ROOT, redact, stamp  # noqa: E402
from serial_watch import SerialWatch  # noqa: E402
from udp_record import TAP_REQUEST_PORT, local_addresses, route_address  # noqa: E402

T_CMD, T_REPLY, T_HB, T_HB_ACK = 1, 2, 3, 4
ST_OK, ST_UNKNOWN, ST_BAD, ST_NO_TX = 0, 1, 2, 3
BAUD = 115200
USTAT = re.compile(r"ustat baud=(\d+) rx_level=(\d) rx_bytes=(\d+) frames=(\d+) bad_hdr=(\d+) bad_crc=(\d+) trunc=(\d+) "
                   r"noise=(\d+) brk=(\d+) frm=(\d+) ovf=(\d+) cmds=(\d+) dups=(\d+) replies=(\d+) hb=(\d+) acks=(\d+) "
                   r"bad_type=(\d+) tx_fail=(\d+)")
USTAT_KEYS = ("baud", "rx_level", "rx_bytes", "frames", "bad_hdr", "bad_crc", "trunc", "noise", "brk", "frm", "ovf", "cmds",
              "dups", "replies", "hb", "acks", "bad_type", "tx_fail")


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def frame(kind, seq, payload=b"", flip_crc=False, flip_header=False):
    """A frame as the firmware builds it, or with a wrong check, for the tests of its parser."""
    head = bytes([kind, seq & 0xFF, seq >> 8, len(payload) & 0xFF, len(payload) >> 8])
    check = head[0] ^ head[1] ^ head[2] ^ head[3] ^ head[4]
    crc = crc16(head + bytes([check]) + payload)
    if flip_crc:
        crc ^= 0x0001
    if flip_header:
        check ^= 0x01
    return bytes([0xA5, 0x5A]) + head + bytes([check]) + payload + bytes([crc & 0xFF, crc >> 8])


def parse_ustat(line):
    m = USTAT.search(line)
    return dict(zip(USTAT_KEYS, (int(v) for v in m.groups()))) if m else None


class Channel:
    def __init__(self, board=None):
        self.board = board or os.environ.get("BENCH_S3_ADDR")
        self.socks = []
        for ip in local_addresses():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                s.bind((ip, 0))
            except OSError:
                s.close()
                continue
            s.setblocking(False)
            self.socks.append(s)
        if not self.socks:
            raise OSError("no local address")
        self.seq = random.randrange(1, 60000)
        self.attempts = self.retries = 0

    def close(self):
        for s in self.socks:
            s.close()

    def _send(self, text):
        data = text.encode("ascii") if isinstance(text, str) else text
        if self.board:
            # One socket only: each copy through another interface would reach the board as a repeat of the command.
            local = route_address(self.board)
            socks = [s for s in self.socks if s.getsockname()[0] == local] or self.socks[:1]
        else:
            socks = self.socks
        for s in socks:
            try:
                s.sendto(data, (self.board or "255.255.255.255", TAP_REQUEST_PORT))
            except OSError:
                pass

    def _recv(self, timeout):
        """One datagram from the board, or None."""
        import select
        ready, _, _ = select.select(self.socks, [], [], timeout)
        for s in ready:
            try:
                data, peer = s.recvfrom(4096)
            except OSError:
                continue
            if self.board is None or peer[0] == self.board:
                return data, peer[0]
        return None

    def _ask(self, text, accept, timeout=0.5, tries=4):
        for _ in range(tries):
            self._send(text)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                got = self._recv(max(0.0, end - time.monotonic()))
                if got and accept(got[0]):
                    if self.board is None:
                        self.board = got[1]
                    return got[0]
        return None

    def find(self, timeout=8.0):
        end = time.monotonic() + timeout
        self.board_before = self.board
        while time.monotonic() < end:
            if self.query() is not None:
                return True
            self.board = None
        return False

    def query(self):
        got = self._ask("UQ", lambda d: d.startswith(b'{"fw":"s3tap","boot"') and b'"tx_mode"' in d)
        return json.loads(got.decode()) if got else None

    def set_tx(self, mode):
        got = self._ask("UT %s" % mode, lambda d: d.startswith(b"UT "))
        return got.decode() if got else None

    def set_baud(self, baud):
        got = self._ask("UB %d" % baud, lambda d: d.startswith(b"UB "))
        return int(got.split()[1]) if got else None

    def raw(self, data):
        """Bytes onto the line toward the LyraT; returns how many the S3 took, -1 with its TX off."""
        got = self._ask("UW " + data.hex(), lambda d: d.startswith(b"UW "))
        return int(got.split()[1]) if got else None

    def command(self, text, timeout=0.25, retries=7, seq=None):
        """One command with its retries, the same seq in all of them. Returns a dict with status, text, attempts, ms."""
        if seq is None:
            self.seq = (self.seq + 1) & 0xFFFF
            seq = self.seq
        request = ("UC %d %s" % (seq, text)).encode("ascii")
        prefix = ("UR %d " % seq).encode("ascii")
        t0 = time.monotonic()
        for attempt in range(1, retries + 2):
            self.attempts += 1
            if attempt > 1:
                self.retries += 1
            self._send(request)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                got = self._recv(max(0.0, end - time.monotonic()))
                if not got or not got[0].startswith(prefix):
                    continue    # an old reply of a former attempt, or an answer to something else
                head, _, body = got[0].partition(b"\n")
                return {"status": int(head.split()[2]), "text": body.decode("ascii", "replace"), "seq": seq,
                        "attempts": attempt, "ms": round((time.monotonic() - t0) * 1000.0, 1)}
        return {"status": None, "text": None, "seq": seq, "attempts": retries + 1,
                "ms": round((time.monotonic() - t0) * 1000.0, 1)}


def console_ustat(watch, timeout=3.0):
    """Counters of the LyraT from its own console, a witness the UART cannot influence."""
    for _ in range(3):
        since = watch.now()
        watch.send("ustat")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            watch.pump(time.monotonic() + 0.1)
            for t, line in reversed(watch.recent):
                if t < since:
                    break
                got = parse_ustat(line)
                if got:
                    return got
    return None


def set_lyrat_baud(watch, baud):
    since = watch.now()
    watch.send("ubaud %d" % baud)
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        watch.pump(time.monotonic() + 0.1)
        if any(t >= since and "ubaud = %d" % baud in line for t, line in watch.recent):
            return True
    return False


def line_check(watch, channel, baud=9600, seconds=6.0):
    """Both directions at a low speed. The S3 starts to drive GPIO1 only after it read valid frames from the LyraT."""
    r = {"baud": baud, "seconds": seconds, "checks": {}}
    c = r["checks"]
    channel.set_tx(0)
    time.sleep(0.2)
    idle = channel.query()
    r["idle"] = idle
    c["s3_answers"] = idle is not None
    if idle is None:
        return r
    # With the TX an input, GPIO1 reads the pull-down of the LyraT through the wire, GPIO2 the pull-up.
    c["gpio1_input"] = idle["tx_on"] == 0 and idle["pins_ok"] == 1
    r["gpio1_level_undriven"] = idle["tx_level"]
    c["gpio1_sees_pulldown"] = idle["tx_level"] == 0
    c["baud_set"] = bool(set_lyrat_baud(watch, baud) and channel.set_baud(baud) == baud)
    time.sleep(1.5)
    before = channel.query()
    watch.pump(time.monotonic() + seconds)
    after = channel.query()
    r["lyrat_to_s3"] = {k: after[k] - before[k] for k in ("hb", "frames", "bad_hdr", "bad_crc", "trunc", "noise", "rx_bytes")}
    r["lyrat_to_s3"]["gpio2_level"] = after["rx_level"]
    d = r["lyrat_to_s3"]
    c["lyrat_to_s3_frames"] = d["hb"] >= seconds - 2 and d["bad_crc"] == 0 and d["bad_hdr"] == 0
    if c["lyrat_to_s3_frames"]:
        before_ack = console_ustat(watch)
        channel.set_tx(1)
        time.sleep(0.3)
        driven = channel.query()
        r["driven"] = {k: driven[k] for k in ("tx_on", "tx_level", "pins_ok")}
        c["gpio1_output_ok"] = driven["tx_on"] == 1 and driven["pins_ok"] == 1 and driven["tx_level"] == 1
        watch.pump(time.monotonic() + seconds)
        after_ack = console_ustat(watch)
        r["s3_to_lyrat"] = {"acks": after_ack["acks"] - before_ack["acks"], "bad_crc": after_ack["bad_crc"] - before_ack["bad_crc"],
                            "bad_hdr": after_ack["bad_hdr"] - before_ack["bad_hdr"], "gpio13_level": after_ack["rx_level"],
                            "breaks": after_ack["brk"] - before_ack["brk"], "frm": after_ack["frm"] - before_ack["frm"]}
        c["s3_to_lyrat_frames"] = r["s3_to_lyrat"]["acks"] >= seconds - 2 and r["s3_to_lyrat"]["bad_crc"] == 0
        c["gpio13_high_when_driven"] = after_ack["rx_level"] == 1
        echo = channel.command("ustat", timeout=1.0)
        r["echo"] = {"status": echo["status"], "attempts": echo["attempts"], "ms": echo["ms"]}
        c["command_echo"] = echo["status"] == ST_OK and parse_ustat(echo["text"] or "") is not None
    c["baud_restored"] = bool(set_lyrat_baud(watch, BAUD) and channel.set_baud(BAUD) == BAUD)
    r["tx_mode_restored"] = channel.set_tx("A")
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("query")
    p = sub.add_parser("cmd")
    p.add_argument("text")
    p = sub.add_parser("tx")
    p.add_argument("mode", choices=["0", "1", "A"])
    p = sub.add_parser("linecheck")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--seconds", type=float, default=6.0)
    p.add_argument("--port", default=DEFAULT_PORT)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    channel = Channel()
    if not channel.find():
        print("S3 not found")
        return 2
    if args.action == "query":
        print(json.dumps(channel.query()))
    elif args.action == "cmd":
        got = channel.command(args.text)
        print(redact(got["text"] or ""))
        print(json.dumps({k: got[k] for k in ("status", "attempts", "ms")}))
        return 0 if got["status"] == ST_OK else 1
    elif args.action == "tx":
        print(channel.set_tx(args.mode))
    else:
        out = RUNS_ROOT / "uart" / ("%s-linecheck" % stamp())
        watch = SerialWatch(args.port, out)
        watch.pump(time.monotonic() + 8, until=lambda: watch.sp is not None)
        result = line_check(watch, channel, args.baud, args.seconds)
        watch.close_port()
        watch.finish(0)
        result["pass"] = bool(result["checks"]) and all(result["checks"].values())
        (out / "linecheck.json").write_text(redact(json.dumps(result, indent=2)), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0 if result["pass"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
