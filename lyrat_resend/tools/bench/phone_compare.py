#!/usr/bin/env python3
"""What the LyraT mic sent against what the phone got: align two recordings and mark each defect with its time.

  python phone_compare.py DIR                      analyse a run of call_check.py --compare again (DIR keeps up_full.wav,
                                                   phone_raw.wav and compare.json; that is, a run made with --keep)
  python phone_compare.py --ref A.wav --phone B.wav --out DIR    any two 16-bit WAV recordings of the same sound (any
                                                   rate, mono or stereo: brought to 16 kHz mono)
  python phone_compare.py --tone-check WINDOW_DIR [--tone 1800]  the detector against tone_blocks on a window of
                                                   round.py btloss --keep-wav (phone.wav + events.json): the reference
                                                   is the tone itself, fitted to the recording

The reference (up_s3) is the LyraT's ADC as the S3 sees it on the I2S bus: before the board's processing (HPF, gain, noise
suppression, PLC) and before mSBC. The phone side is the recording of the probe app (source raw). Nothing is played (R15).

Alignment (goal 099, DECISIONS D3): a whitened (PHAT) cross-correlation of the whole recordings in 100-4000 Hz gives the
offset; local offsets in 1 s windows every 0.5 s give the drift of the two clocks (a straight line, robust fit) and any
jump of the time line (samples lost or added on the way), which splits the line into pieces. The phone recording is moved
onto the time line of the reference by interpolation. "Converged": half the windows or more carry a clear peak, the fit
residual is within 0.5 sample, the drift within 300 ppm. Without convergence no defect is looked for: a wrong alignment
would only invent losses.

Defects (D4): phone ~ H(f,t) * reference, where H (the board's noise suppression, gain, the codec, the phone) changes
slowly. H is estimated in each STFT frame (240 samples = two mSBC frames, hop 60) from the frames around it, leaving the
frame and its close neighbours out, so a defect cannot hide in its own estimate. Where the phone side is mostly predictable
(coherent), a frame is bad when the prediction error rises 10 dB above its own running level, or the level falls 12 dB
below or rises 10 dB above what is expected. Runs of digital zeros on the phone while the reference has sound are gaps.
Types: тишина (zeros), провал (level fell), щелчок (a short burst above), маскированный кадр (a short wrong piece at the
right level: the phone's concealment of a lost mSBC frame, the "обрыв" of round.py btloss), искажение (longer: metallic),
сдвиг (the time line jumped). Whether it is audible is an estimate from depth, length and level, not a listening.

Outputs in the run folder: up_s3.wav (reference under the phone recording), phone.wav (the phone recording on the same time
line), compare_both.wav (stereo: left before Bluetooth, right the phone, level-matched), cut_<mm.ss.mmm>.wav (stereo, 1 s
around each defect, at most MAX_CUTS), report.md and report.json. Made by goal 099.
"""
import argparse
import json
import math
import sys
import wave
from pathlib import Path

import numpy as np

RATE = 16000
FRAME = 120                     # one mSBC frame, 7.5 ms
WIN, HOP, NFFT = 240, 60, 256   # STFT of the detector
BAND_ALIGN = (100.0, 4000.0)
BAND_DETECT = (100.0, 7000.0)
SEARCH_S = 3.0                  # the coarse offset is looked for this far around the expected one
LOCAL_WIN_S, LOCAL_STEP_S = 1.0, 0.5
LOCAL_SEARCH = 200              # samples around the last clear offset: a frame lost on the way (120) moves the line this much
MIN_PSR = 6.0                   # a local peak this many times above the rest of the correlation is clear
MIN_RELIABLE = 0.5
MAX_RESIDUAL = 0.5              # samples
MAX_PPM = 300.0
JUMP = 2.0                      # samples: a step of the time line
H_SIDE, H_GAP, H_SIDE_WIDE = 12, 3, 36           # neighbours used for H: |k| <= H_SIDE, |k| > H_GAP (STFT frames)
MIN_COHERENT_DB = -3.0          # share of the phone's energy H explains around the frame
MIN_LEVEL_DBFS = -70.0
COH_S = 0.25                    # half width of the running median of the coherence
MAX_BAD_SNR = 20.0              # dB: a frame predicted better than this is not a defect, whatever its neighbours
DROP_DB = 10.0                  # the prediction error over its running level
BASE_S = 0.5                    # half width of the running level
LOW_DB, HIGH_DB = -20.0, 10.0          # level against expected: a dip the suppression cannot make; a burst (type only)
MERGE_FRAMES = 2
CORE_DB = 10.0                  # second pass: what of a first-pass defect is left out of H
THIN = 8                        # fewer kept neighbours than this: H from the wide neighbourhood
MIN_BAD_FRAMES = 3
QUIET_DBFS = -45.0              # a defect in a place quieter than this is hardly heard
ZERO_RUN = 60                   # samples of digital silence
CUT_S = 1.0
JOIN_S = 0.015                  # a detector event this close to a step of the time line is that step
MAX_CUTS = 20


# --- files ---------------------------------------------------------------------------------------------------------------

