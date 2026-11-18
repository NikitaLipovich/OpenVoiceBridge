#!/usr/bin/env python3
"""The live stream of a call: both lines of the S3 aligned in real time, for a local consumer such as a transcriber.

  call_stream.py serve [--seconds N] [--pcm-port 5010] [--hold-ms 60]    take the S3 stream, serve it on 127.0.0.1
  call_stream.py listen [--port 5010] [--seconds N] [--wav OUT.wav]     a consumer: reads, checks, measures the latency

The pipeline (goal 097, DECISIONS D1 of the goal): the receiver (udp_record.TapRecorder) hands the first copy of each packet
to LivePipeline; the pipeline places the samples of both lines on one session time line, holds them in a jitter buffer
and gives out blocks of 20 ms, stereo s16le, left the line to the headphones (down, DSDIN), right the headset mic (up,
ASDOUT, its two ADC channels averaged as the LyraT firmware does), as soon as both lines cover a block, or at the deadline
with zeros where a line is missing (a flag says so). A packet later than its deadline is counted and left out of the live
stream; the recording of call_check.py does not lose it, it is made from the receiver's full store.

Clocks. The S3 puts no time into a packet; each of its two I2S channels counts frames from its own start, both on the
LyraT's LRCK. A packet leaves when its last frame was on the bus, so for each line min(arrival - end frame / 16000) over
10 s is the PC time of frame 0 plus the fastest way through: the lower envelope. The difference of the two envelopes
aligns the lines (to about 1 ms); the drift of the bus clock against the PC moves both alike. t_sound of a block is the PC
time (time.monotonic, QueryPerformanceCounter on Windows, shared by processes) when its first sample was on the I2S bus:
the envelope minus the one way delay of the network, taken as half the shortest round trip of the TAP1 requests.
ClockModel.s3_mark() holds that against the S3's own counters in its TAP1 replies.

Output format, version 1, TCP on 127.0.0.1 only (the conversation does not leave the PC): first a JSON line
  {"proto": "callpcm", "version": 1, "rate": 16000, "channels": 2, "layout": ["down", "up"], "sample": "s16le",
   "block_frames": 320, "header": "<4sIQHHd"}
then blocks: header b"CPB1", u32 block number (a gap: this client was too slow and lost blocks), u64 session frame of the
first sample, u16 frames, u16 flags (1 no down, 2 no up, 4 a break before this block: restart, bus stop, re-alignment),
f64 t_sound (NaN while the clocks are not known), then frames x 2 x int16 little endian. Each client has its own queue
of 2 s; a slow client loses its oldest blocks and never slows the pipeline or the recording.

Nothing here plays sound (R15) or transcribes. As a module: LivePipeline(consumers).feed(stream, data, arrival),
.tick(now), or .start()/.stop() as a thread fed by TapRecorder(on_packet=pipeline.on_packet); PcmServer, LiveMeter,
LatencyStats are consumers.
"""
import argparse
import collections
import json
import math
import os
import queue
import socket
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

RATE = 16000
S3_BLOCK = 240                  # frames per S3 packet (DMA_FRAMES of s3_eth_bridge)
OUT_BLOCK = 320                 # frames per block given out: 20 ms
HOLD_S = 0.060                  # waiting for a late packet beyond the earliest it could come
ENVELOPE_S = 10.0               # window of the lower envelope
LOCK_PACKETS = 20               # packets of both lines before the alignment is trusted
LOCK_AT = (LOCK_PACKETS, 200)   # the alignment is set exactly at these counts of the mic's packets: 0.3 s and 3 s
RELOCK_FRAMES = 16              # the alignment moved by more than 1 ms ...
RELOCK_S = 0.5                  # ... for this long: re-align, with a break flag
JUMP_S = 2.0                    # a frame jump beyond this is a new segment, not a loss
PAST_S = 0.5                    # a packet this far behind the output is a stopped bus that came back: new segment
RING_S = 8
PCM_PORT = 5010 + int(os.environ.get("BENCH_PORT_OFFSET", "0") or 0)   # benchlib.bench_port (goal 103)
CLIENT_QUEUE = 100              # blocks: 2 s
TICK_S = 0.005
HEADER = struct.Struct("<4sIQHHd")
MAGIC = b"CPB1"
F_NO_DOWN, F_NO_UP, F_BREAK = 1, 2, 4
LINES = ("call", "mic")         # down, up
TAP = struct.Struct("<2sBBIIHBBII")


