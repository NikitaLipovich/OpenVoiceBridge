#!/usr/bin/env python3
"""Record the board's UDP audio streams into WAV files: duplicates removed, losses counted.

  udp_record.py --seconds N --out DIR [--ports 5004,5005]
  udp_record.py --tap --seconds N --out DIR [--slots L,L]

Two sources use the same ports. --tap asks the S3-ETH board for the I2S bus as PCM (TapRecorder), the default
form takes the mu-law stream of a LyraT that has Wi-Fi.

TapRecorder finds the board by itself: it broadcasts "TAP1 <call> <mic>" to port 5006 once a second and turns to
unicast as soon as a stream arrives, returning to broadcast when the stream stops, so a restarted board needs no
action. Packets without the S3 magic are the LyraT's own stream and are kept apart as lyrat_call/lyrat_mic.
It writes call.wav and mic.wav (16 kHz, 1 or 2 channels as asked) with silence in place of what was lost, so the
time axis stays real, and DIR/tap.json with, for each stream: packets, unique, duplicates, reordered, lost_packets,
loss_pct, max_burst, gaps ([first frame, frames] in the WAV), boots, ovf and send_fail (growth during the
recording, these are losses on the board), rate_hz (samples against the PC clock, least squares over arrival
times) and frames. As a module: TapRecorder(slots).start(), .stop(), .save(out_dir), .report().

Stream names: 5004 is call.wav (phone to headphones), 5005 is mic.wav (headset mic to phone).
A packet is [4 bytes seq, LE][3 zero bytes][1 byte frame count][mu-law, 1 byte per sample]. The board
sends each one three times. seq counts frames of SAMPLES_PER_FRAME samples, so a jump in seq is a
measured loss and is written as that many samples of silence: the time axis of the WAV stays real.

Output: DIR/call.wav, DIR/mic.wav (16 kHz 16 bit mono) and DIR/udp.json with, for each stream,
packets, unique, duplicates, lost_frames, gap_events, gaps, samples, seconds, first_packet_s.
gaps lists [start sample, length] of the silence put in place of lost packets. A judge of the sound must
leave those stretches out: they are a loss of this recording channel, not a property of the sound.
As a module: UdpRecorder(ports).start(), .stop(), .save(out_dir).
Based on scripts/udp_capture.py. Recordings stay outside git (bench_runs).
"""
import argparse
import array
import json
import select
import socket
import struct
import sys
import threading
import time
import wave
from pathlib import Path

RATE = 16000
SAMPLES_PER_FRAME = 240
HEADER = 8
MAX_GAP_FRAMES = RATE * 30 // SAMPLES_PER_FRAME   # a larger jump is a board restart, not a loss
NAMES = {5004: "call", 5005: "mic"}
SOCKET_BUFFER = 1 << 20


def _ulaw_table():
    table = []
    for b in range(256):
        v = ~b & 0xFF
        exponent = (v >> 4) & 7
        sample = (((v & 0x0F) << 3) + 0x84) << exponent
        sample -= 0x84
        table.append(-sample if v & 0x80 else sample)
    return table


ULAW = _ulaw_table()


class _Stream:
    def __init__(self, port):
        self.port = port
        self.name = NAMES.get(port, str(port))
        self.chunks = {}        # seq -> (frame count, payload)
        self.packets = 0
        self.duplicates = 0
        self.first_at = None

    def add(self, data, at):
        if len(data) <= HEADER:
            return
        seq = struct.unpack_from("<I", data)[0]
        self.packets += 1
        if self.first_at is None:
            self.first_at = at
        if seq in self.chunks:
            self.duplicates += 1
        else:
            self.chunks[seq] = (data[7], data[HEADER:])

    def assemble(self):
        """PCM samples in sequence order, silence for what never arrived; (samples, stats)."""
        pcm = []
        lost = 0
        gaps = []
        expect = None
        for seq in sorted(self.chunks):
            frames, payload = self.chunks[seq]
            if expect is not None and seq != expect:
                missing = seq - expect
                if 0 < missing <= MAX_GAP_FRAMES:
                    lost += missing
                    gaps.append([len(pcm), missing * SAMPLES_PER_FRAME])
                    pcm.extend([0] * (missing * SAMPLES_PER_FRAME))
            pcm.extend(ULAW[b] for b in payload)
            expect = seq + max(frames, 1)
        return pcm, {"packets": self.packets, "unique": len(self.chunks), "duplicates": self.duplicates,
                     "lost_frames": lost, "gap_events": len(gaps), "gaps": gaps, "samples": len(pcm),
                     "seconds": round(len(pcm) / RATE, 3)}


