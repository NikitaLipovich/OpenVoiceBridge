#!/usr/bin/env python3
"""A voice-like test signal and the comparison of a recording with it. Standard library only.

  voice_check.py make FILE.wav [--seconds 12] [--level 8000]
  voice_check.py check REF.wav REC.wav

A pure tone flatters a concealment: a whole number of its periods fits the search window, and a repeat matches the lost
frame exactly. This signal cannot be repeated that way: a pitch that glides between 100 and 210 Hz, harmonics up to
3.4 kHz falling with frequency, syllables at about 4 per second with short pauses.

check finds the delay of the recording against the reference, follows it in windows of 250 ms (the clocks of the phone
and of the bus differ), scales the reference to the recording and cuts the residual into blocks of one frame, 7.5 ms.
Only blocks where the reference is active count. Output (JSON on stdout):
  snr_db             reference power / residual power over all active blocks; lost frames and codec noise both lower it
  block_snr_median_db, block_snr_p02_db   the blocks one by one; the low percentile is where frame losses show
  blocks_under_0db_pct   blocks where the residual is louder than the signal: what is left of a loss is audible there
  zero_runs          stretches of digital silence of at least one concealment frame where the signal is sustained
  aligned_seconds, delay_s, drift_ppm
Exit code: 0 measured, 2 not measurable. As a module: make(), compare(ref, rec, rate).
"""
import argparse
import array
import json
import math
import sys
import wave
from operator import mul
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_check  # noqa: E402

RATE = 16000
WINDOW_S = 0.25
SEARCH_S = 0.001            # the delay moves little from one window to the next
BLOCK_S = 0.0075
ACTIVE_FRACTION = 0.05      # a block counts when its reference rms is above this part of the overall rms
ZERO_MAX = 2                # what counts as digital silence on the bus
ZERO_RUN_S = 0.0045         # a lost CVSD frame gives 75 to 97 zero samples, the half-band filter eats the rest
SUSTAINED_FRACTION = 0.25   # zero runs count only where the signal has been going on: a loss on the very start of a
                            # syllable has nothing to be concealed from, and silence is what a concealment leaves there
DECIMATE = 8


def make(seconds=12.0, level=8000):
    """The samples of the test signal at RATE, peak at level."""
    n = int(seconds * RATE)
    out = [0.0] * n
    phase = 0.0
    for i in range(n):
        t = i / RATE
        f0 = 155 + 55 * math.sin(2 * math.pi * 0.7 * t) * math.cos(2 * math.pi * 0.23 * t)
        phase += 2 * math.pi * f0 / RATE
        env = max(0.0, math.sin(2 * math.pi * 2.1 * t)) ** 0.5 * (0.7 + 0.3 * math.sin(2 * math.pi * 0.37 * t))
        v = 0.0
        if env > 0:
            for k in range(1, int(3400 / f0) + 1):
                v += math.sin(k * phase) / k
        out[i] = env * v
    peak = max(abs(v) for v in out) or 1.0
    return [int(level * v / peak) for v in out]


def write_wav(path, samples, rate=RATE):
    data = array.array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def decimate(x, m):
    return [sum(x[i:i + m]) / m for i in range(0, len(x) - m + 1, m)]


def coarse_delay(ref, rec):
    """Where the start of ref lies in rec, in samples of the full rate; rough, by correlation at a low rate."""
    a, b = decimate(ref, DECIMATE), decimate(rec, DECIMATE)
    size = 1
    while size < len(a) + len(b):
        size *= 2
    fa = audio_check.fft([complex(v) for v in a] + [0j] * (size - len(a)))
    fb = audio_check.fft([complex(v) for v in b] + [0j] * (size - len(b)))
    # Inverse transform through the forward one: conjugate on the way in, the real part stays.
    cc = audio_check.fft([(x.conjugate() * y).conjugate() for x, y in zip(fa, fb)])
    best = max(range(size), key=lambda k: cc[k].real)
    lag = best if best < size // 2 else best - size
    return lag * DECIMATE


def interp(x, pos):
    """Catmull-Rom interpolation at a fractional index."""
    i = int(math.floor(pos))
    if i < 1 or i + 2 >= len(x):
        return 0.0
    t = pos - i
    a, b, c, d = x[i - 1], x[i], x[i + 1], x[i + 2]
    return b + 0.5 * t * (c - a + t * (2 * a - 5 * b + 4 * c - d + t * (3 * (b - c) + d - a)))