def read_wav(path, any_rate=False):
    """16-bit WAV as float, channels averaged. any_rate: another sample rate is brought to 16 kHz (FFT resampling: a
    recording from elsewhere, e.g. the other party's side of a call); otherwise only 16 kHz is taken."""
    with wave.open(str(path), "rb") as w:
        ch, rate, width = w.getnchannels(), w.getframerate(), w.getsampwidth()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    if width != 2:
        raise ValueError("%s: %d-byte samples, 16-bit expected" % (path, width))
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != RATE:
        if not any_rate:
            raise ValueError("%s: %d Hz, 16000 expected" % (path, rate))
        n_out = int(round(len(x) * RATE / rate))
        spec = np.fft.rfft(x)
        size = n_out // 2 + 1
        spec = spec[:size] if len(spec) >= size else np.concatenate([spec, np.zeros(size - len(spec), complex)])
        x = np.fft.irfft(spec, n_out) * (n_out / len(x))
    return x


def write_wav(path, data, channels=1):
    data = np.clip(np.round(np.asarray(data, dtype=np.float64)), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.ascontiguousarray(data).tobytes())


# --- alignment -----------------------------------------------------------------------------------------------------------

def _size(n):
    s = 1
    while s < n:
        s *= 2
    return s


def _band_mask(size, band):
    f = np.fft.rfftfreq(size, 1.0 / RATE)
    return ((f >= band[0]) & (f <= band[1])).astype(np.float64)


def phat(ref, ph, lo, hi):
    """Whitened cross-correlation; c[k] for offsets k in [lo, hi]: phone[j] ~ ref[j + k]. Returns (lags, c)."""
    size = _size(len(ref) + len(ph))
    cross = np.fft.rfft(ref, size) * np.conj(np.fft.rfft(ph, size))
    cross = cross / (np.abs(cross) + 1e-9 * np.abs(cross).max() + 1e-30) * _band_mask(size, BAND_ALIGN)
    cc = np.fft.irfft(cross, size)
    lags = np.arange(lo, hi + 1)
    return lags, cc[lags % size]


def _peak(lags, c):
    i = int(np.argmax(c))
    frac = 0.0
    if 0 < i < len(c) - 1:
        a, b, d = c[i - 1], c[i], c[i + 1]
        den = a - 2 * b + d
        if den < 0:
            frac = 0.5 * (a - d) / den
    rest = np.delete(np.abs(c), range(max(0, i - 3), min(len(c), i + 4)))
    psr = float(c[i] / (np.sqrt(np.mean(rest ** 2)) + 1e-30)) if len(rest) else 0.0
    return float(lags[i] + frac), psr


def coarse_offset(ref, ph, expected=None):
    """Where the phone's sample 0 lies in the reference (samples, may be negative), with the peak's clarity."""
    if expected is None:
        lo, hi = -(len(ph) - 1), len(ref) - 1
    else:
        lo, hi = int(expected - SEARCH_S * RATE), int(expected + SEARCH_S * RATE)
    lags, c = phat(ref, ph, lo, hi)
    return _peak(lags, c)


def local_offsets(ref, ph, coarse):
    """[(phone sample of the window centre, offset, psr)] in windows of LOCAL_WIN_S every LOCAL_STEP_S."""
    win, step = int(LOCAL_WIN_S * RATE), int(LOCAL_STEP_S * RATE)
    rows = []
    centre = coarse
    for j0 in range(0, len(ph) - win + 1, step):
        seg = ph[j0:j0 + win]
        a = int(round(j0 + centre)) - LOCAL_SEARCH - 4
        if a < 0 or a + win + 2 * LOCAL_SEARCH + 8 > len(ref):
            continue
        r = ref[a:a + win + 2 * LOCAL_SEARCH + 8]
        lags, c = phat(r, seg, 0, 2 * LOCAL_SEARCH + 8)
        off, psr = _peak(lags, c)
        rows.append((j0 + win / 2.0, a + off - j0, psr))
        if psr >= MIN_PSR:
            centre = a + off - j0      # follow a drift beyond the search span over a long recording
    return rows


def _robust_line(t, y):
    a, b = float(np.median(y)), 0.0
    keep = np.ones(len(t), bool)
    for _ in range(3):
        if keep.sum() >= 2 and np.ptp(t[keep]) > 0:
            b, a = np.polyfit(t[keep], y[keep], 1)
        res = np.abs(y - (a + b * t))
        keep = res <= max(1.0, 3 * np.median(res))
    return float(a), float(b), keep


def segment(t, y):
    """Split the clear windows where the offset steps by JUMP or more and stays there (the next window agrees): samples
    lost or added on the way. A window off its neighbours on both sides is an outlier. Returns ([index lists], outliers)."""
    segs, outliers = [[0]], []
    for i in range(1, len(t)):
        cur = segs[-1]
        if len(cur) >= 2:
            a, b, _ = _robust_line(t[cur], y[cur])
            pred = a + b * t[i]
        else:
            pred = y[cur[-1]]
        if abs(y[i] - pred) < JUMP:
            cur.append(i)
        elif i + 1 < len(t) and abs(y[i + 1] - y[i]) < JUMP:
            segs.append([i])
        elif len(cur) == 1 and i + 1 < len(t) and abs(y[i + 1] - pred) >= JUMP:
            # The piece so far is one window that the next two disagree with: that one was the outlier.
            outliers.append(cur.pop())
            cur.append(i)
        else:
            outliers.append(i)
    keep = []
    for seg in segs:
        if len(seg) >= 2:
            # A piece at the level of the one before it (an outlier piece between them was dropped) joins it.
            if keep and abs(np.median(y[seg]) - np.median(y[keep[-1]])) < JUMP:
                keep[-1].extend(seg)
            else:
                keep.append(seg)
        else:
            outliers.extend(seg)
    return [np.array(s) for s in keep], sorted(outliers)