def parse(data):
    """(line, frame, boot, ovf, mono int16) of an S3 packet; None for a foreign one."""
    if len(data) <= TAP.size or data[:2] != b"S3" or data[2] != 1:
        return None
    _, _, stream, _seq, frame, boot, channels, _f8, ovf, _sf = TAP.unpack_from(data)
    pcm = np.frombuffer(data[TAP.size:TAP.size + (len(data) - TAP.size) // 2 * 2], dtype="<i2")
    if channels == 2:
        pcm = ((pcm[0::2].astype(np.int32) + pcm[1::2].astype(np.int32)) >> 1).astype(np.int16)
    return LINES[stream] if stream < 2 else None, frame, boot, ovf, pcm


class Envelope:
    """Sliding minimum over a time window."""

    def __init__(self, window=ENVELOPE_S):
        self.window = window
        self.q = collections.deque()       # (at, value), values increasing
        self.count = 0

    def add(self, at, value):
        while self.q and self.q[-1][1] >= value:
            self.q.pop()
        self.q.append((at, value))
        while self.q[0][0] < at - self.window:
            self.q.popleft()
        self.count += 1

    def value(self):
        return self.q[0][1] if self.q else None


class _Segment:
    """One line between two breaks: one S3 boot, the bus running, frames counted from one origin."""

    def __init__(self, boot, ovf, frame, at):
        self.boot, self.ovf0 = boot, ovf
        self.next_frame = frame
        self.env = Envelope()
        self.base = None                   # session frame = frame + 240 x (ovf - ovf0) + base
        self.started = at
        self.off_since = None

    def epoch(self):
        return self.env.value()


class ClockModel:
    """The S3's TAP1 replies against the PC clock: the shortest round trip, and the S3 mark of the latency."""

    def __init__(self, recorder=None):
        self.recorder = recorder

    def marks(self):
        return list(self.recorder.reply_marks) if self.recorder is not None else []

    def rtt_min(self):
        rtts = [b - a for a, b, _ in self.marks() if b >= a]
        return min(rtts) if rtts else None

    def oneway(self):
        rtt = self.rtt_min()
        return rtt / 2 if rtt is not None else 0.0

    def s3_mark(self, envelope_epoch, boot, since, line="call"):
        """The interval of the PC time of frame 0 that the S3's replies allow: frame F already on the bus when it answered,
        F+240 not yet (true while its I2S task keeps up). The envelope epoch minus the one way delay should be inside."""
        lo, hi, n = -math.inf, math.inf, 0
        for asked, replied, reply in self.marks():
            if asked < since or reply.get("boot") != boot or not isinstance(reply.get(line), dict):
                continue
            f = reply[line].get("frames")
            if f is None:
                continue
            lo = max(lo, asked - (f + S3_BLOCK) / RATE)
            hi = min(hi, replied - f / RATE)
            n += 1
        if not n or envelope_epoch is None:
            return {"replies": n}
        est = envelope_epoch - self.oneway()
        return {"replies": n, "lo_ms_vs_est": round((lo - est) * 1000, 2), "hi_ms_vs_est": round((hi - est) * 1000, 2),
                "width_ms": round((hi - lo) * 1000, 2), "consistent": bool(lo - 0.002 <= est <= hi + 0.002)}


Block = collections.namedtuple("Block", "index frame pcm flags t_sound emitted_at")


class LivePipeline:
    def __init__(self, consumers=(), hold_s=HOLD_S, clock=None):
        self.consumers = list(consumers)
        self.hold_s = hold_s
        self.clock = clock or ClockModel()
        self.inbox = collections.deque()
        self.seg = {line: None for line in LINES}
        self.segments = {line: 0 for line in LINES}
        self.sigma = None                  # PC time of session frame 0, plus the fastest way through
        self.ring = {line: np.zeros(RATE * RING_S, dtype=np.int16) for line in LINES}
        self.valid = {line: np.zeros(RATE * RING_S, dtype=bool) for line in LINES}
        self.pos = None
        self.index = 0
        self.pending_break = False
        self.stats = {"packets": {l: 0 for l in LINES}, "late": {l: 0 for l in LINES}, "blocks": 0,
                      "blocks_no_down": 0, "blocks_no_up": 0, "breaks": 0, "relocks": 0, "consumer_errors": 0,
                      "align_frames": None, "align_history": [], "segment_log": []}
        self.halt = threading.Event()
        self.thread = None

    # --- input ------------------------------------------------------------------------------------------------------

    def on_packet(self, stream, data, arrival):
        """The receiver's hook: never blocks."""
        self.inbox.append((data, arrival))

    def feed(self, data, arrival):
        got = parse(data)
        if got is None or got[0] is None:
            return
        line, frame, boot, ovf, pcm = got
        self.stats["packets"][line] += 1
        seg = self.seg[line]
        n = len(pcm)
        why = "first"
        if seg is not None:
            eff = frame + S3_BLOCK * (ovf - seg.ovf0)
            jump = eff - seg.next_frame
            if boot != seg.boot:
                seg, why = None, "boot"
            elif abs(jump) > JUMP_S * RATE:   # a small negative jump is a packet out of order
                seg, why = None, "jump %d" % jump
            elif seg.base is not None and self.pos is not None and eff + seg.base + n < self.pos - PAST_S * RATE:
                # the frames stood still while time went on: the bus stopped
                seg, why = None, "past %d" % (self.pos - eff - seg.base)
        if seg is None:
            self.stats["segment_log"].append([line, round(arrival, 3), why])
            del self.stats["segment_log"][:-20]
            seg = _Segment(boot, ovf, frame, arrival)
            self.seg[line] = seg
            self.segments[line] += 1
            if self.segments[line] > 1:
                self.pending_break = True
        eff = frame + S3_BLOCK * (ovf - seg.ovf0)
        seg.next_frame = max(seg.next_frame, eff + n)
        seg.env.add(arrival, arrival - (eff + n) / RATE)
        if seg.base is None:
            self._anchor(line, seg)
        if line == "call" or self.seg["call"] is None:
            self.sigma = seg.epoch() - seg.base / RATE
        else:
            self._check_alignment(seg, arrival)
        self._place(line, eff + seg.base, pcm)

    def _anchor(self, line, seg):
        if self.sigma is None:
            seg.base = 0
            self.sigma = seg.epoch()
        else:
            seg.base = int(round((seg.epoch() - self.sigma) * RATE))

    def _check_alignment(self, seg, at):
        """The mic's place against the call: the difference of the envelopes, which the drift does not touch."""
        call = self.seg["call"]
        if call is None or call.env.count < LOCK_PACKETS or seg.env.count < LOCK_PACKETS:
            return
        want = int(round((seg.epoch() - self.sigma) * RATE))
        self.stats["align_frames"] = want - call.base
        hist = self.stats["align_history"]
        hist.append(want - call.base)
        del hist[:-200]
        if seg.env.count in LOCK_AT:
            # Fixed points: the first lock, then once more on a longer envelope. A break only when it moved.
            if want != seg.base:
                seg.base = want
                self.stats["relocks"] += 1
                self.pending_break = True
            seg.off_since = None
            return
        if abs(want - seg.base) <= RELOCK_FRAMES:
            seg.off_since = None
            return
        if seg.off_since is None:
            seg.off_since = at
        # The first lock comes at once; later the difference has to hold, one slow packet does not move it.
        if at - seg.off_since >= RELOCK_S:
            seg.base = want
            seg.off_since = None
            self.stats["relocks"] += 1
            self.pending_break = True

    def _place(self, line, s, pcm):
        n = len(pcm)
        if self.pos is None:
            self.pos = s if line == "call" else None
            if self.pos is None:
                return
        if s + n <= self.pos:
            self.stats["late"][line] += 1
            return
        cut = max(self.pos - s, 0)
        if s + n > self.pos + len(self.ring[line]):
            return                         # beyond the buffer; a segment check catches a real jump
        idx = (np.arange(s + cut, s + n)) % len(self.ring[line])
        self.ring[line][idx] = pcm[cut:]
        self.valid[line][idx] = True

    # --- output -----------------------------------------------------------------------------------------------------

    def deadline(self, frame_end):
        return self.sigma + (frame_end + S3_BLOCK) / RATE + self.hold_s

    def tick(self, now):
        while self.inbox:
            self.feed(*self.inbox.popleft())
        if self.pos is None or self.sigma is None:
            return
        size = len(self.ring["call"])
        while True:
            idx = np.arange(self.pos, self.pos + OUT_BLOCK) % size
            have = {line: bool(self.valid[line][idx].all()) for line in LINES}
            if not all(have.values()) and now < self.deadline(self.pos + OUT_BLOCK):
                return
            pcm = np.zeros((OUT_BLOCK, 2), dtype=np.int16)
            flags = F_BREAK if self.pending_break else 0
            self.pending_break = False
            for c, line in enumerate(LINES):
                ok = self.valid[line][idx]
                pcm[:, c] = np.where(ok, self.ring[line][idx], 0)
                if not ok.all():
                    flags |= (F_NO_DOWN, F_NO_UP)[c]
                self.valid[line][idx] = False
            t_sound = self.sigma + self.pos / RATE - self.clock.oneway()
            block = Block(self.index, self.pos, pcm, flags, t_sound, now)
            self.index += 1
            self.pos += OUT_BLOCK
            self.stats["blocks"] += 1
            self.stats["blocks_no_down"] += bool(flags & F_NO_DOWN)
            self.stats["blocks_no_up"] += bool(flags & F_NO_UP)
            self.stats["breaks"] += bool(flags & F_BREAK)
            for c in self.consumers:
                try:
                    c.consume(block)
                except Exception:          # a consumer's fault must not stop the stream  # noqa: BLE001
                    self.stats["consumer_errors"] += 1

    def _run(self):
        while not self.halt.is_set():
            self.tick(time.monotonic())
            self.halt.wait(TICK_S)

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.halt.set()
        if self.thread:
            self.thread.join(timeout=2)
        for c in self.consumers:
            if hasattr(c, "close"):
                c.close()

    def summary(self):
        s = {k: v for k, v in self.stats.items() if k != "align_history"}
        s["segments"] = dict(self.segments)
        s["align_ms"] = round(s["align_frames"] / RATE * 1000, 2) if s["align_frames"] is not None else None
        hist = self.stats["align_history"]
        s["align_spread_frames"] = (max(hist) - min(hist)) if hist else None
        s["rtt_min_ms"] = round(self.clock.rtt_min() * 1000, 2) if self.clock.rtt_min() is not None else None
        call = self.seg["call"]
        if call is not None and call.epoch() is not None:
            s["s3_mark"] = self.clock.s3_mark(call.epoch(), call.boot, call.started)
        return s


# --- consumers -----------------------------------------------------------------------------------------------------------

class LatencyStats:
    """Sound on the I2S bus to the block given out, for the first sample of each block (its last is 20 ms less)."""

    def __init__(self):
        self.values = []
        self.at = []

    def consume(self, block):
        if not math.isnan(block.t_sound):
            self.values.append(block.emitted_at - block.t_sound)
            self.at.append(block.emitted_at)

    def summary(self, within=None):
        """within: [[from, to], ...] in time.monotonic: only the blocks given out then (the call, not the checks before)."""
        if within is not None:
            keep = [i for i, t in enumerate(self.at) if any(a <= t <= b for a, b in within)]
            return LatencyStats._of([self.values[i] for i in keep], [self.at[i] for i in keep])
        return LatencyStats._of(self.values, self.at)

    @staticmethod
    def _of(values, at):
        s = latency_summary(values)
        if values:
            # Where the slow blocks were, seconds from the first block: a cluster at the start is the lock, spread is the network.
            s["over_100ms_at_s"] = [round(t - at[0], 1) for t, v in zip(at, values) if v > 0.1][:40]
        return s


def latency_summary(values):
    if not values:
        return {"n": 0}
    v = np.array(values) * 1000.0
    return {"n": len(v), "median_ms": round(float(np.median(v)), 1), "p95_ms": round(float(np.percentile(v, 95)), 1),
            "p99_ms": round(float(np.percentile(v, 99)), 1), "max_ms": round(float(v.max()), 1),
            "min_ms": round(float(v.min()), 1), "over_100ms": int((v > 100).sum()), "over_300ms": int((v > 300).sum())}


class LiveMeter:
    """Levels of both lines over the last second, for the live line of call_check."""

    def __init__(self):
        self.lock = threading.Lock()
        self.window = collections.deque(maxlen=RATE // OUT_BLOCK)
        self.missing = [0, 0]

    def consume(self, block):
        x = block.pcm.astype(np.float64)
        with self.lock:
            self.window.append(((x ** 2).sum(axis=0), len(x)))
            self.missing[0] += bool(block.flags & F_NO_DOWN)
            self.missing[1] += bool(block.flags & F_NO_UP)

    def levels_db(self):
        with self.lock:
            if not self.window:
                return None, None
            power = sum(p for p, _ in self.window) / sum(n for _, n in self.window)
        return tuple(round(10 * math.log10(max(p, 1e-9) / 32768.0 ** 2), 1) for p in power)


class PcmServer:
    """The live stream on 127.0.0.1: several clients, each with its own queue."""

    def __init__(self, port=PCM_PORT, host="127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.clients = []
        self.lock = threading.Lock()
        self.halt = threading.Event()
        self.dropped = 0
        self.served = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def hello(self):
        return (json.dumps({"proto": "callpcm", "version": 1, "rate": RATE, "channels": 2, "layout": ["down", "up"],
                            "sample": "s16le", "block_frames": OUT_BLOCK, "header": "<4sIQHHd"}) + "\n").encode()

    def _accept(self):
        self.sock.settimeout(0.2)
        while not self.halt.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            q = queue.Queue(maxsize=CLIENT_QUEUE)
            with self.lock:
                self.clients.append(q)
                self.served += 1
            threading.Thread(target=self._send, args=(conn, q), daemon=True).start()

    def _send(self, conn, q):
        try:
            conn.sendall(self.hello())
            while not self.halt.is_set():
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                conn.sendall(item)
        except OSError:
            pass
        finally:
            with self.lock:
                if q in self.clients:
                    self.clients.remove(q)
            conn.close()

    def consume(self, block):
        data = HEADER.pack(MAGIC, block.index & 0xFFFFFFFF, block.frame & 0xFFFFFFFFFFFFFFFF, len(block.pcm), block.flags,
                           block.t_sound) + block.pcm.astype("<i2").tobytes()
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                try:
                    q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(data)
                except queue.Full:
                    self.dropped += 1

    def close(self):
        self.halt.set()
        with self.lock:
            for q in self.clients:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
        self.sock.close()


# --- a consumer on the other side of the socket --------------------------------------------------------------------------

def read_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def listen(port=PCM_PORT, seconds=None, wav=None, host="127.0.0.1", connect_timeout=10.0, on_block=None):
    """Read the live stream as a transcriber would. Returns the counts and the latency at this end."""
    end_connect = time.monotonic() + connect_timeout
    while True:
        try:
            conn = socket.create_connection((host, port), timeout=2.0)
            break
        except OSError:
            if time.monotonic() > end_connect:
                return {"connected": False}
            time.sleep(0.2)
    conn.settimeout(5.0)
    line = b""
    while not line.endswith(b"\n"):
        ch = conn.recv(1)
        if not ch:
            return {"connected": True, "hello": None}
        line += ch
    hello = json.loads(line)
    out = None
    if wav:
        out = wave.open(str(wav), "wb")
        out.setnchannels(hello["channels"])
        out.setsampwidth(2)
        out.setframerate(hello["rate"])
    lat, gaps, blocks, flagged, frames = [], 0, 0, collections.Counter(), 0
    last_index = None
    start = time.monotonic()
    try:
        while seconds is None or time.monotonic() - start < seconds:
            try:
                head = read_exact(conn, HEADER.size)
            except socket.timeout:
                break                      # the server gives a block every 20 ms while it runs: 5 s of nothing is its end
            if head is None:
                break
            magic, index, frame, n, flags, t_sound = HEADER.unpack(head)
            if magic != MAGIC:
                raise ValueError("bad block header")
            data = read_exact(conn, n * hello["channels"] * 2)
            if data is None:
                break
            now = time.monotonic()
            if last_index is not None and index != last_index + 1:
                gaps += 1
            last_index = index
            blocks += 1
            frames += n
            for bit, name in ((F_NO_DOWN, "no_down"), (F_NO_UP, "no_up"), (F_BREAK, "break")):
                if flags & bit:
                    flagged[name] += 1
            if not math.isnan(t_sound):
                lat.append(now - t_sound)
            if out:
                out.writeframes(data)
            if on_block:
                on_block(index, frame, flags, t_sound, data)
    finally:
        conn.close()
        if out:
            out.close()
    return {"connected": True, "hello": hello, "blocks": blocks, "frames": frames, "seconds": round(frames / RATE, 2),
            "index_gaps": gaps, "flags": dict(flagged), "latency": latency_summary(lat)}


def serve(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from udp_record import TapRecorder
    server = PcmServer(args.pcm_port)
    meter, latency = LiveMeter(), LatencyStats()
    rec = TapRecorder(("L", "LR"))
    pipe = LivePipeline([server, meter, latency], hold_s=args.hold_ms / 1000.0, clock=ClockModel(rec))
    rec.on_packet = pipe.on_packet
    pipe.start()
    rec.start()
    print("serving on 127.0.0.1:%d" % server.port, flush=True)
    start = time.monotonic()
    try:
        while args.seconds is None or time.monotonic() - start < args.seconds:
            time.sleep(1.0)
            down, up = meter.levels_db() if meter.window else (None, None)
            print("%5.0f s  down %s dB  up %s dB  blocks %d  late %s" % (
                time.monotonic() - start, down, up, pipe.stats["blocks"], pipe.stats["late"]), flush=True)
    except KeyboardInterrupt:
        pass
    rec.stop()
    pipe.stop()
    print(json.dumps({"pipeline": pipe.summary(), "latency": latency.summary(), "dropped": server.dropped}, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("serve")
    a.add_argument("--seconds", type=float)
    a.add_argument("--pcm-port", type=int, default=PCM_PORT)
    a.add_argument("--hold-ms", type=float, default=HOLD_S * 1000)
    b = sub.add_parser("listen")
    b.add_argument("--port", type=int, default=PCM_PORT)
    b.add_argument("--seconds", type=float)
    b.add_argument("--wav")
    args = ap.parse_args()
    if args.cmd == "serve":
        return serve(args)
    print(json.dumps(listen(args.port, args.seconds, args.wav), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
