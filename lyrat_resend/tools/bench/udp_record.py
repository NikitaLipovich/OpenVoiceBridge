#!/usr/bin/env python3
"""Record the board's UDP audio streams into WAV files: duplicates removed, losses counted.

  udp_record.py --tap [--seconds N] [--out DIR] [--slots L,LR] [--forward HOST:PORT,HOST:PORT]
  udp_record.py --receive PORT,PORT [--seconds N] [--idle-exit S] --out DIR
  udp_record.py --seconds N --out DIR [--ports 5004,5005]

Three forms. --tap asks the S3-ETH board for the I2S bus as PCM (TapRecorder): this is the recording of a conversation.
Without --seconds it runs until Ctrl+C, without --out it writes to <bench runs>/record/<stamp>. It plays nothing on the PC.
--receive is the far end of --forward, an example of a second consumer and the one the tests use. The last form takes the
mu-law stream of a LyraT that has Wi-Fi, firmware before goal 044.

--forward gives the live stream to a second consumer while recording, a translator for instance: the first copy of each
packet goes out as it arrives, unchanged and still with its S3 header (docs/S3_TAP.md), the call to the first address and
the mic to the second. A consumer that is absent or slow costs the recording nothing: the send does not wait.

TapRecorder finds the board by itself: it broadcasts "TAP1 <call> <mic>" to port 5006 once a second and turns to
unicast as soon as a stream arrives, returning to broadcast when the stream stops, so a restarted board needs no
action. Packets without the S3 magic are the LyraT's own stream and are kept apart as lyrat_call/lyrat_mic.
It writes call.wav and mic.wav (16 kHz, 1 or 2 channels as asked) with silence in place of what was lost, so the
time axis stays real, and DIR/tap.json with, for each stream: packets, unique, duplicates, reordered, lost_packets,
loss_pct, max_burst, gaps ([first frame, frames] in the WAV), boots, ovf and send_fail (growth during the
recording, these are losses on the board), rate_hz (samples against the PC clock, least squares over arrival
times), frames, seconds, first_at and last_at (time.monotonic of the first and last packet, one clock for all processes
of this PC), wall_s (first packet to the end of the last, on the PC clock) and marks ([seq, arrival] of every 500th
packet), floor_rms (per channel, the 10th percentile of the 100 ms level: a voice or music leaves quiet moments, the codec
noise seen in goal 050 #001 does not); with --forward also forward: target, forwarded, failed for each stream.
As a module: TapRecorder(slots, forward=..., listen_only=...).start(), .stop(), .save(out_dir).
journal_dir= writes every first copy of a packet to DIR/packets.journal as it arrives, so a dead process loses nothing that
reached the disk: TapRecorder.from_journal(DIR).save(DIR) makes the files from it. bus_gap_s= keeps the time axis real when the
I2S bus stops, a LyraT reset for one: the S3 sends nothing then and its frame counter stands still, so the stop is measured on
the arrival clock and written as silence, listed in gaps and in bus_stops ([first frame, frames]).

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
import os
import select
import socket
import struct
import sys
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import run_dir  # noqa: E402

RATE = 16000
PROGRESS_S = 30.0
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
MARK_EVERY = 500          # about every 7.5 s at 240 frames a packet
JOURNAL_NAME = "packets.journal"
JOURNAL_FLUSH_S = 1.0     # to the OS: survives this process
JOURNAL_SYNC_S = 5.0      # to the disk: survives the PC
FLOOR_WINDOW = RATE // 10
MIC_FLOOR_WARN = 400.0    # a healthy mic stays near 100 in the quiet moments; the noisy periods of #001 show 450 and up
MIC_WARNING = ("mic floor %s: steady noise on the mic line of the LyraT. It came for minutes at a time on the bench, resets "
               "did not clear it, and this part of mic.wav is spoiled. Steady loud sound in the room looks the same.")


def local_addresses():
    """IPv4 addresses of this PC: a broadcast leaves through one interface only, so each gets its own."""
    try:
        found = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        found = []
    return [a for a in found if not a.startswith("127.")] or ["0.0.0.0"]


def route_address(target):
    """The local address the routing table picks for target. UDP connect sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((target, TAP_REQUEST_PORT))
        return s.getsockname()[0]


