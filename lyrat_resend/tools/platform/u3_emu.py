"""Emulator of U3 (and U1, U2 behind it) on 127.0.0.1, for platform_ota.py and udplog.py without the boards. Goal 095,
based on lyrat_resend/tools/ota_test/s3_emu.py (its Board, Uart and receiver classes are imported, not copied).

UDP 5007: HELLO, QUERY (target 0: U3; 1/2: a node in detail), BEGIN/DATA/END/ABORT/STATUS for targets 0, 1, 2, 0x11,
0x12 as docs/PLATFORM_OTA.md says. The receivers, the relay sender and the UART frames are the C code of goal 090
(otahost.dll); the ROM job is rom_flasher.c (plathost.dll) against rom_emu.Esp32Rom, one per node. What this file adds
is the glue of u3_ota.c / u3_log.c / node_link.c written again in Python: nonce, staging, relay states, ROM job states,
restarts, self-test, rollback, consoles, CMD and MODE (R21 reserved: answered unsupported).

UDP 5008: LOG1 [u3] [u1] [u2] / LOG0 / CON / CMD / MODE; lines of U3, of U1 and U2 through ctl frames CTL_T_LOG into the
relay rings, and of their UART0 consoles (boards 0x83, 0x84; a node's boot text after a reset or a ROM job).

  python u3_emu.py --port 0 --log-port 0 --token-file F [--u1-image BIN] [--u2-image BIN] [--u3-image BIN]
                   [--u2-absent] [--uart-ber X] [--log-loss X] [--rom-faults JSON]
Prints one JSON line with the ports, then serves until killed.
"""
import argparse
import hashlib
import json
import os
import random
import select
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOLS / "ota_test"))
sys.path.insert(0, str(TOOLS / "bench"))
import otahost as H  # noqa: E402
import ota  # noqa: E402
import rom_emu  # noqa: E402
import rombundle  # noqa: E402
import romhost  # noqa: E402
from s3_emu import Board, LyratRx, Uart, now_ms  # noqa: E402

NODES = (("u1", 1, "u1_hf", 3), ("u2", 2, "u2_ag", 4))      # name, target, project, log board
SLOT_U3, SLOT_NODE, STAGE = 0x300000, 0x1F0000, 0x200000


class NodeRx(LyratRx):
    """The node's receiver with the STATUS text of node_link.c (the same fields as the LyraT's)."""


class BundleRx(H.CReceiver):
    """The ROM bundle receiver of u3_ota.c: the header check is rom_bundle.c."""

    def __init__(self, token, project):
        super().__init__(STAGE, None, None, token=token)
        self.size = 0
        self.node_project = project

    def _begin(self, ctx, size, flags):
        self.size = size
        return super()._begin(ctx, size, flags)

    def _head(self, ctx, p, n):
        import ctypes
        name, b, _ = romhost.bundle_parse(ctypes.string_at(p, n), size=self.size)
        if b:
            romhost.lib.host_free(b)
        return 0 if name == "ok" else ota.ST["bad_image"]

    def _finish(self, ctx, size, sha, flags):
        # rom_finish of u3_ota.c: the app in the bundle must be this node's project.
        data = bytes(self.flash[:size])
        for s in rombundle.parse(data)["segments"]:
            if s["offset"] == 0x20000 and ota.Image(data[s["data_at"]:s["data_at"] + s["size"]]).project != self.node_project:
                return ota.ST["bad_image"]
        return super()._finish(ctx, size, sha, flags)


