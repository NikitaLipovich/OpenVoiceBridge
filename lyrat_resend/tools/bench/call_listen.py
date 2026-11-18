#!/usr/bin/env python3
"""The live call cut into utterances: who spoke when, a file per utterance, a hook for a speech recogniser (goal 101).

  call_listen.py [--port 5010] [--out DIR] [--seconds N] [--follow] [--wait S] [--hang-ms 450] [--no-wav]
                 [--stt package.module:Class | file.py:Class] [--stt-arg key=value ...]
  call_listen.py --file both.wav [--out DIR]      the same over a stereo WAV of call_check (left down, right up)

It reads the local stream of call_check.py / call_stream.py serve (TCP 127.0.0.1, format callpcm v1: a JSON hello line,
then blocks of 20 ms, stereo s16le, left the other party = "down", right the headset mic = "up"; see call_stream.py) and,
for each direction on its own, in real time:
  - a voice activity detector (VAD) on 20 ms frames: the level of the 150-4000 Hz band against a background that follows
    the line (fast down, slowly up, faster for noise-like frames), and the spectral flatness of the band (noise is flat,
    voice and tones are not); two thresholds (start / hold) and a hangover make the borders of utterances;
  - timeline.jsonl: a line per finished utterance (kind "utt": n, dir, session, start, end, dur in seconds of the
    stream, wall_start / wall_end in epoch seconds, level_db, peak_db, reason, lost_ms, breaks, file, onset_lag_ms,
    end_lag_ms, onset_decision_ms, end_decision_ms) and a line per connection (kind "session");
  - utterances/<NNNN>_<dir>.wav, mono 16 kHz, 0.1 s of margin each side;
  - a live line: who speaks and how long, utterances so far, the delay of the last decision, losses;
  - SttBackend: begin(utt) / feed(pcm, ts) / finish() -> SttResult(text, segments with times) / cancel(); NullStt is the
    stub. A backend runs in its own thread per direction: a slow engine never holds the VAD back.
  - summary.json: utterances and speech time per direction, overlap, latencies, losses, CPU.

Borders (DECISIONS D1 of goal 101): an utterance starts when 3 of the last 5 frames are voice, at the first of them; it
ends after 0.45 s (--hang-ms) without a frame that holds, at the end of the last one; shorter than 8 holding frames it is
dropped (a click); longer than 30 s it is cut without a hole; a second of only weak frames (no voice) ends it as "noise"
(the background rose faster than its estimate). Missing sound (flags of callpcm, or blocks this client lost) is neither
voice nor pause: up to 0.3 s inside an utterance is kept as zeros, more ends it ("loss"). A dropped connection ends the
open utterances ("disconnect"); with --follow the listener connects again and counts a new session.

Latency: onset_lag_ms / end_lag_ms are the algorithmic part, in stream time (the end of the block that decided minus the
border); onset_decision_ms / end_decision_ms are the whole way, the PC time of the decision minus t_sound of the border
sample (sound on the I2S bus, goal 097). proc_ms in the summary is the time from a block read to its VAD decision.

Hooking a recogniser later (nothing is installed here): write a class with the four methods of SttBackend, constructed
as Class(direction, **stt_args), and pass --stt. Sketches, with the engine's own API:
  Vosk:            begin: self.rec = vosk.KaldiRecognizer(model, 16000); feed: self.rec.AcceptWaveform(pcm.tobytes());
                   finish: json.loads(self.rec.FinalResult()) -> text, words with start/end (+ utt.start).
  faster-whisper:  feed: collect pcm; finish: model.transcribe(np.concatenate(chunks) / 32768.0) -> segments.
  whisper.cpp:     finish: write the samples to a WAV, run whisper-cli -f file -oj, read the JSON.
The times a backend gives are relative to its first fed sample; add ts of that sample to put them on the stream's time.

Files are written by their own thread (DiskWriter), so a slow disk never holds the VAD. load_pct_of_audio in the summary
is the VAD's own time over the sound's; cpu_pct_of_audio (process_time) is only a hint on Windows, which charges CPU by
timer ticks. Nothing here plays sound (R15). The recordings are the owner's conversation: they go to goals_harness/bench_runs/listen
(outside git, R6). As a module: Listener(out).block(index, frame, flags, t_sound, pcm, now) per block, run_socket(),
run_file(). Made by goal 101.
"""
import argparse
import collections
import importlib
import importlib.util
import json
import math
import queue
import socket
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import call_stream as cs  # noqa: E402

RATE = cs.RATE
BLOCK = cs.OUT_BLOCK                      # 320 frames, 20 ms
FRAME_S = BLOCK / RATE
DIRS = ("down", "up")                     # channel 0, channel 1 of callpcm
FULL = 32768.0
REPO = Path(__file__).resolve().parents[3]
RUNS = REPO / "goals_harness" / "bench_runs" / "listen"