def fit_alignment(rows):
    """Piecewise straight lines offset(j) = a + b*j over the clear windows. Splits where the line jumps."""
    clear = [(j, o) for j, o, p in rows if p >= MIN_PSR]
    out = {"windows": len(rows), "clear": len(clear), "clear_pct": round(100.0 * len(clear) / len(rows), 1) if rows else 0.0,
           "pieces": [], "jumps": [], "residual": None, "ppm": None, "converged": False, "why": None}
    if len(clear) < 2:
        out["why"] = "ясных окон корреляции меньше двух"
        return out
    t = np.array([c[0] for c in clear])
    y = np.array([c[1] for c in clear])
    segs, outliers = segment(t, y)
    out["outliers"] = [round(float(t[i])) for i in outliers]
    if not segs:
        out["why"] = "нет двух согласных ясных окон подряд"
        return out
    res_all, fits = [], []
    for seg in segs:
        a, b, keep = _robust_line(t[seg], y[seg])
        res_all.extend(np.abs(y[seg] - (a + b * t[seg]))[keep])
        fits.append({"from": float(t[seg[0]]), "to": float(t[seg[-1]]), "a": a, "b": b, "windows": len(seg)})
    # The pieces cover the whole phone recording: each runs to the middle between its windows and the next piece's.
    for i in range(len(fits)):
        fits[i]["start"] = -math.inf if i == 0 else (fits[i - 1]["to"] + fits[i]["from"]) / 2.0
        fits[i]["end"] = math.inf if i == len(fits) - 1 else (fits[i]["to"] + fits[i + 1]["from"]) / 2.0
    for f1, f2 in zip(fits, fits[1:]):
        at = f2["start"]
        out["jumps"].append({"phone_sample": round(at), "step": round((f2["a"] + f2["b"] * at) - (f1["a"] + f1["b"] * at), 2)})
    out["pieces"] = fits
    out["residual"] = round(float(np.median(res_all)), 3) if res_all else None
    # The drift from the long pieces: a short one has a slope of little weight.
    long_ = [f for f in fits if f["windows"] >= 3] or fits
    out["ppm"] = round(float(np.average([f["b"] for f in long_], weights=[f["windows"] for f in long_])) * 1e6, 1)
    why = []
    if len(clear) < MIN_RELIABLE * len(rows):
        why.append("ясных окон %d из %d" % (len(clear), len(rows)))
    if out["residual"] is None or out["residual"] > MAX_RESIDUAL:
        why.append("остаток %s отсчёта" % out["residual"])
    if abs(out["ppm"]) > MAX_PPM:
        why.append("дрейф %s ppm" % out["ppm"])
    out["converged"] = not why
    out["why"] = "; ".join(why) or None
    return out


def offset_at(fit, j):
    j = np.asarray(j, dtype=np.float64)
    off = np.zeros_like(j)
    for f in fit["pieces"]:
        sel = (j >= f["start"]) & (j < f["end"])
        off[sel] = f["a"] + f["b"] * j[sel]
    return off


def phone_on_ref(ph, fit, n0, n1):
    """The phone recording on the reference's samples n0..n1: for each n the phone position j with j + offset(j) = n."""
    n = np.arange(n0, n1, dtype=np.float64)
    j = n - offset_at(fit, n - fit["pieces"][0]["a"])
    for _ in range(2):              # offset depends on j only through a slow drift: two fixed-point steps suffice
        j = n - offset_at(fit, j)
    out = np.interp(j, np.arange(len(ph)), ph, left=np.nan, right=np.nan)
    return out


def _block_rho(ref, ph, fit_piece, starts, size):
    """Normalized correlation of phone blocks with the reference under one piece's line."""
    out = []
    grid = np.arange(len(ref))
    for j in starts:
        seg = ph[j:j + size]
        pos = np.arange(j, j + size) + fit_piece["a"] + fit_piece["b"] * np.arange(j, j + size)
        r = np.interp(pos, grid, ref)
        den = math.sqrt(float(seg @ seg) * float(r @ r))
        out.append(float(seg @ r) / den if den > 0 else 0.0)
    return np.array(out)


def refine_jumps(ref, ph, fit, size=240, hop=60):
    """Put each step of the time line where it is, to a quarter of an mSBC frame: between the last window of one piece and
    the first of the next, the switch point that leaves the least correlation unexplained."""
    pieces = fit["pieces"]
    for f1, f2 in zip(pieces, pieces[1:]):
        lo, hi = int(f1["to"]) - size, int(f2["from"]) + size
        lo, hi = max(lo, 0), min(hi, len(ph) - size)
        starts = np.arange(lo, hi, hop)
        if len(starts) < 2:
            continue
        old = 1.0 - np.abs(_block_rho(ref, ph, f1, starts, size))
        new = 1.0 - np.abs(_block_rho(ref, ph, f2, starts, size))
        cost = np.concatenate([[0.0], np.cumsum(old)]) + np.concatenate([np.cumsum(new[::-1])[::-1], [0.0]])
        k = int(np.argmin(cost))
        at = float(starts[min(k, len(starts) - 1)] + size / 2.0) if 0 < k < len(starts) else (f1["end"] if k == 0 else f2["start"])
        f1["end"] = f2["start"] = at
    fit["jumps"] = [{"phone_sample": round(f2["start"]),
                     "step": round((f2["a"] + f2["b"] * f2["start"]) - (f1["a"] + f1["b"] * f2["start"]), 2)}
                    for f1, f2 in zip(pieces, pieces[1:])]
    return fit