class Node:
    def __init__(self, name, target, project, board_id, image, token, absent=False, rom_faults=None, ber=0.0):
        self.name, self.target, self.project, self.board_id = name, target, project, board_id
        self.board = Board(name, image)
        self.token = token
        self.absent = absent
        self.rx = NodeRx(SLOT_NODE, ota.CHIP_ESP32, project)
        self.rx.board = self.board
        self.uart = Uart(ber, seed=board_id)
        self.arm()
        self.relay, self.err, self.tx, self.staged = "idle", "", None, None
        self.stage_image, self.stage_flags, self.stage_elf = b"", 0, ""
        self.status, self.status_t, self.verify_until, self.next_status = "", 0.0, 0.0, 0.0
        self.rom_job, self.rom_err, self.rom_info, self.rom_staged, self.rom_t = "idle", "", {}, None, 0.0
        self.rom_faults = dict(rom_faults or {})
        self.clock = rom_emu.Clock()
        self.rom = rom_emu.Esp32Rom(self.clock, **self.rom_faults)
        self.ring = H.Ring(64, board_id, self.board.boot)
        self.relay_ring = H.Ring(64, board_id, 0)
        self.con_ring = H.Ring(32, 0x80 | board_id, 0)
        self.held = False
        self.lines = 0
        for i in range(3):
            self.ring.log(i, "I (%d) %s: boot line %d" % (i, project, i))
            self.con_ring.log(i, "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)" if i == 0 else
                              "I (%d) boot: console line %d" % (i, i))

    def arm(self):
        self.rx_app = H.CReceiver(SLOT_NODE, ota.CHIP_ESP32, self.project, token=self.token)
        self.rx_rom = BundleRx(self.token, self.project)
        self.rx_app.nonce = self.rx_rom.nonce = None

    def alive(self):
        return not self.absent and not self.held and self.board.up()

    def jobs_busy(self):
        return self.staged is not None or self.relay in ("send", "verify") or self.rom_staged is not None or \
            self.rom_job == "run"


