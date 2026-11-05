#!/usr/bin/env python3
"""Shape of each uplink break in a phone.wav kept by round.py btloss --keep-wav. Usage: break_shape.py LABEL  (goal 062)

For each event: the level inside the break and the phase of the tone before and after it. Lost radio frames keep the
time base of the stream, so the tone comes back in phase; samples lost in the phone's audio path shift it. 1800 Hz has
a period of 8.9 samples, so a whole number of periods cannot be told from no shift: compare with the random baseline
(windows with no event) and look at the spread of the jumps.
"""
import collections
import glob
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import RUNS_ROOT  # noqa: E402

BTLOSS = str(RUNS_ROOT / "btloss")
RATE, TONE = 16000, 1800
d = glob.glob("%s/*-%s" % (BTLOSS, sys.argv[1]))[-1]
with wave.open(d + "/asis/phone.wav") as w:
    x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
ev = json.load(open(d + "/asis/events.json", encoding="utf-8"))["up"]
print("samples", len(x), "seconds", len(x) / RATE, "events", len(ev))


def phase(a, b):
    n = np.arange(a, b)
    z = np.sum(x[a:b] * np.exp(-2j * np.pi * TONE * n / RATE)) * 2 / (b - a)
    return np.angle(z), abs(z)


SIDE, GAP = int(0.05 * RATE), int(0.03 * RATE)
rows = []
for e in ev:
    at = int(e["t"] * RATE)
    a0, a1 = at - GAP - SIDE, at - GAP
    span = int(e["blocks"] * 120)
    b0, b1 = at + span + GAP, at + span + GAP + SIDE
    if a0 < 0 or b1 > len(x):
        continue
    p0, l0 = phase(a0, a1)
    p1, l1 = phase(b0, b1)
    jump = np.angle(np.exp(1j * (p1 - p0))) / (2 * np.pi) * RATE / TONE     # in samples, within half a period
    seg = x[at - 240:at + span + 240]
    # Length of the stretch where the local level is under a quarter of the tone's.
    env = np.sqrt(np.convolve(seg ** 2, np.ones(16) / 16, "same"))
    low = np.mean(env < l0 / 4 / np.sqrt(2)) * len(seg) / RATE * 1000
    rows.append((e["t"], e["blocks"], round(e["min_db"], 1), round(float(jump), 2), round(float(low), 1), round(float(l0)), round(float(l1))))
print("t blocks min_db jump_samples low_ms level_before level_after (first 25)")
for r in rows[:25]:
    print(*r)
j = np.array([r[3] for r in rows])
print("jump |median| %.2f, share under 0.5 sample %.2f" % (np.median(np.abs(j)), np.mean(np.abs(j) < 0.5)))
print("low_ms histogram", sorted(collections.Counter(int(r[4] // 2.5 * 2.5) for r in rows).items()))
# Baseline: the same measure at places without an event.
rng = np.random.default_rng(1)
times = np.array([e["t"] for e in ev])
base = []
for _ in range(300):
    t = rng.uniform(5, len(x) / RATE - 5)
    if times.size and np.min(np.abs(times - t)) < 0.3:
        continue
    at = int(t * RATE)
    p0, _l = phase(at - GAP - SIDE, at - GAP)
    p1, _l = phase(at + 360 + GAP, at + 360 + GAP + SIDE)
    base.append(np.angle(np.exp(1j * (p1 - p0))) / (2 * np.pi) * RATE / TONE)
base = np.array(base)
print("baseline |median| %.3f max %.3f, n=%d" % (np.median(np.abs(base)), np.max(np.abs(base)), len(base)))
