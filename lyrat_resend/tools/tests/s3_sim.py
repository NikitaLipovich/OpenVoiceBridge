"""The S3 (Wi-Fi firmware of s3_eth_bridge) and the LyraT behind it, on 127.0.0.1, for the bench tools without the bench.

  python s3_sim.py [--host 127.0.0.1] [--tap-port 5006] [--ota-port 5007] [--log-port 5008] [--stream-port-call 5004]
                   [--stream-port-mic 5005] [--token-file F] [--s3-image BIN] [--lyrat-image BIN] [--uart-ber X]
                   [--stream-loss P] [--stream-dup P] [--lyrat-silent]
Prints one JSON line with the ports, then serves until killed. The tools reach it with BENCH_S3_ADDR=127.0.0.1.

What it repeats, each from the firmware source, not from the documents:
  5006  TAP1 [<call> <mic>] / TAP0 with the JSON reply of request_task(); UC/UQ/UT/UB/UW of ctl_request(): the command
        goes as a ctl frame (the firmware's C framing, otahost.dll) over a modelled UART to a modelled LyraT console,
        which runs it once per seq and answers a repeat from its copy, as ctl_on_command() of lyrat_resend/main/main.c
  5004/5005  stream packets of listener_send(): 24-byte header, PCM 16 bit, DMA_FRAMES frames each, real time;
        the call carries 1 kHz on L and R, the mic 440 Hz on L and 2 kHz on R; LEASE_MS after the last TAP1
  5007/5008  OTA and log of ota_test/s3_emu.py (the C receivers and log rings); the LOG reply as in s3_dbglog.c,
        and a stat line every STATUS_MS in the format of app_main() with net=wifi rssi=
facts() parses the numbers, the JSON keys and the header layout from s3_eth_bridge/main/main.c; test_s3_sim.py holds the
simulator to them. Made by goal 092.
Goal 093: UX <boot> (restart of the S3 with a new boot id) and UP [qdbm] (Wi-Fi power cap, txp= in stat); the LyraT console
knows s (print_status of the firmware: bonds, SLC, peers by their last two bytes, heap), reboot [1] (restart after
REBOOT_DELAY_MS with a new boot id and the boot line "BOOT reset_reason=SW code=3", then "SLC CONNECTED" while it has a
bond), unpair and ota; its log lines carry the uptime since its own start.
Goal 097: console "simsco 1|0" opens or closes the headset channel with the firmware's lines (test only); --mic-frame-offset
puts the mic's counter behind as on the board; --clicks marks both lines at the same bus frame for alignment tests.
Goal 101: --play-call / --play-mic WAV play a file on that line in a loop from the S3's start, instead of the tones.
Goal 096: the LyraT is the updater in factory plus the headset firmware in ota_0 (upd_sim.py): every start goes
through the updater (C rules of upd_mode.c, clock sped up by --upd-scale), "upd [8]" goes there, the updater takes
images on its own UDP port (--lyrat-port, printed as lyrat_port) and from the relay, the headset firmware has no slot.
"""
import argparse
import json
import math
import os
import random
import re
import select
import socket
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
REPO = TOOLS.parent.parent
sys.path.insert(0, str(TOOLS / "ota_test"))
sys.path.insert(0, str(TOOLS / "bench"))
sys.path.insert(0, str(HERE))
import otahost as H  # noqa: E402
import s3_emu  # noqa: E402
import otahost  # noqa: E402
import images  # noqa: E402
import upd_sim  # noqa: E402
from benchlib import port_offset  # noqa: E402

S3_MAIN = REPO / "s3_eth_bridge" / "main" / "main.c"
S3_DBGLOG = REPO / "s3_eth_bridge" / "main" / "s3_dbglog.c"
CTL_HEADER = TOOLS.parent / "main" / "ctl_frame.h"


# --- facts of the firmware source -------------------------------------------------------------------------------------

def _defines(text):
    return {m.group(1): int(m.group(2), 0) for m in re.finditer(r"^#define\s+(\w+)\s+(-?(?:0x)?[0-9A-Fa-f]+)\b", text, re.M)}


