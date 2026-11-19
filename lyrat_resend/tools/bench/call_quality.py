#!/usr/bin/env python3
"""Quality numbers of a recorded call line and the verdict of a call, for call_check.py. numpy only, no bench.

line_metrics(x, gaps=None) for one line, 16 kHz int16 mono; gaps: [start, length] of silence the recorder put in place
of packets lost on the way from the S3 (counted as stream losses, never as drop-outs of the sound):
  seconds, peak_dbfs, rms_dbfs (over active 20 ms windows), floor_dbfs and floor_rms (10th percentile of the 100 ms
  level, windows of pure zeros left out: the noise in quiet moments), dynamic (loud windows 6 dB and more over the floor:
  a line with pauses; a steady tone or hum is not, and has no snr_db), active_pct, snr_db (active minus floor),
  silent (the line carries nothing: peak below SILENT_PEAK), clip_samples, clip_pct, clip_runs (3+ samples in a row at
  full scale), dropouts and dropout_ms (digital zero runs of at least one mSBC frame, 7.5 ms, with sound on both sides,
  outside the gaps: what a lost Bluetooth frame leaves), dropouts_per_min.
verdict(report) -> (ok, problems): plain Russian lines; "качество: норм" only with no problem.
python call_quality.py FILE.wav prints line_metrics of each channel.
"""
import json
import math
import sys
import wave

import numpy as np

RATE = 16000
FULL = 32768.0
SILENT_PEAK = 64                 # about -54 dBFS: below this a line carries nothing
CLIP_LEVEL = 32700
CLIP_RUN = 3
DROPOUT_MIN = 120                # one mSBC frame, 7.5 ms
DROPOUT_CONTEXT = 160            # 10 ms of sound wanted on both sides of a zero run
ACTIVE_OVER_FLOOR_DB = 12.0
ACTIVE_MIN_DBFS = -55.0
WIN = 320                        # 20 ms
FLOOR_WIN = 1600                 # 100 ms

# Limits of the verdict
MAX_LOSS_PCT = 2.0
MAX_DROPOUTS_PER_MIN = 6.0       # a few lost frames a minute are heard as rare clicks; more is a fault
MAX_CLIP_PCT = 0.1
MIN_SNR_DB = 15.0
MIN_ACTIVE_DBFS = -50.0
MIC_FLOOR_WARN = 400.0           # udp_record.MIC_FLOOR_WARN, #005 of goal 057
MAX_LATENCY_MS = 300.0
WANT_CODEC = "mSBC"


def db(v):
    return round(20 * math.log10(max(v, 1e-9) / FULL), 1)


def _runs(mask):
    """[start, length] of the True runs of a bool array."""
    if not mask.size:
        return []
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return [[int(a), int(b - a)] for a, b in zip(starts, ends)]


def line_metrics(x, gaps=None, rate=RATE):
    x = np.asarray(x, dtype=np.int16)
    n = len(x)
    out = {"seconds": round(n / rate, 3)}
    if not n:
        out.update(silent=True, dynamic=False, peak_dbfs=None, rms_dbfs=None, floor_dbfs=None, floor_rms=None, active_pct=0.0, snr_db=None,
                   clip_samples=0, clip_pct=0.0, clip_runs=0, dropouts=0, dropout_ms=0.0, dropouts_per_min=0.0)
        return out
    f = x.astype(np.float64)
    gap_mask = np.zeros(n, dtype=bool)
    for start, length in gaps or ():
        gap_mask[max(start, 0):max(start + length, 0)] = True
    peak = float(np.abs(f).max())
    out["peak_dbfs"] = db(peak)
    out["silent"] = peak < SILENT_PEAK
    # The floor: quiet moments of the line; windows of pure zeros are losses or a stopped line, not noise.
    m = n // FLOOR_WIN
    fl = np.sqrt((f[:m * FLOOR_WIN].reshape(m, FLOOR_WIN) ** 2).mean(axis=1)) if m else np.array([])
    fl = fl[fl > 0]
    floor = float(np.percentile(fl, 10)) if fl.size else 0.0
    out["floor_rms"] = round(floor, 1)
    out["floor_dbfs"] = db(floor) if floor else None
    k = n // WIN
    w = np.sqrt((f[:k * WIN].reshape(k, WIN) ** 2).mean(axis=1)) if k else np.array([])
    # Speech has pauses: its loud windows stand well above the floor. A steady sound (a tone, music, hum) does not, and
    # then its floor is the sound itself: everything above the minimum level is active and there is no SNR to tell.
    p90 = float(np.percentile(w, 90)) if k else 0.0
    dynamic = floor > 0 and p90 >= floor * 2
    out["dynamic"] = bool(dynamic)
    threshold = max(min(floor * 10 ** (ACTIVE_OVER_FLOOR_DB / 20), p90 / 2), FULL * 10 ** (ACTIVE_MIN_DBFS / 20))
    active = w > threshold
    out["active_pct"] = round(100.0 * active.mean(), 1) if k else 0.0
    rms_active = float(np.sqrt((w[active] ** 2).mean())) if active.any() else 0.0
    out["rms_dbfs"] = db(rms_active) if rms_active else None
    out["snr_db"] = round(out["rms_dbfs"] - out["floor_dbfs"], 1) if dynamic and out["rms_dbfs"] is not None else None
    clip = np.abs(f) >= CLIP_LEVEL
    out["clip_samples"] = int(clip.sum())
    out["clip_pct"] = round(100.0 * clip.mean(), 4)
    out["clip_runs"] = sum(1 for _, length in _runs(clip) if length >= CLIP_RUN)
    zero = (x == 0) & ~gap_mask
    drops = []
    for start, length in _runs(zero):
        if length < DROPOUT_MIN:
            continue
        before = f[max(start - DROPOUT_CONTEXT, 0):start]
        after = f[start + length:start + length + DROPOUT_CONTEXT]
        # Sound on both sides: a lost frame in the middle of sound, not the start or end of a quiet stretch.
        if before.size == DROPOUT_CONTEXT and after.size == DROPOUT_CONTEXT and \
                np.sqrt((before ** 2).mean()) > threshold and np.sqrt((after ** 2).mean()) > threshold:
            drops.append(length)
    out["dropouts"] = len(drops)
    out["dropout_ms"] = round(1000.0 * sum(drops) / rate, 1)
    out["dropouts_per_min"] = round(len(drops) / (n / rate / 60.0), 2)
    return out


