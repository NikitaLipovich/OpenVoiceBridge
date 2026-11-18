#!/usr/bin/env python3
"""The console of a bench board without USB: NetWatch, a SerialWatch whose lines come over the network. Goal 093.

The LyraT's log lines reach the PC through the S3 (UART, then UDP 5008, docs/OTA.md "Debug log"), its console commands go
through the S3's relay (UDP 5006 "UC", docs/S3_UART.md), and its reset is the console command "reboot 1": the S3 has no
wire to the LyraT's EN (R13). The S3's own lines come the same way, and its restart is "UX <boot>" on 5006.

NetWatch keeps the interface of serial_watch.SerialWatch, so the bench tools take either:
  pump(end, until)    reads lines until a time or a condition; finds the S3 and starts listening as needed
  send(text)          one console command; the lines of its reply are written as the console prints them
                      ("I (<uptime>) CMD: <line>"), so the parsers of the tools read them unchanged
  pulse_reset()       LyraT: "reboot 1" through the relay; S3: "UX <boot>"
  recent, resets, slc_ups, slc_up, panics ...; finish(seconds) -> summary.json as SerialWatch, plus "transport": "net"
                      and "net": records, lost on the board, lost on the way, restarts seen
  sp                  None until the S3 answered; sp.close() lets go as a lost port would (the next pump comes back)
A restart is a new boot id in the records. It is written into serial.log as a ROM banner line,
"rst:0x<code> (<REASON>_RESET) [net: new boot id ...]", with the reason and code of the firmware's first line
"BOOT reset_reason=<REASON> code=<code>" when that line is the first record of the new boot (UNKNOWN otherwise), so the
same parsers count it. Limits against USB: the LyraT sends at most 2 KB/s and keeps 96 lines (more is counted as lost);
lines from before the log ring starts (ROM banner, "App version") never come; a LyraT that hangs cannot be reset this way.

One listener per S3: the S3 sends its log to the last PC that asked. All NetWatch of one process share one listener (LogHub).
While benchd runs (goal 103) LogHub listens to its relay instead (benchd_client.log_endpoint), so tools and the service
do not take the log from one another.
open_watch(port, out, ...) picks the transport: USB when a port is given, --no-open is asked, or BENCH_USB=1; the network
otherwise. As a module: NetWatch, LogHub, open_watch(), usb_mode(), add_usb_args(), apply_usb_args().
"""
import argparse
import collections
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import DEFAULT_PORT  # noqa: E402
from serial_watch import FW_RST, SerialWatch  # noqa: E402
import udplog  # noqa: E402

BOARD_IDS = {"s3": 0, "lyrat": 1}
BOOT_CODE = re.compile(r"BOOT reset_reason=([A-Z0-9_]+) code=(\d+)")
REBOOT_RETRIES = 2          # 3 tries in 0.75 s: all of them before the LyraT restarts (REBOOT_DELAY_MS, 1 s)
LOG_LEVELS = (3, 3, 3)
JOIN_WAIT_S = 0.3      # I for both boards; the third is U2's and ignored by an S3


def usb_mode():
    return os.environ.get("BENCH_USB") == "1"


def add_usb_args(ap, port_help="console port of the LyraT; giving it means USB"):
    ap.add_argument("--usb", action="store_true", help="the console over USB (COM) instead of the network")
    ap.add_argument("--port", default=None, help=port_help)


def apply_usb_args(args):
    """--usb or --port: USB for this tool and the tools it starts (BENCH_USB=1). Returns the port or None."""
    if getattr(args, "usb", False) or getattr(args, "port", None):
        os.environ["BENCH_USB"] = "1"
        args.port = args.port or DEFAULT_PORT
    return args.port


def find_host():
    host = os.environ.get("BENCH_S3_ADDR")
    if host:
        return host
    import ota
    return ota.find_host(argparse.Namespace(host=None))