def floor_rms(pcm, channels):
    """Per channel: the 10th percentile of the level over 100 ms windows; windows of pure silence (losses) do not count."""
    result = []
    for c in range(channels or 0):
        data = pcm[c::channels]
        levels = []
        for start in range(0, len(data) - FLOOR_WINDOW + 1, FLOOR_WINDOW):
            power = sum(v * v for v in data[start:start + FLOOR_WINDOW])
            if power:
                levels.append(power)
        levels.sort()
        result.append(round((levels[len(levels) // 10] / FLOOR_WINDOW) ** 0.5, 1) if levels else None)
    return result


def floor_too_high(floors):
    return any(f is not None and f > MIC_FLOOR_WARN for f in floors or ())


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
        self.bus_gap_s = None    # arrival time ahead of the frames by more than this: the bus stopped

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
            return False
        if self.last_seq is not None and packets and seq < self.last_seq:
            self.reordered += 1
        self.last_seq = seq
        packets[seq] = (frame, channels, at, ovf, send_fail, data[TAP_HEADER:])
        return True

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
        gaps, bursts, bus_stops = [], [], []
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
            prev_at = None
            for seq in seqs:
                frame, ch, at, _, _, payload = packets[seq]
                if ch != channels:
                    continue        # the slots were changed in flight; never mix layouts in one file
                if self.bus_gap_s and expect is not None and prev_at is not None:
                    stopped = int((at - prev_at - (frame - expect + SAMPLES_PER_FRAME) / RATE) * RATE)
                    if stopped > self.bus_gap_s * RATE:
                        gaps.append([len(pcm) // channels, stopped])
                        bus_stops.append([len(pcm) // channels, stopped])
                        pcm.extend([0] * (stopped * channels))
                prev_at = at
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
                 "bus_stops": bus_stops, "boots": len(self.boots), "ovf": ovf, "send_fail": send_fail,
                 "rate_hz": round(sum(t * r for t, r in rates) / seconds, 2) if seconds else None,
                 "channels": channels, "frames": len(pcm) // (channels or 1),
                 "seconds": round(len(pcm) / (channels or 1) / RATE, 3), "floor_rms": floor_rms(pcm, channels),
                 "first_at": self.first_at, "last_at": self.last_at,
                 "wall_s": round(self.last_at - self.first_at + SAMPLES_PER_FRAME / RATE, 3) if self.first_at else None,
                 "marks": [[q, round(p[q][2], 4)] for p in self.boots.values() for q in sorted(p) if q % MARK_EVERY == 0]}
        return channels, pcm, stats


class TapRecorder:
    def __init__(self, slots=("L", "L"), board=None, forward=None, listen_only=False, ports=None, journal_dir=None,
                 bus_gap_s=None):
        """forward: {"call": (host, port), "mic": (host, port)}. listen_only: never talk to the board, take what
        arrives on ports ({"call": port, "mic": port}): the far end of someone else's forward. journal_dir, bus_gap_s: see
        the top of this file."""
        self.journal = None
        self.journal_error = None
        self.journal_flushed = self.journal_synced = 0.0
        if journal_dir:
            Path(journal_dir).mkdir(parents=True, exist_ok=True)
            self.journal = open(Path(journal_dir) / JOURNAL_NAME, "ab")
        self.request = ("TAP1 %s %s" % tuple(slots)).encode()
        self.board = board          # known address, or None to find it by broadcast
        self.listen_only = listen_only
        self.forward = dict(forward or {})
        self.forwarded = {name: 0 for name in self.forward}
        self.forward_failed = {name: 0 for name in self.forward}
        self.forward_sock = None
        if self.forward:
            self.forward_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.forward_sock.setblocking(False)
        self.socks = {}
        for name, port in (ports or TAP_PORTS).items():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
            s.bind(("0.0.0.0", port))
            s.setblocking(False)
            self.socks[s] = name
        self.senders = []
        for address in [] if listen_only else local_addresses():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((address, 0))
            s.setblocking(False)
            self.senders.append(s)
        self.streams = {name: _TapStream(name) for name in TAP_PORTS}
        for stream in self.streams.values():
            stream.bus_gap_s = bus_gap_s
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
                    # Unicast only from the address the route to the board uses. Another adapter may be a phone's
                    # USB tethering, which forwards the request by NAT: the board then sees the phone as the
                    # requester and turns the stream to it, and most packets never reach this PC.
                    if s.getsockname()[0] == route_address(self.board):
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
            if now >= next_ask and not self.listen_only:
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
                        stream = TAP_STREAMS.get(data[3], name)
                        first = self.streams[stream].add(data, now)
                        if first and self.journal:
                            self._journal_add(data, now)
                        if first and stream in self.forward:
                            self._pass_on(stream, data)
                    else:
                        self.foreign[name].add(data, now)
                else:
                    self.replies += 1
                    try:
                        self.board_status = json.loads(data.decode("ascii", "replace"))
                        self.board = sender[0]
                    except ValueError:
                        pass

    def _journal_add(self, data, at):
        """An error here must not stop the recording in memory: the journal only guards against a dead process."""
        try:
            self.journal.write(struct.pack("<dI", at, len(data)) + data)
            if at - self.journal_flushed >= JOURNAL_FLUSH_S:
                self.journal.flush()
                self.journal_flushed = at
            if at - self.journal_synced >= JOURNAL_SYNC_S:
                os.fsync(self.journal.fileno())
                self.journal_synced = at
        except OSError as e:
            self.journal_error = str(e)

    @classmethod
    def from_journal(cls, directory, bus_gap_s=None):
        """A recorder holding what a journal has, for save(); a torn record at the end is left out. Opens no socket."""
        self = cls.__new__(cls)
        self.streams = {name: _TapStream(name) for name in TAP_PORTS}
        for stream in self.streams.values():
            stream.bus_gap_s = bus_gap_s
        self.foreign = {}
        self.forward = {}
        self.requests = self.broadcasts = self.replies = 0
        self.board_status = None
        self.t0 = None
        head = struct.calcsize("<dI")
        raw = (Path(directory) / JOURNAL_NAME).read_bytes()
        pos = 0
        while pos + head <= len(raw):
            at, size = struct.unpack_from("<dI", raw, pos)
            data = raw[pos + head:pos + head + size]
            if len(data) < size or data[:2] != TAP_MAGIC:
                break
            if self.t0 is None:
                self.t0 = at
            self.streams[TAP_STREAMS.get(data[3], "call")].add(data, at)
            pos += head + size
        return self

    def _pass_on(self, stream, data):
        """Never waits and never raises: the recording does not depend on the second consumer."""
        try:
            self.forward_sock.sendto(data, self.forward[stream])
            self.forwarded[stream] += 1
        except OSError:
            self.forward_failed[stream] += 1

    def start(self):
        self.t0 = time.monotonic()
        self._thread.start()
        return self

    def stop(self, release=True):
        self._stop.set()
        self._thread.join(timeout=2)
        if release and self.board and self.senders:
            try:
                next((x for x in self.senders if x.getsockname()[0] == route_address(self.board)),
                     self.senders[0]).sendto(b"TAP0", (self.board, TAP_REQUEST_PORT))
            except OSError:
                pass
        for s in list(self.socks) + self.senders + ([self.forward_sock] if self.forward_sock else []):
            s.close()
        if self.journal:
            self.journal.close()
        return self

    def recent_floor(self, name, packets=200):
        """floor_rms of about the last 3 s of a stream, while recording."""
        boots = list(self.streams[name].boots.values())
        recent = list(boots[-1].values())[-packets:] if boots else []
        if not recent:
            return []
        pcm = array.array("h")
        for item in recent:
            pcm.frombytes(item[5][:len(item[5]) // 2 * 2])
        return floor_rms(pcm, recent[0][1])

    def received(self, name):
        """Packets of one stream so far; safe to read while recording."""
        return self.streams[name].packets

    def save(self, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = {"requests": self.requests, "broadcasts": self.broadcasts, "replies": self.replies,
                   "board_status": self.board_status}
        if self.forward:
            summary["forward"] = {name: {"target": "%s:%d" % target, "forwarded": self.forwarded[name],
                                         "failed": self.forward_failed[name]} for name, target in self.forward.items()}
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


def parse_forward(text):
    """HOST:PORT,HOST:PORT into {"call": ..., "mic": ...}."""
    targets = {}
    parts = text.split(",")
    if len(parts) != 2:
        raise ValueError("--forward takes two addresses, call and mic: %s" % text)
    for name, part in zip(("call", "mic"), parts):
        host, port = part.rsplit(":", 1)
        targets[name] = (host, int(port))
    return targets


def wait_recording(rec, seconds):
    """Until the time is up or Ctrl+C, with a line of progress; a board that is not there is said at once."""
    start = time.monotonic()
    next_line = start + PROGRESS_S
    try:
        while seconds is None or time.monotonic() - start < seconds:
            time.sleep(0.2)
            now = time.monotonic()
            if now >= next_line:
                next_line = now + PROGRESS_S
                got = {name: rec.received(name) for name in rec.streams}
                note = "" if all(got.values()) else "  no stream yet: is the S3-ETH board on and in this network?"
                print("%6.0f s  packets call %d, mic %d%s" % (now - start, got["call"], got["mic"], note), flush=True)
                floors = rec.recent_floor("mic")
                if floor_too_high(floors):
                    print("WARNING " + MIC_WARNING % floors, flush=True)
    except KeyboardInterrupt:
        print("stopping", flush=True)


def brief(summary):
    """The numbers a person looks at; everything else is in tap.json."""
    keep = ("seconds", "wall_s", "rate_hz", "unique", "lost_packets", "loss_pct", "max_burst", "ovf", "send_fail", "channels",
            "floor_rms")
    result = {name: {k: summary[name].get(k) for k in keep} for name in ("call", "mic")}
    if "forward" in summary:
        result["forward"] = summary["forward"]
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, help="length; with --tap and --receive it may be left out")
    ap.add_argument("--out", help="directory; with --tap the default is <bench runs>/record/<stamp>")
    ap.add_argument("--ports", default="5004,5005")
    ap.add_argument("--tap", action="store_true", help="take the PCM streams of the S3-ETH board")
    ap.add_argument("--slots", default="L,LR", help="--tap: slots of the call and mic lines: L, R or LR. The call has the "
                                                    "same samples in both slots, the mic has two different channels")
    ap.add_argument("--forward", help="--tap: HOST:PORT,HOST:PORT of a second consumer, call then mic")
    ap.add_argument("--receive", help="PORT,PORT, call then mic: be the second consumer of a --forward")
    ap.add_argument("--idle-exit", type=float, help="--receive: stop this long after the last packet")
    args = ap.parse_args()
    if args.receive:
        return receive(args)
    if args.tap:
        forward = parse_forward(args.forward) if args.forward else None
        out = Path(args.out) if args.out else run_dir("record")
        rec = TapRecorder(tuple(args.slots.split(",")), forward=forward).start()
        print("recording to %s, %s" % (out, "Ctrl+C stops" if args.seconds is None else "%g s" % args.seconds), flush=True)
        wait_recording(rec, args.seconds)
        summary = rec.stop().save(out)
        print(json.dumps(brief(summary), indent=2))
        if floor_too_high(summary["mic"]["floor_rms"]):
            print("WARNING " + MIC_WARNING % summary["mic"]["floor_rms"], flush=True)
        print("written: %s" % out, flush=True)
        return 0 if summary["call"]["unique"] and summary["mic"]["unique"] else 2
    if args.seconds is None or not args.out:
        ap.error("the form without --tap needs --seconds and --out")
    rec = UdpRecorder([int(p) for p in args.ports.split(",")]).start()
    time.sleep(args.seconds)
    summary = rec.stop().save(args.out)
    print(json.dumps(summary, indent=2))
    return 0 if any(s["unique"] for s in summary.values()) else 2


def receive(args):
    """The far end of --forward: says when it listens, ends after the stream has stopped, saves what it got."""
    if not args.out:
        raise SystemExit("--receive needs --out")
    call, mic = (int(p) for p in args.receive.split(","))
    rec = TapRecorder(listen_only=True, ports={"call": call, "mic": mic}).start()
    print("listening", flush=True)
    start = time.monotonic()
    try:
        while args.seconds is None or time.monotonic() - start < args.seconds:
            time.sleep(0.05)
            last = max((st.last_at for st in rec.streams.values() if st.last_at), default=None)
            if args.idle_exit and last and time.monotonic() - last > args.idle_exit:
                break
    except KeyboardInterrupt:
        pass
    summary = rec.stop(release=False).save(args.out)
    print(json.dumps(brief(summary), indent=2))
    return 0 if summary["call"]["unique"] and summary["mic"]["unique"] else 2


if __name__ == "__main__":
    sys.exit(main())