def verdict(r):
    """Problems of a call report (call_check.build_report), each a line for a person."""
    p = []
    lines = r.get("lines") or {}
    names = {"down": "собеседник (вниз, телефон -> наушники)", "up": "ваш микрофон (вверх, к телефону)"}
    if not r.get("sco", {}).get("intervals"):
        p.append("канал гарнитуры (SCO) не открывался: разговора в записи нет. На телефоне выбрана гарнитура LyraT_Phone?")
    for key in ("down", "up"):
        m = lines.get(key)
        if not m:
            continue
        name = names[key]
        if key == "up" and (r.get("mute") or {}).get("whole"):
            continue            # goal 111: the owner kept the mic off the whole call: its silence is no defect
        if m.get("silent"):
            p.append("%s: линия молчит, в записи нет звука." % name)
            continue
        if m.get("clip_pct", 0) > MAX_CLIP_PCT:
            p.append("%s: перегруз (клиппинг) %.2f %% отсчётов, %d участков." % (name, m["clip_pct"], m["clip_runs"]))
        if m.get("dropouts_per_min", 0) > MAX_DROPOUTS_PER_MIN:
            p.append("%s: выпадения звука %.1f в минуту (%d за запись), слышны как щелчки и пропуски." % (
                name, m["dropouts_per_min"], m["dropouts"]))
        if m.get("active_pct", 0) > 1 and m.get("rms_dbfs") is not None and m["rms_dbfs"] < MIN_ACTIVE_DBFS:
            p.append("%s: очень тихо, %.0f дБFS в речи." % (name, m["rms_dbfs"]))
        if m.get("active_pct", 0) > 1 and m.get("snr_db") is not None and m["snr_db"] < MIN_SNR_DB:
            p.append("%s: шумно, речь выше фона всего на %.0f дБ." % (name, m["snr_db"]))
    # The mic's noise is judged per ADC channel, the measure and limit of udp_record (#005 of goal 057/061).
    floors = ((r.get("stream") or {}).get("up") or {}).get("floor_rms_channels") or []
    if any(f is not None and f > MIC_FLOOR_WARN for f in floors):
        p.append("шум на линии микрофона: фон %s при пределе %.0f (#005 гоала 057)." % (floors, MIC_FLOOR_WARN))
    for key, s in (r.get("stream") or {}).items():
        if not isinstance(s, dict):
            continue
        if s.get("loss_pct") is not None and s["loss_pct"] > MAX_LOSS_PCT:
            p.append("поток S3 -> ПК (%s): потери %.2f %% при пределе %.1f %%." % (key, s["loss_pct"], MAX_LOSS_PCT))
        if s.get("ovf"):
            p.append("S3 теряла данные шины (%s): ovf %d." % (key, s["ovf"]))
    ev = r.get("events") or {}
    for k, text in (("sco_breaks", "канал гарнитуры рвался посреди записи %d раз"), ("slc_drops", "связь с телефоном обрывалась %d раз"),
                    ("lyrat_resets", "LyraT перезагружалась %d раз"), ("s3_restarts", "S3 перезагружалась %d раз"),
                    ("stream_silences", "поток с S3 замолкал %d раз")):
        if ev.get(k):
            p.append((text % ev[k]) + ".")
    codec = r.get("sco", {}).get("codec")
    if r.get("sco", {}).get("intervals") and codec and codec != WANT_CODEC:
        p.append("кодек %s вместо %s: узкая полоса, качество хуже." % (codec, WANT_CODEC))
    lat = (r.get("latency") or {}).get("pipeline") or {}
    if lat.get("median_ms") is not None and lat["median_ms"] > MAX_LATENCY_MS:
        p.append("задержка живого потока %.0f мс при цели %.0f мс." % (lat["median_ms"], MAX_LATENCY_MS))
    return not p, p


def verdict_line(ok, problems):
    return "качество: норм" if ok else "качество: есть проблемы: " + "; ".join(problems)


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        ch, rate = w.getnchannels(), w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return data.reshape(-1, ch) if ch > 1 else data.reshape(-1, 1), rate


def main():
    data, rate = read_wav(sys.argv[1])
    print(json.dumps({"channel_%d" % c: line_metrics(data[:, c], rate=rate) for c in range(data.shape[1])}, indent=2))


if __name__ == "__main__":
    main()
