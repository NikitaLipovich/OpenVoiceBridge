"""Emulator of the S3-ETH (and the LyraT behind it) on 127.0.0.1, for ota.py --dry-run and udplog.py without the bench.

UDP OTA port: HELLO, QUERY, BEGIN/DATA/END/ABORT/STATUS for target 0 (the S3) and 1 (the LyraT), as docs/OTA.md says.
The receivers, the relay sender and the UART frames are the firmware's own C code (otahost.dll); what this file adds is
the glue of s3_ota.c written again in Python: nonce, staging, relay states, restarts, self-test, rollback.

UDP log port: LOG1/LOG0; lines of both boards from the C log ring, the LyraT's through a ctl frame CTL_T_LOG and the
S3's relay ring, as on the boards. --log-loss drops whole datagrams, so udplog.py must count them as lost on the way.

  python s3_emu.py --port 0 --log-port 0 --token-file F [--uart-ber 1e-6] [--lyrat-slc] [--failtest-ignored]
Prints one JSON line with the ports, then serves until killed.
"""
import argparse
import hashlib
import heapq
import json
import os
import random
import select
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "bench"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BENCH))
import otahost as H  # noqa: E402
import ota  # noqa: E402

BUILDS = Path(os.environ.get("OTA_TEST_BUILDS", HERE / "images"))
S3_BIN = BUILDS / "s3-base" / "s3_eth_bridge.bin"
LYRAT_BIN = BUILDS / "lyrat-base" / "lyrat_resend.bin"


def now_ms():
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


class Board:
    """Running image, its state, restarts and the self-test."""

    def __init__(self, name, image: bytes, selftest_s=2.0):
        self.name = name
        self.run = ota.Image(image)
        self.slot = "ota_0"
        self.img = "valid"
        self.selftest = "off"
        self.boot = random.randrange(65536)
        self.last_invalid = None
        self.pending = None            # (restart time, new image, failtest)
        self.selftest_s = selftest_s
        self.check_at = None
        self.failtest = False
        self.old = None
        self.restarts = 0
        self.down_until = 0.0

    def activate(self, image: bytes, failtest: bool, delay=0.5):
        self.pending = (time.monotonic() + delay, ota.Image(image), failtest)

    def step(self):
        t = time.monotonic()
        if self.pending and t >= self.pending[0]:
            _, img, failtest = self.pending
            self.pending = None
            self.old, self.run = self.run, img
            self.slot = "ota_1" if self.slot == "ota_0" else "ota_0"
            self.img, self.selftest = "pending", "wait"
            self.failtest = failtest
            self.check_at = t + self.selftest_s
            self.boot = random.randrange(65536)
            self.restarts += 1
            self.down_until = t + 1.0
            return "restarted"
        if self.check_at and t >= self.check_at:
            self.check_at = None
            if self.failtest:          # the new image fails on purpose: the bootloader brings the old one back
                self.last_invalid = self.run
                self.run = self.old
                self.slot = "ota_1" if self.slot == "ota_0" else "ota_0"
                self.img, self.selftest = "valid", "off"
                self.boot = random.randrange(65536)
                self.restarts += 1
                self.down_until = t + 1.0
                return "rolled_back"
            self.img, self.selftest = "valid", "pass"
        return None

    def up(self):
        t = time.monotonic()
        return t >= self.down_until and (self.pending is None or t < self.pending[0])


class LyratRx(H.CReceiver):
    """The LyraT's receiver with the STATUS text of lyrat_ota.c."""

    board = None

    def _stext(self, ctx, out, cap):
        b = self.board
        inv = b.last_invalid.elf[:16] if b.last_invalid else "-"
        text = ("run=%s ver=%s elf=%s img=%s inv=%s selftest=%s baud=115200" % (
            b.slot, b.run.version.replace(" ", "_")[:23], b.run.elf[:16], b.img, inv, b.selftest)).encode()[:cap - 1]
        import ctypes
        ctypes.memmove(out, text, len(text))
        return len(text)