def compare(ref, rec, rate=RATE):
    window = int(WINDOW_S * rate)
    search = int(SEARCH_S * rate)
    block = int(round(BLOCK_S * rate))
    delay = coarse_delay(ref, rec)
    start_delay = delay
    residuals = []
    powers = []
    active_ref = []
    zero_runs = 0
    run = 0
    used_windows = 0
    overall = math.sqrt(sum(v * v for v in ref) / max(len(ref), 1)) or 1.0
    threshold = (ACTIVE_FRACTION * overall) ** 2 * block
    sustained = (SUSTAINED_FRACTION * overall) ** 2 * block
    was_sustained = False
    last_delay = float(delay)
    for at in range(0, len(ref) - window, window):
        rec_at = at + delay
        if rec_at - search < 2 or rec_at + window + search + 2 >= len(rec):
            continue
        segment = ref[at:at + window]
        energy = sum(map(mul, segment, segment))
        if energy < threshold:
            continue
        # The window's own delay: strongest correlation, then a parabola for the fraction.
        corr = []
        for d in range(-search, search + 1):
            corr.append(sum(map(mul, segment, rec[rec_at + d:rec_at + d + window])))
        k = max(range(len(corr)), key=corr.__getitem__)
        frac = 0.0
        if 0 < k < len(corr) - 1:
            den = corr[k - 1] - 2 * corr[k] + corr[k + 1]
            frac = 0.5 * (corr[k - 1] - corr[k + 1]) / den if den else 0.0
        shift = k - search + frac
        delay += k - search     # follows the drift, the next window starts from here
        last_delay = delay - (k - search) + shift
        # Reference on the time grid of the recording.
        aligned = [interp(ref, j - shift) for j in range(at, at + window)]
        chunk = rec[rec_at + 0:rec_at + window]
        den = sum(map(mul, aligned, aligned))
        gain = sum(map(mul, aligned, chunk)) / den if den else 0.0
        if gain <= 0:
            continue    # the very end of a track: a whisper of the reference against nothing recorded
        used_windows += 1
        for j in range(0, window - block + 1, block):
            a = [gain * v for v in aligned[j:j + block]]
            b = chunk[j:j + block]
            p = sum(map(mul, a, a))
            strong = p >= sustained * gain * gain
            if not (strong and was_sustained):
                # A pause or a start of the signal says nothing about a lost frame.
                if run >= ZERO_RUN_S * rate:
                    zero_runs += 1
                run = 0
            else:
                for v in b:
                    if -ZERO_MAX <= v <= ZERO_MAX:
                        run += 1
                    else:
                        if run >= ZERO_RUN_S * rate:
                            zero_runs += 1
                        run = 0
            was_sustained = strong
            if p < threshold * gain * gain:
                continue
            r = sum((x - y) ** 2 for x, y in zip(a, b))
            powers.append(p)
            residuals.append(r)
    if not powers:
        return {"error": "no active blocks aligned", "delay_s": round(start_delay / rate, 3)}
    snrs = sorted(10 * math.log10(p / r) if r > 0 else 99.0 for p, r in zip(powers, residuals))
    total = sum(residuals)
    return {
        "snr_db": round(10 * math.log10(sum(powers) / total), 2) if total > 0 else 99.0,
        "block_snr_median_db": round(snrs[len(snrs) // 2], 1),
        "block_snr_p02_db": round(snrs[len(snrs) // 50], 1),
        "blocks_under_0db_pct": round(100.0 * sum(1 for v in snrs if v < 0) / len(snrs), 3),
        "blocks": len(snrs),
        "zero_runs": zero_runs,
        "aligned_seconds": round(used_windows * WINDOW_S, 2),
        "delay_s": round(start_delay / rate, 4),
        "drift_ppm": round((last_delay - start_delay) / max(used_windows, 1) / window * 1e6, 1),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    m = sub.add_parser("make")
    m.add_argument("wav")
    m.add_argument("--seconds", type=float, default=12.0)
    m.add_argument("--level", type=int, default=8000)
    c = sub.add_parser("check")
    c.add_argument("ref")
    c.add_argument("rec")
    args = ap.parse_args()
    if args.action == "make":
        write_wav(args.wav, make(args.seconds, args.level))
        return 0
    ref, rate = audio_check.read_wav(args.ref)
    rec, rate2 = audio_check.read_wav(args.rec)
    if rate != rate2:
        print(json.dumps({"error": "rates differ: %d %d" % (rate, rate2)}))
        return 2
    result = compare(ref, rec, rate)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