_WIN = np.hanning(BLOCK)
_FREQS = np.fft.rfftfreq(BLOCK, 1.0 / RATE)


@dataclass
class Config:
    band: tuple = (150.0, 4000.0)         # Hz: the band of voice the level and the flatness are taken over
    on_db: float = 10.0                   # a voice frame: this far over the background ...
    on_abs_db: float = -65.0              # ... and at least this loud
    flat_max: float = 0.45                # ... and not flat like noise
    strong_db: float = 18.0               # ... unless this far over the background
    hold_db: float = 6.0                  # a frame that holds an utterance open
    hold_abs_db: float = -70.0
    start_need: int = 3                   # voice frames ...
    start_window: int = 5                 # ... out of the last frames: an utterance starts
    hang_s: float = 0.45                  # without a holding frame for this long: the utterance ended
    min_hold_frames: int = 8              # fewer holding frames: a click, dropped
    max_s: float = 30.0                   # an utterance this long is cut
    pad_s: float = 0.1                    # margin of the files
    loss_max_s: float = 0.3               # missing sound inside an utterance beyond this ends it
    gap_max_s: float = 2.0                # a jump of the stream beyond this (or back) ends the utterances at once
    floor_fall: float = 0.3               # share of the step toward a lower level per frame
    floor_rise_noise_dbps: float = 10.0   # dB per second up for a noise-like frame
    floor_rise_dbps: float = 0.5          # dB per second up for any other frame
    floor_min_db: float = -96.0
    floor_init_frames: int = 5
    floor_init_max_db: float = -55.0      # the first background is at most this: a stream may start in the middle of speech
    voice_gap_s: float = 1.0              # holding frames but no voice frame for this long: noise rising, not speech
    stt_backlog_s: float = 60.0           # sound waiting for the recogniser beyond this: new utterances skip it

    @property
    def hang_frames(self):
        return max(1, int(round(self.hang_s / FRAME_S)))


# --- the recogniser interface ---------------------------------------------------------------------------------------------

@dataclass
class SttResult:
    text: str = ""
    segments: list = field(default_factory=list)    # [(t0, t1, text)] in seconds of the stream
    partial: bool = False


class SttBackend:
    """A speech recogniser fed one utterance at a time. Constructed as Class(direction, **args); all calls come from one
    thread per direction, in order: begin, feed..., then finish or cancel."""

    def __init__(self, direction=None, **args):
        self.direction = direction
        self.args = args

    def begin(self, utt):
        """utt: dict with n, dir, session, start (seconds of the stream)."""

    def feed(self, pcm, ts):
        """pcm: int16 mono 16 kHz; ts: stream time of its first sample. May return a partial SttResult."""
        return None

    def finish(self):
        return SttResult()

    def cancel(self):
        """The utterance was dropped (too short): forget it."""
        self.finish()


class NullStt(SttBackend):
    """The stub: takes the sound, gives no text."""


def load_backend(spec, args=None):
    """--stt "package.module:Class" or "path/file.py:Class" -> a factory direction -> backend."""
    if spec in (None, "", "null"):
        return lambda d: NullStt(d)
    mod_name, _, cls_name = spec.rpartition(":")
    if not mod_name or not cls_name:
        raise ValueError("--stt wants module:Class, got %r" % spec)
    if mod_name.endswith(".py") or "/" in mod_name or "\\" in mod_name:
        path = Path(mod_name).resolve()
        loader = importlib.util.spec_from_file_location("stt_" + path.stem, path)
        if loader is None:
            raise ValueError("cannot load %s" % path)
        mod = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(mod)
    else:
        mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    args = dict(args or {})
    return lambda d: cls(d, **args)