class Uart:
    """Both directions of the control UART: ctl frames from the C code, bit errors, a latency, the C parser."""

    def __init__(self, ber=0.0, latency_ms=5, seed=1):
        self.ber, self.latency = ber, latency_ms
        self.rnd = random.Random(seed)
        self.q = []
        self.n = 0
        self.parsers = {"to_lyrat": H.CtlParser(), "to_s3": H.CtlParser()}
        self.lost = 0

    def send(self, direction, kind, payload):
        raw = bytearray(H.ctl_frame(kind, self.n & 0xFFFF, payload))
        if self.ber:
            for i in range(len(raw)):
                for bit in range(8):
                    if self.rnd.random() < self.ber:
                        raw[i] ^= 1 << bit
        self.n += 1
        heapq.heappush(self.q, (time.monotonic() + self.latency / 1000.0, self.n, direction, bytes(raw)))

    def due(self):
        out = []
        t = time.monotonic()
        while self.q and self.q[0][0] <= t:
            _, _, d, raw = heapq.heappop(self.q)
            frames = self.parsers[d].feed(raw, now_ms())
            if not frames:
                self.lost += 1
            out += [(d, k, p) for (k, _s, p) in frames]
        return out


class Emulator:
    def __init__(self, args):
        self.token = ota.read_token(args.token_file) if args.token_file else None
        self.s3 = Board("s3", (Path(args.s3_image) if args.s3_image else S3_BIN).read_bytes())
        self.lyrat = Board("lyrat", (Path(args.lyrat_image) if args.lyrat_image else LYRAT_BIN).read_bytes())
        self.rx = [H.CReceiver(0x300000, ota.CHIP_ESP32S3, "s3_eth_bridge", token=self.token or b""),
                   H.CReceiver(0x1F0000, ota.CHIP_ESP32, "lyrat_resend", token=self.token or b"")]
        for r in self.rx:
            r.nonce = None
        self.ly_rx = LyratRx(0x1F0000, ota.CHIP_ESP32, "lyrat_resend")
        self.ly_rx.board = self.lyrat
        self.ly_rx.slc = args.lyrat_slc
        self.uart = Uart(args.uart_ber)
        self.nonce = None
        self.nonce_t = 0.0
        self.relay = "idle"
        self.relay_err = ""
        self.tx = None
        self.staged = None
        self.stage_flags = 0
        self.lyrat_status = ""
        self.lyrat_status_t = 0.0
        self.verify_until = 0.0
        self.next_status = 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", args.port))
        self.log = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.log.bind(("127.0.0.1", args.log_port))
        self.log_loss = args.log_loss
        self.log_peer = None
        self.log_until = 0.0
        self.s3_ring = H.Ring(128, 0, self.s3.boot)
        self.ly_ring = H.Ring(96, 1, self.lyrat.boot)
        self.relay_ring = H.Ring(128, 1, 0)
        self.line_n = 0
        self.next_line = 0.0
        self.dropped_datagrams = 0
        self.rnd = random.Random(7)
        for i in range(5):          # lines of the start, before any listener
            self.s3_ring.log(i, "I (%d) s3eth: boot line %d" % (i, i))
            self.ly_ring.log(i, "I (%d) LyraT: boot line %d" % (i, i))

    # --- target receivers: the token check as in s3_ota.c ------------------------------------------------------------
    def handle_ota(self, data, peer):
        if len(data) < 5 or data[:2] != b"OT" or data[2] != 1:
            return
        target, op = data[3], data[4]
        head = data[:4]
        if not self.s3.up():
            return                      # restarting: nobody answers
        if op == ota.OP_HELLO:
            self.nonce, self.nonce_t = os.urandom(16), time.monotonic()
            rep = bytes([op]) + bytes(9) + self.nonce.hex().encode()
        elif op == ota.OP_QUERY:
            rep = bytes([op]) + bytes(9) + json.dumps(self.query()).encode()
        elif target in (0, 1):
            if target == 1 and self.rx[1].info()["state_name"] == "done" and self.relay not in ("send", "verify")                     and self.staged is None:
                self.rx[1] = H.CReceiver(0x1F0000, ota.CHIP_ESP32, "lyrat_resend", token=self.token or b"")
            r = self.rx[target]
            # The nonce lives 60 s and serves one BEGIN; without a token everything is refused.
            fresh = self.nonce is not None and time.monotonic() - self.nonce_t < 60
            r.nonce = self.nonce if (fresh and self.token) else None
            if op == ota.OP_BEGIN:
                busy = self.relay in ("send", "verify") or self.staged is not None
                if busy and not (r.info()["state_name"] == "recv" and
                                 int.from_bytes(data[5:9], "little") == r.info()["session"]):
                    rep = bytes([op, ota.ST["refused"]]) + data[5:9] + bytes(4)
                    self.sock.sendto(head + rep, peer)
                    return
                if r.info()["state_name"] != "recv" or int.from_bytes(data[5:9], "little") != r.info()["session"]:
                    self.nonce = None
            rep = r.handle(data[4:], now_ms())
            ev = r.events()
            if target == 0 and ev & H.EV_DONE:
                self.s3.activate(r.active, bool(r.info()["flags"] & ota.F_FAILTEST))
                self.rx[0] = H.CReceiver(0x300000, ota.CHIP_ESP32S3, "s3_eth_bridge", token=self.token or b"")
            if target == 1 and op == ota.OP_END and rep[1] == 0:
                info = r.info()
                image = r.active if r.active is not None else bytes(r.flash[:info["size"]])
                self.staged = (image, info["flags"], info["baud"])
        else:
            return
        self.sock.sendto(head + rep, peer)

    # --- relay to the LyraT, the states of s3_ota.c ------------------------------------------------------------------
    def relay_step(self):
        t = time.monotonic()
        if self.staged is not None and self.relay not in ("send", "verify"):
            image, flags, baud = self.staged
            self.staged = None
            self.stage_image, self.stage_flags = image, flags
            self.stage_elf = ota.Image(image).elf[:16]
            self.tx = H.CSender(image, window=2, rto=600)
            self.tx.start(random.randrange(1 << 31), hashlib.sha256(image).digest(), flags, 0, now_ms())
            self.relay, self.relay_err = "send", ""
        for d, kind, payload in self.uart.due():
            if d == "to_lyrat" and kind == H.CTL_T_OTA:
                if self.lyrat.up():
                    rep = self.ly_rx.handle(payload, now_ms())
                    ev = self.ly_rx.events()
                    if rep:
                        self.uart.send("to_s3", H.CTL_T_OTA_ACK, rep)
                    if ev & H.EV_DONE:
                        self.lyrat.activate(self.ly_rx.active, bool(self.ly_rx.info()["flags"] & ota.F_FAILTEST),
                                            delay=0.3)
            elif d == "to_s3" and kind == H.CTL_T_OTA_ACK:
                if payload[0] == ota.OP_STATUS:
                    self.lyrat_status = payload[10:].decode("ascii", "replace")
                    self.lyrat_status_t = t
                elif self.relay == "send":
                    self.tx.on_reply(payload, now_ms())
            elif d == "to_s3" and kind == H.CTL_T_LOG:
                at = 0
                while at < len(payload):
                    rc, used = self.relay_ring.push_record(payload[at:])
                    if not used:
                        break
                    at += used
        self.ly_rx.tick(now_ms())
        if self.relay == "send":
            while True:
                req = self.tx.poll(now_ms())
                if req is None:
                    break
                self.uart.send("to_lyrat", H.CTL_T_OTA, req)
            st = self.tx.state
            if st == "done":
                if self.stage_flags & ota.F_DRYRUN:
                    self.relay = "done"
                else:
                    self.relay, self.verify_until, self.next_status = "verify", t + 180, t + 0.5
                    self.lyrat_status = ""
            elif st == "failed":
                self.relay, self.relay_err = "failed", self.tx.info()["fail_name"]
        elif self.relay == "verify":
            f = dict(kv.split("=", 1) for kv in self.lyrat_status.split() if "=" in kv)
            if f.get("elf") == self.stage_elf and f.get("img") == "valid":
                self.relay = "done"
            elif f and f.get("elf") != self.stage_elf and f.get("inv") == self.stage_elf:
                self.relay = "rolled_back"
            elif f.get("state") == "done":
                self.verify_until = t + 180     # image taken, the restart waits for the end of a call
                if t >= self.next_status:
                    self.next_status = t + 0.5
                    self.lyrat_status = ""
                    self.uart.send("to_lyrat", H.CTL_T_OTA, ota.build_short(ota.OP_STATUS, 0))
            elif t > self.verify_until:
                self.relay, self.relay_err = "failed", "verify_timeout"
            elif t >= self.next_status:
                self.next_status = t + 0.5
                self.lyrat_status = ""
                self.uart.send("to_lyrat", H.CTL_T_OTA, ota.build_short(ota.OP_STATUS, 0))
        elif t >= self.next_status:
            self.next_status = t + 2.0
            self.uart.send("to_lyrat", H.CTL_T_OTA, ota.build_short(ota.OP_STATUS, 0))

    def query(self):
        tx = self.tx.info() if self.tx else {}
        inv = self.s3.last_invalid
        return {"fw": "s3tap", "boot": self.s3.boot, "up_ms": now_ms(), "ver": self.s3.run.version,
                "elf": self.s3.run.elf[:16], "run": self.s3.slot, "img": self.s3.img, "selftest": self.s3.selftest,
                "last_invalid": {"ver": inv.version, "elf": inv.elf[:16]} if inv else None,
                "token": 1 if self.token else 0,
                "rx": [{"target": i, "state": r.info()["state_name"], "size": r.info()["size"],
                        "next": r.info()["next"], "err": r.info()["err_name"]} for i, r in enumerate(self.rx)],
                "lyrat": {"relay": self.relay, "err": self.relay_err, "tx": tx.get("state_name", "failed"),
                          "sent": tx.get("sent", 0), "acked": tx.get("acked", 0), "size": len(self.stage_image)
                          if self.tx else 0, "resent": tx.get("resent", 0), "busy": tx.get("busy_replies", 0),
                          "baud": 115200, "elf_want": getattr(self, "stage_elf", ""), "status": self.lyrat_status,
                          "status_age_ms": int((time.monotonic() - self.lyrat_status_t) * 1000)
                          if self.lyrat_status_t else -1}}

    # --- log ----------------------------------------------------------------------------------------------------------
    def log_step(self):
        t = time.monotonic()
        if t >= self.next_line:
            self.next_line = t + 0.05
            self.line_n += 1
            self.s3_ring.log(now_ms(), "I (%d) s3eth: stat line %d" % (now_ms(), self.line_n))
            self.ly_ring.log(now_ms(), "\x1b[0;33mW (%d) LyraT: line %d\x1b[0m" % (now_ms(), self.line_n))
        listening = self.log_peer is not None and t < self.log_until
        if listening:
            payload, n = self.ly_ring.pack(600)          # the LyraT drains only with a listener
            if n:
                self.uart.send("to_s3", H.CTL_T_LOG, payload)
            body, a = self.s3_ring.pack(1396)
            body2, b = self.relay_ring.pack(1396 - len(body))
            if a + b:
                if self.rnd.random() < self.log_loss:
                    self.dropped_datagrams += 1
                else:
                    try:
                        self.log.sendto(b"UL" + bytes([1, a + b]) + body + body2, self.log_peer)
                    except OSError:
                        pass

    def handle_log(self, data, peer):
        if data.startswith(b"LOG1"):
            self.log_peer, self.log_until = peer, time.monotonic() + 10
        elif data.startswith(b"LOG0"):
            self.log_peer = None
        state = {"emulator": True, "dropped_datagrams": self.dropped_datagrams}
        self.log.sendto(b"LOG %d " % (self.log_peer is not None) + json.dumps(state).encode(), peer)

    def serve(self):
        while True:
            ready, _, _ = select.select([self.sock, self.log], [], [], 0.002)
            for s in ready:
                try:
                    data, peer = s.recvfrom(4096)
                except OSError:         # Windows: an ICMP "port unreachable" of an earlier send
                    continue
                if s is self.sock:
                    self.handle_ota(data, peer)
                else:
                    self.handle_log(data, peer)
            for r in self.rx:
                r.tick(now_ms())
            ev = self.s3.step()
            if ev:
                self.rx = [H.CReceiver(0x300000, ota.CHIP_ESP32S3, "s3_eth_bridge", token=self.token or b""),
                           H.CReceiver(0x1F0000, ota.CHIP_ESP32, "lyrat_resend", token=self.token or b"")]
            if self.lyrat.step():
                self.ly_rx.reset()      # a restart of the LyraT: its session is gone
            self.relay_step()
            self.log_step()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--log-port", type=int, default=0)
    ap.add_argument("--token-file")
    ap.add_argument("--s3-image")
    ap.add_argument("--lyrat-image")
    ap.add_argument("--uart-ber", type=float, default=0.0)
    ap.add_argument("--lyrat-slc", action="store_true")
    ap.add_argument("--log-loss", type=float, default=0.0)
    args = ap.parse_args()
    emu = Emulator(args)
    print(json.dumps({"port": emu.sock.getsockname()[1], "log_port": emu.log.getsockname()[1]}), flush=True)
    emu.serve()


if __name__ == "__main__":
    main()
