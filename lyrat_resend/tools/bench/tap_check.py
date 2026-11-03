#!/usr/bin/env python3
"""Judge what the S3-ETH board captured from the I2S bus, without any sound played for the test. Standard library only.

  tap_check.py match --tap mic.wav --lyrat lyrat_mic.wav --line mic|call [--gaps-json tap.json]
  tap_check.py live FILE.wav [--slot 0]

match: the LyraT reads the same line and sends it to UDP as mu-law, so its stream is a reference for the capture,
sample by sample. For the mic line this holds with its DSP off: the reference is then mu-law of the fixed 4 kHz low
pass of (L + R) >> 1, which predict_mic() computes from the capture. For the call line the reference is mu-law of the
samples themselves. Any slip of a bit or a slot, or a lost block, breaks the equality, whatever the signal is.
The reference is not complete: over Wi-Fi it loses frames, and around such a hiccup the time between two of its packets
is not what its sequence says (goal 042, incident 002). So the verdict is by blocks of 120 samples: each one must be
on the capture exactly and in order; what the capture holds beyond the reference is reported as bus_extra, not judged.
Reported: blocks, blocks_found_pct, bus_extra, matched_pct (one offset for the whole recording, for information),
shifted_pct, the same comparison one second away, and other_line_pct, blocks of the reference searched in the other
line. Both must be low: they prove this comparison can fail. It says nothing about what the mic hears.

live: is anything other than steady noise on the line. Room sound is an uncontrolled source, so this is a liveness
check only and never gives an SNR or a frequency. Modulation (p90/p10 of the short time level) and flux (change of a
coarse spectrum between neighbouring frames) are compared with a phase randomised copy of the same recording: same
long term spectrum, no change over time. A second copy against the first is the control and must not pass.

As a module: read_channels, ulaw_roundtrip, predict_mic, match, liveness.
"""
import argparse
import cmath
import json
import math
import random
import sys
import wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audio_check import fft  # noqa: E402

RATE = 16000
BIAS = 0x84
CLIP = 32635
LPF_ALPHA_Q15 = 20316       # lyrat_resend/main/main.c, mic_rx_task
MATCH_WINDOW = 480
MATCH_MIN_LEVELS = 8        # fewer distinct codes in the reference: the signal cannot prove anything
MATCH_MIN_PCT = 99.0        # the limits chosen before any trial of round.py tap
MATCH_MAX_SHIFTED_PCT = 50.0
MATCH_MAX_OTHER_PCT = 1.0
MATCH_BLOCK = 120
MATCH_SEARCH = 8000         # how far past the last block the next one may lie
MATCH_OTHER_BLOCKS = 50
SHIFT = RATE                # the comparison one second away
LIVE_WINDOW = int(0.25 * RATE)
LIVE_FRAME = 1024
LIVE_BANDS = (100, 200, 400, 800, 1600, 3200, 6400, 8000)
LIVE_MIN_RATIO = 1.5        # chosen before the first measurement
LIVE_FFT = 1 << 16


