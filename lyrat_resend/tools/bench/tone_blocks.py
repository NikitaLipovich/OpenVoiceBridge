#!/usr/bin/env python3
"""Find each failure of a long tone recording with its time. Standard library only.

  tone_blocks.py FILE.wav --tones 1000 [--min-snr 20] [--exclude START:LEN,...] [--events]

audio_check.py judges short recordings as a whole; this tool takes hours of audio and says when. The recording is cut
into blocks of one mSBC frame (7.5 ms). Each block gets its own best-fit sine at the strongest of the tones, so a tone
sequence works too, and a block under --min-snr is impaired. Impaired blocks closer than MERGE_BLOCKS form one event.

Output (JSON on stdout): blocks, impaired, impaired_pct, events, events_per_min, seconds_measured, snr_per_second
{worst_db, under_min, p01_db, median_db}, event_list [{t, blocks, min_db}], intervals_top [[seconds, count]].
t counts from the start of the file. Stretches without signal at the two ends are left out, and a block at a change of
tone in a sequence is not a failure. Exit code: 0 measured, 2 no signal found.
As a module: analyse(samples, rate, tones, min_snr, exclude).
"""
import argparse
import json
import math
import sys
from collections import Counter
from operator import mul
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_check  # noqa: E402

BLOCK_S = 0.0075
SILENT_RMS = 30.0        # a level far below any control tone
MERGE_BLOCKS = 2
EDGE_S = 1.0             # the ends of a signal stretch, where the tone may start or stop inside a block
INTERVAL_BIN_S = 0.01


class _Fit:
    def __init__(self, tone, rate, size):
        w = 2 * math.pi * tone / rate
        self.c = [math.cos(w * k) for k in range(size)]
        self.s = [math.sin(w * k) for k in range(size)]
        self.cc = sum(map(mul, self.c, self.c))
        self.ss = sum(map(mul, self.s, self.s))
        self.cs = sum(map(mul, self.c, self.s))
        self.det = self.cc * self.ss - self.cs * self.cs

    def power(self, block):
        """Energy the least squares sine takes from the block."""
        xc = sum(map(mul, block, self.c))
        xs = sum(map(mul, block, self.s))
        a = (xc * self.ss - xs * self.cs) / self.det
        b = (xs * self.cc - xc * self.cs) / self.det
        return a * xc + b * xs


def analyse(samples, rate, tones, min_snr=20.0, exclude=()):
    size = int(round(BLOCK_S * rate))
    fits = [_Fit(t, rate, size) for t in tones]
    count = len(samples) // size
    skip = bytearray(count)
    for at, length in exclude:
        for i in range(at // size, min((at + length) // size + 1, count)):
            skip[i] = 1
    mean = sum(samples) / len(samples) if samples else 0.0
    best = [-1] * count       # index of the strongest tone, -1 for a silent block
    snr = [None] * count
    signal = [0.0] * count
    residual = [0.0] * count
    silent_energy = SILENT_RMS * SILENT_RMS * size
    for i in range(count):
        block = [v - mean for v in samples[i * size:(i + 1) * size]]
        energy = sum(map(mul, block, block))
        if energy < silent_energy:
            residual[i] = energy
            snr[i] = -99.0
            continue
        power, index = max((f.power(block), n) for n, f in enumerate(fits))
        rest = max(energy - power, 1e-9)
        best[i], signal[i], residual[i] = index, power, rest
        snr[i] = 10 * math.log10(max(power, 1e-9) / rest)

    # The measured stretch: from the first signal to the last, without the edges of the tone.
    voiced = [i for i in range(count) if best[i] >= 0]
    if not voiced:
        return {"blocks": 0, "error": "no signal"}
    edge = int(EDGE_S / BLOCK_S)
    first, last = voiced[0] + edge, voiced[-1] - edge
    bad = []
    measured = 0
    for i in range(first, last + 1):
        if skip[i]:
            continue
        measured += 1
        if snr[i] >= min_snr:
            continue
        # A block holding the change from one tone to the next, with its neighbours clean on both sides.
        if (best[i - 1] != best[i + 1] and best[i - 1] >= 0 and best[i + 1] >= 0 and snr[i - 1] >= min_snr
                and snr[i + 1] >= min_snr):
            continue
        bad.append(i)

    events = []
    for i in bad:
        if events and i - events[-1]["end"] <= MERGE_BLOCKS + 1:
            events[-1]["end"] = i
            events[-1]["blocks"] += 1
            events[-1]["min_db"] = min(events[-1]["min_db"], snr[i])
        else:
            events.append({"start": i, "end": i, "blocks": 1, "min_db": snr[i]})
    per_block = size / rate
    event_list = [{"t": round(e["start"] * per_block, 3), "blocks": e["blocks"], "min_db": round(e["min_db"], 1)}
                  for e in events]
    gaps = Counter(round(round((b["t"] - a["t"]) / INTERVAL_BIN_S) * INTERVAL_BIN_S, 2)
                   for a, b in zip(event_list, event_list[1:]))

    per_second = int(round(1.0 / per_block))
    series = []
    for i in range(first, last + 1 - per_second, per_second):
        sig = sum(signal[i:i + per_second])
        res = sum(residual[i:i + per_second])
        series.append(10 * math.log10(max(sig, 1e-9) / max(res, 1e-9)))
    series.sort()
    minutes = measured * per_block / 60.0
    return {
        "blocks": measured, "impaired": len(bad), "impaired_pct": round(100.0 * len(bad) / measured, 3) if measured else None,
        "events": len(events), "events_per_min": round(len(events) / minutes, 2) if minutes else None,
        "seconds_measured": round(measured * per_block, 1),
        "snr_per_second": {"worst_db": round(series[0], 1), "under_min": sum(1 for v in series if v < min_snr),
                           "p01_db": round(series[len(series) // 100], 1), "median_db": round(series[len(series) // 2], 1),
                           "seconds": len(series)} if series else None,
        "event_list": event_list, "intervals_top": [[k, v] for k, v in gaps.most_common(8)],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav")
    ap.add_argument("--tones", default="400,1000,1800", help="comma separated frequencies in Hz")
    ap.add_argument("--min-snr", type=float, default=20.0)
    ap.add_argument("--exclude", default="", help="START:LEN sample ranges to leave out, comma separated")
    ap.add_argument("--events", action="store_true", help="keep the full event list in the output")
    args = ap.parse_args()
    samples, rate = audio_check.read_wav(args.wav)
    exclude = [tuple(int(v) for v in item.split(":")) for item in args.exclude.split(",") if item]
    result = analyse(samples, rate, [int(t) for t in args.tones.split(",")], args.min_snr, exclude)
    if not args.events:
        result.pop("event_list", None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("blocks") else 2


if __name__ == "__main__":
    sys.exit(main())
