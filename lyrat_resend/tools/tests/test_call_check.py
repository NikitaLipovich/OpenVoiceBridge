"""call_check.py, call_quality.py and call_stream.py without the bench (goal 097).

Quality numbers on synthetic lines: tone level, silence, cut pieces (drop-outs) and the stream's own gaps, clipping, noise;
the verdict. Directions shifted on purpose: the files come out aligned. The live pipeline: a known offset of the mic's frame
counter, network jitter, losses, a restart of the S3, a stopped bus; the local PCM output read by its client. End to end
against s3_sim.py: the channel opened and closed by the simulated LyraT, both lines with clicks at the same bus frame, the
mic counter behind: the report, the files aligned, the latency within the goal. Plays nothing (R15), no phone, no adb.
"""
import json
import math
import os
import random
import struct
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
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(HERE))
import call_check  # noqa: E402
import call_quality as cq  # noqa: E402
import call_stream as cs  # noqa: E402

RATE = 16000
TMP = Path(tempfile.mkdtemp(prefix="t097_"))


def tone(hz, seconds, level=8000):
    n = int(seconds * RATE)
    return (level * np.sin(2 * np.pi * hz * np.arange(n) / RATE)).astype(np.int16)


def speechish(seconds, seed=1):
    """Bursts of tone with pauses of low noise: a line with active and quiet parts."""
    rnd = np.random.default_rng(seed)
    x = rnd.normal(0, 30, int(seconds * RATE))
    for start in range(0, len(x), RATE):
        n = int(0.6 * RATE)
        x[start:start + n] += 6000 * np.sin(2 * np.pi * 300 * np.arange(min(n, len(x) - start)) / RATE)
    return np.clip(x, -32768, 32767).astype(np.int16)