class Emulator:
    def __init__(self, args):
        self.token = ota.read_token(args.token_file) if args.token_file else None
        tok = self.token or b""
        imgs = {k: Path(getattr(args, k + "_image")).read_bytes() for k in ("u3", "u1", "u2")}
        self.u3 = Board("u3", imgs["u3"])
        faults = json.loads(args.rom_faults) if args.rom_faults else {}
        self.nodes = [Node(n, t, p, b, imgs[n], tok, absent=(n == "u2" and args.u2_absent), rom_faults=faults,
                           ber=args.uart_ber) for n, t, p, b in NODES]
        self.rx_self = H.CReceiver(SLOT_U3, ota.CHIP_ESP32S3, "u3_relay", token=tok)
        self.rx_self.nonce = None
        self.nonce, self.nonce_t = None, 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", args.port))
        self.log = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.log.bind(("127.0.0.1", args.log_port))
        self.log_loss = args.log_loss
        self.log_peer, self.log_until = None, 0.0
        self.levels = [3, 3, 3]
        self.con_mode = "auto"
        self.askers = {}
        self.u3_ring = H.Ring(96, 2, self.u3.boot)
        for i in range(5):
            self.u3_ring.log(i, "I (%d) u3: boot line %d" % (i, i))
        self.line_n, self.next_line = 0, 0.0
        self.dropped_datagrams = 0
        self.rnd = random.Random(7)

    # --- UDP 5007 ----------------------------------------------------------------------------------------------------
    def node_of(self, target):
        i = (target & 0x0F) - 1
        return self.nodes[i] if 0 <= i < len(self.nodes) and not target & ~0x1F else None

    def handle_ota(self, data, peer):
        if len(data) < 5 or data[:2] != b"OT" or data[2] != 1:
            return
        target, op = data[3], data[4]
        head = data[:4]
        if not self.u3.up():
            return
        if op == ota.OP_HELLO:
            self.nonce, self.nonce_t = os.urandom(16), time.monotonic()
            rep = bytes([op]) + bytes(9) + self.nonce.hex().encode()
        elif op == ota.OP_QUERY:
            rep = bytes([op]) + bytes(9) + json.dumps(self.query(target)).encode()
        else:
            node = self.node_of(target) if target else None
            if target and not node:
                return
            rom = bool(target & 0x10)
            r = self.rx_self if not target else (node.rx_rom if rom else node.rx_app)
            fresh = self.nonce is not None and time.monotonic() - self.nonce_t < 60
            r.nonce = self.nonce if (fresh and self.token) else None
            if op == ota.OP_BEGIN:
                same = r.info()["state_name"] == "recv" and int.from_bytes(data[5:9], "little") == r.info()["session"]
                other = node and (node.rx_app if rom else node.rx_rom).info()["state_name"] == "recv"
                busy = (any(n.jobs_busy() for n in self.nodes) if not target else node.jobs_busy() or other)
                if busy and not same:
                    self.sock.sendto(head + bytes([op, ota.ST["refused"]]) + data[5:9] + bytes(4), peer)
                    return
                if not same:
                    self.nonce = None
            rep = r.handle(data[4:], now_ms())
            ev = r.events()
            if not target and ev & H.EV_DONE:
                self.u3.activate(r.active, bool(r.info()["flags"] & ota.F_FAILTEST))
            if target and op == ota.OP_BEGIN and r.info()["state_name"] == "recv":
                node.baud = r.info()["baud"]
            if target and op == ota.OP_END and rep[1] == 0:
                info = r.info()
                blob = r.active if r.active is not None else bytes(r.flash[:info["size"]])
                if rom:
                    if not info["flags"] & ota.F_DRYRUN:
                        node.rom_staged = (blob, getattr(node, "baud", 0))
                else:
                    node.staged = (blob, info["flags"])     # a dry run goes on to the node with its flag
        self.sock.sendto(head + rep, peer)

    # --- relay of a node's app (u3_ota.c relay_step) -----------------------------------------------------------------
    def relay_step(self, n):
        t = time.monotonic()
        if n.staged is not None and n.relay not in ("send", "verify"):
            image, flags = n.staged
            n.staged = None
            n.stage_image, n.stage_flags, n.stage_elf = image, flags, ota.Image(image).elf[:16]
            n.tx = H.CSender(image, window=2, rto=600)
            n.tx.start(random.randrange(1 << 31), hashlib.sha256(image).digest(), flags, 0, now_ms())
            n.relay, n.err, n.tx_wait = "send", "", t
        for d, kind, payload in n.uart.due():
            if d == "to_lyrat" and kind == H.CTL_T_OTA:
                if n.alive():
                    rep = n.rx.handle(payload, now_ms())
                    ev = n.rx.events()
                    if rep:
                        n.uart.send("to_s3", H.CTL_T_OTA_ACK, rep)
                    if ev & H.EV_DONE:
                        n.board.activate(n.rx.active, bool(n.rx.info()["flags"] & ota.F_FAILTEST), delay=0.3)
            elif d == "to_s3" and kind == H.CTL_T_OTA_ACK:
                if payload[0] == ota.OP_STATUS:
                    n.status, n.status_t = payload[10:].decode("ascii", "replace"), t
                elif n.relay == "send":
                    n.tx.on_reply(payload, now_ms())
            elif d == "to_s3" and kind == H.CTL_T_LOG:
                at = 0
                while at < len(payload):
                    rc, used = n.relay_ring.push_record(payload[at:])
                    if not used:
                        break
                    at += used
        n.rx.tick(now_ms())
        if n.relay == "send":
            if not n.alive():
                if t - n.tx_wait > 30:
                    n.relay, n.err = "failed", "node_silent"
                    n.arm()
                return
            n.tx_wait = t
            while True:
                req = n.tx.poll(now_ms())
                if req is None:
                    break
                n.uart.send("to_lyrat", H.CTL_T_OTA, req)
            st = n.tx.state
            if st == "done":
                if n.stage_flags & ota.F_DRYRUN:
                    n.relay = "done"
                    n.arm()
                else:
                    n.relay, n.verify_until, n.next_status, n.status = "verify", t + 180, t + 0.5, ""
            elif st == "failed":
                n.relay, n.err = "failed", n.tx.info()["fail_name"]
                n.arm()
        elif n.relay == "verify":
            f = dict(kv.split("=", 1) for kv in n.status.split() if "=" in kv)
            if f.get("elf") == n.stage_elf and f.get("img") == "valid":
                n.relay = "done"
                n.arm()
            elif f and f.get("elf") != n.stage_elf and f.get("inv") == n.stage_elf:
                n.relay = "rolled_back"
                n.arm()
            elif t > n.verify_until:
                n.relay, n.err = "failed", "verify_timeout"
                n.arm()
            elif t >= n.next_status:
                n.next_status, n.status = t + 0.5, ""
                n.uart.send("to_lyrat", H.CTL_T_OTA, ota.build_short(ota.OP_STATUS, 0))
        elif t >= n.next_status and n.alive():
            n.next_status = t + 2.0
            n.uart.send("to_lyrat", H.CTL_T_OTA, ota.build_short(ota.OP_STATUS, 0))

    # --- ROM job (u3_ota.c rom_task) ---------------------------------------------------------------------------------
    def rom_step(self, n):
        if n.rom_job == "come_up":
            if n.alive():
                n.rom_job = "done"
                n.arm()
            elif time.monotonic() - n.rom_t > 60:
                n.rom_job = "no_link"
                n.arm()
            return
        if n.rom_staged is None:
            return
        bundle, baud = n.rom_staged
        n.rom_job, n.held = "run", True
        f = romhost.CRomFlasher(n.rom, bundle, baud_high=baud)
        r = f.flash()
        n.rom.advance_to(n.clock.ms + 50)
        n.rom_info = f.info()
        f.close()
        n.rom_staged, n.held, n.rom_t = None, False, time.monotonic()
        boot_text = rom_emu.BOOT_TEXT_RUN.decode().strip().splitlines()
        for line in boot_text:
            if line:
                n.con_ring.log(now_ms(), line)
        if r == 0:
            # The node starts from the flash it was given: its app is what the bundle put at 0x20000.
            app = [s for s in rombundle.parse(bundle)["segments"] if s["offset"] == 0x20000]
            if app:
                s = app[0]
                n.board.run = ota.Image(bundle[s["data_at"]:s["data_at"] + s["size"]])
                n.board.slot, n.board.img, n.board.selftest = "ota_0", "undefined", "off"
            n.board.boot = random.randrange(65536)
            n.board.restarts += 1
            n.board.down_until = time.monotonic() + 0.5
            n.rx.reset()
            n.rom_job = "come_up"
        else:
            n.rom_job, n.rom_err = "failed", romhost.err_name(r)
            n.arm()

    def query(self, target):
        if target == 0:
            inv = self.u3.last_invalid
            q = {"fw": "u3relay", "boot": self.u3.boot, "up_ms": now_ms(), "ver": self.u3.run.version,
                 "elf": self.u3.run.elf[:16], "run": self.u3.slot, "img": self.u3.img, "selftest": self.u3.selftest,
                 "last_invalid": {"ver": inv.version, "elf": inv.elf[:16]} if inv else None,
                 "token": 1 if self.token else 0, "ip": "127.0.0.1", "rssi": -50, "console": self.con_mode,
                 "rx": {"target": 0, "state": self.rx_self.info()["state_name"]}}
            for n in self.nodes:
                q[n.name] = {"alive": int(n.alive()), "relay": n.relay, "rom": n.rom_job}
            return q
        n = self.node_of(target)
        if not n:
            return {}
        tx = n.tx.info() if n.tx else {}
        ri = n.rom_info
        return {"node": n.name, "link": {"alive": int(n.alive()), "boot": n.board.boot},
                "rx": [{"target": n.target, "state": n.rx_app.info()["state_name"],
                        "err": n.rx_app.info()["err_name"]},
                       {"target": 0x10 | n.target, "state": n.rx_rom.info()["state_name"],
                        "err": n.rx_rom.info()["err_name"]}],
                "relay": n.relay, "err": n.err, "tx": tx.get("state_name", "idle"), "sent": tx.get("sent", 0),
                "acked": tx.get("acked", 0), "size": len(n.stage_image), "resent": tx.get("resent", 0),
                "busy": tx.get("busy_replies", 0), "baud": 115200, "elf_want": n.stage_elf, "status": n.status,
                "run_elf": n.board.run.elf[:16], "run_project": n.board.run.project,
                "rom": {"job": n.rom_job, "phase": ri.get("phase_name", "idle"), "err": n.rom_err,
                        "seg": ri.get("seg", 0), "done": ri.get("done", 0), "total": ri.get("total", 0),
                        "syncs": ri.get("syncs", 0), "session_restarts": ri.get("session_restarts", 0),
                        "segment_restarts": ri.get("segment_restarts", 0), "blocks": ri.get("blocks", 0),
                        "baud": ri.get("baud", 0), "ms": ri.get("ms", 0)}}

    # --- log ---------------------------------------------------------------------------------------------------------
    def log_step(self):
        t = time.monotonic()
        if t >= self.next_line:
            self.next_line = t + 0.05
            self.line_n += 1
            self.u3_ring.log(now_ms(), "I (%d) u3: stat line %d" % (now_ms(), self.line_n))
            for n in self.nodes:
                if n.alive():
                    n.ring.log(now_ms(), "\x1b[0;33mW (%d) %s: line %d\x1b[0m" % (now_ms(), n.project, self.line_n))
            if self.line_n % 10 == 0:
                for n in self.nodes:
                    if n.alive() and self.listening_console(n):
                        n.con_ring.log(now_ms(), "I (%d) %s: console line %d" % (now_ms(), n.project, self.line_n))
        listening = self.log_peer is not None and t < self.log_until
        if not listening:
            return
        for n in self.nodes:
            if n.alive():
                payload, k = n.ring.pack(600)       # a node drains only with a listener
                if k:
                    n.uart.send("to_s3", H.CTL_T_LOG, payload)
        parts, count, room = [], 0, 1396
        for ring in [self.u3_ring] + [r for n in self.nodes for r in (n.relay_ring, n.con_ring)]:
            body, k = ring.pack(room)
            parts.append(body)
            count += k
            room -= len(body)
        if count:
            if self.rnd.random() < self.log_loss:
                self.dropped_datagrams += 1
            else:
                try:
                    self.log.sendto(b"UL" + bytes([1, min(count, 255)]) + b"".join(parts), self.log_peer)
                except OSError:
                    pass

    def listening_console(self, n):
        return self.con_mode == n.name or (self.con_mode == "auto" and n.name == "u1")

    def handle_log(self, data, peer):
        text = data.decode("ascii", "replace").strip()
        if text.startswith("LOG1"):
            vals = [int(x) for x in text.split()[1:4] if x.isdigit()]
            self.levels = (vals + self.levels[len(vals):])[:3]
            self.log_peer, self.log_until = peer, time.monotonic() + 10
        elif text.startswith("LOG0"):
            self.log_peer = None
        elif text.startswith("CON"):
            arg = text[3:].strip()
            if arg in ("auto", "u1", "u2", "off"):
                self.con_mode = arg
            listen = "u1" if self.con_mode == "auto" else self.con_mode
            self.log.sendto(("CON " + json.dumps({"mode": self.con_mode, "listening": listen})).encode(), peer)
            return
        elif text.startswith("CMD") or text.startswith("MODE"):
            parts = text.split(None, 2)
            n = {"u1": self.nodes[0], "u2": self.nodes[1]}.get(parts[1] if len(parts) > 1 else "")
            kind = "MA" if text.startswith("MODE") else "UR"
            if not n or len(parts) < 3:
                self.log.sendto(("%s bad_request" % kind).encode(), peer)
            elif not n.alive():
                self.log.sendto(("%s %s not_connected" % (kind, n.name)).encode(), peer)
            elif kind == "MA":
                self.log.sendto(("MA %s 2 %d" % (n.name, int(parts[2]))).encode(), peer)      # R21: unsupported
            elif parts[2] == "ver":
                self.log.sendto(("UR %s 0 %s %s" % (n.name, n.board.run.project, n.board.run.version)).encode(), peer)
            else:
                self.log.sendto(("UR %s 1 " % n.name).encode(), peer)
            return
        state = {"emulator": True, "levels": self.levels, "dropped_datagrams": self.dropped_datagrams}
        self.log.sendto(b"LOG %d " % (self.log_peer is not None) + json.dumps(state).encode(), peer)

    def serve(self):
        while True:
            ready, _, _ = select.select([self.sock, self.log], [], [], 0.002)
            for s in ready:
                try:
                    data, peer = s.recvfrom(4096)
                except OSError:
                    continue
                if s is self.sock:
                    self.handle_ota(data, peer)
                else:
                    self.handle_log(data, peer)
            self.rx_self.tick(now_ms())
            if self.u3.step():
                self.rx_self = H.CReceiver(SLOT_U3, ota.CHIP_ESP32S3, "u3_relay", token=self.token or b"")
                self.rx_self.nonce = None
            for n in self.nodes:
                n.rx_app.tick(now_ms())
                n.rx_rom.tick(now_ms())
                if n.board.step():
                    n.rx.reset()
                self.relay_step(n)
                self.rom_step(n)
            self.log_step()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--log-port", type=int, default=0)
    ap.add_argument("--token-file")
    ap.add_argument("--u3-image", required=True)
    ap.add_argument("--u1-image", required=True)
    ap.add_argument("--u2-image", required=True)
    ap.add_argument("--u2-absent", action="store_true")
    ap.add_argument("--uart-ber", type=float, default=0.0)
    ap.add_argument("--log-loss", type=float, default=0.0)
    ap.add_argument("--rom-faults")
    args = ap.parse_args()
    emu = Emulator(args)
    print(json.dumps({"port": emu.sock.getsockname()[1], "log_port": emu.log.getsockname()[1]}), flush=True)
    emu.serve()


if __name__ == "__main__":
    main()