def align(ref, ph, expected=None):
    coarse, psr = coarse_offset(ref, ph, expected)
    rows = local_offsets(ref, ph, coarse)
    fit = fit_alignment(rows)
    if len(fit["pieces"]) > 1:
        refine_jumps(ref, ph, fit)
    fit["coarse"] = round(coarse, 2)
    fit["coarse_psr"] = round(psr, 1)
    fit["local"] = [[round(j), round(o, 3), round(p, 1)] for j, o, p in rows]
    return fit


# --- detector ------------------------------------------------------------------------------------------------------------

def stft(x):
    w = np.hanning(WIN + 2)[1:-1]
    count = max(0, (len(x) - WIN) // HOP + 1)
    idx = np.arange(WIN)[None, :] + HOP * np.arange(count)[:, None]
    return np.fft.rfft(x[idx] * w, NFFT, axis=1)


def _window_sum(a, side, gap):
    """Sum over |k| <= side minus |k| <= gap along axis 0, zero padded at the ends."""
    c = np.concatenate([np.zeros((1,) + a.shape[1:], a.dtype), np.cumsum(a, axis=0)])
    n = a.shape[0]
    i = np.arange(n)

    def span(h):
        hi = np.minimum(i + h + 1, n)
        lo = np.maximum(i - h, 0)
        return c[hi] - c[lo]
    return span(side) - span(gap)


def _running_median(x, half):
    """Median of the finite values in [i - half, i + half]; NaN where there are none."""
    n = len(x)
    if n == 0:
        return x.copy()
    pad = np.concatenate([np.full(half, np.nan), x, np.full(half, np.nan)])
    win = np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmedian(win, axis=1)


def _smooth_freq(a):
    """[1, 2, 1] / 4 across frequency: H of a noise suppressor is smooth in frequency, and fewer degrees of freedom keep
    it steady where the neighbours carry little energy in some bins."""
    out = a * 0.5
    out[:, 1:] += 0.25 * a[:, :-1]
    out[:, :-1] += 0.25 * a[:, 1:]
    out[:, 0] += 0.25 * a[:, 0]
    out[:, -1] += 0.25 * a[:, -1]
    return out


def frames(ref, ph, keep=None):
    """Per STFT frame: prediction error (snr), level against expected (ratio), coherence of the neighbourhood, level.
    keep: frames allowed into the estimate of H (the second pass leaves the bad ones of the first pass out)."""
    R, P = stft(ref), stft(ph)
    f = np.fft.rfftfreq(NFFT, 1.0 / RATE)
    band = (f >= BAND_DETECT[0]) & (f <= BAND_DETECT[1])
    R, P = R[:, band], P[:, band]
    m = np.ones((len(R), 1)) if keep is None else keep.astype(np.float64)[:, None]

    def sums(side):
        return [_smooth_freq(_window_sum(m * v, side, H_GAP)) for v in (P * np.conj(R), np.abs(R) ** 2, np.abs(P) ** 2)]
    s_pr, s_rr, s_pp = sums(H_SIDE)
    if keep is not None:
        # Around a long defect most neighbours are left out: there H comes from a wider neighbourhood.
        count = _window_sum(m, H_SIDE, H_GAP)[:, 0]
        thin = count < THIN
        if thin.any():
            wide = sums(H_SIDE_WIDE)
            for a, b in zip((s_pr, s_rr, s_pp), wide):
                a[thin] = b[thin]
    eps = 1e-3 * np.median(s_rr, axis=0, keepdims=True) + 1e-9
    H = s_pr / (s_rr + eps)
    Y = H * R
    E = P - Y
    pp, ee, yy = (np.abs(P) ** 2).sum(1), (np.abs(E) ** 2).sum(1), (np.abs(Y) ** 2).sum(1)
    # Error with a free real gain in every bin: the board's noise suppression scales each bin up and down from frame to
    # frame and keeps its phase; a piece the phone made up (concealment of a lost frame) has the wrong phase. What a
    # positive gain cannot explain is |P|^2 sin^2 of the phase error, or all of |P|^2 when the phase is opposite.
    cross = P * np.conj(Y)
    explained = np.maximum(cross.real, 0.0) ** 2 / (np.abs(Y) ** 2 + 1e-12)
    pe = np.maximum(np.abs(P) ** 2 - explained, 0.0).sum(1)
    coh = (np.abs(s_pr) ** 2 / (s_rr + eps)).sum(1) / (s_pp.sum(1) + 1e-9)
    tiny = 1e-6
    snr = 10 * np.log10((pp + tiny) / (pe + tiny))
    snr_full = 10 * np.log10((pp + tiny) / (ee + tiny))
    ratio = 10 * np.log10((pp + tiny) / (yy + tiny))
    w = np.hanning(WIN + 2)[1:-1]
    norm = (w ** 2).sum() * NFFT / 2.0 * 32768.0 ** 2
    level = 10 * np.log10(yy / norm + 1e-12)
    good = np.isfinite(coh) & (coh > 0.5)
    gain = np.sqrt(np.median((np.abs(H) ** 2).mean(1)[good])) if np.any(good) else 1.0
    return {"snr": snr, "snr_full": snr_full, "ratio": ratio, "coh_db": 10 * np.log10(coh + 1e-9), "level": level, "gain": float(gain)}


def zero_runs(ph, ref, active_level=-60.0):
    """[(start, length)] of digital zeros in the phone line while the reference has sound."""
    z = np.concatenate([[0], (ph == 0).astype(np.int8), [0]])
    d = np.diff(z)
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    out = []
    for s, e in zip(starts, ends):
        if e - s >= ZERO_RUN:
            seg = ref[s:e]
            if len(seg) and 20 * np.log10(np.sqrt(np.mean(seg ** 2)) / 32768.0 + 1e-12) > active_level:
                out.append((int(s), int(e - s)))
    return out


def _bad(fr):
    # Whether there is something to compare is a property of the place, not of the frame: a running median, so a burst
    # the phone made (its concealment can click 40 dB over the line) does not switch its own frames off.
    coh = _running_median(fr["coh_db"], int(COH_S * RATE / HOP))
    active = (coh >= MIN_COHERENT_DB) & (fr["level"] >= MIN_LEVEL_DBFS)
    snr_a = np.where(active, fr["snr"], np.nan)
    base = _running_median(snr_a, int(BASE_S * RATE / HOP))
    low = active & (fr["ratio"] < LOW_DB)
    high = active & (fr["ratio"] > HIGH_DB)
    # A concealed frame is wrong by about its own level: an error within MAX_BAD_SNR of the signal. The ceiling keeps a
    # very clean line (a tone: 70 dB) from calling a 15 dB wobble of its reference a defect.
    bad = (active & (fr["snr"] < np.minimum(base - DROP_DB, MAX_BAD_SNR))) | low
    return bad, active, base, low, high, snr_a


def detect(ref, ph):
    """Defects of the phone line against the reference on the same samples. Returns (events, frame stats).
    Two passes: the frames found bad in the first are left out of the estimate of H in the second, so a defect does
    not spoil the prediction of its neighbours (which would smear its time and length)."""
    fr = frames(ref, ph)
    bad, _, base, *_ = _bad(fr)
    n = len(fr["snr"])
    zeros = zero_runs(ph, ref)
    zero_frames = np.zeros(n, bool)
    for s, length in zeros:
        a, b = max(0, (s - WIN) // HOP + 1), min(n, (s + length) // HOP + 1)
        zero_frames[a:b] = True
    # Only the core of each first-pass defect leaves the estimate: the frames within CORE_DB of its worst. Its smeared
    # edges stay in, else the neighbourhood of a long defect would be empty.
    score = np.where(np.isfinite(base), base - fr["snr"], 0.0)
    score = np.maximum(score, np.maximum(fr["ratio"] - HIGH_DB, LOW_DB - fr["ratio"]) + DROP_DB)
    core = zero_frames.copy()
    idx = np.where(bad)[0]
    for k in (np.split(idx, np.where(np.diff(idx) > MERGE_FRAMES + 1)[0] + 1) if len(idx) else []):
        core[k[score[k] >= score[k].max() - CORE_DB]] = True
    core = np.convolve(core.astype(int), np.ones(3, int), mode="same") > 0
    fr = frames(ref, ph, keep=~core)
    bad, active, base, low, high, snr_a = _bad(fr)
    bad |= zero_frames
    events = []
    idx = np.where(bad)[0]
    groups = np.split(idx, np.where(np.diff(idx) > MERGE_FRAMES + 1)[0] + 1) if len(idx) else []
    for k in groups:
        if len(k) >= MIN_BAD_FRAMES or zero_frames[k].any():
            events.append(_event(k, fr, base, low, high, zero_frames, zeros))
    stats = {"frames": n, "active_pct": round(100.0 * float(active.mean()), 1) if n else 0.0,
             "snr_median_db": round(float(np.nanmedian(snr_a)), 1) if np.any(active) else None,
             "gain": round(fr["gain"], 4)}
    return events, stats


def _event(k, fr, base, low, high, zero_frames, zeros):
    centres = k * HOP + WIN / 2.0
    excess = base[k] - fr["snr"][k]
    excess = np.where(np.isfinite(excess), excess, 0.0)
    over = np.maximum(fr["ratio"][k] - HIGH_DB, LOW_DB - fr["ratio"][k])
    score = np.maximum(excess, over)
    # The core: frames within 6 dB of the worst. A loss of d samples makes about (d + 60) / HOP core frames.
    core = score >= score.max() - 6.0
    centre = float(centres[core].mean())
    dur = max(FRAME, int(core.sum()) * HOP - 60)
    is_zero = bool(zero_frames[k].any())
    if is_zero:
        s, length = next(((s, ln) for s, ln in zeros if s <= centres[-1] + WIN and s + ln >= centres[0] - WIN), (None, None))
        if s is not None:
            centre, dur = s + length / 2.0, length
    depth_low = float(np.min(fr["ratio"][k]))
    depth_high = float(np.max(fr["ratio"][k]))
    drop = float(np.nanmax(base[k] - fr["snr"][k])) if np.any(np.isfinite(base[k])) else None
    if not is_zero and low[k].sum() >= 2:
        dur = int(low[k].sum()) * HOP + 180
        centre = float(centres[low[k]].mean())
    if is_zero:
        kind = "тишина"
    elif low[k].sum() >= 2:
        kind = "провал"
    elif high[k].any() and dur <= 240:
        kind = "щелчок"
    elif dur <= 480:
        kind = "маскированный кадр"
    else:
        kind = "искажение"
    level = float(np.max(fr["level"][k]))
    ms = dur * 1000.0 / RATE
    quiet = level < QUIET_DBFS
    if kind == "щелчок":
        audible = "слышно"
    elif quiet:
        audible = "вряд ли (тихий участок)"
    elif kind in ("тишина", "провал", "искажение") or ms >= 20.0 or (drop or 0) >= 20.0:
        audible = "слышно"
    else:
        audible = "щелчок, слышно вблизи"
    return {"t": round(float(centre - dur / 2.0) / RATE, 3), "centre": round(float(centre) / RATE, 4), "ms": round(float(ms), 1), "kind": kind,
            "snr_drop_db": None if drop is None else round(drop, 1), "level_db": round(depth_low if kind == "провал" else depth_high, 1),
            "frame_dbfs": round(level, 1), "audible": audible}


# --- the run ---------------------------------------------------------------------------------------------------------------

def compare(ref, ph, expected=None):
    """Align and detect. ref: the whole reference line; ph: the phone recording. Returns a dict with the aligned pair."""
    fit = align(ref, ph, expected)
    out = {"alignment": {k: v for k, v in fit.items() if k != "local"}, "local": fit["local"]}
    if not fit["pieces"]:
        out["events"] = None
        return out
    n0 = int(math.ceil(fit["pieces"][0]["a"]))
    n1 = int(math.floor(len(ph) + offset_at(fit, np.array([len(ph) - 1.0]))[0]))
    n0, n1 = max(n0, 0), min(n1, len(ref))
    p = phone_on_ref(ph, fit, n0, n1)
    good = np.isfinite(p)
    a, b = int(np.argmax(good)), len(good) - int(np.argmax(good[::-1]))
    n0, n1, p = n0 + a, n0 + b, np.nan_to_num(p[a:b])
    r = ref[n0:n1]
    out.update({"n0": n0, "n1": n1, "ref": r, "phone": p})
    if not fit["converged"]:
        out["events"] = None
        return out
    events, stats = detect(r, p)
    for j in fit["jumps"]:
        at = (j["phone_sample"] + offset_at(fit, np.array([float(j["phone_sample"])]))[0] - n0) / RATE
        # The frame put in or taken out is heard where the line steps: the detector's event there is the same defect.
        near = [e for e in events if not e["kind"].startswith("сдвиг") and abs(e["centre"] - at) <= JOIN_S]
        for e in near:
            events.remove(e)
        what = "лишний кадр" if j["step"] < 0 else "пропал кадр"
        audible = near[0]["audible"] if near else "вряд ли"
        events.append({"t": round(at, 3), "centre": round(at, 4), "ms": round(abs(j["step"]) * 1000.0 / RATE, 2),
                       "kind": "сдвиг (%s)" % what, "snr_drop_db": near[0]["snr_drop_db"] if near else None,
                       "level_db": near[0]["level_db"] if near else None, "frame_dbfs": near[0]["frame_dbfs"] if near else None,
                       "audible": audible, "step_samples": j["step"]})
    events.sort(key=lambda e: e["t"])
    out["events"] = events
    out["detector"] = stats
    return out


def pkt_near(events, pkt_log, start_at, seconds=1.0):
    """For each event the LyraT counters' change in the read that covers it (+-seconds): rx_none, rx_err, tx_discarded."""
    if not pkt_log or start_at is None:
        return
    for e in events:
        t = start_at + e["centre"]
        before = [p for p in pkt_log if p["t"] <= t - seconds]
        after = [p for p in pkt_log if p["t"] >= t + seconds]
        if before and after:
            a, b = before[-1], after[0]
            e["pkt_near"] = {k: b[k] - a[k] for k in ("rx_none", "rx_err", "tx_discarded") if k in a and k in b}
            e["pkt_near"]["span_s"] = round(b["t"] - a["t"], 2)


def stamp_of(t):
    ms = int(round(t * 1000))
    return "%02d.%02d.%03d" % (ms // 60000, (ms // 1000) % 60, ms % 1000)


def write_files(out, res):
    out = Path(out)
    r, p = res["ref"], res["phone"]
    gain = (res.get("detector") or {}).get("gain") or 1.0
    p_level = p / gain if gain > 0 else p
    peak = max(np.max(np.abs(p_level)) if len(p_level) else 1.0, 1.0)
    if peak > 32000:
        p_level = p_level * (32000.0 / peak)
    write_wav(out / "up_s3.wav", r)
    write_wav(out / "phone.wav", p)
    write_wav(out / "compare_both.wav", np.stack([r, p_level], axis=1), 2)
    for old in out.glob("cut_*.wav"):
        old.unlink()
    cuts = []
    for e in (res.get("events") or [])[:MAX_CUTS]:
        c = int(e["centre"] * RATE)
        a, b = max(0, c - int(CUT_S * RATE)), min(len(r), c + int(CUT_S * RATE))
        name = "cut_%s.wav" % stamp_of(e["t"])
        write_wav(out / name, np.stack([r[a:b], p_level[a:b]], axis=1), 2)
        e["cut"] = name
        cuts.append(name)
    return {"up_s3": "up_s3.wav", "phone": "phone.wav", "both": "compare_both.wav", "cuts": cuts, "right_gain": round(1.0 / gain, 3)}


def summary_line(res):
    al = res["alignment"]
    if res.get("events") is None:
        return "сравнение не сделано: выравнивание не сошлось (%s)" % (al.get("why") or "нет данных")
    ev = res["events"]
    if not ev:
        return "дефектов не найдено за %.1f с (выравнивание сошлось, дрейф %s ppm)" % (res["seconds"], al["ppm"])
    kinds = {}
    for e in ev:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    audible = sum(e["audible"].startswith("слышно") or e["audible"].startswith("щелчок") for e in ev)
    return "дефектов %d за %.1f с (%s), из них слышно %d" % (
        len(ev), res["seconds"], ", ".join("%s %d" % kv for kv in sorted(kinds.items())), audible)


def render_md(rep):
    al = rep["alignment"]
    lines = ["# Что ушло с микрофона LyraT и что пришло на телефон", "", "**Итог:** " + rep["summary"], ""]
    lines += ["Файлы (слушать плеером, ПК сам ничего не проигрывает):", "",
              "- `compare_both.wav` — стерео: **слева до Bluetooth** (микрофон LyraT, как его видит S3, до обработки платы), "
              "**справа — что получил телефон**; выровнены по времени, правый канал приведён к уровню левого "
              "(множитель %s)." % rep["files"].get("right_gain"),
              "- `up_s3.wav`, `phone.wav` — то же по отдельности (телефон без приведения уровня).",
              "- `cut_<мм.сс.ммм>.wav` — по 1 с до и после каждого дефекта, стерео как выше.", ""]
    lines += ["Важно: слева звук **до шумоподавления платы** (отвода после него нет), справа — после шумоподавления, mSBC и "
              "Bluetooth. Ровный фон слева, которого нет справа, — это работа шумоподавления, а не дефект.", ""]
    lines += ["## Дефекты", ""]
    if rep["events"] is None:
        lines += ["Не искались: выравнивание не сошлось (%s)." % al.get("why"), ""]
    elif not rep["events"]:
        lines += ["Не найдено.", ""]
    else:
        lines += ["| время (мм:сс.мс) | длительность, мс | тип | глубина, дБ | слышно ли (оценка) | счётчики LyraT рядом | вырезка |",
                  "|---|---|---|---|---|---|---|"]
        for e in rep["events"]:
            pk = e.get("pkt_near")
            pk_s = "—" if not pk else ", ".join("%s %+d" % (k, v) for k, v in pk.items() if k != "span_s") or "0"
            depth = e["snr_drop_db"] if e["snr_drop_db"] is not None else e["level_db"]
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                stamp_of(e["t"]).replace(".", ":", 1), e["ms"], e["kind"], "—" if depth is None else depth, e["audible"],
                pk_s, e.get("cut", "—")))
        lines.append("")
    lines += ["Типы: **маскированный кадр** — телефон не получил кадр mSBC (7,5 мс) и подставил похожий звук: короткий щелчок "
              "или «металл», это и есть «обрыв вверх» из `round.py btloss`; **тишина** — цифровые нули; **провал** — уровень "
              "упал; **щелчок** — короткий лишний звук; **искажение** — дольше 30 мс; **сдвиг** — шкала времени сдвинулась "
              "на целый кадр: телефон получил лишний кадр или кадр пропал по дороге, звучит как маскированный кадр). «Слышно ли» — оценка по глубине, длительности и громкости места, не "
              "прослушивание.", "",
              "Счётчики LyraT (`pkt`): `rx_*` — приём от телефона (вниз), `tx_discarded` — кадры, которые плата не отправила. "
              "Потерю кадра в эфире на пути к телефону плата не видит, поэтому совпадение со счётчиками не ожидается.", ""]
    lines += ["## Выравнивание", "",
              "| сошлось | ясных окон | остаток, отсчёта | дрейф часов, ppm | сдвиги шкалы | смещение записи телефона, с |",
              "|---|---|---|---|---|---|",
              "| %s | %s из %s | %s | %s | %d | %s |" % ("да" if al["converged"] else "нет: " + str(al.get("why")), al["clear"],
                                                      al["windows"], al["residual"], al["ppm"], len(al["jumps"]),
                                                      round(al["coarse"] / RATE, 3) if al.get("coarse") is not None else "—"), ""]
    d = rep.get("detector") or {}
    if d:
        lines += ["Детектор: кадров %s, из них со сравнимым звуком %s %%, медиана точности предсказания %s дБ." % (
            d.get("frames"), d.get("active_pct"), d.get("snr_median_db")), "",
                  "В тихих местах (телефон получил только шум, который шумоподавление убрало) сравнивать нечего: потерянный "
                  "кадр там не виден и почти не слышен.", ""]
    return "\n".join(lines)


def analyse(out, ref, ph, expected=None, start_at=None, pkt_log=None, extra=None):
    """The whole comparison into the folder out. ref: the whole up line (float), ph: the phone recording."""
    out = Path(out)
    res = compare(ref, ph, expected)
    res["seconds"] = round((res.get("n1", 0) - res.get("n0", 0)) / RATE, 2)
    files = write_files(out, res) if "ref" in res else {}
    if res.get("events"):
        pkt_near(res["events"], pkt_log, None if start_at is None else start_at + res["n0"] / RATE)
    rep = {"summary": summary_line(res), "seconds": res["seconds"], "alignment": res["alignment"],
           "events": res.get("events"), "detector": res.get("detector"), "files": files,
           "ref_start_s": None if "n0" not in res else round(res["n0"] / RATE, 4), "pkt_reads": len(pkt_log or []),
           **(extra or {})}
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(render_md(rep), encoding="utf-8")
    (out / "alignment_local.json").write_text(json.dumps(res["local"]), encoding="utf-8")
    return rep


# --- the detector against tone_blocks ------------------------------------------------------------------------------------

def tone_reference(x, tone):
    """A clean tone fitted to the recording: frequency from the phase slope of 0.25 s pieces, amplitude and phase per piece,
    interpolated. A loss shorter than a piece barely moves the fit."""
    n = len(x)
    piece = RATE // 4
    t = np.arange(n) / RATE
    rows = []
    for a in range(0, n - piece + 1, piece):
        seg = x[a:a + piece]
        tt = t[a:a + piece]
        c, s = np.cos(2 * np.pi * tone * tt), np.sin(2 * np.pi * tone * tt)
        A = np.stack([c, s], 1)
        coef, *_ = np.linalg.lstsq(A, seg, rcond=None)
        amp = math.hypot(*coef)
        if amp > 100:
            rows.append((a + piece / 2.0, amp, math.atan2(-coef[1], coef[0])))
    if len(rows) < 4:
        return None
    centre = np.array([r[0] for r in rows]) / RATE
    phase = np.unwrap(np.array([r[2] for r in rows]))
    amp = np.array([r[1] for r in rows])
    keep = np.ones(len(centre), bool)
    for _ in range(3):
        slope, phi0 = np.polyfit(centre[keep], phase[keep], 1)
        resid = np.abs(phase - (phi0 + slope * centre))
        keep = resid <= max(0.05, 3 * np.median(resid))
    # One straight line of phase for the whole recording: the tone comes from the LyraT and the two clocks agree (D3), and
    # a loss leaves the phase after it intact (goal 062). Pieces with a loss in them do not bend the reference.
    ref = float(np.median(amp[keep])) * np.cos(2 * np.pi * tone * t + phi0 + slope * t)
    span = (int(rows[0][0] - piece / 2), int(rows[-1][0] + piece / 2))
    return ref, span, {"freq_hz": round(tone + slope / (2 * np.pi), 3)}


def tone_check(window_dir, tone=1800, tol_s=0.02):
    """The detector against the events tone_blocks found in the same phone.wav of a btloss window."""
    window_dir = Path(window_dir)
    x = read_wav(window_dir / "phone.wav")
    known = json.loads((window_dir / "events.json").read_text(encoding="utf-8")).get("up") or []
    fitted = tone_reference(x, tone)
    if fitted is None:
        return {"dir": str(window_dir), "error": "тон не найден"}
    ref, (a, b), info = fitted
    events, stats = detect(ref[a:b], x[a:b])
    # tone_blocks measures from the first block with sound + 1 s to the last - 1 s (EDGE_S): compare on that stretch only.
    blk = int(0.0075 * RATE)
    loud = np.where(np.sqrt((x[: len(x) // blk * blk].reshape(-1, blk) ** 2).mean(1)) >= 30.0)[0]
    lo, hi = (loud[0] * blk / RATE + 1.0, (loud[-1] + 1) * blk / RATE - 1.0) if len(loud) else (0.0, 0.0)
    found_all = [e["centre"] + a / RATE for e in events]
    found = [f for f in found_all if lo <= f <= hi]
    matched = sum(any(abs(f - (k["t"] + 0.0075 * k.get("blocks", 1) / 2.0)) <= tol_s + 0.0075 * k.get("blocks", 1) / 2.0
                      for f in found) for k in known)
    extra = [f for f in found if not any(abs(f - (k["t"] + 0.0075 * k.get("blocks", 1) / 2.0)) <= tol_s + 0.0075 * k.get("blocks", 1) / 2.0
                                         for k in known)]
    return {"dir": str(window_dir), "tone": info, "measured_s": [round(lo, 3), round(hi, 3)],
            "outside_measured": [round(f, 3) for f in found_all if not lo <= f <= hi],
            "btloss_events": len(known), "found": len(found), "matched": matched,
            "extra": len(extra), "extra_at": [round(v, 3) for v in extra], "btloss_at": [round(k["t"], 3) for k in known],
            "detector": stats}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?")
    ap.add_argument("--ref")
    ap.add_argument("--phone")
    ap.add_argument("--out")
    ap.add_argument("--tone-check", nargs="+", metavar="WINDOW_DIR")
    ap.add_argument("--tone", type=float, default=1800.0)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")      # Russian lines into a pipe or a log on Windows
        except (AttributeError, ValueError):
            pass
    if args.tone_check:
        rows = [tone_check(d, args.tone) for d in args.tone_check]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if args.ref and args.phone:
        out = Path(args.out or ".")
        out.mkdir(parents=True, exist_ok=True)
        rep = analyse(out, read_wav(args.ref, any_rate=True), read_wav(args.phone, any_rate=True))
    elif args.dir:
        d = Path(args.dir)
        meta = json.loads((d / "compare.json").read_text(encoding="utf-8"))
        rep = analyse(d, read_wav(d / "up_full.wav"), read_wav(d / "phone_raw.wav"), meta.get("expected"),
                      meta.get("start_at"), meta.get("pkt_log"), {k: meta[k] for k in ("run",) if k in meta})
    else:
        ap.error("DIR, --ref/--phone or --tone-check")
    print(rep["summary"])
    return 0 if rep["events"] is not None else 2


if __name__ == "__main__":
    sys.exit(main())