def pkt(stream, seq, frame, boot, pcm, ch, ovf=0):
    return struct.pack("<2sBBIIHBBII", b"S3", 1, stream, seq, frame & 0xFFFFFFFF, boot, ch, len(pcm) // ch // 8, ovf, 0) + \
        np.asarray(pcm, dtype="<i2").tobytes()


def clicks(total):
    sig = np.zeros(total, dtype=np.int16)
    for k in range(0, total, RATE // 4):
        sig[k:k + 40] = 10000
    return sig


def onsets(x, level=5000):
    m = np.abs(x.astype(np.int32)) > level
    return np.flatnonzero(m[1:] & ~m[:-1]) + 1


class Collect:
    def __init__(self):
        self.blocks = []

    def consume(self, b):
        self.blocks.append(b)


def drive(pipe, events, t_from, t_to, step=0.005):
    events = sorted(events, key=lambda e: e[0])
    k, t = 0, t_from
    while t < t_to:
        while k < len(events) and events[k][0] <= t:
            pipe.feed(events[k][1], events[k][0])
            k += 1
        pipe.tick(t)
        t += step


def synth_events(seconds, D=-37, jitter=0.01, loss=0.0, seed=3, t0=100.0, boot=7, frame0=0, seq0=0):
    """Both lines of one S3 boot: the call's counter is the bus frame, the mic's D frames off; arrival after the block."""
    rnd = random.Random(seed)
    total = int(seconds * RATE)
    sig = clicks(total + 1000)
    ev = []
    for i in range(total // 240):
        bus = i * 240
        end = t0 + (bus + 240) / RATE
        for stream, frame, data, ch in ((0, frame0 + bus, sig[bus:bus + 240], 1),
                                        (1, frame0 + bus + D, np.repeat(sig[bus:bus + 240], 2), 2)):
            if frame < 0 or rnd.random() < loss:
                continue
            ev.append((end + 0.002 + (rnd.expovariate(1 / jitter) if jitter else 0.0), pkt(stream, seq0 + i, frame, boot, data, ch)))
    return ev


def lag_of(out):
    """For each click of the left line, the nearest click of the right one: lost blocks do not break the pairs."""
    a, b = onsets(out[:, 0]), onsets(out[:, 1])
    if not len(a) or not len(b):
        return np.array([])
    idx = np.clip(np.searchsorted(b, a), 1, len(b) - 1) if len(b) > 1 else np.zeros(len(a), dtype=int)
    near = np.where(np.abs(b[idx] - a) < np.abs(b[idx - 1] - a), b[idx], b[idx - 1]) if len(b) > 1 else b[idx]
    return near - a


class QualityTest(unittest.TestCase):
    def test_tone_level_and_no_problems(self):
        m = cq.line_metrics(tone(1000, 5, level=3277))           # -20 dBFS peak, -23 dBFS rms
        self.assertAlmostEqual(m["rms_dbfs"], -23.0, delta=0.3)
        self.assertAlmostEqual(m["peak_dbfs"], -20.0, delta=0.2)
        self.assertFalse(m["silent"])
        self.assertFalse(m["dynamic"])
        self.assertIsNone(m["snr_db"])                          # a steady tone has no pauses to measure the floor in
        self.assertEqual((m["dropouts"], m["clip_runs"]), (0, 0))

    def test_silence_is_found_and_judged(self):
        m = cq.line_metrics(np.zeros(5 * RATE, dtype=np.int16))
        self.assertTrue(m["silent"])
        ok, problems = cq.verdict({"sco": {"intervals": [[1, 4]], "codec": "mSBC"}, "lines": {"down": m, "up": cq.line_metrics(speechish(5))}})
        self.assertFalse(ok)
        self.assertTrue(any("собеседник" in p and "молчит" in p for p in problems), problems)

    def test_cut_pieces_counted_but_not_the_stream_gaps(self):
        x = speechish(6).copy()
        cuts = [int(0.1 * RATE) + k * RATE for k in range(5)]        # inside the active part of each second
        for c in cuts:
            x[c:c + 240] = 0                                          # a lost 15 ms frame
        m = cq.line_metrics(x)
        self.assertEqual(m["dropouts"], 5)
        self.assertAlmostEqual(m["dropout_ms"], 75.0, delta=0.1)
        m2 = cq.line_metrics(x, gaps=[[c, 240] for c in cuts[:3]])   # three are losses of the S3 stream, counted there
        self.assertEqual(m2["dropouts"], 2)
        self.assertGreater(m["dropouts_per_min"], cq.MAX_DROPOUTS_PER_MIN)
        ok, problems = cq.verdict({"sco": {"intervals": [[0, 6]], "codec": "mSBC"}, "lines": {"down": m}})
        self.assertTrue(any("выпадения" in p for p in problems), problems)

    def test_clipping(self):
        x = np.clip(40000 * np.sin(2 * np.pi * 500 * np.arange(3 * RATE) / RATE), -32768, 32767).astype(np.int16)
        m = cq.line_metrics(x)
        self.assertGreater(m["clip_pct"], 1.0)
        self.assertGreater(m["clip_runs"], 100)
        ok, problems = cq.verdict({"sco": {"intervals": [[0, 3]], "codec": "mSBC"}, "lines": {"up": m}})
        self.assertTrue(any("клиппинг" in p for p in problems), problems)

    def test_noise_floor_and_snr(self):
        rnd = np.random.default_rng(5)
        x = speechish(6).astype(np.float64) + rnd.normal(0, 2000, 6 * RATE)
        m = cq.line_metrics(np.clip(x, -32768, 32767).astype(np.int16))
        self.assertLess(m["snr_db"], cq.MIN_SNR_DB)
        clean = cq.line_metrics(speechish(6))
        self.assertGreater(clean["snr_db"], 30)

    def test_verdict_norm_and_its_parts(self):
        good = cq.line_metrics(speechish(6))
        r = {"sco": {"intervals": [[0, 6]], "codec": "mSBC"}, "lines": {"down": good, "up": good},
             "stream": {"down": {"loss_pct": 0.0, "ovf": 0}, "up": {"loss_pct": 0.1, "ovf": 0, "floor_rms_channels": [90, 95]}},
             "events": {}, "latency": {"pipeline": {"median_ms": 90}}}
        ok, problems = cq.verdict(r)
        self.assertTrue(ok, problems)
        self.assertEqual(cq.verdict_line(ok, problems), "качество: норм")
        bad = json.loads(json.dumps(r))
        bad["sco"]["codec"] = "CVSD"
        bad["stream"]["down"]["loss_pct"] = 3.0
        bad["stream"]["up"]["floor_rms_channels"] = [450, 90]
        bad["events"] = {"sco_breaks": 1, "lyrat_resets": 1}
        bad["latency"]["pipeline"]["median_ms"] = 400
        ok, problems = cq.verdict(bad)
        self.assertFalse(ok)
        text = " ".join(problems)
        for word in ("CVSD", "потери 3.00", "шум на линии микрофона", "рвался", "LyraT перезагружалась", "задержка"):
            self.assertIn(word, text)
        none = cq.verdict({"sco": {"intervals": []}, "lines": {}})
        self.assertIn("не открывался", none[1][0])


class FilesTest(unittest.TestCase):
    def test_directions_shifted_come_out_aligned(self):
        """The recorder's two WAV files start at different times; t0_at puts them on one line."""
        out = TMP / "files"
        out.mkdir()
        base = clicks(10 * RATE)
        late = 1234                                  # the mic's file starts 1234 samples later on the bus
        down = base
        up_stereo = np.repeat(base[late:], 2).reshape(-1, 2)
        for name, data, ch in (("call.wav", down, 1), ("mic.wav", up_stereo.reshape(-1), 2)):
            with wave.open(str(out / name), "wb") as w:
                w.setnchannels(ch)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(np.asarray(data, dtype="<i2").tobytes())
        t0 = 50.0
        tap = {"call": {"t0_at": t0, "gaps": [], "first_frame": 1000}, "mic": {"t0_at": t0 + late / RATE, "gaps": [[100, 50]], "first_frame": 1000 + late - 37}}
        intervals = [[t0 + 2.0, t0 + 8.0]]
        f = call_check.build_files(out, "x", tap, intervals)
        self.assertEqual(f["seconds"], 8.0)                              # 6 s of channel and 1 s each side
        both = call_check._read(out / "x_both.wav")
        lag = lag_of(both)
        self.assertGreater(len(lag), 20)
        self.assertTrue((np.abs(lag) <= 1).all(), lag)
        self.assertEqual(f["counter_offset"], 37)
        self.assertEqual(f["sco"], [[1.0, 7.0]])
        self.assertFalse((out / "call.wav").exists())                      # the raw files go once the three are made


class PipelineTest(unittest.TestCase):
    def run_pipe(self, events, seconds, t0=100.0, hold=0.06):
        c = Collect()
        pipe = cs.LivePipeline([c], hold_s=hold)
        drive(pipe, events, t0, t0 + seconds + 1.0)
        return pipe, c

    def test_offset_jitter_and_loss(self):
        for D, jitter, loss in ((-37, 0.01, 0.0), (0, 0.002, 0.0), (-1234, 0.02, 0.02)):
            with self.subTest(D=D, jitter=jitter, loss=loss):
                pipe, c = self.run_pipe(synth_events(8, D=D, jitter=jitter, loss=loss), 8)
                out = np.concatenate([b.pcm for b in c.blocks])
                lag = lag_of(out[RATE:7 * RATE])
                paired = lag[np.abs(lag) < 1000]          # a click lost with its block has no pair
                self.assertGreaterEqual(len(paired), 0.8 * len(lag), (D, lag))
                self.assertTrue(len(paired) > 10 and (np.abs(paired) <= 16).all(), (D, lag))          # 1 ms
                self.assertEqual(abs(pipe.stats["align_frames"] - (-D)) <= 2, True, pipe.stats["align_frames"])
                lat = [b.emitted_at - b.t_sound for b in c.blocks[50:-60]]
                self.assertLess(max(lat), 0.060 + 0.020 + 0.015 + 0.005, max(lat))     # hold + block + S3 block + tick
                if loss:
                    self.assertGreater(pipe.stats["blocks_no_down"] + pipe.stats["blocks_no_up"], 0)

    def test_s3_restart_and_stopped_bus(self):
        ev = synth_events(4, D=-20, jitter=0.003, seed=4)
        # The S3 restarts: new boot id, counters from zero, 1 s later.
        ev += synth_events(3, D=-55, jitter=0.003, seed=5, t0=105.0, boot=9)
        # The bus stops 1.5 s (a LyraT reset): the counters stand still, then go on.
        ev += synth_events(2, D=-55, jitter=0.003, seed=6, t0=109.5, boot=9, frame0=3 * RATE, seq0=1000)
        pipe, c = self.run_pipe(ev, 11.5)
        self.assertGreaterEqual(pipe.segments["call"], 3)
        breaks = [b for b in c.blocks if b.flags & cs.F_BREAK]
        self.assertGreaterEqual(len(breaks), 2)
        out = np.concatenate([b.pcm for b in c.blocks])
        # Time keeps its pace across both: the output covers the PC time, not just the frames that came.
        self.assertAlmostEqual(len(out) / RATE, 11.5 + 1.0 - 0.1, delta=0.3)
        self.assertEqual(pipe.stats["align_frames"], 55)
        tail = out[-int(2.5 * RATE):-int(1.2 * RATE)]
        lag = lag_of(tail)
        self.assertTrue(len(lag) and (np.abs(lag) <= 2).all(), lag)

    def test_latency_of_the_call_only(self):
        lat = cs.LatencyStats()
        for i in range(100):
            t = 10.0 + i * 0.02
            lat.consume(cs.Block(i, i * 320, None, 0, t - (0.7 if i < 10 else 0.05), t))   # a slow start before the call
        self.assertAlmostEqual(lat.summary()["max_ms"], 700.0, delta=0.1)
        during = lat.summary(within=[[10.5, 12.0]])
        self.assertAlmostEqual(during["max_ms"], 50.0, delta=0.1)
        self.assertEqual(during["n"], 75)

    def test_late_packet_is_counted_not_placed(self):
        ev = synth_events(3, D=0, jitter=0.001, seed=7)
        t, data = ev[100]
        ev[100] = (t + 0.5, data)                      # half a second late
        pipe, c = self.run_pipe(ev, 3)
        self.assertEqual(sum(pipe.stats["late"].values()), 1)


class PcmServerTest(unittest.TestCase):
    def test_client_reads_the_stream(self):
        server = cs.PcmServer(0)
        pipe = cs.LivePipeline([server])
        got = {}
        t = threading.Thread(target=lambda: got.update(cs.listen(server.port, seconds=10, wav=TMP / "client.wav")), daemon=True)
        t.start()
        time.sleep(0.5)
        # Real time: packets fed as their arrival comes on this PC's clock.
        now = time.monotonic()
        ev = synth_events(3, D=-10, jitter=0.002, seed=8, t0=now)
        pipe.start()
        for at, data in sorted(ev, key=lambda e: e[0]):
            delay = at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            pipe.on_packet("x", data, at)
        time.sleep(0.5)
        pipe.stop()
        t.join(timeout=10)
        self.assertTrue(got.get("connected"))
        self.assertEqual(got["hello"]["layout"], ["down", "up"])
        self.assertEqual(got["index_gaps"], 0)
        self.assertGreater(got["seconds"], 2.5)
        self.assertLess(got["latency"]["median_ms"], 150)
        with wave.open(str(TMP / "client.wav"), "rb") as w:
            self.assertEqual((w.getnchannels(), w.getframerate()), (2, RATE))
            self.assertEqual(w.getnframes(), got["frames"])
        self.assertTrue(server.sock.fileno() == -1)


class EndToEndTest(unittest.TestCase):
    """call_check.py as the owner runs it, against the simulator: the channel opens and closes on the simulated LyraT."""

    @classmethod
    def setUpClass(cls):
        import simproc
        os.environ["BENCH_S3_ADDR"] = "127.0.0.1"
        cls.simproc = simproc

    def one(self, name, *sim_args, open_s=8.0):
        import s3_uart
        out = TMP / name
        with self.simproc.Sim(*sim_args):
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            p = subprocess.Popen([sys.executable, str(BENCH / "call_check.py"), "--out", str(out), "--no-phone", "--grace", "2",
                                  "--pcm-port", "0"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                                 text=True, encoding="utf-8", env=env)
            lines = []
            threading.Thread(target=lambda: lines.extend(p.stdout), daemon=True).start()
            end = time.monotonic() + 60
            while not any("ЗВОНИТЕ" in x for x in lines) and p.poll() is None and time.monotonic() < end:
                time.sleep(0.2)
            self.assertTrue(any("ЗВОНИТЕ" in x for x in lines), "".join(lines))
            ch = s3_uart.Channel()
            ch.find(timeout=3)
            time.sleep(1.0)
            self.assertEqual(ch.command("simsco 1", timeout=0.5, retries=5)["status"], 0)
            time.sleep(open_s)
            self.assertEqual(ch.command("simsco 0", timeout=0.5, retries=5)["status"], 0)
            ch.close()
            p.wait(timeout=90)
        text = "".join(lines)
        self.assertNotIn("Traceback", text)
        return p.returncode, json.loads((out / "report.json").read_text(encoding="utf-8")), text, out

    def test_call_recorded_aligned_and_reported(self):
        rc, r, text, out = self.one("e2e", "--mic-frame-offset", "37", "--clicks")
        for word in ("[ок] S3 отвечает", "[ок] телефон подключён", "[ок] поток S3 включён", "[ок] часы S3 и ПК сведены", "вниз", "вверх"):
            self.assertIn(word, text)
        self.assertEqual(r["sco"]["opens"], 1)
        self.assertAlmostEqual(r["sco"]["seconds"], 8.0, delta=1.0)
        self.assertEqual(r["sco"]["codec"], "mSBC")
        self.assertEqual((r["stream"]["down"]["lost"], r["stream"]["up"]["lost"]), (0, 0))
        self.assertTrue(r["alignment"]["agree"], r["alignment"])
        self.assertLessEqual(abs(r["alignment"]["counter_offset_files"] - 37), 16)
        self.assertTrue(r["latency"]["s3_mark"]["consistent"], r["latency"]["s3_mark"])
        self.assertLess(r["latency"]["pipeline"]["median_ms"], 300)
        for k in ("down", "up", "both"):
            self.assertTrue(Path(r["files"][k]).exists())
        both = call_check._read(r["files"]["both"])
        self.assertAlmostEqual(len(both) / RATE, r["sco"]["seconds"] + 2.0, delta=0.5)
        lag = lag_of(both)
        self.assertGreater(len(lag), 10)
        self.assertTrue((np.abs(lag) <= 16).all(), lag)          # 1 ms
        self.assertIn(r["verdict"]["line"].split(":")[0], "качество")
        self.assertIn(rc, (0, 1))
        md = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn(r["verdict"]["line"], md)

    def test_losses_are_counted_and_judged(self):
        rc, r, text, out = self.one("e2e_loss", "--stream-loss", "0.05", open_s=6.0)
        self.assertGreater(r["stream"]["down"]["lost"], 0)
        self.assertGreater(r["stream"]["down"]["loss_pct"], 2.0)
        self.assertFalse(r["verdict"]["ok"])
        self.assertIn("потери", r["verdict"]["line"])
        self.assertEqual(rc, 1)
        # Directions: the call carries 1 kHz, the mic 440 Hz and 2 kHz (the simulator's lines).
        for key, want in (("down", (1000,)), ("up", (440, 2000))):
            x = call_check._read(r["files"][key])[:, 0].astype(float)[RATE:3 * RATE]
            spec = np.abs(np.fft.rfft(x))
            hz = (np.argmax(spec[40:]) + 40) * RATE / len(x)
            self.assertTrue(any(abs(hz - w) < 5 for w in want), (key, hz))

class RealCallGuardTest(unittest.TestCase):
    """A run without a call stops and keeps no sound when a real call reaches the phone (R3/R6)."""

    @staticmethod
    def states(*seq):
        it = iter(seq)
        last = [None]

        def state():
            last[0] = next(it, last[0])
            return last[0]
        return state

    def idle(self, history=10):
        return {"calls_now": 0, "telephony_busy": 0, "historical_lines": history, "telephony_states": 2}

    def test_ringing_or_active_call_trips(self):
        import owner_common as oc
        for busy in ({"telephony_busy": 1}, {"calls_now": 1}):
            g = oc.RealCallGuard(state=self.states(self.idle(), self.idle(), dict(self.idle(), **busy)), every=0.01).start()
            time.sleep(0.2)
            self.assertIn("звонок", g.stop() or "")

    def test_call_come_and_gone_between_looks_trips(self):
        import owner_common as oc
        g = oc.RealCallGuard(state=self.states(self.idle(10), self.idle(10), self.idle(14)), every=0.01).start()
        time.sleep(0.2)
        self.assertIn("история", g.stop())

    def test_quiet_phone_and_unknown_state_do_not_trip(self):
        import owner_common as oc
        g = oc.RealCallGuard(state=self.states(self.idle(), None, self.idle()), every=0.01).start()
        time.sleep(0.1)
        self.assertIsNone(g.stop())
        self.assertGreaterEqual(g.unknown, 1)

    def test_run_stops_at_once_and_the_sound_goes(self):
        import owner_common as oc

        class Rec:
            intervals, view, silences, watch, t_start = [[0.0, None]], True, 0, None, time.monotonic()

            def tick(self):
                return None
        cc = call_check.CallCheck.__new__(call_check.CallCheck)
        cc.rec, cc.args, cc.say = Rec(), type("A", (), {"seconds": None, "grace": 2})(), lambda *a: None
        cc.meter, cc.latency, cc.counter = cs.LiveMeter(), cs.LatencyStats(), call_check.Counter()
        cc.live_last, cc.step = 0.0, type("S", (), {"log": open(os.devnull, "w", encoding="utf-8")})()
        cc.live_line = lambda *a, **k: None             # the console of the test may not take Cyrillic

        class Driver:
            def begin(self):
                pass

            def tick(self, now):
                pass

            def timed_out(self, now):
                return False

            def end(self):
                self.ended = True
        guard = oc.RealCallGuard(state=self.states(self.idle(), dict(self.idle(), telephony_busy=1)), every=0.05).start()
        d = Driver()
        t0 = time.monotonic()
        reason = cc.run(d, guard=guard)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(reason.startswith("настоящий звонок"), reason)
        self.assertTrue(d.ended)
        guard.stop()
        run = TMP / "discard"
        (run / "sub").mkdir(parents=True)
        for name in ("a_down.wav", "sub/mic.wav", "packets.journal", "report.json"):
            (run / name).write_bytes(b"x")
        gone = oc.discard_audio(run)
        self.assertEqual(sorted(gone), ["a_down.wav", "mic.wav", "packets.journal"])
        self.assertTrue((run / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