def read_channels(path):
    """(list of channels, each a list of ints)."""
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = array("h")
    data.frombytes(raw[:len(raw) // 2 * 2])
    if sys.byteorder == "big":
        data.byteswap()
    return [list(data[i::channels]) for i in range(channels)]


def _encode(v):
    """pcm_to_ulaw of lyrat_resend/main/ulaw.c: sign, exponent and mantissa of the same sample."""
    negative = v < 0
    if negative:
        v = -v
    v = min(v, CLIP) + BIAS
    exponent = 7
    for e in range(8):
        if v < (0x100 << e):
            exponent = e
            break
    return negative, exponent, (v >> (exponent + 3)) & 0x0F


def ulaw_roundtrip(v):
    """The value this sample reads as after mu-law: udp_record writes these to the WAV of the LyraT stream."""
    negative, exponent, mantissa = _encode(v)
    sample = ((mantissa << 3) + BIAS << exponent) - BIAS
    return -sample if negative else sample


def predict_mic(left, right):
    """mic_rx_task of the LyraT with the DSP off: L + R averaged, then its fixed low pass."""
    out = []
    prev = 0
    for a, b in zip(left, right):
        x = (a + b) >> 1
        prev = (LPF_ALPHA_Q15 * x + (32768 - LPF_ALPHA_Q15) * prev) >> 15
        out.append(prev)
    return out


def _find_offset(pred, ref, starts):
    """Offset d with ref[i] == pred[i + d]; (d, matched in the window).

    A window of a tone also fits one period away, so every offset that fits the window is a candidate and the
    whole recording decides between them.
    """
    candidates = {}
    for start in starts:
        window = ref[start:start + MATCH_WINDOW]
        if len(window) < MATCH_WINDOW or len(set(window)) < MATCH_MIN_LEVELS:
            continue
        for at in range(0, len(pred) - MATCH_WINDOW):
            if pred[at] != window[0]:
                continue
            hits = sum(1 for k in range(MATCH_WINDOW) if pred[at + k] == window[k])
            if hits >= MATCH_WINDOW * 0.9:
                candidates[at - start] = max(candidates.get(at - start, 0), hits)
        if candidates:
            break
    if not candidates:
        return None, 0

    def score(d):
        points = range(max(0, -d), min(len(ref), len(pred) - d), 7)
        return sum(1 for i in points if pred[i + d] == ref[i])

    offset = max(candidates, key=score)
    return offset, candidates[offset]


def _blocks_in_order(coded, ref, ref_mask, offset):
    """(blocks with a signal, found exactly and in order, capture samples beyond the reference between them)."""
    total = found = extra = 0
    pos = None
    for start in range(0, len(ref) - MATCH_BLOCK, MATCH_BLOCK):
        block = ref[start:start + MATCH_BLOCK]
        if any(ref_mask[start:start + MATCH_BLOCK]) or len(set(block)) < MATCH_MIN_LEVELS:
            continue
        total += 1
        begin = max(0, start + offset - MATCH_SEARCH) if pos is None else pos
        end = min(len(coded) - MATCH_BLOCK, begin + 2 * MATCH_SEARCH)
        at = next((i for i in range(begin, end) if coded[i] == block[0] and coded[i:i + MATCH_BLOCK] == block), None)
        if at is None:
            continue
        found += 1
        if pos is not None:
            extra += at - pos
        pos = at + MATCH_BLOCK
    return total, found, extra


def _other_line_pct(other, ref, ref_mask):
    """Share of the reference's blocks that also lie in another line: a comparison that can fail reads near zero."""
    coded = [ulaw_roundtrip(v) for v in other]
    starts = [s for s in range(0, len(ref) - MATCH_BLOCK, MATCH_BLOCK) if not any(ref_mask[s:s + MATCH_BLOCK])
              and len(set(ref[s:s + MATCH_BLOCK])) >= MATCH_MIN_LEVELS]
    picked = starts[::max(len(starts) // MATCH_OTHER_BLOCKS, 1)][:MATCH_OTHER_BLOCKS]
    hits = 0
    for s in picked:
        block = ref[s:s + MATCH_BLOCK]
        hits += any(coded[i] == block[0] and coded[i:i + MATCH_BLOCK] == block for i in range(len(coded) - MATCH_BLOCK))
    return round(100.0 * hits / len(picked), 3) if picked else None


def match(pred, ref, ref_gaps=(), pred_gaps=(), other=None):
    """Equality of the capture's prediction (PCM) and the LyraT's stream (decoded mu-law); other is the other line."""
    coded = [ulaw_roundtrip(v) for v in pred]
    starts = list(range(RATE // 2, max(len(ref) - MATCH_WINDOW, 0), RATE // 2))
    offset, hits = _find_offset(coded, ref, starts)
    result = {"offset": offset, "window_hits": hits, "window": MATCH_WINDOW, "levels": len(set(ref)), "compared": 0,
              "matched_pct": None, "shifted_pct": None, "ref_samples": len(ref), "pass": False}
    if offset is None:
        result["error"] = "no window of the reference lines up with the capture"
        return result

    ref_mask = bytearray(len(ref))
    for at, length in ref_gaps:
        ref_mask[at:at + length] = b"\x01" * min(length, max(len(ref) - at, 0))
    pred_mask = bytearray(len(coded))
    for at, length in pred_gaps:
        # The low pass needs a few samples to settle after a gap in the capture.
        pred_mask[at:at + length + 32] = b"\x01" * min(length + 32, max(len(coded) - at, 0))
    compared = hits = shifted_compared = shifted_hits = 0
    for i, value in enumerate(ref):
        if ref_mask[i]:
            continue
        at = i + offset
        if 32 <= at < len(coded) and not pred_mask[at]:
            compared += 1
            hits += coded[at] == value
        at += SHIFT
        if 32 <= at < len(coded) and not pred_mask[at]:
            shifted_compared += 1
            shifted_hits += coded[at] == value
    result["compared"] = compared
    result["matched_pct"] = round(100.0 * hits / compared, 3) if compared else None
    result["shifted_pct"] = round(100.0 * shifted_hits / shifted_compared, 3) if shifted_compared else None
    blocks, found, extra = _blocks_in_order(coded, ref, ref_mask, offset)
    result.update(blocks=blocks, blocks_found=found, bus_extra=extra,
                  blocks_found_pct=round(100.0 * found / blocks, 3) if blocks else None)
    result["other_line_pct"] = _other_line_pct(other, ref, ref_mask) if other is not None else None
    result["pass"] = bool(blocks * MATCH_BLOCK >= len(ref) // 2 and result["levels"] >= MATCH_MIN_LEVELS
                          and result["blocks_found_pct"] is not None and result["blocks_found_pct"] >= MATCH_MIN_PCT
                          and result["shifted_pct"] is not None and result["shifted_pct"] <= MATCH_MAX_SHIFTED_PCT
                          and (result["other_line_pct"] is None or result["other_line_pct"] <= MATCH_MAX_OTHER_PCT))
    return result


def _spectral_frames(x):
    """Log energy in coarse bands for consecutive Hann frames."""
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / LIVE_FRAME) for i in range(LIVE_FRAME)]
    bin_hz = RATE / LIVE_FRAME
    frames = []
    for start in range(0, len(x) - LIVE_FRAME + 1, LIVE_FRAME):
        spec = fft([complex(v * w) for v, w in zip(x[start:start + LIVE_FRAME], hann)])
        power = [abs(c) ** 2 for c in spec[:LIVE_FRAME // 2 + 1]]
        frames.append([math.log10(1.0 + sum(power[int(lo / bin_hz):int(hi / bin_hz)]))
                       for lo, hi in zip(LIVE_BANDS, LIVE_BANDS[1:])])
    return frames


def _measures(x):
    levels = []
    for start in range(0, len(x) - LIVE_WINDOW + 1, LIVE_WINDOW):
        chunk = x[start:start + LIVE_WINDOW]
        levels.append(math.sqrt(sum(v * v for v in chunk) / len(chunk)))
    levels.sort()
    p50 = levels[len(levels) // 2]
    p10 = levels[len(levels) // 10]
    p90 = levels[len(levels) * 9 // 10]
    frames = _spectral_frames(x)
    flux = [sum(abs(a - b) for a, b in zip(f, g)) / len(f) for f, g in zip(frames, frames[1:])]
    return {"modulation": round(p90 / p10, 3) if p10 > 0 else None, "flux": round(sum(flux) / len(flux), 4) if flux else None,
            "level_p10": round(p10, 1), "level_p50": round(p50, 1), "level_p90": round(p90, 1)}


def _surrogate(x, seed):
    """Same magnitudes, random phases: the long term spectrum of x without any change over time."""
    spec = fft([complex(v) for v in x])
    n = len(x)
    rng = random.Random(seed)
    for k in range(1, n // 2):
        phase = cmath.exp(2j * math.pi * rng.random())
        spec[k] = abs(spec[k]) * phase
        spec[n - k] = spec[k].conjugate()
    return [c.conjugate().real / n for c in fft([c.conjugate() for c in spec])]


def _ratio(a, b):
    return round(a / b, 3) if a is not None and b else None


def liveness(samples):
    """Measures of one excerpt against its stationary copies. Never an SNR: the source is not known."""
    if len(samples) < LIVE_FFT:
        return {"pass": False, "error": "shorter than %d samples" % LIVE_FFT}
    x = samples[:LIVE_FFT]
    mean = sum(x) / len(x)
    x = [v - mean for v in x]
    rms = math.sqrt(sum(v * v for v in x) / len(x))
    real = _measures(x)
    first = _measures(_surrogate(x, 1))
    second = _measures(_surrogate(x, 2))
    result = {"rms": round(rms, 1), "dc": round(mean, 1), "peak": max(abs(v) for v in x), **real,
              "stationary_modulation": first["modulation"], "stationary_flux": first["flux"],
              "modulation_ratio": _ratio(real["modulation"], first["modulation"]),
              "flux_ratio": _ratio(real["flux"], first["flux"]),
              "control_modulation_ratio": _ratio(second["modulation"], first["modulation"]),
              "control_flux_ratio": _ratio(second["flux"], first["flux"])}
    passes = lambda m, f: bool(m and f and m >= LIVE_MIN_RATIO and f >= LIVE_MIN_RATIO)  # noqa: E731
    result["control_pass"] = passes(result["control_modulation_ratio"], result["control_flux_ratio"])
    result["pass"] = passes(result["modulation_ratio"], result["flux_ratio"]) and not result["control_pass"]
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("match")
    p.add_argument("--tap", required=True)
    p.add_argument("--lyrat", required=True)
    p.add_argument("--line", required=True, choices=["mic", "call"])
    p.add_argument("--gaps-json", help="tap.json of the same recording")
    p = sub.add_parser("live")
    p.add_argument("wav")
    p.add_argument("--slot", type=int, default=0)
    args = ap.parse_args()
    if args.action == "live":
        result = liveness(read_channels(args.wav)[args.slot])
        print(json.dumps(result, indent=2))
        return 0 if result["pass"] else 1
    channels = read_channels(args.tap)
    pred = predict_mic(channels[0], channels[-1]) if args.line == "mic" else channels[0]
    ref_gaps, pred_gaps = (), ()
    if args.gaps_json:
        with open(args.gaps_json, encoding="utf-8") as fh:
            summary = json.load(fh)
        pred_gaps = summary[args.line]["gaps"]
        ref_gaps = (summary.get("lyrat_" + args.line) or {}).get("gaps", ())
    result = match(pred, read_channels(args.lyrat)[0], ref_gaps, pred_gaps)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