class LogHub:
    """One UDP listener of the S3's log for this process: asks with LOG1 every 2 s, hands every record to the watches."""

    hubs = {}
    lock = threading.Lock()

    @classmethod
    def get(cls, host, port=udplog.LOG_PORT):
        with cls.lock:
            hub = cls.hubs.get((host, port))
            if hub is None or not hub.thread.is_alive():
                hub = cls.hubs[(host, port)] = LogHub(host, port)
            return hub

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.subs = {}
        self.datagrams = 0
        self.relay = None
        self.halt = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("", 0))
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def subscribe(self, watch):
        with LogHub.lock:
            self.subs[id(watch)] = collections.deque()
            return self.subs[id(watch)]

    def unsubscribe(self, watch):
        with LogHub.lock:
            self.subs.pop(id(watch), None)
            last = not self.subs
            if last:
                LogHub.hubs.pop((self.host, self.port), None)
        if last:
            self.halt.set()
            self.thread.join(timeout=2.0)

    def run(self):
        request = ("LOG1 %d %d %d" % LOG_LEVELS).encode()
        next_ask = 0.0
        try:
            while not self.halt.is_set():
                now = time.monotonic()
                if now >= next_ask:
                    try:
                        self.sock.sendto(request, (self.host, self.port))
                    except OSError:
                        pass
                    next_ask = now + udplog.LEASE_REPEAT_S
                try:
                    data, peer = self.sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:         # Windows: the ICMP of an earlier send while the S3 restarts
                    continue
                if peer[0] != self.host:
                    continue
                if data.startswith(b"LOG "):
                    self.relay = data[4:200].decode("ascii", "replace")
                    continue
                recs = udplog.parse_datagram(data)
                if recs is None:
                    continue
                self.datagrams += 1
                at = time.monotonic()
                with LogHub.lock:
                    queues = list(self.subs.values())
                for q in queues:
                    for rec in recs:
                        q.append((at, rec))
        finally:
            try:
                self.sock.sendto(b"LOG0", (self.host, self.port))
            except OSError:
                pass
            self.sock.close()


class _Handle:
    """What SerialWatch keeps in sp: here only close(), which drops the listener as a lost port would."""

    def __init__(self, watch):
        self.watch = watch

    def close(self):
        self.watch.lose()


class NetWatch(SerialWatch):
    transport = "net"

    def __init__(self, out, board="lyrat", echo=False, host=None):
        super().__init__("net:" + board, out, False, echo=echo)
        self.board, self.board_id = board, BOARD_IDS[board]
        self.host = host
        self.hub = self.queue = self.channel = None
        self.boot = self.seq = self.lost = None
        self.joiner = udplog.Joiner()
        self.idle_since = time.monotonic()
        self.datagrams = self.hub_datagrams_at = 0
        self.net = {"records": 0, "lost_board": 0, "lost_way": 0, "duplicates": 0, "boots": 0, "replies": 0,
                    "commands_failed": 0}

    # --- the connection ----------------------------------------------------------------------------------------------
    def connect(self):
        host = self.host or find_host()
        if not host:
            self.event("open_failed", denied=False, err="S3 not found on the network")
            return False
        self.host = host
        import s3_uart
        try:
            self.channel = s3_uart.Channel(host)
        except OSError as e:
            self.event("open_failed", denied=False, err=str(e)[:80])
            return False
        if self.channel.query() is None:
            self.channel.close()
            self.channel = None
            self.event("open_failed", denied=False, err="S3 does not answer")
            return False
        import benchd_client
        # While benchd runs it holds the S3's log; its relay speaks the same protocol (goal 103).
        self.hub = LogHub.get(*(benchd_client.log_endpoint(host) or (host, udplog.LOG_PORT)))
        self.queue = self.hub.subscribe(self)
        self.hub_datagrams_at = self.hub.datagrams
        self.sp = _Handle(self)
        self.port_opens += 1
        self.event("port_open", transport="net", board=self.board)
        return True

    def lose(self):
        """The listener goes, as a USB port that vanished: counted as a drop, the next pump connects again."""
        if self.sp is None:
            return
        self.disconnect()
        self.port_drops += 1
        self.event("port_drop", transport="net")

    def disconnect(self):
        for whole in self.joiner.flush():
            self.line(whole)
        if self.hub is not None:
            self.datagrams += self.hub.datagrams - self.hub_datagrams_at
            self.hub.unsubscribe(self)
        if self.channel is not None:
            self.channel.close()
        self.hub = self.queue = self.channel = self.sp = None

    # --- lines ---------------------------------------------------------------------------------------------------------
    def line(self, rec):
        self.feed(rec["text"].encode("utf-8", "replace"))

    def record(self, rec):
        if rec["board"] != self.board_id:
            return
        self.net["records"] += 1
        if self.boot is not None and rec["boot"] != self.boot:
            for whole in self.joiner.flush():
                self.line(whole)
            self.net["boots"] += 1
            m = BOOT_CODE.search(rec["text"]) if rec["seq"] == 0 else None
            reason, code = (m.group(1), int(m.group(2))) if m else ("UNKNOWN", 0)
            self.feed(("rst:0x%x (%s_RESET) [net: new boot id %04x, first line %d]"
                       % (code, reason, rec["boot"], rec["seq"])).encode())
            self.seq = self.lost = None
        self.boot = rec["boot"]
        if self.seq is not None:
            if rec["seq"] <= self.seq:
                self.net["duplicates"] += 1
                return
            gap = rec["seq"] - self.seq - 1
            on_board = max(0, rec["lost"] - self.lost)
            self.net["lost_board"] += on_board
            self.net["lost_way"] += max(0, gap - on_board)
        self.seq, self.lost = rec["seq"], rec["lost"]
        for whole in self.joiner.feed(rec):
            self.line(whole)

    def pump(self, end, until=None):
        while time.monotonic() < end and not (until and until()):
            if self.sp is None:
                if not self.connect():
                    time.sleep(0.5)
                continue
            took = False
            queue = self.queue          # sp.close() from another thread (call_record's fault) may drop it meanwhile
            while queue:
                took = True
                self.record(queue.popleft()[1])
                if until and until():
                    return
            if took:
                self.idle_since = time.monotonic()
            else:
                # A line held for its continuation is complete when nothing followed it for a while.
                if self.joiner.held and time.monotonic() - self.idle_since > JOIN_WAIT_S:
                    for whole in self.joiner.flush():
                        self.line(whole)
                time.sleep(0.02)

    # --- commands and reset --------------------------------------------------------------------------------------------
    def send(self, text, timeout=0.25, retries=7):
        if self.board != "lyrat":
            self.event("send_no_console", text=text)
            return False
        if self.channel is None:
            self.event("send_no_port", text=text)
            return False
        got = self.channel.command(text, timeout=timeout, retries=retries)
        if got["status"] is None:
            self.net["commands_failed"] += 1
            self.event("send_failed", text=text, attempts=got["attempts"])
            return False
        self.net["replies"] += 1
        self.event("sent", text=text, status=got["status"], attempts=got["attempts"], ms=got["ms"])
        up = self.last_uptime if self.last_uptime is not None else 0
        for reply in (got["text"] or "").splitlines():
            self.feed(("I (%d) CMD: %s" % (up, reply)).encode("utf-8", "replace"))
        return True

    def pulse_reset(self, hold_s=0.15):
        if self.channel is None:
            self.pump(time.monotonic() + 8, until=lambda: self.sp is not None)
        if self.channel is None:
            self.event("reset_failed", err="no S3")
            return False
        if self.board == "lyrat":
            ok = self.send("reboot 1", retries=REBOOT_RETRIES)
        else:
            q = self.channel.query()
            got = self.channel._ask("UX %d" % q["boot"], lambda d: d.startswith(b"UX ")) if q else None
            ok = bool(got and got.split()[-1] == b"1")
        self.event("reset_pulse" if ok else "reset_failed", transport="net")
        return ok

    def close_port(self):
        """Nothing to let go for esptool here: the listener stays until finish()."""

    def summary(self, seconds):
        out = super().summary(seconds)
        out["transport"] = "net"
        live = self.hub.datagrams - self.hub_datagrams_at if self.hub else 0
        out["net"] = dict(self.net, host_found=self.host is not None, datagrams=self.datagrams + live)
        return out

    def finish(self, seconds):
        self.disconnect()
        return super().finish(seconds)