class SttWorker:
    """One direction's recogniser in its own thread; the VAD only puts work into its queue."""

    def __init__(self, backend, cfg, on_result):
        self.backend, self.cfg, self.on_result = backend, cfg, on_result
        self.q = queue.Queue()
        self.backlog_s = 0.0
        self.lock = threading.Lock()
        self.errors = self.dropped = self.done = 0
        self.active = None                 # the utterance being fed, or None when skipped
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def begin(self, utt):
        with self.lock:
            over = self.backlog_s > self.cfg.stt_backlog_s
        if over:
            self.dropped += 1
            self.active = None
            return False
        self.active = dict(utt)
        self.q.put(("begin", self.active))
        return True

    def feed(self, pcm, ts):
        if self.active is None:
            return
        with self.lock:
            self.backlog_s += len(pcm) / RATE
        self.q.put(("feed", pcm, ts))

    def end(self, keep, info=None):
        if self.active is None:
            return
        self.q.put(("finish" if keep else "cancel", dict(self.active, **(info or {}))))
        self.active = None

    def close(self, timeout=5.0):
        self.q.put(None)
        self.thread.join(timeout)

    def _run(self):
        broken = False                     # after an error, skip to the next utterance
        cur = {}
        while True:
            item = self.q.get()
            if item is None:
                return
            kind = item[0]
            try:
                if kind == "begin":
                    broken = False
                    cur = item[1]
                    self.backend.begin(dict(cur))
                elif kind == "feed":
                    with self.lock:
                        self.backlog_s -= len(item[1]) / RATE
                    if not broken:
                        part = self.backend.feed(item[1], item[2])
                        if part is not None and getattr(part, "text", ""):
                            self.on_result(cur, part, partial=True)
                elif kind == "finish":
                    if not broken:
                        res = self.backend.finish()
                        self.done += 1
                        self.on_result(item[1], res if res is not None else SttResult())
                elif kind == "cancel":
                    if not broken:
                        self.backend.cancel()
            except Exception:              # a recogniser's fault must not stop the listener  # noqa: BLE001
                self.errors += 1
                broken = True


# --- one direction --------------------------------------------------------------------------------------------------------

class Utterance:
    def __init__(self, n, direction, session, start, origin):
        self.n, self.dir, self.session = n, direction, session
        self.start = start                 # session frame of the border
        self.origin = origin               # session frame of the first sample kept (start minus the margin)
        self.chunks = []                   # contiguous blocks from origin: (frame, pcm int16, missing bool)
        self.last_hold_end = start
        self.last_voice_end = start
        self.holds = 0
        self.missing_run = 0
        self.lost_frames = 0
        self.breaks = 0
        self.silent_run = 0
        self.onset_lag_ms = None
        self.onset_decision_ms = None


