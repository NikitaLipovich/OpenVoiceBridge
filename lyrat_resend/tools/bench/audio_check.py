#!/usr/bin/env python3
"""Check a recording for control tones: presence, SNR and frequency shift. Standard library only.

  audio_check.py FILE.wav --tones 400,1000,1800 [--min-snr 20] [--max-shift 1.0] [--min-seconds 0.5]
                 [--criterion strict|blocks] [--max-impaired 30] [--exclude START:LEN,...]

The tones are expected one after another (a sequence), each lasting about a second or more. The
recording is cut into 256 ms Hann frames. A frame belongs to a tone when its strongest component lies
within 5 % of that tone, which is wide enough to report a wrong frequency as a shift rather than as
silence. A sample rate error of 2 lands outside every window and reads as a missing tone.

For each tone, over the frames inside its runs (the first and the last frame of a run overlap the
neighbouring tone and are left out; so are frames of low level at the very start and end of the signal in the file, where
the window holds the start or the end of the track itself, and which of them count would otherwise depend on the grid):
  snr_db     tone power / everything else above 50 Hz, summed over the frames, so that short dropouts
             lower it instead of disappearing in a median
  freq_hz    median of the peak frequency, parabolic interpolation on the log spectrum
  shift_pct  (freq_hz - tone) / tone * 100
  seconds    time covered by its frames
  block_snr_median_db, impaired_pct   the same recording cut into blocks of one mSBC frame (7.5 ms). Each block
             gets its own best-fit sine, so a block is judged on its shape alone. A block under --min-snr
             is impaired. A lost frame replaced by the receiver leaves a burst of impaired blocks in an
             otherwise clean tone, and this metric keeps the two facts apart

criterion strict (default): snr_db >= min_snr. A periodic loss of one frame in 15 sinks it even when the
sound between the losses is perfect.
criterion blocks: block_snr_median_db >= min_snr and impaired_pct <= max_impaired. It tolerates isolated
losses only, and it still fails silence, noise, a wrong rate and frequent gaps. Both are always reported.

--exclude names sample ranges known to be missing from the recording (UDP losses, see udp_record.py).
Frames that touch them are left out of every measure, and the tone must still cover --min-seconds.

Every tone must pass. Exit code: 0 PASS, 1 FAIL, 2 file unreadable. The JSON goes to stdout.
As a module: check(samples, rate, tones, ...) and read_wav(path).
"""
import argparse
import array
import cmath
import json
import math
import sys
import wave

WINDOW_S = 0.256
HOP_S = 0.128
ASSIGN_TOLERANCE = 0.05     # a frame's peak within 5 % of the tone belongs to that tone
TONE_HALF_BINS = 3          # the Hann main lobe plus a little
LOW_CUT_HZ = 50.0           # DC and hum are not what this tool judges
SILENCE_RMS = 3.0           # frames below this level belong to no tone
BLOCK_S = 0.0075            # one mSBC frame
EDGE_LEVEL = 0.9            # a frame at an edge of the signal under this part of the run's level holds the end of the track