def _block(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


def json_keys(fmt_source):
    """Keys of a JSON object written by snprintf, in order, from the C text of the format."""
    return re.findall(r'\\"(\w+)\\":', fmt_source)


def stat_keys(fmt_source):
    """Names of the key=value fields of a printf format, in order (" dsdin rate=" gives "rate")."""
    return re.findall(r"(\w+)=%", fmt_source)


def facts():
    src = S3_MAIN.read_text(encoding="utf-8")
    d = _defines(src)
    d.update({k: v for k, v in _defines(CTL_HEADER.read_text(encoding="utf-8")).items() if k.startswith("CTL_")})
    tap = _block(src, "static void request_task", "sendto(sock, reply, len")
    tap = tap[tap.index("int len = snprintf"):]
    uq = _block(src, 'strcmp(text, "UQ") == 0', "} else {")
    send = _block(src, "static void listener_send", "struct sockaddr_in to")
    layout = {}
    for m in re.finditer(r"packet\[(\d+)\]\s*=\s*([^;]+);", send):
        layout[int(m.group(1))] = (1, m.group(2).strip())
    for m in re.finditer(r"memcpy\(packet \+ (\d+), &(\w+), (\d+)\);", send):
        layout[int(m.group(1))] = (int(m.group(3)), m.group(2))
    loop = _block(src, "int n = snprintf(line, sizeof(line), \"stat", "s3_log_line(\"%s\", line);")
    report = _block(src, "static int report(listener_t", "\n}\n")
    stat = stat_keys(loop.split("n += report")[0])
    per_listener = stat_keys(report)
    stat += per_listener + per_listener + stat_keys(loop.split("snprintf(line + n")[1])
    log_answer = _block(S3_DBGLOG.read_text(encoding="utf-8"), "static void answer", "sendto(")
    ur = re.search(r'"(UR %u %u\\n)"', src).group(1).replace("\\n", "\n")
    return {"defines": d, "tap_keys": json_keys(tap), "uq_keys": json_keys(uq), "layout": dict(sorted(layout.items())),
            "stat_keys": stat, "log_keys": json_keys(log_answer), "ur_format": ur,
            "s3_net_eth_default": d.get("S3_NET_ETH")}


def flat_keys(obj):
    """Keys of a parsed JSON object in order, nested objects and the objects of lists included."""
    out = []
    for k, v in obj.items():
        out.append(k)
        if isinstance(v, dict):
            out += flat_keys(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    out += flat_keys(item)
    return out


F = facts()
D = F["defines"]
RATE = D["SAMPLE_RATE"]
FRAMES = D["DMA_FRAMES"]
HEADER = D["HEADER_BYTES"]
LEASE_S = D["LEASE_MS"] / 1000.0
STATUS_S = D["STATUS_MS"] / 1000.0
TX_RELEASE_S = D["CTL_TX_RELEASE_MS"] / 1000.0
TX_OFF, TX_ON, TX_AUTO = D["TX_MODE_OFF"], D["TX_MODE_ON"], D["TX_MODE_AUTO"]
ST_OK, ST_UNKNOWN, ST_BAD, ST_NO_TX = 0, 1, 2, D["CTL_ST_NO_TX"]
SLOTS = {"L": 0, "R": 1, "LR": 2}
T = {m.group(1): int(m.group(2)) for m in re.finditer(r"CTL_T_(\w+)\s*=\s*(\d+)", CTL_HEADER.read_text(encoding="utf-8"))}


# --- the LyraT console behind the UART ----------------------------------------------------------------------------

LYRAT_MAIN = TOOLS.parent / "main" / "main.c"
REBOOT_S = int(re.search(r"#define REBOOT_DELAY_MS (\d+)", LYRAT_MAIN.read_text(encoding="utf-8")).group(1)) / 1000.0
RESTART_S = D["RESTART_DELAY_MS"] / 1000.0
PEER = "xx:xx:xx:xx:3c:7a"            # the peer as the firmware shows it on the channel: the last two bytes only
SLC_AFTER_BOOT_S = 1.5


class LyratConsole:
    """ctl_on_command() of the LyraT: a command runs once per seq, a repeat gets the stored reply again."""

    def __init__(self, sim=None):
        self.sim = sim
        self.bonds = 1
        self.last_seq = None
        self.last_reply = None
        self.cmds = self.dups = self.replies = self.hb = self.acks = 0
        self.tone = 0
        self.muted = False      # goal 111: a boot starts with the mic on
        self.slc = 1
        self.sco = 0
        self.hfm = None         # the mode rules of goal 102, made at the first "mode"
        self.ran = []           # every command that ran, for the tests: a repeat must not run twice
        self.start = time.monotonic()

    def run(self, text):
        word, _, arg = text.partition(" ")
        if self.sim:
            got = self.sim.lyrat.command(word, arg, self.sco)
            if got is not None:
                return got
        if word == "tone":
            self.tone = 1 if arg.strip() == "1" else 0
            return ST_OK, "tone = %d" % self.tone
        if word == "mute":
            # Goal 111: main.c's "mute" with its log line; the state goes up in the heartbeat flags (bit 2).
            a, was = arg.strip(), self.muted
            if a in ("on", "off", "toggle"):
                self.muted = (not was) if a == "toggle" else (a == "on")
                if self.muted != was and self.sim:
                    self.sim.lyrat_log("MIC MUTE %s src=cmd sco=%d" % ("on" if self.muted else "off", self.sco), tag="AUDIO")
                return ST_OK, "mute=%s (was %s)" % ("on" if self.muted else "off", "on" if was else "off")
            if a in ("", "?"):
                return ST_OK, "mute=%s" % ("on" if self.muted else "off")
            return ST_OK, "usage: mute on|off|toggle|?"
        if word == "simsco" and self.sim:
            # Test only (goal 097): the headset channel opens or closes as a call would, with the firmware's log lines.
            self.sco = 1 if arg.strip() == "1" else 0
            if self.sco:
                self.sim.lyrat_log("SCO audio state=2 codec=mSBC", tag="BT")
                self.sim.lyrat_log("*** SCO OPEN ***", tag="BT")
            else:
                self.sim.lyrat_log("SCO audio state=0 codec=mSBC", tag="BT")
                self.sim.lyrat_log("*** SCO CLOSED ***", tag="BT")
            return ST_OK, "simsco = %d" % self.sco
        if word == "ns":
            return ST_OK, "ns = %s" % (arg.strip() or "off")
        if word == "mode":
            # Goal 102: the firmware's own rules (hf_mode.c); the phone in its slot, the PC paired in the PC slot.
            if self.hfm is None:
                self.hfm = otahost.CHfm(nvs_mode="phone", phone=bytes.fromhex("02112233" + PEER[-5:].replace(":", "")),
                                        pc=bytes.fromhex("021122334402"))
            live = self.hfm.slot(self.hfm.mode) if self.slc else None
            act, out = self.hfm.command(arg, self.sco, live)
            if arg.strip() and self.sim:
                self.sim.lyrat_log("mode cmd: %s" % out, tag="BT")
            if act & otahost.HFM_DO_DROP:
                self.slc, self.sco = 1, 0          # the link drops and the AG of the new mode comes up (model: at once)
            return ST_OK, out
        if word == "ver":
            return ST_OK, "lyrat_resend sim"
        if word == "status":
            return ST_OK, "slc=%d sco=%d tone=%d" % (self.slc, self.sco, self.tone)
        if word == "s":
            peers = ["  peer[0] = %s" % PEER] if self.bonds else []
            return ST_OK, "\n".join(["=== DSP Pipeline Status ===", "  tone=%d" % self.tone, "  boot_reason=%s" % (
                self.sim.lyrat_reason if self.sim else "POWERON"), "  SCO     = %s bt_rate=16000" % (
                "active" if self.sco else "idle"), "  SLC     = %s" % ("up" if self.slc else "down"), "  plc=1",
                "  bonds=%d" % self.bonds, "  peers=%d" % len(peers)] + peers + ["  heap    = 123456"])
        if word == "reboot":
            if self.sco and arg.strip() != "1":
                return ST_OK, "reboot refused: SCO active, 'reboot 1' forces"
            if self.sim:
                self.sim.lyrat_reboot_at = time.monotonic() + REBOOT_S
            return ST_OK, "reboot in %d ms" % int(REBOOT_S * 1000)
        if word == "unpair":
            if self.sco:
                return ST_OK, "unpair refused: SCO active"
            before, self.bonds, self.slc = self.bonds, 0, 0
            return ST_OK, "unpair removed=%d bonds_before=%d peers=0" % (before, before)
        if word == "ota" and self.sim:
            return ST_OK, self.sim.lyrat_ota_text()
        if word == "_pair" and self.sim:
            # Not a firmware command: what the phone's pairing does to the board, for the tests of round.py link.
            self.bonds = 1
            self.sim.lyrat_log("SSP confirm %s" % (arg.strip() or "123456"), tag="BT")
            self.sim.slc_at = time.monotonic() + 0.5
            return ST_OK, "paired"
        if word == "ustat":
            return ST_OK, ("ustat baud=115200 rx_level=1 rx_bytes=0 frames=%d bad_hdr=0 bad_crc=0 trunc=0 noise=0 brk=0 "
                           "frm=0 ovf=0 cmds=%d dups=%d replies=%d hb=%d acks=%d bad_type=0 tx_fail=0"
                           % (self.cmds + self.dups, self.cmds, self.dups, self.replies, self.hb, self.acks))
        return ST_UNKNOWN, "unknown command: %s" % word

    def on_command(self, seq, payload):
        if self.last_seq is not None and seq == self.last_seq:
            self.dups += 1
            return self.last_reply
        valid = 0 < len(payload) < 128 and all(0x20 <= b < 0x7F for b in payload)
        if valid:
            self.cmds += 1
            text = payload.decode("ascii")
            self.ran.append(text)
            status, out = self.run(text)
        else:
            status, out = ST_BAD, ""
        self.last_seq, self.last_reply = seq, bytes([status]) + out.encode("ascii")
        self.replies += 1
        return self.last_reply

    def heartbeat(self):
        self.hb += 1
        flags = (1 if self.slc else 0) | (2 if self.sco else 0) | (4 if self.muted else 0)
        return struct.pack("<IBB", int(time.monotonic() - self.start), flags, self.tone)


class Uart(s3_emu.Uart):
    """The UART of s3_emu with the frame's own seq kept: CMD, REPLY, HB and HB_ACK go to the S3 end of the relay here."""

    def __init__(self, sim, ber):
        super().__init__(ber)
        self.sim = sim

    def send(self, direction, kind, payload, seq=None):
        raw = bytearray(H.ctl_frame(kind, (self.n if seq is None else seq) & 0xFFFF, payload))
        if self.ber:
            for i in range(len(raw)):
                for bit in range(8):
                    if self.rnd.random() < self.ber:
                        raw[i] ^= 1 << bit
        self.n += 1
        s3_emu.heapq.heappush(self.q, (time.monotonic() + self.latency / 1000.0, self.n, direction, bytes(raw)))

    def due(self):
        out = []
        t = time.monotonic()
        while self.q and self.q[0][0] <= t:
            _, _, d, raw = s3_emu.heapq.heappop(self.q)
            frames = self.parsers[d].feed(raw, s3_emu.now_ms())
            if not frames:
                self.lost += 1
            for kind, seq, payload in frames:
                if d == "to_s3":
                    self.sim.last_frame = time.monotonic()
                if kind in (T["CMD"], T["REPLY"], T["HB"], T["HB_ACK"]):
                    self.sim.on_frame(d, kind, seq, payload)
                else:
                    out.append((d, kind, payload))
        return out


def read_play(path):
    """The first channel of a 16 kHz 16 bit WAV as a list of ints (goal 101: recordings and scripts through the S3)."""
    import wave
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2:
            raise SystemExit("--play-*: %s is not 16 kHz 16 bit" % path)
        ch = w.getnchannels()
        data = struct.unpack("<%dh" % (w.getnframes() * ch), w.readframes(w.getnframes()))
    return data[0::ch] or (0,)


class Stream:
    def __init__(self, name, stream, port, freqs):
        self.name, self.stream, self.port, self.freqs = name, stream, port, freqs
        self.slots = 2
        self.seq = 0
        self.total = 0
        self.sent = self.send_fail = self.overflows = self.timeouts = 0
        self.phase = [0.0, 0.0]
        self.play = None                 # goal 101: mono int16 samples played on both channels, in a loop

    def pcm(self, frames, clicks=False):
        """clicks (goal 097): a 2 ms burst every 0.5 s of the bus, at the same bus frame on both lines."""
        if self.play is not None:
            n = len(self.play)
            return [[int(self.play[(self.total + i) % n])] * 2 for i in range(frames)]
        out = []
        for i in range(frames):
            pair = []
            for c in range(2):
                self.phase[c] += 2 * math.pi * self.freqs[c] / RATE
                pair.append(int(8000 * math.sin(self.phase[c])))
            if clicks:
                pair = [20000, 20000] if (self.total + i) % (RATE // 2) < 32 else [v // 8 for v in pair]
            out.append(pair)
        return out


class Sim(s3_emu.Emulator):
    def __init__(self, args):
        super().__init__(args)
        self.host = args.host
        self.uart = Uart(self, args.uart_ber)
        self.tap = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tap.bind((args.host, args.tap_port))
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.out.setblocking(False)
        self.streams = [Stream("dsdin", 0, args.stream_port_call, (1000.0, 1000.0)),
                        Stream("asdout", 1, args.stream_port_mic, (440.0, 2000.0))]
        self.dest = None
        self.lease_until = 0.0
        self.clock = time.monotonic()
        self.loss, self.dup = args.stream_loss, args.stream_dup
        self.mic_frame_offset, self.clicks = args.mic_frame_offset, args.clicks
        for s, path in zip(self.streams, (args.play_call, args.play_mic)):
            if path:
                s.play = read_play(path)
        self.rnd_stream = random.Random(11)
        self.console = LyratConsole(self)
        self.lyrat_reboot_at = None
        self.lyrat_t0 = time.monotonic()
        self.lyrat_reason = "POWERON"
        self.slc_at = time.monotonic() + SLC_AFTER_BOOT_S
        self.s3_restart_at = None
        self.txp = 80            # the driver's own cap after start, 20 dBm
        self.lyrat_silent = args.lyrat_silent
        self.tx_mode = TX_AUTO
        self.tx_edges = 0
        self.tx_on_now = False
        self.last_frame = 0.0
        self.peer = None
        self.baud = D["CTL_BAUD_DEFAULT"]
        self.refused = self.cmds_sent = self.relayed = self.hb_seen = 0
        self.last_hb = None
        self.next_hb = 0.0
        self.next_stat = time.monotonic() + STATUS_S
        self.started = time.monotonic()
        self.log_requests = 0
        # Goal 096: the LyraT with its updater (upd_sim.py).
        self.lyrat = upd_sim.UpdBoard(Path(args.lyrat_image).read_bytes(), scale=args.upd_scale)
        self.lyrat.log = lambda text, tag="LyraT": self.lyrat_log(text, tag=tag)
        self.ly_rx = upd_sim.V2Rx(0, s3_emu.ota.CHIP_ESP32, "lyrat_resend")
        self.ly_rx.board, self.ly_rx.sim, self.ly_rx.slc = self.lyrat, self, args.lyrat_slc
        self.ly_net = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ly_net.bind((args.host, args.lyrat_port))
        self.ly_net_rx = upd_sim.NetRx(self)
        self.lyrat.active = lambda: (self.ly_rx.info()["state_name"] == "recv" or
                                     self.ly_net_rx.rx.info()["state_name"] == "recv")

    # --- UART relay ---------------------------------------------------------------------------------------------------
    def tx_on(self):
        alive = self.last_frame and time.monotonic() - self.last_frame < TX_RELEASE_S
        on = self.tx_mode == TX_ON or (self.tx_mode == TX_AUTO and bool(alive))
        if on != self.tx_on_now:
            self.tx_on_now = on
            self.tx_edges += 1
        return on

    def on_frame(self, direction, kind, seq, payload):
        if direction == "to_lyrat":
            if not self.lyrat.up() or self.lyrat_silent:
                return
            if kind == T["CMD"]:
                self.uart.send("to_s3", T["REPLY"], self.console.on_command(seq, payload), seq)
            elif kind == T["HB_ACK"]:
                self.console.acks += 1
            return
        if kind == T["REPLY"] and self.peer and payload:
            # ctl_reply_to_pc(): the seq of the frame, the status byte, then the text
            head = (F["ur_format"] % (seq, payload[0])).encode()
            self.tap.sendto(head + payload[1:], self.peer)
            self.relayed += 1
        elif kind == T["HB"]:
            self.hb_seen += 1
            if len(payload) == 6:
                self.last_hb = struct.unpack("<IBB", payload)
            if self.tx_on():
                self.uart.send("to_lyrat", T["HB_ACK"], bytes([1 if self.log_peer else 0, 3]), seq)

    def uq(self):
        now = time.monotonic()
        c = self.uart.parsers["to_s3"].counts()
        hb = self.last_hb
        return {"fw": "s3tap", "boot": self.s3.boot, "up_ms": int((now - self.started) * 1000), "pins_ok": 1,
                "tx_mode": self.tx_mode, "tx_on": int(self.tx_on()), "tx_edges": self.tx_edges, "baud": self.baud,
                "rx_level": 1, "tx_level": 1, "rx_bytes": c["frames"] * 16, "frames": c["frames"],
                "bad_hdr": c["bad_header"], "bad_crc": c["bad_crc"], "trunc": c["truncated"], "noise": c["noise"],
                "replies": self.relayed, "sent": self.cmds_sent, "refused": self.refused, "hb": self.hb_seen,
                "last_frame_ms": int((now - self.last_frame) * 1000) if self.last_frame else -1,
                "lyrat_up_s": hb[0] if hb else -1, "lyrat_flags": hb[1] if hb else -1, "sent_rx": 0, "sent_udp": 0, "sent_fail": 0}

    def handle_ctl(self, text, peer):
        """ctl_request(): True when the text was a control word."""
        reply = None
        if text.startswith("UC "):
            m = re.match(r"UC (\d+) (.*)$", text, re.S)
            if not m or int(m.group(1)) > 0xFFFF:
                reply = F["ur_format"] % (0, ST_BAD) + "bad request"
            elif not self.tx_on():
                self.refused += 1
                reply = F["ur_format"] % (int(m.group(1)), ST_NO_TX) + "no_tx"
            else:
                self.peer = peer
                self.uart.send("to_lyrat", T["CMD"], m.group(2).encode("latin-1")[:D["CTL_MAX_PAYLOAD"]], int(m.group(1)))
                self.cmds_sent += 1
                return True
        elif text.startswith("UW "):
            raw = bytes.fromhex(re.match(r"[0-9a-fA-F]*", text[3:]).group(0)[: (len(text) - 3) // 2 * 2] or "")
            reply = "UW %d" % (len(raw) if self.tx_on() else -1)
        elif text.startswith("UT "):
            self.tx_mode = {"0": TX_OFF, "1": TX_ON, "A": TX_AUTO}.get(text[3:4], self.tx_mode)
            reply = "UT %d" % self.tx_mode
        elif text.startswith("UB "):
            try:
                baud = int(text[3:].split()[0])
            except (ValueError, IndexError):
                baud = 0
            if 1200 <= baud <= 921600:
                self.baud = baud
            reply = "UB %d" % self.baud
        elif text.startswith("UX "):
            try:
                boot = int(text[3:].split()[0])
            except (ValueError, IndexError):
                boot = -1
            now = boot == self.s3.boot
            reply = "UX %d %d" % (max(boot, 0), int(now))
            if now:
                self.s3_restart_at = time.monotonic() + RESTART_S
        elif text == "UP" or text.startswith("UP "):
            err = 0
            if text.startswith("UP "):
                try:
                    q = int(text[3:].split()[0])
                except (ValueError, IndexError):
                    q = 0
                if 8 <= q <= 84:
                    self.txp = q
                else:
                    err = 0x102          # ESP_ERR_INVALID_ARG
            reply = "UP %d %d" % (self.txp, err)
        elif text == "UQ":
            reply = json.dumps(self.uq(), separators=(",", ":"))
        else:
            return False
        self.tap.sendto(reply.encode("latin-1"), peer)
        return True

    # --- TAP requests and streams ---------------------------------------------------------------------------------------
    def tap_reply(self, ok):
        c, m = self.streams
        part = lambda s: {"frames": s.total - (self.mic_frame_offset if s.stream == 1 else 0), "sent": s.sent, "send_fail": s.send_fail, "ovf": s.overflows,  # noqa: E731
                          "timeouts": s.timeouts}
        return {"fw": "s3tap", "ok": int(ok), "boot": self.s3.boot, "up_ms": int((time.monotonic() - self.started) * 1000),
                "pins_ok": 1, "call": part(c), "mic": part(m)}

    def handle_tap(self, data, peer):
        if not self.s3.up():
            return
        text = data.decode("latin-1")
        if self.handle_ctl(text, peer):
            return
        words = text.split()
        if not words:
            return
        ok = False
        if words[0] == "TAP0":
            self.dest = None
            ok = True
        elif words[0] == "TAP1":
            fields = words[1:3]
            ok = len(fields) != 1 and all(f in SLOTS for f in fields)
            if ok:
                if fields:
                    self.streams[0].slots, self.streams[1].slots = SLOTS[fields[0]], SLOTS[fields[1]]
                else:
                    self.streams[0].slots = self.streams[1].slots = SLOTS["LR"]
                self.lease_until = time.monotonic() + LEASE_S
                self.dest = peer[0]
        else:
            return
        self.tap.sendto(json.dumps(self.tap_reply(ok), separators=(",", ":")).encode(), peer)

    def packet(self, s, pcm):
        channels = 2 if s.slots == 2 else 1
        if channels == 1:
            samples = [p[s.slots] for p in pcm]
        else:
            samples = [v for p in pcm for v in p]
        seq = s.seq
        s.seq += 1
        # The mic's I2S channel starts after the call's on the board: its counter is behind by mic_frame_offset.
        first = (s.total - len(pcm) - (self.mic_frame_offset if s.stream == 1 else 0)) & 0xFFFFFFFF
        head = struct.pack("<2sBBIIHBBII", b"S3", D["PACKET_VERSION"], s.stream, seq, first, self.s3.boot, channels,
                           len(pcm) // 8, s.overflows, s.send_fail)
        return head + struct.pack("<%dh" % len(samples), *samples)

    def stream_step(self):
        now = time.monotonic()
        if not self.s3.up():
            self.clock = now
            for s in self.streams:
                s.seq = s.total = 0      # a restarted board counts from zero, with a new boot id
            return
        while now - self.clock >= FRAMES / RATE:
            self.clock += FRAMES / RATE
            for s in self.streams:
                pcm = s.pcm(FRAMES, self.clicks)
                s.total += FRAMES
                if self.dest and now < self.lease_until:
                    data = self.packet(s, pcm)
                    if self.rnd_stream.random() < self.loss:
                        s.sent += 1         # gone on the way: the board counted it as sent
                        continue
                    for _ in range(2 if self.rnd_stream.random() < self.dup else 1):
                        try:
                            self.out.sendto(data, (self.dest, s.port))
                            s.sent += 1
                        except OSError:
                            s.send_fail += 1

    # --- the LyraT's heartbeat and the S3's stat line -------------------------------------------------------------------
    def lyrat_status_text(self):
        """port_status_text() of lyrat_ota.c: the keys of the S3 first, then lyrat_mode_text() (goal 096)."""
        b = self.lyrat
        inv = b.last_invalid.elf[:16] if b.last_invalid else "-"
        return ("run=%s ver=%s elf=%s img=%s inv=%s selftest=%s baud=115200 %s"
                % (b.slot, b.run.version.replace(" ", "_")[:23], b.run.elf[:16], b.img, inv, b.selftest,
                   b.mode_text(self.host, self.ly_net.getsockname()[1])))

    def lyrat_ota_text(self):
        return "ota state=idle " + self.lyrat_status_text()

    def lyrat_log(self, text, level="I", tag="LyraT"):
        up = int((time.monotonic() - self.lyrat_t0) * 1000)
        self.ly_ring.log(s3_emu.now_ms(), "%s (%d) %s: %s" % (level, up, tag, text))

    def lyrat_booted(self, reason, code):
        """A start of the LyraT: new boot id (Board did it, or here), a new log ring, the first lines of app_main()."""
        self.lyrat_t0 = time.monotonic()
        self.lyrat_reason = reason
        self.ly_ring = H.Ring(96, 1, self.lyrat.boot)
        self.lyrat_log("BOOT reset_reason=%s code=%d" % (reason, code))
        self.lyrat_log("=== LyraT Phone Stream - All Phases ===")
        c = self.console
        c.last_seq = c.last_reply = None
        c.slc = c.sco = 0
        c.start = time.monotonic()
        if c.bonds:
            self.lyrat_log("peer[0] = 11:22:33:44:3c:7a", tag="BT")
        self.slc_at = time.monotonic() + SLC_AFTER_BOOT_S if c.bonds else None
        if self.lyrat.mode == "updater":
            b = self.lyrat
            self.slc_at = None                  # no Bluetooth in the updater
            self.lyrat_log("updater: app ver=%s elf=%s state=%s tries=%d" % (
                b.app.version if b.app else "-", b.app.elf[:16] if b.app else "-", b.app_st, b.tries), tag="updater")

    def s3_booted(self, new_boot=True):
        """esp_restart() of the S3 (UX, or an update): new boot id, a new log ring, the relay's state back to its start."""
        if new_boot:
            self.s3.boot = random.randrange(65536)
            self.s3.restarts += 1
            self.s3.down_until = time.monotonic() + 1.0
        self.s3_ring = H.Ring(128, 0, self.s3.boot)
        self.s3_ring.log(s3_emu.now_ms(), "I (25) s3eth: start after a restart over the network")
        self.relay_ring = H.Ring(128, 1, 0)
        self.log_peer = None             # a new start knows no listener until the next LOG1
        self.tx_mode, self.peer, self.dest = TX_AUTO, None, None
        self.started = time.monotonic()
        self.txp = 80

    def lyrat_step(self):
        t = time.monotonic()
        if self.lyrat_reboot_at and t >= self.lyrat_reboot_at:
            self.lyrat_reboot_at = None
            self.lyrat._boot("updater", 0)         # every start goes through the updater (goal 096)
            self.ly_rx.reset()
            self.ly_net_rx.new()
            self.lyrat_booted("SW", 3)
        if self.s3_restart_at and t >= self.s3_restart_at:
            self.s3_restart_at = None
            self.s3_booted()
        if self.slc_at and t >= self.slc_at and self.lyrat.up() and self.console.bonds:
            self.slc_at = None
            self.console.slc = 1
            self.lyrat_log("SLC CONNECTED", tag="BT")
        if t >= self.next_hb:
            self.next_hb = t + 1.0
            if self.lyrat.up() and not self.lyrat_silent:
                self.uart.send("to_s3", T["HB"], self.console.heartbeat())
        if t >= self.next_stat:
            self.next_stat = t + STATUS_S
            self.s3_ring.log_long(s3_emu.now_ms(), self.stat_line())   # s3_log_line(): no level prefix

    def stat_line(self):
        c, m = self.streams
        streaming = int(bool(self.dest) and time.monotonic() < self.lease_until)
        cnt = self.uart.parsers["to_s3"].counts()
        return ("stat up=%d eth=ok link=1 ip=%s dsdin rate=%d peak=8000/8000 asdout rate=%d peak=8000/8000 "
                "timeouts=%d/%d ovf=%d/%d send_fail=%d/%d sent=%d/%d streaming=%d pins_ok=1 uart tx=%d rx=%d "
                "frames=%d bad=%d/%d lrelay_lost=0 selftest=%s net=wifi rssi=-52 txp=%d"
                % (int(time.monotonic() - self.started), self.host, RATE, RATE, c.timeouts, m.timeouts, c.overflows,
                   m.overflows, c.send_fail, m.send_fail, c.sent, m.sent, streaming, int(self.tx_on()),
                   cnt["frames"] * 16, cnt["frames"], cnt["bad_header"], cnt["bad_crc"], self.s3.selftest, self.txp))

    def log_step(self):
        """log_step() of s3_emu with the uptime of each board since its own start in its lines, as ESP_LOG prints it:
        a restart makes it small again (goal 093). The LyraT writes nothing while it is down."""
        t = time.monotonic()
        if t >= self.next_line:
            self.next_line = t + 0.05
            self.line_n += 1
            self.s3_ring.log(s3_emu.now_ms(), "I (%d) s3eth: stat line %d" % (int((t - self.started) * 1000), self.line_n))
            if self.lyrat.up():
                self.lyrat_log("line %d" % self.line_n, level="W")
        listening = self.log_peer is not None and t < self.log_until
        if listening and self.lyrat.up() and not self.lyrat_silent:
            payload, n = self.ly_ring.pack(600)          # the LyraT drains only with a listener
            if n:
                self.uart.send("to_s3", H.CTL_T_LOG, payload)
        if listening and self.s3.up():
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
        """on_request() and answer() of s3_dbglog.c."""
        self.log_requests += 1
        if data.startswith(b"LOG1"):
            self.log_peer, self.log_until = peer, time.monotonic() + 10
        elif data.startswith(b"LOG0"):
            self.log_peer = None
        else:
            return
        s3i, rli = self.s3_ring.info(), self.relay_ring.info()
        state = {"s3_level": 3, "lyrat_level": 3,
                 "own": {"pending": s3i["count"], "lost": s3i["lost"], "sent": s3i["taken"]},
                 "relay": {"pending": rli["count"], "lost": rli["lost"], "sent": rli["taken"]},
                 "datagrams": self.line_n, "send_fail": 0}
        listening = int(self.log_peer is not None and time.monotonic() < self.log_until)
        self.log.sendto(b"LOG %d " % listening + json.dumps(state, separators=(",", ":")).encode(), peer)

    def serve(self):
        while True:
            ready, _, _ = select.select([self.sock, self.log, self.tap, self.ly_net], [], [], 0.002)
            for s in ready:
                try:
                    data, peer = s.recvfrom(4096)
                except OSError:         # Windows: an ICMP "port unreachable" of an earlier send
                    continue
                if s is self.ly_net:
                    self.ly_net_rx.handle(data, peer, self.ly_net)
                elif s is self.sock:
                    self.handle_ota(data, peer)
                elif s is self.log:
                    self.handle_log(data, peer)
                else:
                    self.handle_tap(data, peer)
            for r in self.rx:
                r.tick(s3_emu.now_ms())
            if self.s3.step():
                self.s3_booted(new_boot=False)     # an update activated (or rolled back): Board gave the new boot id
                self.rx = [H.CReceiver(0x300000, s3_emu.ota.CHIP_ESP32S3, "s3_eth_bridge", token=self.token or b""),
                           H.CReceiver(0x1F0000, s3_emu.ota.CHIP_ESP32, "lyrat_resend", token=self.token or b"")]
            if self.lyrat.step():
                self.ly_rx.reset()
                self.ly_net_rx.new()
                self.lyrat_booted("SW", 3)     # an update activated (or rolled back): esp_restart()
            self.relay_step()
            self.log_step()
            self.lyrat_step()
            self.stream_step()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    off = port_offset()          # BENCH_PORT_OFFSET moves every default port, as in the tools (goal 103)
    ap.add_argument("--tap-port", type=int, default=D["PORT_REQUEST"] + off)
    ap.add_argument("--ota-port", dest="port", type=int, default=5007 + off)
    ap.add_argument("--log-port", type=int, default=5008 + off)
    ap.add_argument("--stream-port-call", type=int, default=D["PORT_CALL"] + off)
    ap.add_argument("--stream-port-mic", type=int, default=D["PORT_MIC"] + off)
    ap.add_argument("--token-file")
    ap.add_argument("--s3-image")
    ap.add_argument("--lyrat-image")
    ap.add_argument("--uart-ber", type=float, default=0.0)
    ap.add_argument("--lyrat-slc", action="store_true")
    ap.add_argument("--log-loss", type=float, default=0.0)
    ap.add_argument("--stream-loss", type=float, default=0.0)
    ap.add_argument("--stream-dup", type=float, default=0.0)
    ap.add_argument("--mic-frame-offset", type=int, default=0, help="the mic's frame counter this far behind the call's (097)")
    ap.add_argument("--clicks", action="store_true", help="a 2 ms burst every 0.5 s on both lines at one bus frame (097)")
    ap.add_argument("--lyrat-port", type=int, default=0, help="UDP port of the LyraT's updater, 0: any (goal 096)")
    ap.add_argument("--upd-scale", type=float, default=20.0,
                    help="the updater's clock runs this much faster: 10 s window in 0.5 s (goal 096)")
    ap.add_argument("--lyrat-silent", action="store_true", help="no frames from the LyraT: the S3 keeps its TX off")
    ap.add_argument("--play-call", help="WAV 16 kHz 16 bit (first channel) played on the call line in a loop (goal 101)")
    ap.add_argument("--play-mic", help="WAV 16 kHz 16 bit (first channel) played on the mic line in a loop (goal 101)")
    args = ap.parse_args(argv)
    tmp = None
    if not args.s3_image or not args.lyrat_image:
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="s3_sim_"))
        images.prepare(tmp)
        args.s3_image = args.s3_image or str(tmp / "s3-base" / "s3_eth_bridge.bin")
        args.lyrat_image = args.lyrat_image or str(tmp / "lyrat-base" / "lyrat_resend.bin")
    sim = Sim(args)
    print(json.dumps({"host": args.host, "tap_port": sim.tap.getsockname()[1], "port": sim.sock.getsockname()[1],
                      "log_port": sim.log.getsockname()[1], "call_port": args.stream_port_call,
                      "mic_port": args.stream_port_mic, "lyrat_port": sim.ly_net.getsockname()[1],
                      "pid": os.getpid()}), flush=True)
    sim.serve()


if __name__ == "__main__":
    main()
