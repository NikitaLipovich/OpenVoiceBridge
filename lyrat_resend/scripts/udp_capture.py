#!/usr/bin/env python3
"""
UDP audio capture + µ-law decode + command sender — all in one.

Records call (5004) and mic (5005), decodes G.711 µ-law → PCM WAV.
Sends commands to ESP32 on port 5006.
ESP32 IP is auto-detected from first received packet.

Controls (type while recording):
  Pipeline:  hw N | hpf | mg N | notch | w | g | aec | sns 0/1/2 | snsg N
  Other:     lim | cg N | vol N | wifi | all | s | h
  Recorder:  q = stop recording

Press Ctrl+C to stop.
"""

import os
import socket
import wave
import signal
import threading
import time
import struct
import sys
from datetime import datetime

SAMPLE_RATE  = 16000
SAMPLE_WIDTH = 2
CHANNELS     = 1
SAMPLES_PER_FRAME = 240
CMD_PORT     = 5006

STREAMS = [
    {"port": 5004, "label": "call"},
    {"port": 5005, "label": "mic"},
]

stop_event = threading.Event()
esp32_ip = [None]  # mutable, set from first packet

# ── G.711 µ-law decoder ──────────────────────────────────────────────────────

BIAS = 0x84  # 132

def ulaw_decode_byte(b):
    b = ~b & 0xFF
    sign = b & 0x80
    exponent = (b >> 4) & 0x07
    mantissa = b & 0x0F
    sample = (mantissa << (exponent + 3)) + BIAS * (1 << exponent) - BIAS
    return -sample if sign else sample


def ulaw_decode(data):
    return [ulaw_decode_byte(b) for b in data]


# ── Command sender ───────────────────────────────────────────────────────────

def send_cmd(cmd):
    ip = esp32_ip[0]
    if not ip:
        print("  [!] ESP32 IP not yet detected (waiting for first audio packet)")
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(cmd.encode('ascii') + b'\n', (ip, CMD_PORT))
    sock.close()
    print(f"  -> {ip}:{CMD_PORT} '{cmd}'")


# ── Capture thread ───────────────────────────────────────────────────────────

def handle_sigint(signum, frame):
    stop_event.set()


def capture_stream(port, label, out_path, stats):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)

    wf = wave.open(out_path, "wb")
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(SAMPLE_WIDTH)
    wf.setframerate(SAMPLE_RATE)

    packets_ok    = 0
    frames_ok     = 0
    frames_lost   = 0
    total_samples = 0
    expected_seq  = None

    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            continue
        if len(data) < 9:
            continue

        # Auto-detect ESP32 IP
        if esp32_ip[0] is None:
            esp32_ip[0] = addr[0]
            print(f"  [*] ESP32 detected: {addr[0]}")

        seq = struct.unpack_from("<I", data, 0)[0]
        n_frames = data[7]
        ulaw_data = data[8:]

        if n_frames == 0:
            n_frames = max(1, len(ulaw_data) // SAMPLES_PER_FRAME)

        expected_samples = n_frames * SAMPLES_PER_FRAME

        if expected_seq is not None and seq > expected_seq:
            gap_frames = seq - expected_seq
            silence_samples = gap_frames * SAMPLES_PER_FRAME
            wf.writeframes(b'\x00' * (silence_samples * 2))
            frames_lost += gap_frames
            total_samples += silence_samples

        if expected_seq is not None and seq < expected_seq:
            continue

        expected_seq = seq + n_frames

        # Decode µ-law → PCM (no state, no crossfade needed)
        pcm = ulaw_decode(ulaw_data)
        if len(pcm) > expected_samples:
            pcm = pcm[:expected_samples]
        elif len(pcm) < expected_samples:
            pcm.extend([0] * (expected_samples - len(pcm)))

        wf.writeframes(struct.pack(f"<{len(pcm)}h", *pcm))
        packets_ok  += 1
        frames_ok   += n_frames
        total_samples += len(pcm)

    wf.close()
    sock.close()

    stats["packets"]     = packets_ok
    stats["frames_ok"]   = frames_ok
    stats["frames_lost"] = frames_lost
    stats["duration"]    = total_samples / SAMPLE_RATE if total_samples else 0


# ── Input thread (commands) ──────────────────────────────────────────────────

def input_thread():
    print("\nCommands: hw N | hpf | mg N | notch | w | g | aec")
    print("          sns 0=off 1=fft 2=mask | snsg N | lim | s | h")
    print("          q = stop\n")
    try:
        while not stop_event.is_set():
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line.lower() == 'q':
                stop_event.set()
                break
            send_cmd(line)
    except KeyboardInterrupt:
        stop_event.set()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dump_dir   = os.path.join(script_dir, "dump")
    os.makedirs(dump_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    files     = {}
    threads   = []
    stats_all = {}

    for s in STREAMS:
        filename = f"{ts}_{s['label']}.wav"
        path     = os.path.join(dump_dir, filename)
        files[s["label"]]     = path
        stats_all[s["label"]] = {}
        t = threading.Thread(
            target=capture_stream,
            args=(s["port"], s["label"], path, stats_all[s["label"]]),
            daemon=True,
        )
        threads.append(t)

    signal.signal(signal.SIGINT, handle_sigint)

    print(f"\nRecording started at {ts}")
    print(f"Output: {dump_dir}")
    for s in STREAMS:
        print(f"  [{s['label']:4s}] port {s['port']} -> {os.path.basename(files[s['label']])}")

    for t in threads:
        t.start()

    # Command input on main thread
    input_thread()

    stop_event.set()
    for t in threads:
        t.join()

    print("\n--- Recording stopped ---\n")
    for s in STREAMS:
        label = s["label"]
        st = stats_all[label]
        ok   = st.get("frames_ok", 0)
        lost = st.get("frames_lost", 0)
        total = ok + lost
        loss_pct = (lost / total * 100) if total > 0 else 0
        print(f"  [{label:4s}]  {os.path.basename(files[label])}")
        print(f"         packets: {st.get('packets', 0)} (µ-law)")
        print(f"         frames : {ok} ok, {lost} lost ({loss_pct:.1f}%)")
        print(f"         duration: {st.get('duration', 0):.1f}s")
    print()


if __name__ == "__main__":
    main()
