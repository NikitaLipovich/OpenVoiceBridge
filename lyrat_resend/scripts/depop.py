#!/usr/bin/env python3
"""
Remove ADPCM pop/puk artifacts from audio recording.

Detects rapid sign-alternating patterns (oscillation from ADPCM quantization)
and smooths them with local interpolation.

Usage: python depop.py input.wav [output.wav]
"""

import sys
import os
import wave
import struct
import numpy as np


def read_wav(path):
    w = wave.open(path, "rb")
    rate = w.getframerate()
    n = w.getnframes()
    data = w.readframes(n)
    samples = np.array(struct.unpack(f"<{n}h", data), dtype=np.float64)
    w.close()
    return samples, rate


def write_wav(path, samples, rate):
    out = np.clip(samples, -32768, 32767).astype(np.int16)
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(struct.pack(f"<{len(out)}h", *out))
    w.close()


def detect_pops(samples, min_amp=150, min_run=3):
    """Find regions with rapid sign alternation (ADPCM oscillation)."""
    n = len(samples)
    is_pop = np.zeros(n, dtype=bool)

    # Mark sign-alternating runs
    sign_change = np.zeros(n, dtype=bool)
    for i in range(1, n):
        if samples[i] * samples[i-1] < 0 and abs(samples[i]) > min_amp and abs(samples[i-1]) > min_amp:
            sign_change[i] = True

    # Find runs of consecutive sign changes (3+ = pop)
    run_start = -1
    for i in range(n):
        if sign_change[i]:
            if run_start < 0:
                run_start = i - 1
        else:
            if run_start >= 0:
                run_len = i - run_start
                if run_len >= min_run:
                    # Extend the pop region slightly
                    start = max(0, run_start - 2)
                    end = min(n, i + 2)
                    is_pop[start:end] = True
                run_start = -1

    return is_pop


def smooth_pops(samples, is_pop, window=5):
    """Replace pop regions with median-filtered values."""
    result = samples.copy()

    # Find contiguous pop regions
    regions = []
    in_region = False
    start = 0
    for i in range(len(is_pop)):
        if is_pop[i] and not in_region:
            start = i
            in_region = True
        elif not is_pop[i] and in_region:
            regions.append((start, i))
            in_region = False
    if in_region:
        regions.append((start, len(is_pop)))

    for start, end in regions:
        # Get context: clean samples before and after
        ctx_before = max(0, start - 8)
        ctx_after = min(len(samples), end + 8)

        # Use values at edges for interpolation
        val_before = np.mean(samples[ctx_before:start]) if start > ctx_before else 0
        val_after = np.mean(samples[end:ctx_after]) if ctx_after > end else 0

        # Linear interpolation across the pop
        length = end - start
        for i in range(length):
            alpha = i / max(length - 1, 1)
            result[start + i] = val_before * (1 - alpha) + val_after * alpha

    return result, len(regions)


def median_filter(samples, kernel=3):
    """Apply median filter — natural despeckle for impulse noise."""
    pad = kernel // 2
    padded = np.pad(samples, pad, mode='edge')
    result = np.zeros_like(samples)
    for i in range(len(samples)):
        result[i] = np.median(padded[i:i+kernel])
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python depop.py input.wav [output.wav]")
        sys.exit(1)

    in_path = sys.argv[1]
    base, ext = os.path.splitext(in_path)
    out_path = sys.argv[2] if len(sys.argv) >= 3 else f"{base}_depop{ext}"

    samples, rate = read_wav(in_path)
    n = len(samples)

    # Count pops before
    pops_before = detect_pops(samples)
    n_before = np.sum(pops_before)

    print(f"Input: {in_path} ({n/rate:.1f}s, rms={np.sqrt(np.mean(samples**2)):.0f})")
    print(f"  Pop samples detected: {n_before} ({100*n_before/n:.2f}%)")

    # Step 1: Interpolate detected pop regions
    print("  Step 1: Interpolate pop regions...")
    result, n_regions = smooth_pops(samples, pops_before)
    print(f"    Smoothed {n_regions} regions")

    # Step 2: Light median filter (kernel=3) to catch remaining single-sample spikes
    print("  Step 2: Median filter (k=3)...")
    result = median_filter(result, kernel=3)

    # Count pops after
    pops_after = detect_pops(result)
    n_after = np.sum(pops_after)
    print(f"  Pop samples after: {n_after} ({100*n_after/n:.2f}%)")
    print(f"  Reduction: {100*(1-n_after/(n_before+1)):.0f}%")

    write_wav(out_path, result, rate)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