class Direction:
    """VAD and borders of one line. Frames come in order; a missing frame is passed with missing=True."""

    def __init__(self, name, cfg, owner):
        self.name, self.cfg, self.owner = name, cfg, owner
        self.ring = collections.deque(maxlen=max(2, int(math.ceil(0.5 / FRAME_S))))
        self.recent = collections.deque(maxlen=cfg.start_window)
        self.floor = None
        self.init_levels = []
        self.utt = None
        self.frames = self.voice_frames = 0
        self.lo, self.hi = cfg.band
        self.band = (_FREQS >= self.lo) & (_FREQS <= self.hi)

    # features
    def features(self, x):
        xf = x.astype(np.float64)
        if not xf.any():
            return -math.inf, 1.0
        spec = np.abs(np.fft.rfft((xf - xf.mean()) * _WIN)) ** 2
        p = spec[self.band]
        # Parseval with the window: the band's share of the frame's mean square
        ms = p.sum() * 2.0 / (BLOCK * (_WIN ** 2).sum())
        level = 10 * math.log10(max(ms, 1e-12) / FULL ** 2)
        pm = p + 1e-9
        flat = float(math.exp(np.mean(np.log(pm))) / np.mean(pm))
        return level, flat

    def classify(self, level, flat):
        f = self.floor if self.floor is not None else level
        over = level - f
        voice = level >= self.cfg.on_abs_db and over >= self.cfg.on_db and (flat < self.cfg.flat_max or over >= self.cfg.strong_db)
        hold = voice or (level >= self.cfg.hold_abs_db and over >= self.cfg.hold_db)
        return "voice" if voice else "hold" if hold else "quiet"

    def update_floor(self, level, flat):
        if level == -math.inf:
            level = self.cfg.floor_min_db
        if self.floor is None:
            self.init_levels.append(level)
            if len(self.init_levels) >= self.cfg.floor_init_frames:
                self.floor = min(max(min(self.init_levels), self.cfg.floor_min_db), self.cfg.floor_init_max_db)
            return
        if level < self.floor:
            self.floor += self.cfg.floor_fall * (level - self.floor)
        else:
            rate = self.cfg.floor_rise_noise_dbps if flat >= self.cfg.flat_max else self.cfg.floor_rise_dbps
            self.floor = min(level, self.floor + rate * FRAME_S)
        self.floor = max(self.floor, self.cfg.floor_min_db)

    # frames
    def frame(self, f, x, missing, brk, now):
        """One 20 ms frame at session frame f. Returns its kind."""
        self.frames += 1
        if missing:
            kind, level, flat = "unknown", None, None
        else:
            level, flat = self.features(x)
            if self.floor is None:
                kind = "quiet"             # the background is not known yet
            else:
                kind = self.classify(level, flat)
            self.update_floor(level, flat)
        self.voice_frames += kind == "voice"
        self.ring.append((f, x, missing))
        self.recent.append((f, kind))
        if self.utt is None:
            voiced = [g for g, k in self.recent if k == "voice"]
            if len(voiced) >= self.cfg.start_need:
                self._start(voiced[0], now, f)
            return kind
        u = self.utt
        u.chunks.append((f, x, missing))
        if brk:
            u.breaks += 1
        self.owner.stt_feed(self.name, x, f)
        if kind == "voice":
            u.last_voice_end = f + BLOCK
        if kind in ("voice", "hold"):
            u.last_hold_end = f + BLOCK
            u.holds += 1
            u.silent_run = u.missing_run = 0
        elif kind == "quiet":
            u.silent_run += 1
            u.missing_run = 0
        else:
            u.missing_run += 1
            u.lost_frames += 1
            if u.missing_run * FRAME_S > self.cfg.loss_max_s + 1e-9:
                self._end("loss", now, f + BLOCK, pad=False)
                return kind
        if u.silent_run >= self.cfg.hang_frames:
            self._end("pause", now, f + BLOCK)
        elif f + BLOCK - u.last_voice_end > self.cfg.voice_gap_s * RATE:
            # only weak frames for a second: the background rose under a level the floor has not caught up with yet
            self._end("noise", now, f + BLOCK,
                      cut_at=min(u.last_hold_end, u.last_voice_end + self.cfg.hang_frames * BLOCK))
        elif f + BLOCK - u.start >= self.cfg.max_s * RATE:
            # cut and go on at once, pause or not: the next part ends by the usual rules, or is dropped if nothing follows
            self._end("max", now, f + BLOCK, pad=False, cut_at=f + BLOCK)
            self._start(f + BLOCK, now, f, carry=True)
        return kind

    def _start(self, start, now, f_now, carry=False):
        self.owner.counter += 1
        u = Utterance(self.owner.counter, self.name, self.owner.session, start, start)
        if not carry:
            origin = max(start - int(self.cfg.pad_s * RATE), self.ring[0][0])
            u.origin = origin
            for g, x, missing in self.ring:
                if g + BLOCK > origin:
                    u.chunks.append((g, x, missing))
            u.origin = u.chunks[0][0] if u.chunks else start
            for g, k in self.recent:
                if g >= start and k in ("voice", "hold"):
                    u.holds += 1
                    u.last_hold_end = g + BLOCK
        if carry:
            u.last_voice_end = u.last_hold_end = start
        decided = f_now + BLOCK
        u.onset_lag_ms = round((decided - start) / RATE * 1000, 1)
        t = self.owner.t_of(start)
        u.onset_decision_ms = round((now - t) * 1000, 1) if t is not None and now is not None else None
        self.utt = u
        self.recent.clear()
        self.owner.on_start(u)
        for g, x, _ in u.chunks:
            self.owner.stt_feed(self.name, x, g)

    def _end(self, reason, now, decided, pad=True, cut_at=None):
        u = self.utt
        self.utt = None
        self.recent.clear()
        end = cut_at if cut_at is not None else u.last_hold_end
        file_end = end + (int(self.cfg.pad_s * RATE) if pad else 0)
        self.owner.on_end(u, end, file_end, reason, now, decided)

    def close(self, reason, now):
        """End the open utterance, if any, at its last holding frame (the connection or the stream ended)."""
        if self.utt is not None:
            last = self.utt.chunks[-1][0] + BLOCK if self.utt.chunks else self.utt.start
            self._end(reason, now, last, pad=False)

    def reset(self):
        self.ring.clear()
        self.recent.clear()


# --- both directions, the outputs -----------------------------------------------------------------------------------------