def read_wav(path):
    """Mono 16 bit samples and the sample rate; several channels are reduced to the first."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16 bit PCM, got %d bytes per sample" % w.getsampwidth())
        channels = w.getnchannels()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    data = array.array("h")
    data.frombytes(raw[:len(raw) // 2 * 2])
    if sys.byteorder == "big":
        data.byteswap()
    return list(data[::channels]), rate


def fft(x):
    """Iterative radix-2 FFT of a complex list; the length must be a power of two."""
    n = len(x)
    out = list(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            out[i], out[j] = out[j], out[i]
    size = 2
    while size <= n:
        half = size // 2
        step = cmath.exp(-2j * math.pi / size)
        for start in range(0, n, size):
            w = 1 + 0j
            for k in range(start, start + half):
                t = out[k + half] * w
                out[k + half] = out[k] - t
                out[k] = out[k] + t
                w *= step
        size *= 2
    return out


def frame_power(frame, hann):
    spec = fft([s * w for s, w in zip(frame, hann)])
    return [abs(c) ** 2 for c in spec[:len(frame) // 2 + 1]]


def peak_of(power, bin_hz, low_bin):
    """Strongest bin at or above low_bin and its interpolated frequency."""
    k = max(range(low_bin, len(power) - 1), key=power.__getitem__)
    a, b, c = (math.log(max(power[i], 1e-9)) for i in (k - 1, k, k + 1))
    denom = a - 2 * b + c
    delta = 0.5 * (a - c) / denom if denom else 0.0
    return k, (k + delta) * bin_hz


def analyse_frames(samples, rate, exclude=()):
    size = 1
    while size < WINDOW_S * rate:
        size *= 2
    hop = size // 2
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / size) for i in range(size)]
    bin_hz = rate / size
    low_bin = max(1, int(math.ceil(LOW_CUT_HZ / bin_hz)))
    frames = []
    for start in range(0, len(samples) - size + 1, hop):
        if any(start < at + length and at < start + size for at, length in exclude):
            frames.append({"start": start, "rms": 0.0, "peak_hz": None, "excluded": True})
            continue
        chunk = samples[start:start + size]
        rms = math.sqrt(sum(s * s for s in chunk) / size)
        if rms < SILENCE_RMS:
            frames.append({"start": start, "rms": rms, "peak_hz": None})
            continue
        power = frame_power(chunk, hann)
        k, hz = peak_of(power, bin_hz, low_bin)
        lo, hi = max(low_bin, k - TONE_HALF_BINS), min(len(power), k + TONE_HALF_BINS + 1)
        band = sum(power[lo:hi])
        total = sum(power[low_bin:])
        frames.append({"start": start, "rms": rms, "peak_hz": hz, "band": band, "rest": max(total - band, 0.0)})
    return frames, hop / rate


def assign(frame, tones):
    hz = frame["peak_hz"]
    if hz is None:
        return None
    best = min(tones, key=lambda t: abs(hz - t))
    return best if abs(hz - best) <= best * ASSIGN_TOLERANCE else None


def block_metric(samples, rate, ranges, hz, min_snr):
    """Median block SNR and share of impaired blocks over the given (start, end) sample ranges."""
    size = int(round(BLOCK_S * rate))
    w = 2 * math.pi * hz / rate
    c = [math.cos(w * k) for k in range(size)]
    s = [math.sin(w * k) for k in range(size)]
    cc = sum(v * v for v in c)
    ss = sum(v * v for v in s)
    cs = sum(a * b for a, b in zip(c, s))
    det = cc * ss - cs * cs
    snrs = []
    for start, end in ranges:
        # The mean of a whole range: a block with a half-integer number of cycles has a mean of its own.
        mean = sum(samples[start:end]) / max(end - start, 1)
        for i in range(start, end - size + 1, size):
            block = samples[i:i + size]
            xc = sum((v - mean) * a for v, a in zip(block, c))
            xs = sum((v - mean) * b for v, b in zip(block, s))
            a = (xc * ss - xs * cs) / det
            b = (xs * cc - xc * cs) / det
            signal = residual = 0.0
            for v, ci, si in zip(block, c, s):
                fit = a * ci + b * si
                signal += fit * fit
                residual += (v - mean - fit) ** 2
            if signal <= 0:
                snrs.append(-99.0)
            else:
                snrs.append(10 * math.log10(signal / residual) if residual > 0 else 99.0)
    if not snrs:
        return None, None
    snrs.sort()
    bad = sum(1 for v in snrs if v < min_snr)
    return round(snrs[len(snrs) // 2], 1), round(100.0 * bad / len(snrs), 1)


def merge_ranges(frames, frame_size):
    ranges = []
    for f in sorted(frames, key=lambda f: f["start"]):
        if ranges and f["start"] <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], f["start"] + frame_size)
        else:
            ranges.append([f["start"], f["start"] + frame_size])
    return ranges


def check(samples, rate, tones, min_snr=20.0, max_shift=1.0, min_seconds=0.5, criterion="strict", max_impaired=30.0,
          exclude=()):
    result = {"rate": rate, "samples": len(samples), "seconds": round(len(samples) / rate, 3) if rate else 0,
              "tones": {}, "pass": False, "error": None}
    if not samples or not rate:
        result["error"] = "empty recording"
        return result
    frames, hop_s = analyse_frames(samples, rate, exclude)
    frame_size = int(round(hop_s * rate)) * 2
    result["criterion"] = criterion
    if not frames:
        result["error"] = "recording shorter than one analysis window"
        return result
    labels = [assign(f, tones) for f in frames]
    result["peak_abs"] = max(abs(s) for s in samples)
    result["excluded_frames"] = sum(1 for f in frames if f.get("excluded"))
    result["silent_frames"] = sum(1 for f in frames if f["peak_hz"] is None and not f.get("excluded"))
    result["frames"] = len(frames)
    result["unassigned_frames"] = sum(1 for f, label in zip(frames, labels) if label is None and f["peak_hz"] is not None)

    # Interior frames of each run; a run shorter than three frames has none.
    interior = {t: [] for t in tones}
    assigned = [k for k, label in enumerate(labels) if label is not None]
    first_signal, last_signal = (assigned[0], assigned[-1]) if assigned else (-1, -1)
    i = 0
    while i < len(frames):
        j = i
        while j + 1 < len(frames) and labels[j + 1] == labels[i]:
            j += 1
        if labels[i] is not None:
            run = frames[i + 1:j]
            if run:
                level = sorted(f["rms"] for f in run)[len(run) // 2] * EDGE_LEVEL
                if i == first_signal:
                    while run and run[0]["rms"] < level:
                        run = run[1:]
                if j == last_signal:
                    while run and run[-1]["rms"] < level:
                        run = run[:-1]
            interior[labels[i]].extend(run)
        i = j + 1

    all_ok = True
    for tone in tones:
        chosen = interior[tone]
        entry = {"seconds": round(len(chosen) * hop_s, 3), "frames": len(chosen)}
        if chosen:
            band = sum(f["band"] for f in chosen)
            rest = sum(f["rest"] for f in chosen)
            hz = sorted(f["peak_hz"] for f in chosen)[len(chosen) // 2]
            entry.update(freq_hz=round(hz, 2), shift_pct=round((hz - tone) / tone * 100, 3),
                         snr_db=round(10 * math.log10(band / rest), 1) if rest > 0 else 99.0,
                         rms=round(math.sqrt(sum(f["rms"] ** 2 for f in chosen) / len(chosen)), 1))
            entry["block_snr_median_db"], entry["impaired_pct"] = block_metric(
                samples, rate, merge_ranges(chosen, frame_size), hz, min_snr)
            if criterion == "blocks":
                level_ok = (entry["block_snr_median_db"] is not None and entry["block_snr_median_db"] >= min_snr
                            and entry["impaired_pct"] <= max_impaired)
            else:
                level_ok = entry["snr_db"] >= min_snr
            entry["pass"] = bool(entry["seconds"] >= min_seconds and level_ok and abs(entry["shift_pct"]) <= max_shift)
        else:
            entry["pass"] = False
        all_ok = all_ok and entry["pass"]
        result["tones"][str(tone)] = entry
    result["pass"] = all_ok
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav")
    ap.add_argument("--tones", default="400,1000,1800", help="comma separated frequencies in Hz")
    ap.add_argument("--min-snr", type=float, default=20.0)
    ap.add_argument("--max-shift", type=float, default=1.0, help="percent")
    ap.add_argument("--min-seconds", type=float, default=0.5, help="time each tone must cover")
    ap.add_argument("--exclude", default="", help="START:LEN sample ranges to leave out, comma separated")
    ap.add_argument("--criterion", choices=["strict", "blocks"], default="strict")
    ap.add_argument("--max-impaired", type=float, default=30.0, help="percent of blocks, criterion blocks")
    args = ap.parse_args()
    tones = [int(t) for t in args.tones.split(",")]
    try:
        samples, rate = read_wav(args.wav)
    except (OSError, ValueError, EOFError, wave.Error) as e:
        print(json.dumps({"pass": False, "error": str(e)}))
        return 2
    exclude = [tuple(int(v) for v in item.split(":")) for item in args.exclude.split(",") if item]
    result = check(samples, rate, tones, args.min_snr, args.max_shift, args.min_seconds, args.criterion,
                   args.max_impaired, exclude)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
