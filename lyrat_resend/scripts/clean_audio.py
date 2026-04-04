#!/usr/bin/env python3
"""
Audio cleaner for LyraT MEMS mic recordings using spectral gating (noisereduce).
Usage: python clean_audio.py input.wav [output.wav]

Produces 3 versions:
  _clean.wav       — full pipeline (bandpass + spectral denoise + normalize)
  _clean_light.wav — lighter denoise (less aggressive, preserves more detail)
  _loud.wav        — just amplified, no filtering (reference)
"""

import sys
import os
import numpy as np
import noisereduce as nr
from scipy.io import wavfile
from scipy.signal import butter, sosfilt, iirnotch, tf2sos


def read_wav(path):
    rate, data = wavfile.read(path)
    return data.astype(np.float64) / 32768.0, rate


def write_wav(path, data, rate):
    out = np.clip(data * 32768, -32768, 32767).astype(np.int16)
    wavfile.write(path, rate, out)


def bandpass(data, rate, lo=120, hi=3800):
    """4th order Butterworth bandpass."""
    sos_hp = butter(4, lo, btype='high', fs=rate, output='sos')
    sos_lp = butter(4, hi, btype='low', fs=rate, output='sos')
    data = sosfilt(sos_hp, data)
    data = sosfilt(sos_lp, data)
    return data


def remove_hum(data, rate, freqs=[50, 100, 150], q=15):
    """Notch filters for mains hum harmonics."""
    for f in freqs:
        b, a = iirnotch(f, q, fs=rate)
        sos = tf2sos(b, a)
        data = sosfilt(sos, data)
    return data


def normalize(data, target_db=-1):
    peak = np.max(np.abs(data))
    if peak < 1e-6:
        return data
    target = 10 ** (target_db / 20)
    return data * (target / peak)


def main():
    if len(sys.argv) < 2:
        print("Usage: python clean_audio.py input.wav [output.wav]")
        sys.exit(1)

    in_path = sys.argv[1]
    base, ext = os.path.splitext(in_path)

    data, rate = read_wav(in_path)
    dur = len(data) / rate
    rms0 = np.sqrt(np.mean(data ** 2))
    peak0 = np.max(np.abs(data))
    print(f"Input: {in_path} ({dur:.1f}s, {rate}Hz, rms={rms0:.4f}, peak={peak0:.4f})")

    # === Version 1: Full clean ===
    print("\n--- Full clean ---")
    d = data - np.mean(data)  # DC removal

    print("  Bandpass 120-3800 Hz...")
    d = bandpass(d, rate, 120, 3800)

    print("  Notch 50/100/150 Hz...")
    d = remove_hum(d, rate)

    print("  Spectral denoise (stationary, prop=0.90)...")
    d = nr.reduce_noise(
        y=d, sr=rate,
        stationary=True,
        prop_decrease=0.90,
        n_fft=512,
        n_std_thresh_stationary=1.5,
    )

    print("  Normalize to -1 dBFS...")
    d = normalize(d, -1)

    out_path = sys.argv[2] if len(sys.argv) >= 3 else f"{base}_clean{ext}"
    write_wav(out_path, d, rate)
    rms1 = np.sqrt(np.mean(d ** 2))
    print(f"  Saved: {out_path} (rms={rms1:.4f})")

    # === Version 2: Light clean ===
    print("\n--- Light clean ---")
    d2 = data - np.mean(data)
    d2 = bandpass(d2, rate, 120, 3800)
    d2 = remove_hum(d2, rate)

    print("  Spectral denoise (non-stationary, prop=0.70)...")
    d2 = nr.reduce_noise(
        y=d2, sr=rate,
        stationary=False,
        prop_decrease=0.70,
        n_fft=512,
        time_mask_smooth_ms=100,
        freq_mask_smooth_hz=200,
    )
    d2 = normalize(d2, -1)

    light_path = f"{base}_clean_light{ext}"
    write_wav(light_path, d2, rate)
    print(f"  Saved: {light_path}")

    # === Version 3: Just loud ===
    print("\n--- Loud (no filtering) ---")
    d3 = data - np.mean(data)
    d3 = normalize(d3, -1)
    loud_path = f"{base}_loud{ext}"
    write_wav(loud_path, d3, rate)
    print(f"  Saved: {loud_path}")

    print("\nDone!")


if __name__ == "__main__":
    main()
