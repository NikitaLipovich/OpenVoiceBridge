"""call_listen.py without the bench (goal 101): utterances, timeline, files, the recogniser hook, losses, reconnects.

Synthetic lines (speech_synth.py: glottal pulses through formants, syllables, pauses; known borders) go block by block
into the Listener, faster than real time: borders against the truth in quiet and in noise, silence, clicks, a step of
the background, overlap of the two directions, the 30 s cut, a stream that starts in the middle of speech, missing
sound short and long, blocks lost by the client, a jump, the break flag, the files bit for bit, the SttBackend calls,
a failing and a slow recogniser, --stt by path, the latency fields, CPU. Over a socket: call_stream.PcmServer serves,
the listener reads, the server goes away and comes back (a new session). End to end: s3_sim.py plays a scenario
(down: pieces of a real tone recording of a past probe run when this PC has one, else a synthetic tone; up: synthetic
speech) -> call_check.py -> call_listen.py: the utterances where the scenario put them, both directions on one time
line. Plays nothing (R15), no phone, no adb, no voice recordings.
"""
import glob
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "bench"
REPO = HERE.parents[2]
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(HERE))
import call_listen as cl  # noqa: E402
import call_stream as cs  # noqa: E402
import speech_synth as ss  # noqa: E402
from tiers import full_only  # noqa: E402

RATE, BLOCK = cl.RATE, cl.BLOCK
TMP = Path(tempfile.mkdtemp(prefix="t101_"))
NAN = float("nan")
SCENE = [(1, 2.5), (6, 4), (12, 1.0), (15, 6), (24, 3)]


