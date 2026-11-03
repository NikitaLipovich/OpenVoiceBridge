#!/usr/bin/env python3
"""Record the board's UDP audio streams into WAV files: duplicates removed, losses counted.

  udp_record.py --seconds N --out DIR [--ports 5004,5005]

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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ports", default="5004,5005")
    args = ap.parse_args()
    rec = UdpRecorder([int(p) for p in args.ports.split(",")]).start()
    time.sleep(args.seconds)
    summary = rec.stop().save(args.out)
    print(json.dumps(summary, indent=2))
    return 0 if any(s["unique"] for s in summary.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
