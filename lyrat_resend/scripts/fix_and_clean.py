#!/usr/bin/env python3
"""
Diagnose and fix mic recording from LyraT.
Generates multiple versions to find correct playback rate.

Usage: python fix_and_clean.py input_16k.wav
"""

import sys
import os
import wave
import struct
import math


def read_wav(path):
    w = wave.open(path, "rb")
    rate = w.getframerate()
    n = w.getnframes()
    data = w.readframes(n)
    samples = list(struct.unpack(f"<{n}h", data))
    w.close()
    return samples, rate


def write_wav(path, samples, rate):
    clamped = [max(-32768, min(32767, int(round(s)))) for s in samples]
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(struct.pack(f"<{len(clamped)}h", *clamped))
    w.close()


def normalize(samples, target_db=-3):
    peak = max(abs(s) for s in samples) if samples else 1
    if peak < 1:
        peak = 1
    target = 32767 * (10 ** (target_db / 20))
    gain = target / peak
    return [s * gain for s in samples]


def biquad(samples, b0, b1, b2, a1, a2):
    out = []
    x1 = x2 = y1 = y2 = 0.0
    for s in samples:
        x0 = float(s)
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, x0
        y2, y1 = y1, y0
        out.append(y0)
    return out


def hpf(samples, rate, fc=80):
    w0 = 2 * math.pi * fc / rate
    alpha = math.sin(w0) / 1.4142
    c = math.cos(w0)
    a0 = 1 + alpha
    return biquad(samples,
                  (1+c)/2/a0, -(1+c)/a0, (1+c)/2/a0,
                  -2*c/a0, (1-alpha)/a0)


def lpf(samples, rate, fc=3500):
    w0 = 2 * math.pi * fc / rate
    alpha = math.sin(w0) / 1.4142
    c = math.cos(w0)
    a0 = 1 + alpha
    return biquad(samples,
                  (1-c)/2/a0, (1-c)/a0, (1-c)/2/a0,
                  -2*c/a0, (1-alpha)/a0)


def wiener_denoise(samples, rate, noise_sec=0.3):
    """Per-frame Wiener gain based on noise estimate from first N seconds."""
    frame_sz = int(rate * 0.025)  # 25ms frames
    noise_n = int(noise_sec * rate)
    noise_samples = samples[:noise_n]
    noise_rms = math.sqrt(sum(s*s for s in noise_samples) / len(noise_samples)) if noise_samples else 1

    out = list(samples)
    for i in range(0, len(samples), frame_sz):
        frame = samples[i:i+frame_sz]
        if not frame:
            break
        frms = math.sqrt(sum(s*s for s in frame) / len(frame))
        if frms > 0:
            g = max(0.0, 1.0 - (noise_rms / frms) ** 2)
        else:
            g = 0.0
        for j in range(len(frame)):
            out[i+j] = samples[i+j] * g
    return out


def main():
    if len(sys.argv) < 2:
        print("Usage: python fix_and_clean.py input_16k.wav")
        sys.exit(1)

    in_path = sys.argv[1]
    base = os.path.splitext(in_path)[0]
    samples, rate = read_wav(in_path)
    n = len(samples)
    dur = n / rate

    print(f"Input: {in_path}")
    print(f"  {n} samples, {rate} Hz, {dur:.1f}s")
    print(f"  RMS={math.sqrt(sum(s*s for s in samples)/n):.0f}, Peak={max(abs(s) for s in samples)}")

    # =============================================
    # Hypothesis: ESP32 sends STEREO interleaved (L+R) to UDP
    # but Python records it as mono → 2x speed
    # Fix: deinterleave, take one channel
    # =============================================

    L = samples[0::2]  # even = left channel
    R = samples[1::2]  # odd = right channel
    mono_from_stereo = [(l + r) // 2 for l, r in zip(L, R)]

    print(f"\n--- Generating test files ---\n")

    # 1. Original just amplified
    loud = normalize(samples, -3)
    out = f"{base}_v1_loud_16k.wav"
    write_wav(out, loud, 16000)
    print(f"  v1: {out} — original amplified @16kHz ({dur:.1f}s)")

    # 2. Deinterleaved L channel at 8kHz (if stereo→mono bug, this = correct speed)
    loud_L = normalize(L, -3)
    out = f"{base}_v2_deinter_8k.wav"
    write_wav(out, loud_L, 8000)
    print(f"  v2: {out} — deinterleaved L @8kHz ({len(L)/8000:.1f}s)")

    # 3. Deinterleaved L+R averaged at 8kHz
    loud_avg = normalize(mono_from_stereo, -3)
    out = f"{base}_v3_deinter_avg_8k.wav"
    write_wav(out, loud_avg, 8000)
    print(f"  v3: {out} — deinterleaved (L+R)/2 @8kHz ({len(mono_from_stereo)/8000:.1f}s)")

    # 4. Original at 8kHz (plays 2x slow if really 16k)
    out = f"{base}_v4_raw_8k.wav"
    write_wav(out, normalize(samples, -3), 8000)
    print(f"  v4: {out} — raw @8kHz / 2x slow ({n/8000:.1f}s)")

    # 5. Best guess: deinterleaved + cleaned at 8kHz
    print(f"\n--- Processing v5 (deinterleaved + cleaned @8kHz) ---")
    clean = [float(s) for s in mono_from_stereo]
    clean = hpf(clean, 8000, 80)
    clean = lpf(clean, 8000, 3500)
    clean = wiener_denoise(clean, 8000, 0.3)
    clean = normalize(clean, -3)
    out = f"{base}_v5_clean_8k.wav"
    write_wav(out, clean, 8000)
    print(f"  v5: {out} — deinterleaved+HPF+LPF+Wiener @8kHz ({len(clean)/8000:.1f}s)")

    # 6. Original cleaned at 16kHz (in case it IS truly 16k mono)
    print(f"--- Processing v6 (cleaned @16kHz) ---")
    clean16 = [float(s) for s in samples]
    clean16 = hpf(clean16, 16000, 80)
    clean16 = lpf(clean16, 16000, 4000)
    clean16 = wiener_denoise(clean16, 16000, 0.3)
    clean16 = normalize(clean16, -3)
    out = f"{base}_v6_clean_16k.wav"
    write_wav(out, clean16, 16000)
    print(f"  v6: {out} — HPF+LPF+Wiener @16kHz ({len(clean16)/16000:.1f}s)")

    print(f"""
=== LISTEN AND COMPARE ===

If speech sounds FAST/chipmunk → v2, v3, v5 are correct (stereo→mono bug)
If speech sounds SLOW         → v1, v6 are correct (data is truly 16k mono)
If speech sounds NORMAL in v1 → no speed issue, just need cleaning (v6)

Best cleaned version is likely v5 (if sped up) or v6 (if normal speed).
""")


if __name__ == "__main__":
    main()
