"""Speech-like synthetic lines with known borders, for the tests of call_listen.py (goal 101). numpy only, no voice.

A voiced syllable: a glottal pulse train (f0 100-220 Hz with vibrato and jitter) through three formant resonators of a
vowel, under a raised cosine envelope; some syllables start with an unvoiced burst (noise above 2 kHz). Syllables of
0.12-0.3 s with gaps up to 0.15 s, words apart by up to 0.3 s: one utterance. script() lays utterances at given times.
"""
import numpy as np

RATE = 16000
VOWELS = [(730, 1090, 2440), (270, 2290, 3010), (530, 1840, 2480), (570, 840, 2410), (300, 870, 2240), (660, 1720, 2410)]


def _resonator_ir(f, bw, n=400):
    r = np.exp(-np.pi * bw / RATE)
    a1, a2 = 2 * r * np.cos(2 * np.pi * f / RATE), -r * r
    h = np.zeros(n)
    y1 = y2 = 0.0
    for i in range(n):
        y = (1.0 if i == 0 else 0.0) + a1 * y1 + a2 * y2
        h[i] = y
        y2, y1 = y1, y
    return h / np.abs(h).sum()


_IR_CACHE = {}


def _vowel_ir(v):
    if v not in _IR_CACHE:
        h = np.array([1.0])
        for f, bw in zip(VOWELS[v], (80, 100, 140)):
            h = np.convolve(h, _resonator_ir(f, bw))
        _IR_CACHE[v] = h / np.sqrt((h ** 2).sum())
    return _IR_CACHE[v]


def syllable(seconds, rng, f0=140.0):
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    f = f0 * (1 + 0.06 * np.sin(2 * np.pi * 5 * t + rng.uniform(0, 6)) + 0.1 * (t / max(seconds, 1e-3) - 0.5))
    phase = np.cumsum(f / RATE)
    pulses = np.zeros(n)
    idx = np.flatnonzero(np.diff(np.floor(phase)) > 0)
    pulses[idx] = 1.0 + 0.1 * rng.normal(size=len(idx))
    voiced = np.convolve(pulses, _vowel_ir(int(rng.integers(len(VOWELS)))))[:n]
    env = np.ones(n)
    ramp = min(int(0.03 * RATE), n // 2)
    if ramp:
        env[:ramp] = 0.5 - 0.5 * np.cos(np.pi * np.arange(ramp) / ramp)
        env[-ramp:] = env[:ramp][::-1]
    out = voiced * env
    if rng.random() < 0.35:                       # an unvoiced consonant at the start
        m = min(int(0.05 * RATE), n)
        noise = rng.normal(size=m)
        noise = noise - np.convolve(noise, np.ones(4) / 4, mode="same")  # crude high pass
        out[:m] += 0.6 * noise * np.hanning(m) * np.std(voiced) / max(np.std(noise), 1e-9)
    return out


def utterance(seconds, rng, f0=None):
    """About `seconds` long, starting and ending exactly with sound; returns float samples, unit RMS over the voice."""
    f0 = f0 or rng.uniform(100, 220)
    parts, total, n_end = [], 0, int(seconds * RATE)
    while total < n_end:
        syl = syllable(rng.uniform(0.12, 0.3), rng, f0)
        parts.append(syl)
        total += len(syl)
        if total >= n_end:
            break
        gap = int(rng.uniform(0.02, 0.15 if rng.random() < 0.7 else 0.3) * RATE)
        parts.append(np.zeros(gap))
        total += gap
    x = np.concatenate(parts)
    rms = np.sqrt(np.mean(x[np.abs(x) > 0] ** 2))
    return x / rms


def script(total_s, utts, level_db=-26.0, noise_db=None, seed=1, noise_steps=()):
    """utts: [(start_s, dur_s)]. Returns (int16 line, [(start_s, end_s)] true borders). noise_db: white noise RMS in
    dBFS; noise_steps: [(from_s, noise_db)] change the noise level from that time."""
    rng = np.random.default_rng(seed)
    x = np.zeros(int(total_s * RATE))
    truth = []
    amp = 32768 * 10 ** (level_db / 20)
    for start, dur in utts:
        u = utterance(dur, rng) * amp
        a = int(start * RATE)
        u = u[:len(x) - a]
        x[a:a + len(u)] += u
        nz = np.flatnonzero(np.abs(u) > amp * 1e-3)
        truth.append(((a + nz[0]) / RATE, (a + nz[-1] + 1) / RATE))
    if noise_db is not None or noise_steps:
        level = np.full(len(x), -200.0 if noise_db is None else noise_db)
        for at, db in noise_steps:
            level[int(at * RATE):] = db
        x += rng.normal(size=len(x)) * 32768 * 10 ** (level / 20)
    return np.clip(np.round(x), -32768, 32767).astype(np.int16), truth