def feed(lst, down, up, flags=None, drop=None, t0=None, net=0.045, frame0=0, hello=None, end="end"):
    """Both lines through the listener as callpcm blocks. flags(i) -> callpcm flags of block i (missing sound is zeroed as
    the server does); drop(i) -> True: the client lost block i (its index and frames skip); t0: t_sound of frame 0, the
    block arrives net s after its last sample."""
    pcm = np.stack([down, up], 1).astype(np.int16)
    lst.begin_session(hello or {"test": 1})
    for i in range(len(pcm) // BLOCK):
        if drop and drop(i):
            continue
        f = i * BLOCK
        fl = flags(i) if flags else 0
        blk = pcm[f:f + BLOCK].copy()
        if fl & cs.F_NO_DOWN:
            blk[:, 0] = 0
        if fl & cs.F_NO_UP:
            blk[:, 1] = 0
        ts = t0 + f / RATE if t0 is not None else NAN
        now = ts + BLOCK / RATE + net if t0 is not None else None
        lst.block(i, frame0 + f, fl, ts, blk, now)
    if end:
        lst.end_session(end)
    return lst


def utts(lst, d):
    return [u for u in lst.utts if u["dir"] == d]


def covered(intervals, truth):
    """Share of the true speech time inside the found utterances."""
    tot = sum(b - a for a, b in truth)
    hit = sum(max(0.0, min(b, d) - max(a, c)) for a, b in truth for c, d in intervals)
    return hit / tot


def read_wav(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


class Recorder(cl.SttBackend):
    calls = []

    def begin(self, utt):
        self.cur = {"utt": utt, "pcm": [], "ts": []}

    def feed(self, pcm, ts):
        self.cur["pcm"].append(np.array(pcm))
        self.cur["ts"].append(ts)

    def finish(self):
        Recorder.calls.append(("finish", self.direction, self.cur))
        n = sum(len(p) for p in self.cur["pcm"])
        return cl.SttResult("utt %d of %s, %d samples" % (self.cur["utt"]["n"], self.direction, n),
                            [(self.cur["ts"][0], self.cur["ts"][0] + n / RATE, "x")])

    def cancel(self):
        Recorder.calls.append(("cancel", self.direction, self.cur))


class Failing(cl.SttBackend):
    def feed(self, pcm, ts):
        raise RuntimeError("engine broke")


class Slow(cl.SttBackend):
    def feed(self, pcm, ts):
        time.sleep(0.05)


class BordersTest(unittest.TestCase):
    def test_borders_in_quiet_and_noise(self):
        for noise in (None, -60, -50, -41):           # speech at -26 dBFS: SNR down to 15 dB
            down, truth = ss.script(30, SCENE, noise_db=noise, seed=2)
            up, _ = ss.script(30, [], noise_db=noise, seed=3)
            lst = feed(cl.Listener(None), down, up)
            got = utts(lst, "down")
            self.assertEqual(len(got), len(truth), (noise, got))
            self.assertEqual(utts(lst, "up"), [], noise)
            for (a, b), u in zip(truth, got):
                self.assertTrue(-0.04 <= u["start"] - a <= 0.06, (noise, a, u))
                self.assertTrue(-0.06 <= u["end"] - b <= 0.06, (noise, b, u))
                self.assertEqual(u["reason"], "pause")
                self.assertLessEqual(u["onset_lag_ms"], 100)
                self.assertAlmostEqual(u["end_lag_ms"], 440, delta=20)

    def test_snr_8_db(self):
        down, truth = ss.script(30, SCENE, noise_db=-34, seed=2)
        up, _ = ss.script(30, [], noise_db=-34, seed=3)
        lst = feed(cl.Listener(None), down, up)
        got = utts(lst, "down")
        self.assertEqual(len(got), len(truth), got)
        self.assertGreaterEqual(covered([(u["start"], u["end"]) for u in got], truth), 0.95)
        self.assertEqual(utts(lst, "up"), [])

    def test_silence_zeros_and_clicks_make_nothing(self):
        z = np.zeros(RATE * 20, dtype=np.int16)
        c = z.copy()
        c[RATE * np.arange(1, 19)] = 20000
        c[RATE * np.arange(1, 19) + 1] = -20000
        quiet, _ = ss.script(20, [], noise_db=-70, seed=5)
        lst = feed(cl.Listener(None), c, quiet)
        self.assertEqual(lst.utts, [])
        self.assertEqual(feed(cl.Listener(None), z, z).utts, [])

    def test_background_step(self):
        """A sudden +26 dB of noise without speech: at most one short utterance ended as noise, then nothing; speech
        over the new background is found."""
        down, truth = ss.script(30, [(14, 3), (22, 3)], noise_db=-66, noise_steps=[(5, -40)], seed=4)
        lst = feed(cl.Listener(None), down, np.zeros_like(down))
        got = utts(lst, "down")
        early = [u for u in got if u["start"] < 10]
        self.assertLessEqual(len(early), 1, got)
        for u in early:
            self.assertLessEqual(u["dur"], 2.0)
            self.assertEqual(u["reason"], "noise")
        late = [u for u in got if u["start"] >= 10]
        self.assertEqual(len(late), 2, got)
        for (a, b), u in zip(truth, late):
            self.assertAlmostEqual(u["start"], a, delta=0.08)
            self.assertAlmostEqual(u["end"], b, delta=0.08)

    def test_overlap_of_directions(self):
        down, td = ss.script(20, [(1, 4), (10, 3)], noise_db=-60, seed=6)
        up, tu = ss.script(20, [(4, 3), (11, 4)], noise_db=-60, seed=7)
        lst = feed(cl.Listener(None), down, up)
        self.assertEqual((len(utts(lst, "down")), len(utts(lst, "up"))), (2, 2))
        true_overlap = sum(max(0.0, min(b, d) - max(a, c)) for a, b in td for c, d in tu)
        s = lst.close()
        self.assertAlmostEqual(s["overlap_s"], true_overlap, delta=0.15)
        self.assertGreater(s["overlap_s"], 1.0)

    def test_long_speech_cut_without_holes(self):
        down, truth = ss.script(75, [(1, 70)], noise_db=-60, seed=5)
        lst = feed(cl.Listener(None), down, np.zeros_like(down))
        got = utts(lst, "down")
        self.assertEqual([u["reason"] for u in got], ["max", "max", "pause"])
        self.assertEqual(got[0]["end"], got[1]["start"])
        self.assertEqual(got[1]["end"], got[2]["start"])
        self.assertAlmostEqual(got[0]["dur"], 30.0, delta=0.03)
        self.assertAlmostEqual(got[2]["end"], truth[0][1], delta=0.06)

    def test_stream_starting_in_speech(self):
        tone = (0.35 * 32768 * np.sin(2 * np.pi * 1000 * np.arange(RATE * 9) / RATE)).astype(np.int16)
        tone[RATE * 3:RATE * 5] = 0
        lst = feed(cl.Listener(None), tone, np.zeros_like(tone))
        got = utts(lst, "down")
        self.assertEqual(len(got), 2, got)
        self.assertLessEqual(got[0]["start"], 0.12)
        self.assertAlmostEqual(got[0]["end"], 3.0, delta=0.03)
        self.assertAlmostEqual(got[1]["start"], 5.0, delta=0.03)


class LossTest(unittest.TestCase):
    def setUp(self):
        self.down, self.truth = ss.script(10, [(1, 5)], noise_db=-60, seed=8)
        self.up = np.zeros_like(self.down)

    def test_short_missing_sound_kept(self):
        out = TMP / "loss_short"
        lst = feed(cl.Listener(out), self.down, self.up, flags=lambda i: cs.F_NO_DOWN if 150 <= i < 160 else 0)
        got = utts(lst, "down")
        self.assertEqual(len(got), 1, got)
        u = got[0]
        self.assertEqual(u["lost_ms"], 200)
        self.assertAlmostEqual(u["end"], self.truth[0][1], delta=0.06)
        x = read_wav(out / u["file"])
        a = int(round((3.0 - u["file_start"]) * RATE))
        self.assertFalse(x[a:a + 10 * BLOCK].any())                 # the missing part is zeros at its place
        self.assertTrue(x[a - BLOCK:a].any() and x[a + 10 * BLOCK:a + 11 * BLOCK].any())
        self.assertEqual(lst.close()["missing_s"]["down"], 0.2)

    def test_long_missing_sound_ends(self):
        lst = feed(cl.Listener(None), self.down, self.up, flags=lambda i: cs.F_NO_DOWN if 150 <= i < 200 else 0)
        got = utts(lst, "down")
        self.assertEqual([u["reason"] for u in got], ["loss", "pause"], got)
        self.assertTrue(2.65 <= got[0]["end"] <= 3.0, got[0])      # the last holding frame before the loss
        self.assertGreaterEqual(got[1]["start"], 4.0)
        self.assertLessEqual(got[1]["start"], 4.1)

    def test_blocks_lost_by_the_client(self):
        out = TMP / "loss_client"
        lst = feed(cl.Listener(out), self.down, self.up, drop=lambda i: 150 <= i < 155)
        got = utts(lst, "down")
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0]["lost_ms"], 100)
        s = lst.close()
        self.assertEqual(s["index_gaps"], 1)
        x = read_wav(out / got[0]["file"])
        # the file keeps the time line: its length is the span of the stream it covers
        self.assertEqual(len(x), int(round((got[0]["end"] + 0.1 - got[0]["file_start"]) * RATE)))

    def test_jump_ends_the_utterance(self):
        lst = cl.Listener(None)
        half = 150 * BLOCK
        feed(lst, self.down[:half], self.up[:half], end=None)
        for i in range(150, 500):                  # the same stream 5 s later: a gap too long to fill
            f = i * BLOCK
            lst.block(i, f + 5 * RATE, 0, NAN, np.stack([self.down[f:f + BLOCK], self.up[f:f + BLOCK]], 1), None)
        lst.end_session("end")
        got = utts(lst, "down")
        self.assertEqual(got[0]["reason"], "gap")
        self.assertTrue(2.65 <= got[0]["end"] <= 3.0, got[0])      # the last holding frame before the loss
        self.assertEqual(lst.stats["jumps"], 1)

    def test_break_flag_noted_not_cut(self):
        lst = feed(cl.Listener(None), self.down, self.up, flags=lambda i: cs.F_BREAK if i == 150 else 0)
        got = utts(lst, "down")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["breaks"], 1)