class DiskWriter:
    """Files are written in their own thread: a slow disk (or a virus scanner on a new WAV) never holds the VAD."""

    def __init__(self):
        self.q = queue.Queue()
        self.errors = 0
        self.files = {}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def line(self, path, text):
        self.q.put(("line", path, text))

    def wav(self, path, pcm):
        self.q.put(("wav", path, pcm))

    def flush(self, timeout=10.0):
        done = threading.Event()
        self.q.put(("mark", done))
        done.wait(timeout)

    def close(self):
        self.flush()
        self.q.put(None)
        self.thread.join(5)

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                for f in self.files.values():
                    f.close()
                return
            try:
                if item[0] == "mark":
                    item[1].set()
                elif item[0] == "line":
                    f = self.files.get(item[1])
                    if f is None:
                        f = self.files[item[1]] = open(item[1], "a", encoding="utf-8")
                    f.write(item[2] + "\n")
                    f.flush()
                else:
                    with wave.open(str(item[1]), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(RATE)
                        w.writeframes(item[2].astype("<i2").tobytes())
            except OSError:
                self.errors += 1


class Listener:
    def __init__(self, out=None, cfg=None, stt_factory=None, wav=True, on_event=None, say=None):
        self.cfg = cfg or Config()
        self.out = Path(out) if out else None
        self.wav = wav and self.out is not None
        if self.out:
            self.out.mkdir(parents=True, exist_ok=True)
            if self.wav:
                (self.out / "utterances").mkdir(exist_ok=True)
        self.disk = DiskWriter()
        self.on_event = on_event
        self.say = say or (lambda s: None)
        self.dirs = {d: Direction(d, self.cfg, self) for d in DIRS}
        self.stt = {d: SttWorker((stt_factory or (lambda d_: NullStt(d_)))(d), self.cfg, self._stt_result) for d in DIRS}
        self.transcript = []
        self.tlock = threading.Lock()
        self.counter = 0
        self.session = 0
        self.utts = []                     # finished, kept
        self.dropped_short = 0
        self.expect = None                 # next session frame
        self.last_index = None
        self.clock_ref = None              # (frame, t_sound) of the last block with a clock
        self.epoch_offset = time.time() - time.monotonic()
        self.stats = {"blocks": 0, "frames_missing": {d: 0 for d in DIRS}, "index_gaps": 0, "frames_gap": 0,
                      "jumps": 0, "breaks": 0, "sessions": 0, "proc_ms": [], "proc_total_s": 0.0}
        self.last_border = None            # (dir, kind, decision_ms or lag)
        self.cpu0 = time.process_time()
        self.audio_s = 0.0

    # time
    def t_of(self, frame):
        if self.clock_ref is None:
            return None
        f0, t0 = self.clock_ref
        return t0 + (frame - f0) / RATE

    def wall_of(self, frame):
        t = self.t_of(frame)
        return round(t + self.epoch_offset, 3) if t is not None else None

    # sessions
    def begin_session(self, hello=None, now=None):
        self.session += 1
        self.stats["sessions"] += 1
        self.expect = None
        self.last_index = None
        self.clock_ref = None
        for d in self.dirs.values():
            d.reset()
        self._write({"kind": "session", "session": self.session, "event": "connect",
                     "wall": round(time.time(), 3), "hello": hello})

    def end_session(self, reason, now=None):
        for d in self.dirs.values():
            d.close(reason, now)
        self._write({"kind": "session", "session": self.session, "event": reason, "wall": round(time.time(), 3)})
        self.disk.flush()                  # not in the block path: the session is over, its files are on disk now

    # blocks
    def block(self, index, frame, flags, t_sound, pcm, now=None):
        """One callpcm block: pcm int16 (320, 2). now: time.monotonic of its arrival (tests give their own)."""
        t_in = time.perf_counter()
        if now is None:
            now = time.monotonic()
        self.stats["blocks"] += 1
        if t_sound is not None and not math.isnan(t_sound):
            self.clock_ref = (frame, t_sound)
        if self.last_index is not None and index != self.last_index + 1:
            self.stats["index_gaps"] += 1
        self.last_index = index
        if self.expect is not None and frame != self.expect:
            gap = frame - self.expect
            if gap < 0 or gap > self.cfg.gap_max_s * RATE:
                self.stats["jumps"] += 1
                for d in self.dirs.values():
                    d.close("gap", now)
                    d.reset()
            else:
                self.stats["frames_gap"] += gap
                zero = np.zeros(BLOCK, dtype=np.int16)
                for g in range(self.expect, frame - BLOCK + 1, BLOCK):
                    for name, d in self.dirs.items():
                        self.stats["frames_missing"][name] += BLOCK
                        d.frame(g, zero, True, False, now)
        brk = bool(flags & cs.F_BREAK)
        self.stats["breaks"] += brk
        for c, (name, bit) in enumerate(zip(DIRS, (cs.F_NO_DOWN, cs.F_NO_UP))):
            missing = bool(flags & bit)
            if missing:
                self.stats["frames_missing"][name] += BLOCK
            self.dirs[name].frame(frame, np.ascontiguousarray(pcm[:, c]), missing, brk, now)
        self.expect = frame + BLOCK
        self.audio_s += FRAME_S
        took = time.perf_counter() - t_in
        self.stats["proc_total_s"] += took
        self.stats["proc_ms"].append(took * 1000)
        if len(self.stats["proc_ms"]) > 200000:
            del self.stats["proc_ms"][:100000]

    # events of the directions
    def stt_feed(self, name, x, frame):
        self.stt[name].feed(x, frame / RATE)

    def on_start(self, u):
        self.stt[u.dir].begin({"n": u.n, "dir": u.dir, "session": u.session, "start": round(u.start / RATE, 3)})
        self.last_border = (u.dir, "start", u.onset_decision_ms if u.onset_decision_ms is not None else u.onset_lag_ms)
        if self.on_event:
            self.on_event("start", u, None)

    def on_end(self, u, end, file_end, reason, now, decided):
        end_lag = round((decided - end) / RATE * 1000, 1)
        t_end = self.t_of(end)
        end_dec = round((now - t_end) * 1000, 1) if t_end is not None and now is not None else None
        self.last_border = (u.dir, "end", end_dec if end_dec is not None else end_lag)
        if u.holds < self.cfg.min_hold_frames:
            self.dropped_short += 1
            self.stt[u.dir].end(False)
            if self.on_event:
                self.on_event("drop", u, None)
            return
        pcm, mask = self._assemble(u, file_end)
        a, b = int(u.start - u.origin), int(end - u.origin)
        body, ok = pcm[a:b].astype(np.float64), mask[a:b]
        voiced = body[ok]
        rms = math.sqrt(float((voiced ** 2).mean())) if len(voiced) else 0.0
        peak = float(np.abs(voiced).max()) if len(voiced) else 0.0
        rec = {"kind": "utt", "n": u.n, "dir": u.dir, "session": u.session,
               "start": round(u.start / RATE, 3), "end": round(end / RATE, 3), "dur": round((end - u.start) / RATE, 3),
               "wall_start": self.wall_of(u.start), "wall_end": self.wall_of(end),
               "level_db": round(20 * math.log10(max(rms, 1e-9) / FULL), 1),
               "peak_db": round(20 * math.log10(max(peak, 1e-9) / FULL), 1),
               "reason": reason, "lost_ms": round(u.lost_frames * FRAME_S * 1000), "breaks": u.breaks,
               "onset_lag_ms": u.onset_lag_ms, "end_lag_ms": end_lag,
               "onset_decision_ms": u.onset_decision_ms, "end_decision_ms": end_dec, "file": None,
               "file_start": round(u.origin / RATE, 3)}
        if self.wav:
            name = "%04d_%s.wav" % (u.n, u.dir)
            self.disk.wav(self.out / "utterances" / name, pcm)
            rec["file"] = "utterances/" + name
        self.utts.append(rec)
        self._write(rec)
        self.stt[u.dir].end(True, {"end": rec["end"]})
        if self.on_event:
            self.on_event("end", u, rec)

    def _assemble(self, u, file_end):
        parts, masks = [], []
        for g, x, missing in u.chunks:
            if g >= file_end:
                break
            take = min(BLOCK, file_end - g)
            parts.append(x[:take])
            masks.append(np.full(take, not missing))
        if not parts:
            return np.zeros(0, dtype=np.int16), np.zeros(0, dtype=bool)
        return np.concatenate(parts), np.concatenate(masks)

    def _stt_result(self, utt, res, partial=False):
        if partial:
            return
        rec = {"kind": "stt", "n": utt.get("n"), "dir": utt.get("dir"), "session": utt.get("session"),
               "start": utt.get("start"), "end": utt.get("end"), "text": res.text,
               "segments": [list(s) for s in (res.segments or [])]}
        with self.tlock:
            self.transcript.append(rec)
            if self.out and res.text:
                self.disk.line(self.out / "transcript.jsonl", json.dumps(rec, ensure_ascii=False))

    def _write(self, rec):
        if self.out:
            self.disk.line(self.out / "timeline.jsonl", json.dumps(rec, ensure_ascii=False))

    # live line and summary
    def live(self, t_run):
        parts = []
        for name in DIRS:
            u = self.dirs[name].utt
            who = "вниз (собеседник)" if name == "down" else "вверх (микрофон)"
            if u is not None and u.chunks:
                parts.append("%s ГОВОРИТ %4.1f с" % (who, (u.chunks[-1][0] + BLOCK - u.start) / RATE))
            else:
                parts.append("%s —" % who)
        n = collections.Counter(r["dir"] for r in self.utts)
        border = "-"
        if self.last_border:
            d, k, ms = self.last_border
            border = "%s %s %+.0f мс" % (d, "начало" if k == "start" else "конец", ms)
        miss = self.stats["frames_missing"]
        return "%5.0f с | %s | %s | реплик %d/%d | решение VAD: %s | пропуски %.1f/%.1f с" % (
            t_run, parts[0], parts[1], n["down"], n["up"], border, miss["down"] / RATE, miss["up"] / RATE)

    def close(self, reason="end", now=None):
        for d in self.dirs.values():
            d.close(reason, now)
        for w in self.stt.values():
            w.close()
        self.disk.close()
        s = self.summary()
        if self.out:
            (self.out / "summary.json").write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")
        return s

    def summary(self):
        by = {d: [r for r in self.utts if r["dir"] == d] for d in DIRS}
        overlap = 0.0
        for a in by["down"]:
            for b in by["up"]:
                if a["session"] == b["session"]:
                    overlap += max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
        cpu = time.process_time() - self.cpu0

        def dist(values):
            v = [x for x in values if x is not None]
            if not v:
                return {"n": 0}
            a = np.array(v, dtype=float)
            return {"n": len(a), "median": round(float(np.median(a)), 1), "p95": round(float(np.percentile(a, 95)), 1),
                    "max": round(float(a.max()), 1)}

        proc = self.stats["proc_ms"]
        return {
            "sessions": self.session, "blocks": self.stats["blocks"], "audio_s": round(self.audio_s, 2),
            "utterances": {d: len(by[d]) for d in DIRS},
            "speech_s": {d: round(sum(r["dur"] for r in by[d]), 2) for d in DIRS},
            "overlap_s": round(overlap, 2), "dropped_short": self.dropped_short,
            "reasons": dict(collections.Counter(r["reason"] for r in self.utts)),
            "latency_ms": {"onset_lag": dist(r["onset_lag_ms"] for r in self.utts),
                           "end_lag": dist(r["end_lag_ms"] for r in self.utts),
                           "onset_decision": dist(r["onset_decision_ms"] for r in self.utts),
                           "end_decision": dist(r["end_decision_ms"] for r in self.utts),
                           "proc": {"n": len(proc), "median": round(float(np.median(proc)), 3) if proc else None,
                                    "p99": round(float(np.percentile(proc, 99)), 3) if proc else None,
                                    "max": round(float(max(proc)), 3) if proc else None}},
            "missing_s": {d: round(self.stats["frames_missing"][d] / RATE, 2) for d in DIRS},
            "index_gaps": self.stats["index_gaps"], "jumps": self.stats["jumps"], "breaks": self.stats["breaks"],
            # load: the VAD's own time per block (perf_counter) against the sound's time. process_time is kept for
            # reference only: Windows charges CPU by timer ticks and misses short bursts that follow the sender's timer.
            "load_pct_of_audio": round(100 * self.stats["proc_total_s"] / self.audio_s, 3) if self.audio_s else None,
            "disk_errors": self.disk.errors, "cpu_s": round(cpu, 2), "cpu_pct_of_audio": round(100 * cpu / self.audio_s, 2) if self.audio_s else None,
            "stt": {d: {"done": w.done, "errors": w.errors, "dropped": w.dropped} for d, w in self.stt.items()},
            "out": str(self.out) if self.out else None,
        }


# --- sources --------------------------------------------------------------------------------------------------------------

def check_hello(hello):
    want = {"proto": "callpcm", "version": 1, "rate": RATE, "channels": 2, "block_frames": BLOCK}
    bad = {k: hello.get(k) for k, v in want.items() if hello.get(k) != v}
    if bad or hello.get("layout") != list(DIRS):
        raise ValueError("unsupported stream: %s" % json.dumps(hello))


class _Conn:
    """A callpcm client connection with a byte buffer, so a read timeout never splits a block."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def fill(self, need, deadline):
        while len(self.buf) < need:
            if time.monotonic() > deadline:
                return False
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        return True

    def take(self, n):
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def hello(self, deadline):
        while b"\n" not in self.buf:
            if not self.fill(len(self.buf) + 1, deadline):
                raise ConnectionError("no hello")
        i = self.buf.index(b"\n")
        return json.loads(self.take(i + 1))


def connect(host, port, wait_s, halt=None):
    end = time.monotonic() + wait_s if wait_s is not None else None
    while True:
        try:
            s = socket.create_connection((host, port), timeout=2.0)
            s.settimeout(0.2)
            return s
        except OSError:
            if (end is not None and time.monotonic() > end) or (halt is not None and halt.is_set()):
                return None
            time.sleep(0.5)


def run_socket(listener, host="127.0.0.1", port=cs.PCM_PORT, seconds=None, follow=False, wait_s=10.0, idle_s=5.0,
               live=None, halt=None):
    """Read the stream until it ends (or seconds pass, or halt is set); with follow, connect again after a drop."""
    start = time.monotonic()
    connected_once = False
    need = cs.HEADER.size + BLOCK * 4

    def time_left():
        return seconds is None or time.monotonic() - start < seconds

    last_live = 0.0
    while time_left() and not (halt is not None and halt.is_set()):
        sock = connect(host, port, None if (follow and connected_once) else wait_s, halt)
        if sock is None:
            if not connected_once:
                listener.say("нет потока на %s:%d за %.0f с: запущен call_check.py?" % (host, port, wait_s))
            break
        connected_once = True
        conn = _Conn(sock)
        reason = "disconnect"
        try:
            hello = conn.hello(time.monotonic() + idle_s)
            check_hello(hello)
            listener.begin_session(hello)
            listener.say("подключено к %s:%d, сессия %d" % (host, port, listener.session))
            while time_left() and not (halt is not None and halt.is_set()):
                now = time.monotonic()
                if live and now - last_live >= 0.5:
                    last_live = now
                    live(listener.live(now - start))
                idle_end = time.monotonic() + idle_s
                while not conn.fill(need, min(idle_end, time.monotonic() + 0.25)):
                    if time.monotonic() >= idle_end or not time_left() or (halt is not None and halt.is_set()):
                        break
                if len(conn.buf) < need:
                    reason = "idle" if time.monotonic() >= idle_end else "end"
                    break
                magic, index, frame, n, flags, t_sound = cs.HEADER.unpack(conn.take(cs.HEADER.size))
                if magic != cs.MAGIC or n != BLOCK:
                    raise ConnectionError("bad block header")
                pcm = np.frombuffer(conn.take(n * 4), dtype="<i2").reshape(n, 2)
                listener.block(index, frame, flags, t_sound, pcm, time.monotonic())
            else:
                reason = "end"
        except (ConnectionError, OSError, ValueError) as e:
            reason = "disconnect"
            if isinstance(e, ValueError):
                listener.say("поток не того формата: %s" % e)
                sock.close()
                listener.end_session("bad_stream")
                return 2
        finally:
            sock.close()
        if listener.session:
            listener.end_session(reason)
        if not follow:
            break
    return 0


def run_file(listener, path):
    """A stereo WAV of call_check (left down, right up) through the same listener, as fast as it goes. Times: no clock."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != RATE or w.getsampwidth() != 2 or w.getnchannels() != 2:
            raise ValueError("%s: want stereo 16 kHz 16 bit (the _both.wav of call_check)" % path)
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)
    listener.begin_session({"file": str(path)})
    for i in range(len(data) // BLOCK):
        listener.block(i, i * BLOCK, 0, math.nan, data[i * BLOCK:(i + 1) * BLOCK], None)
    listener.end_session("end")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=cs.PCM_PORT)
    ap.add_argument("--file", help="a stereo WAV of call_check instead of the live stream")
    ap.add_argument("--out", help="folder; default goals_harness/bench_runs/listen/<stamp>")
    ap.add_argument("--seconds", type=float, help="stop after this long")
    ap.add_argument("--follow", action="store_true", help="after the stream ends, wait for the next one (next call)")
    ap.add_argument("--wait", type=float, default=10.0, help="seconds to wait for the stream at the start")
    ap.add_argument("--hang-ms", type=float, default=Config.hang_s * 1000, help="pause that ends an utterance")
    ap.add_argument("--no-wav", action="store_true", help="no utterance files, only the timeline")
    ap.add_argument("--stt", help="recogniser: package.module:Class or file.py:Class (default: none, NullStt)")
    ap.add_argument("--stt-arg", action="append", default=[], metavar="KEY=VALUE", help="argument of the recogniser")
    ap.add_argument("--quiet", action="store_true", help="no live line")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    out = Path(args.out) if args.out else RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
    stt_args = dict(a.split("=", 1) for a in args.stt_arg)
    cfg = Config(hang_s=args.hang_ms / 1000.0)
    listener = Listener(out, cfg, load_backend(args.stt, stt_args), wav=not args.no_wav,
                        say=lambda s: print(s, flush=True))
    tty = sys.stdout.isatty()
    last_print = [0.0]

    def live(text):
        if args.quiet:
            return
        if tty:
            sys.stdout.write("\r" + text + "   ")
            sys.stdout.flush()
        elif time.monotonic() - last_print[0] >= 5.0:
            last_print[0] = time.monotonic()
            print(text, flush=True)

    code = 0
    try:
        if args.file:
            run_file(listener, args.file)
        else:
            code = run_socket(listener, args.host, args.port, args.seconds, args.follow, args.wait, live=live)
    except KeyboardInterrupt:
        pass
    s = listener.close()
    if tty and not args.quiet:
        print()
    print("реплик: вниз %d (%.1f с), вверх %d (%.1f с), перекрытие %.1f с; решение начала медиана %s мс, конца %s мс" % (
        s["utterances"]["down"], s["speech_s"]["down"], s["utterances"]["up"], s["speech_s"]["up"], s["overlap_s"],
        s["latency_ms"]["onset_decision"].get("median", s["latency_ms"]["onset_lag"].get("median")),
        s["latency_ms"]["end_decision"].get("median", s["latency_ms"]["end_lag"].get("median"))))
    print("итог: %s" % (out / "summary.json"), flush=True)
    if not listener.stats["sessions"]:
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
