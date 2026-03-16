#!/usr/bin/env python3
"""
Simple UDP audio capture for ESP32S3 stream.
Receives UDP packets and writes them into a WAV file.
Defaults assume 16 kHz, 16-bit, mono PCM (match your ESP32 settings).
"""

import socket
import wave
import argparse
import signal
import sys
import time


def main():
    parser = argparse.ArgumentParser(description="Capture UDP audio packets and save to WAV")
    parser.add_argument("--bind", default="0.0.0.0", help="IP to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5004, help="UDP port to listen on (default: 5004)")
    parser.add_argument("--outfile", default="received.wav", help="Output WAV file")
    parser.add_argument("--rate", type=int, default=16000, help="Sample rate (Hz)")
    parser.add_argument("--channels", type=int, default=1, help="Number of channels")
    parser.add_argument("--width", type=int, default=2, help="Bytes per sample (2 = 16-bit)")
    args = parser.parse_args()

    addr = (args.bind, args.port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(addr)
    sock.settimeout(1.0)

    wf = wave.open(args.outfile, 'wb')
    wf.setnchannels(args.channels)
    wf.setsampwidth(args.width)
    wf.setframerate(args.rate)

    running = True

    bytes_recv = 0
    packets = 0
    start = time.time()

    def handle_sigint(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_sigint)

    print(f"Listening on {addr[0]}:{addr[1]} -> writing to {args.outfile}")

    try:
        while running:
            try:
                data, peer = sock.recvfrom(65536)
            except socket.timeout:
                continue
            if not data:
                continue
            wf.writeframes(data)
            packets += 1
            bytes_recv += len(data)
            if packets % 100 == 0:
                elapsed = time.time() - start
                rate_kbps = (bytes_recv * 8) / elapsed / 1000 if elapsed > 0 else 0
                print(f"{packets} packets, {bytes_recv} bytes, {rate_kbps:.1f} kbps")
    finally:
        wf.close()
        sock.close()
        elapsed = time.time() - start
        print("\nStopped.")
        print(f"Packets: {packets}")
        print(f"Bytes: {bytes_recv}")
        print(f"Duration: {elapsed:.1f}s")
        if elapsed > 0:
            print(f"Avg bitrate: {(bytes_recv*8)/elapsed/1000:.1f} kbps")


if __name__ == '__main__':
    main()