class OutputTest(unittest.TestCase):
    def test_files_timeline_summary(self):
        out = TMP / "files"
        down, _ = ss.script(12, [(1, 2), (6, 3)], noise_db=-60, seed=9)
        up, _ = ss.script(12, [(3.5, 2)], noise_db=-60, seed=10)
        lst = feed(cl.Listener(out), down, up, t0=1000.0)
        s = lst.close()
        lines = [json.loads(x) for x in (out / "timeline.jsonl").read_text(encoding="utf-8").splitlines()]
        kinds = [x["kind"] for x in lines]
        self.assertEqual(kinds.count("utt"), 3)
        self.assertEqual(kinds.count("session"), 2)
        self.assertEqual(s["utterances"], {"down": 2, "up": 1})
        for u in (x for x in lines if x["kind"] == "utt"):
            for key in ("n", "dir", "session", "start", "end", "dur", "wall_start", "wall_end", "level_db", "peak_db",
                        "reason", "lost_ms", "breaks", "file", "onset_lag_ms", "end_lag_ms", "onset_decision_ms",
                        "end_decision_ms"):
                self.assertIn(key, u)
            src = down if u["dir"] == "down" else up
            x = read_wav(out / u["file"])
            a = int(round(u["file_start"] * RATE))
            self.assertTrue(np.array_equal(x, src[a:a + len(x)]), u)          # bit for bit
            self.assertTrue(Path(out / u["file"]).name.startswith("%04d_%s" % (u["n"], u["dir"])))
            self.assertAlmostEqual(u["level_db"], -26, delta=3)
            # whole way = algorithmic part + the 45 ms the block took to arrive
            self.assertAlmostEqual(u["onset_decision_ms"] - u["onset_lag_ms"], 45, delta=1)
            self.assertAlmostEqual(u["end_decision_ms"] - u["end_lag_ms"], 45, delta=1)
            self.assertAlmostEqual(u["wall_end"] - u["wall_start"], u["dur"], delta=0.002)
        self.assertTrue((out / "summary.json").exists())
        self.assertFalse((out / "transcript.jsonl").exists())        # NullStt gives no text
        self.assertIn("ГОВОРИТ", cl.Listener(None).live(0) + "ГОВОРИТ")

    def test_live_line(self):
        lst = cl.Listener(None)
        down, _ = ss.script(4, [(1, 3)], noise_db=-60, seed=9)
        feed(lst, down, np.zeros_like(down), end=None)
        line = lst.live(4.0)
        self.assertIn("вниз (собеседник) ГОВОРИТ", line)
        self.assertIn("вверх (микрофон) —", line)
        self.assertIn("решение VAD: down начало", line)

    def test_cpu_and_processing_time(self):
        down, _ = ss.script(60, [(t, 3) for t in range(1, 58, 5)], noise_db=-50, seed=11)
        up, _ = ss.script(60, [(t, 2) for t in range(3, 58, 5)], noise_db=-50, seed=12)
        lst = feed(cl.Listener(TMP / "cpu"), down, up)
        s = lst.close()
        self.assertLess(s["cpu_pct_of_audio"], 10.0, s)
        self.assertLess(s["load_pct_of_audio"], 5.0, s)
        # p99 and max are printed, not judged: on a shared PC other processes preempt this one (DECISIONS D2)
        self.assertLess(s["latency_ms"]["proc"]["median"], 2.0, s)
        print("\n  CPU %.2f %% of real time (load %.2f %%), proc median %.3f p99 %.3f max %.3f ms" % (
            s["cpu_pct_of_audio"], s["load_pct_of_audio"], s["latency_ms"]["proc"]["median"], s["latency_ms"]["proc"]["p99"],
            s["latency_ms"]["proc"]["max"]))


