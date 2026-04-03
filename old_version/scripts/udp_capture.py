#!/usr/bin/env python3
"""
Dual UDP audio capture: port 5004 (call audio) + port 5005 (mic audio).
Records both streams simultaneously into timestamped WAV files in the dump/ folder.
Press Ctrl+C to stop — prints filenames and stats on exit.
"""

import os
import socket
import wave
import signal
import threading
import time
from datetime import datetime

SAMPLE_RATE  = 8000
SAMPLE_WIDTH = 2   # bytes (16-bit)
CHANNELS     = 1

STREAMS = [
    {"port": 5004, "label": "call"},   # phone → headphones (call audio from phone)
    {"port": 5005, "label": "mic"},    # headset mic → phone (your voice)
]

stop_event = threading.Event()


def handle_sigint(signum, frame):
    stop_event.set()


def capture_stream(port, label, out_path, stats):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)

    wf = wave.open(out_path, "wb")
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(SAMPLE_WIDTH)
    wf.setframerate(SAMPLE_RATE)

    packets     = 0
    total_bytes = 0

    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(65536)
        except socket.timeout:
            continue
        if not data:
            continue
        wf.writeframes(data)
        packets     += 1
        total_bytes += len(data)

    wf.close()
    sock.close()

    duration = total_bytes / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS) if total_bytes else 0
    stats["packets"]  = packets
    stats["bytes"]    = total_bytes
    stats["duration"] = duration


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dump_dir   = os.path.join(script_dir, "dump")
    os.makedirs(dump_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    files     = {}
    threads   = []
    stats_all = {}

    for s in STREAMS:
        filename      = f"{ts}_{s['label']}.wav"
        path          = os.path.join(dump_dir, filename)
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
    print(f"Output folder: {dump_dir}\n")
    for s in STREAMS:
        label = s["label"]
        print(f"  [{label:4s}] port {s['port']} → {os.path.basename(files[label])}")
    print("\nPress Ctrl+C to stop.\n")

    for t in threads:
        t.start()

    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        stop_event.set()

    for t in threads:
        t.join()

    print("\n--- Recording stopped ---\n")
    for s in STREAMS:
        label = s["label"]
        st    = stats_all[label]
        path  = files[label]
        dur   = st.get("duration", 0)
        pkts  = st.get("packets",  0)
        byts  = st.get("bytes",    0)
        print(f"  [{label:4s}]  file     : {path}")
        print(f"         packets  : {pkts}")
        print(f"         bytes    : {byts}")
        print(f"         duration : {dur:.1f}s")
        print()


if __name__ == "__main__":
    main()