class UdpRecorder:
    def __init__(self, ports=(5004, 5005)):
        self.socks = {}
        self.streams = {}
        for port in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
            s.bind(("0.0.0.0", port))
            s.setblocking(False)
            self.socks[s] = port
            self.streams[port] = _Stream(port)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.t0 = None

    def _run(self):
        while not self._stop.is_set():
            ready, _, _ = select.select(list(self.socks), [], [], 0.1)
            now = time.monotonic()
            for s in ready:
                try:
                    data, _ = s.recvfrom(4096)
                except OSError:
                    continue
                self.streams[self.socks[s]].add(data, now)

    def start(self):
        self.t0 = time.monotonic()
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        for s in self.socks:
            s.close()
        return self

    def save(self, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = {}
        for stream in self.streams.values():
            pcm, stats = stream.assemble()
            stats["first_packet_s"] = round(stream.first_at - self.t0, 3) if stream.first_at else None
            path = out_dir / (stream.name + ".wav")
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(struct.pack("<%dh" % len(pcm), *pcm))
            stats["file"] = path.name
            summary[stream.name] = stats
        (out_dir / "udp.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary


TAP_MAGIC = b"S3"
TAP_VERSION = 1
TAP_HEADER = 24
TAP_REQUEST_PORT = 5006
TAP_REQUEST_S = 1.0
TAP_SILENT_S = 3.0        # no stream for this long: the board may be back with another address, broadcast again
TAP_STREAMS = {0: "call", 1: "mic"}
TAP_PORTS = {"call": 5004, "mic": 5005}


def local_addresses():
    """IPv4 addresses of this PC: a broadcast leaves through one interface only, so each gets its own."""
    try:
        found = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        found = []
    return [a for a in found if not a.startswith("127.")] or ["0.0.0.0"]


class _TapStream:
    def __init__(self, name):
        self.name = name
        self.boots = {}          # boot id -> {seq: (frame, channels, arrival, ovf, send_fail, pcm)}, in arrival order
        self.packets = 0
        self.duplicates = 0
        self.reordered = 0
        self.last_seq = None
        self.first_at = None
        self.last_at = None

    def add(self, data, at):
        seq, frame, boot = struct.unpack_from("<IIH", data, 4)
        channels, ovf, send_fail = data[14], *struct.unpack_from("<II", data, 16)
        packets = self.boots.setdefault(boot, {})
        self.packets += 1
        if self.first_at is None:
            self.first_at = at
        self.last_at = at
        if seq in packets:
            self.duplicates += 1
            return
        if self.last_seq is not None and packets and seq < self.last_seq:
            self.reordered += 1
        self.last_seq = seq
        packets[seq] = (frame, channels, at, ovf, send_fail, data[TAP_HEADER:])

    @staticmethod
    def _rate(points):
        """Least squares slope of frames over arrival time, in samples per second of the PC clock."""
        if len(points) < 2 or points[-1][0] - points[0][0] < 1.0:
            return None
        n = len(points)
        mean_t = sum(p[0] for p in points) / n
        mean_f = sum(p[1] for p in points) / n
        den = sum((p[0] - mean_t) ** 2 for p in points)
        return sum((p[0] - mean_t) * (p[1] - mean_f) for p in points) / den if den else None

    def assemble(self):
        """(channels, PCM array, stats). Losses become silence, a board restart becomes the silence it took."""
        pcm = array.array("h")
        gaps, bursts = [], []
        lost = ovf = send_fail = 0
        rates = []
        channels = None
        prev_end_at = None
        for boot, packets in self.boots.items():
            seqs = sorted(packets)
            if not seqs:
                continue
            channels = channels or packets[seqs[0]][1]
            frame_bytes = channels * 2
            first = packets[seqs[0]]
            last = packets[seqs[-1]]
            ovf += last[3] - first[3]
            send_fail += last[4] - first[4]
            if prev_end_at is not None:
                # The time the board was away cannot be counted in frames: the arrival clock measures it.
                away = max(int((first[2] - prev_end_at) * RATE), 0)
                gaps.append([len(pcm) // channels, away])
                pcm.extend([0] * (away * channels))
            expect = None
            prev_seq = None
            for seq in seqs:
                frame, ch, _, _, _, payload = packets[seq]
                if ch != channels:
                    continue        # the slots were changed in flight; never mix layouts in one file
                if prev_seq is not None and seq != prev_seq + 1:
                    lost += seq - prev_seq - 1
                    bursts.append(seq - prev_seq - 1)
                if expect is not None and frame != expect:
                    missing = frame - expect
                    if 0 < missing <= MAX_GAP_FRAMES * SAMPLES_PER_FRAME:
                        gaps.append([len(pcm) // channels, missing])
                        pcm.extend([0] * (missing * channels))
                samples = array.array("h")
                samples.frombytes(payload[:len(payload) // 2 * 2])
                if sys.byteorder == "big":
                    samples.byteswap()
                pcm.extend(samples)
                expect = frame + len(payload) // frame_bytes
                prev_seq = seq
            prev_end_at = last[2]
            rate = self._rate([(packets[q][2], packets[q][0]) for q in seqs])
            if rate is not None:
                rates.append((last[2] - first[2], rate))
        unique = sum(len(p) for p in self.boots.values())
        seconds = sum(t for t, _ in rates)
        stats = {"packets": self.packets, "unique": unique, "duplicates": self.duplicates, "reordered": self.reordered,
                 "lost_packets": lost, "loss_pct": round(100.0 * lost / (unique + lost), 3) if unique + lost else None,
                 "max_burst": max(bursts) if bursts else 0, "gap_events": len(gaps), "gaps": gaps,
                 "boots": len(self.boots), "ovf": ovf, "send_fail": send_fail,
                 "rate_hz": round(sum(t * r for t, r in rates) / seconds, 2) if seconds else None,
                 "channels": channels, "frames": len(pcm) // (channels or 1),
                 "seconds": round(len(pcm) / (channels or 1) / RATE, 3)}
        return channels, pcm, stats


class TapRecorder:
    def __init__(self, slots=("L", "L"), board=None):
        self.request = ("TAP1 %s %s" % tuple(slots)).encode()
        self.board = board          # known address, or None to find it by broadcast
        self.socks = {}
        for name, port in TAP_PORTS.items():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
            s.bind(("0.0.0.0", port))
            s.setblocking(False)
            self.socks[s] = name
        self.senders = []
        for address in local_addresses():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((address, 0))
            s.setblocking(False)
            self.senders.append(s)
        self.streams = {name: _TapStream(name) for name in TAP_PORTS}
        self.foreign = {name: _Stream(TAP_PORTS[name]) for name in TAP_PORTS}
        self.foreign["call"].name, self.foreign["mic"].name = "lyrat_call", "lyrat_mic"
        self.replies = 0
        self.board_status = None     # the newest reply of the board, parsed
        self.requests = self.broadcasts = 0
        self.t0 = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _ask(self, now):
        fresh = self.board and any(st.last_at and now - st.last_at < TAP_SILENT_S for st in self.streams.values())
        for s in self.senders:
            try:
                if fresh:
                    s.sendto(self.request, (self.board, TAP_REQUEST_PORT))
                else:
                    s.sendto(self.request, ("255.255.255.255", TAP_REQUEST_PORT))
            except OSError:
                pass
        self.requests += 1
        self.broadcasts += 0 if fresh else 1

    def _run(self):
        next_ask = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_ask:
                self._ask(now)
                next_ask = now + TAP_REQUEST_S
            ready, _, _ = select.select(list(self.socks) + self.senders, [], [], 0.05)
            now = time.monotonic()
            for s in ready:
                try:
                    data, sender = s.recvfrom(4096)
                except OSError:
                    continue
                if s in self.socks:
                    name = self.socks[s]
                    if data[:2] == TAP_MAGIC and data[2] == TAP_VERSION and len(data) > TAP_HEADER:
                        self.board = sender[0]
                        self.streams[TAP_STREAMS.get(data[3], name)].add(data, now)
                    else:
                        self.foreign[name].add(data, now)
                else:
                    self.replies += 1
                    try:
                        self.board_status = json.loads(data.decode("ascii", "replace"))
                        self.board = sender[0]
                    except ValueError:
                        pass

    def start(self):
        self.t0 = time.monotonic()
        self._thread.start()
        return self

    def stop(self, release=True):
        self._stop.set()
        self._thread.join(timeout=2)
        if release and self.board and self.senders:
            try:
                self.senders[0].sendto(b"TAP0", (self.board, TAP_REQUEST_PORT))
            except OSError:
                pass
        for s in list(self.socks) + self.senders:
            s.close()
        return self

    def received(self, name):
        """Packets of one stream so far; safe to read while recording."""
        return self.streams[name].packets

    def save(self, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = {"requests": self.requests, "broadcasts": self.broadcasts, "replies": self.replies,
                   "board_status": self.board_status}
        for name, stream in self.streams.items():
            channels, pcm, stats = stream.assemble()
            stats["first_packet_s"] = round(stream.first_at - self.t0, 3) if stream.first_at else None
            stats["last_packet_s"] = round(stream.last_at - self.t0, 3) if stream.last_at else None
            path = out_dir / (name + ".wav")
            if sys.byteorder == "big":
                pcm.byteswap()
            with wave.open(str(path), "wb") as w:
                w.setnchannels(channels or 1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(pcm.tobytes())
            stats["file"] = path.name
            summary[name] = stats
        for name, stream in self.foreign.items():
            if stream.chunks:
                pcm, stats = stream.assemble()
                path = out_dir / (stream.name + ".wav")
                with wave.open(str(path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(RATE)
                    w.writeframes(struct.pack("<%dh" % len(pcm), *pcm))
                stats["file"] = path.name
                summary[stream.name] = stats
        (out_dir / "tap.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ports", default="5004,5005")
    ap.add_argument("--tap", action="store_true", help="take the PCM streams of the S3-ETH board")
    ap.add_argument("--slots", default="L,L", help="--tap: slots of the call and mic lines: L, R or LR")
    args = ap.parse_args()
    if args.tap:
        rec = TapRecorder(tuple(args.slots.split(","))).start()
        time.sleep(args.seconds)
        summary = rec.stop().save(args.out)
        print(json.dumps(summary, indent=2))
        return 0 if summary["call"]["unique"] and summary["mic"]["unique"] else 2
    rec = UdpRecorder([int(p) for p in args.ports.split(",")]).start()
    time.sleep(args.seconds)
    summary = rec.stop().save(args.out)
    print(json.dumps(summary, indent=2))
    return 0 if any(s["unique"] for s in summary.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
