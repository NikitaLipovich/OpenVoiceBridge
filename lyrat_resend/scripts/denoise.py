#!/usr/bin/env python3
"""
Denoise LyraT mic recording. Two-pass spectral gating for maximum noise removal.
Usage: python denoise.py input.wav [output.wav]
"""

import sys
import os
import numpy as np
import noisereduce as nr
from scipy.io import wavfile
from scipy.signal import butter, sosfilt, iirnotch, tf2sos


def main():
    if len(sys.argv) < 2:
        print("Usage: python denoise.py input.wav [output.wav]")
        sys.exit(1)

    in_path = sys.argv[1]
    base, ext = os.path.splitext(in_path)
    out_path = sys.argv[2] if len(sys.argv) >= 3 else f"{base}_denoised{ext}"

    rate, data = wavfile.read(in_path)
    d = data.astype(np.float64) / 32768.0
    print(f"Input: {in_path} ({len(d)/rate:.1f}s, {rate}Hz)")

    # DC removal
    d -= np.mean(d)

    # Bandpass 120-3800 Hz (4th order Butterworth)
    d = sosfilt(butter(4, 120, btype='high', fs=rate, output='sos'), d)
    d = sosfilt(butter(4, 3800, btype='low', fs=rate, output='sos'), d)

    # Notch 50/100/150 Hz
    for f in [50, 100, 150]:
        b, a = iirnotch(f, 15, fs=rate)
        d = sosfilt(tf2sos(b, a), d)

    # Spectral gating (single pass, aggressive)
    d = nr.reduce_noise(y=d, sr=rate, stationary=True,
                        prop_decrease=0.98, n_fft=512, n_std_thresh_stationary=1.0)

    # Normalize to -1 dBFS
    peak = np.max(np.abs(d))
    if peak > 0:
        d = d * (0.89 / peak)

    wavfile.write(out_path, rate, np.clip(d * 32768, -32768, 32767).astype(np.int16))
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