class SttTest(unittest.TestCase):
    def test_backend_gets_each_utterance(self):
        Recorder.calls = []
        out = TMP / "stt"
        down, _ = ss.script(12, [(1, 2), (6, 3)], noise_db=-60, seed=13)
        up, _ = ss.script(12, [(3, 2)], noise_db=-60, seed=14)
        c = np.zeros_like(down)
        c[RATE * 10:RATE * 10 + 1600] = (8000 * np.sin(np.arange(1600))).astype(np.int16)    # 0.1 s: dropped
        lst = feed(cl.Listener(out, stt_factory=lambda d: Recorder(d)), down, up + c)
        s = lst.close()
        fin = [c_ for c_ in Recorder.calls if c_[0] == "finish"]
        self.assertEqual(len(fin), 3)
        self.assertEqual(s["dropped_short"], 1)
        self.assertEqual([c_[0] for c_ in Recorder.calls].count("cancel"), 1)
        for _, d, cur in fin:
            u = [x for x in lst.utts if x["n"] == cur["utt"]["n"]][0]
            self.assertEqual(u["dir"], d)
            fed = np.concatenate(cur["pcm"])
            x = read_wav(out / u["file"])
            self.assertTrue(np.array_equal(fed[:len(x)], x))        # what the engine heard is the file
            self.assertAlmostEqual(cur["ts"][0], u["file_start"], delta=1e-6)
            self.assertTrue(all(abs(b - a - BLOCK / RATE) < 1e-6 for a, b in zip(cur["ts"], cur["ts"][1:])))
        lines = [json.loads(x) for x in (out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(x["text"].startswith("utt ") and x["end"] is not None for x in lines))

    def test_failing_and_slow_backends_do_not_stop_the_listener(self):
        down, _ = ss.script(10, [(1, 2), (5, 3)], noise_db=-60, seed=15)
        lst = feed(cl.Listener(None, stt_factory=lambda d: Failing(d)), down, down)
        s = lst.close()
        self.assertEqual(s["utterances"], {"down": 2, "up": 2})
        self.assertEqual(s["stt"]["down"]["errors"], 2)               # once per utterance, then it is skipped
        cfg = cl.Config(stt_backlog_s=1.0)
        t = time.perf_counter()
        lst = feed(cl.Listener(None, cfg, stt_factory=lambda d: Slow(d)), down, np.zeros_like(down))
        took = time.perf_counter() - t
        self.assertLess(took, 3.0)                                    # 10 s of sound, the engine 2.5x slower than real time
        s = lst.summary()
        self.assertEqual(s["stt"]["down"]["dropped"], 1)              # the second came while the first still waited
        for w in lst.stt.values():
            w.close(timeout=0.1)

    def test_backend_by_path_and_cli(self):
        plug = TMP / "myengine.py"
        plug.write_text("import call_listen as cl\n"
                        "class Engine(cl.SttBackend):\n"
                        "    def begin(self, utt):\n        self.n = 0\n"
                        "    def feed(self, pcm, ts):\n        self.n += len(pcm)\n"
                        "    def finish(self):\n        return cl.SttResult('%s heard %d (lang %s)' % (self.direction, self.n, self.args.get('lang')))\n",
                        encoding="utf-8")
        make = cl.load_backend("%s:Engine" % plug, {"lang": "ru"})
        self.assertEqual(make("up").args, {"lang": "ru"})
        self.assertIsInstance(cl.load_backend(None)("down"), cl.NullStt)
        down, _ = ss.script(8, [(1, 3)], noise_db=-60, seed=16)
        both = TMP / "both.wav"
        with wave.open(str(both), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(np.stack([down, np.zeros_like(down)], 1).astype("<i2").tobytes())
        out = TMP / "cli"
        r = subprocess.run([sys.executable, str(BENCH / "call_listen.py"), "--file", str(both), "--out", str(out),
                            "--stt", "%s:Engine" % plug, "--stt-arg", "lang=ru"], capture_output=True, text=True,
                           encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("реплик: вниз 1", r.stdout)
        tr = [json.loads(x) for x in (out / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(tr), 1)
        self.assertRegex(tr[0]["text"], r"^down heard \d+ \(lang ru\)$")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SocketTest(unittest.TestCase):
    def serve_blocks(self, server, down, up, frame0=0, t0=None, pace=0.01, upto=None):
        pcm = np.stack([down, up], 1).astype(np.int16)
        n = len(pcm) // BLOCK if upto is None else upto
        t0 = time.monotonic() if t0 is None else t0
        for i in range(n):
            f = i * BLOCK
            server.consume(cs.Block(i, frame0 + f, pcm[f:f + BLOCK], 0, t0 + f / RATE, time.monotonic()))
            time.sleep(pace)

    def test_stream_drop_and_reconnect(self):
        port = free_port()
        out = TMP / "sock"
        down, _ = ss.script(6, [(0.5, 1.5), (3.5, 2.5)], noise_db=-60, seed=17)
        up, _ = ss.script(6, [(2.0, 1.0)], noise_db=-60, seed=18)
        lst = cl.Listener(out)
        halt = threading.Event()
        th = threading.Thread(target=cl.run_socket, args=(lst, "127.0.0.1", port, None, True, 10.0, 5.0, None, halt),
                              daemon=True)
        server = cs.PcmServer(port)
        th.start()
        end = time.monotonic() + 5
        while not server.clients and time.monotonic() < end:
            time.sleep(0.05)
        self.serve_blocks(server, down, up, upto=225)        # 4.5 s: the second utterance of down is open
        time.sleep(0.2)
        server.close()
        time.sleep(1.0)                                       # nobody serves: the listener keeps trying
        server = cs.PcmServer(port)
        end = time.monotonic() + 5
        while not server.clients and time.monotonic() < end:
            time.sleep(0.05)
        self.serve_blocks(server, down, up, frame0=777 * BLOCK)
        time.sleep(0.3)
        halt.set()
        th.join(5)
        server.close()
        s = lst.close()
        self.assertEqual(s["sessions"], 2)
        got = lst.utts
        first = [u for u in got if u["session"] == 1]
        second = [u for u in got if u["session"] == 2]
        self.assertEqual([(u["dir"], u["reason"]) for u in first], [("down", "pause"), ("up", "pause"), ("down", "disconnect")])
        self.assertEqual(sorted((u["dir"], u["reason"]) for u in second),
                         [("down", "end"), ("down", "pause"), ("up", "pause")])
        self.assertAlmostEqual(second[0]["start"], 777 * BLOCK / RATE + 0.5, delta=0.06)
        for u in got:
            self.assertIsNotNone(u["onset_decision_ms"])
            self.assertLess(u["onset_decision_ms"], 300)
        events = [json.loads(x) for x in (out / "timeline.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events if e["kind"] == "session"], ["connect", "disconnect", "connect", "end"])
        self.assertEqual(len({u["n"] for u in got}), len(got))       # numbers run on across sessions

    def test_bad_stream_refused(self):
        port = free_port()
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen(1)

        def serve():
            c, _ = srv.accept()
            c.sendall(b'{"proto": "callpcm", "version": 2, "rate": 8000}\n')
            time.sleep(0.5)
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        lst = cl.Listener(None)
        self.assertEqual(cl.run_socket(lst, "127.0.0.1", port, 5, False, 5.0), 2)
        srv.close()

    def test_no_stream(self):
        lst = cl.Listener(None)
        t = time.monotonic()
        self.assertEqual(cl.run_socket(lst, "127.0.0.1", free_port(), 5, False, 1.0), 0)
        self.assertEqual(lst.session, 0)
        self.assertLess(time.monotonic() - t, 4)


def tone_source():
    """Pieces of a real tone recording of a past probe run (the phone's tones, no voice), or a synthetic tone."""
    runs = REPO / "goals_harness" / "bench_runs" / "calls"
    for path in sorted(glob.glob(str(runs / "*probe*" / "*" / "*_down.wav"))):
        try:
            x = read_wav(path)
        except (OSError, wave.Error, EOFError):
            continue
        if len(x) > 20 * RATE and np.sqrt(np.mean(x[5 * RATE:15 * RATE].astype(float) ** 2)) > 1000:
            return x[2 * RATE:], "recording " + Path(path).name
    return (0.35 * 32768 * np.sin(2 * np.pi * 1000 * np.arange(40 * RATE) / RATE)).astype(np.int16), "synthetic 1 kHz"


class EndToEndTest(unittest.TestCase):
    """s3_sim.py plays a scenario on both lines -> call_check.py (the owner's command) -> call_listen.py."""
    LOOP = 24.0
    DOWN = [(1.0, 2.0), (5.5, 1.2), (9.0, 3.0), (15.0, 1.5), (19.5, 2.5)]
    UP = [(2.5, 2.0), (7.0, 2.5), (12.5, 3.0), (16.0, 2.0), (21.0, 1.5)]

    def scenario(self):
        tone, kind = tone_source()
        down = np.zeros(int(self.LOOP * RATE), dtype=np.int16)
        pos = 0
        for a, d in self.DOWN:
            n = int(d * RATE)
            down[int(a * RATE):int(a * RATE) + n] = tone[pos:pos + n]
            pos += n
        up, truth_up = ss.script(self.LOOP, self.UP, noise_db=-65, seed=19)
        files = {}
        for name, x in (("down", down), ("up", up)):
            files[name] = TMP / ("play_%s.wav" % name)
            with wave.open(str(files[name]), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(x.astype("<i2").tobytes())
        return files, {"down": [(a, a + d) for a, d in self.DOWN], "up": truth_up}, kind

    def match(self, found, truth, offset):
        """found utterances against the looped scenario shifted by offset; returns (matched, errors)."""
        errs = []
        for u in found:
            a = (u["start"] - offset) % self.LOOP
            k = min(range(len(truth)), key=lambda i: min(abs(a - truth[i][0]), self.LOOP - abs(a - truth[i][0])))
            ta, tb = truth[k]
            da = (a - ta + self.LOOP / 2) % self.LOOP - self.LOOP / 2
            errs.append((u["n"], u["dir"], round(da, 3), round(u["dur"] - (tb - ta), 3), u["reason"]))
        return errs

    def run_scene(self, name, *sim_args):
        import simproc
        import s3_uart
        files, truth, kind = self.scenario()
        print("\n  [%s] down source: %s" % (name, kind))
        os.environ["BENCH_S3_ADDR"] = "127.0.0.1"
        port = free_port()
        out_cc, out_l = TMP / (name + "_cc"), TMP / (name + "_listen")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with simproc.Sim("--play-call", str(files["down"]), "--play-mic", str(files["up"]), *sim_args):
            p = subprocess.Popen([sys.executable, str(BENCH / "call_check.py"), "--out", str(out_cc), "--no-phone",
                                  "--grace", "2", "--pcm-port", str(port)], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.PIPE, text=True, encoding="utf-8", env=env)
            lines = []
            threading.Thread(target=lambda: lines.extend(p.stdout), daemon=True).start()
            listen = subprocess.Popen([sys.executable, str(BENCH / "call_listen.py"), "--port", str(port), "--out",
                                       str(out_l), "--wait", "60"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", env=env)
            end = time.monotonic() + 60
            while not any("ЗВОНИТЕ" in x for x in lines) and p.poll() is None and time.monotonic() < end:
                time.sleep(0.2)
            self.assertTrue(any("ЗВОНИТЕ" in x for x in lines), "".join(lines))
            ch = s3_uart.Channel()
            ch.find(timeout=3)
            self.assertEqual(ch.command("simsco 1", timeout=0.5, retries=5)["status"], 0)
            time.sleep(self.LOOP + 4)
            self.assertEqual(ch.command("simsco 0", timeout=0.5, retries=5)["status"], 0)
            ch.close()
            p.wait(timeout=90)
            lout, _ = listen.communicate(timeout=30)
            p.stdout.close()
            p.stdin.close()
        self.assertEqual(listen.returncode, 0, lout)
        self.assertNotIn("Traceback", lout + "".join(lines))
        s = json.loads((out_l / "summary.json").read_text(encoding="utf-8"))
        got = [json.loads(x) for x in (out_l / "timeline.jsonl").read_text(encoding="utf-8").splitlines()]
        utt = [u for u in got if u["kind"] == "utt" and u["reason"] != "disconnect"]
        downs = [u for u in utt if u["dir"] == "down"]
        ups = [u for u in utt if u["dir"] == "up"]
        # the offset of the scenario on the stream's time line, from the down line only ...
        best = None
        for u in downs:
            for a, _ in truth["down"]:
                off = u["start"] - a
                errs = self.match(downs, truth["down"], off)
                score = sum(abs(e[2]) <= 0.06 for e in errs)
                if best is None or score > best[0]:
                    best = (score, off)
        off = best[1]
        # ... and the same offset must fit the up line: both directions on one time line
        e_down, e_up = self.match(downs, truth["down"], off), self.match(ups, truth["up"], off)
        print("  offset %.3f s; down %s; up %s" % (off, e_down, e_up))
        print("  summary: utterances %s, overlap %.2f s, missing %s s, lost in utterances %s ms, onset decision %s, "
              "end decision %s, proc %s, load %s %%, cpu %s %%" % (
                  s["utterances"], s["overlap_s"], s["missing_s"], sum(u["lost_ms"] for u in utt),
                  s["latency_ms"]["onset_decision"], s["latency_ms"]["end_decision"], s["latency_ms"]["proc"],
                  s["load_pct_of_audio"], s["cpu_pct_of_audio"]))
        self.assertLess(s["load_pct_of_audio"], 5.0)
        self.assertEqual(s["disk_errors"], 0)
        self.assertGreater(s["audio_s"], self.LOOP + 4)
        self.assertEqual(s["stt"]["down"]["errors"] + s["stt"]["up"]["errors"], 0)
        for u in utt:
            self.assertTrue((out_l / u["file"]).exists())
        return s, truth, e_down, e_up, utt

    def check_matches(self, s, truth, e_down, e_up, tol_start, tol_dur, reasons):
        for errs, truth_k in ((e_down, truth["down"]), (e_up, truth["up"])):
            inner = errs[1:-1]                                 # the first and the last may be cut by the stream's ends
            self.assertGreaterEqual(len(inner), len(truth_k) - 1, errs)
            for n, d, da, dd, reason in inner:
                self.assertLessEqual(abs(da), tol_start, errs)
                self.assertLessEqual(abs(dd), tol_dur, errs)
                self.assertIn(reason, reasons, errs)
        self.assertGreaterEqual(len(e_down) + len(e_up), int(s["audio_s"] / self.LOOP * 10) - 2)

    def test_scenario_through_the_simulator(self):
        s, truth, e_down, e_up, utt = self.run_scene("e2e")
        self.check_matches(s, truth, e_down, e_up, 0.06, 0.08, ("pause",))
        self.assertLess(s["latency_ms"]["onset_decision"]["median"], 300)
        self.assertLess(s["latency_ms"]["end_decision"]["median"], 450 + 300)

    @full_only("the same scenario with 3 % loss; loss in the cutting is LossTest, the scenario itself is in fast")
    def test_scenario_with_losses_on_the_way(self):
        """3 % of the S3's packets lost: the gaps come as missing sound, utterances stay whole, the loss is counted."""
        s, truth, e_down, e_up, utt = self.run_scene("e2e_loss", "--stream-loss", "0.03")
        self.assertGreater(s["missing_s"]["down"] + s["missing_s"]["up"], 0.1)
        self.assertGreater(sum(u["lost_ms"] for u in utt), 0)
        self.check_matches(s, truth, e_down, e_up, 0.08, 0.12, ("pause",))
        self.assertLess(s["latency_ms"]["onset_decision"]["median"], 300)


if __name__ == "__main__":
    unittest.main()