def open_watch(port, out, no_open=False, echo=False, mixin=None, board="lyrat"):
    """SerialWatch on USB when a port is given, --no-open is asked, or BENCH_USB=1; NetWatch otherwise. mixin: a class
    that adds to feed()/add_reset() (call_record.CallMixin), put in front of the transport."""
    if port or no_open or usb_mode():
        base, args = SerialWatch, (port or DEFAULT_PORT, out, no_open)
        kw = {"echo": echo}
    else:
        base, args = NetWatch, (out,)
        kw = {"echo": echo, "board": board}
    cls = type((mixin.__name__ if mixin else "") + base.__name__, (mixin, base), {}) if mixin else base
    return cls(*args, **kw)


def main():
    """A console of the LyraT (or the S3) over the network for N seconds, as serial_watch.py does over USB."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--board", choices=sorted(BOARD_IDS), default="lyrat")
    ap.add_argument("--send", action="append", default=[], help="console command to send once connected (LyraT)")
    ap.add_argument("--reset", action="store_true", help="restart the board once connected")
    ap.add_argument("--echo", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import json
    w = NetWatch(args.out, board=args.board, echo=args.echo)
    end = time.monotonic() + args.seconds
    w.pump(min(end, time.monotonic() + 10), until=lambda: w.sp is not None)
    for text in args.send:
        w.send(text)
    if args.reset:
        w.pulse_reset()
    w.pump(end)
    result = w.finish(args.seconds)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["port_opens"] else 2


if __name__ == "__main__":
    sys.exit(main())
