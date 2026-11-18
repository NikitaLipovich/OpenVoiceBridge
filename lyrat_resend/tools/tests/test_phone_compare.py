"""phone_compare.py and call_check.py --compare without the bench (goal 099).

A synthetic pair stands for the LyraT mic before Bluetooth and the phone's recording: music-like notes and speech-like
bursts; the phone side is delayed by a fraction of a sample, its clock 80 ppm off, a slow gain and a filter on it, and noise
25 dB under the signal like a codec. Lost frames are knocked out at known times the ways the phone shows them (a repeat of
the frame before, a repeat that fades, zeros, a click, a deep dip) and must be found within one mSBC frame (7.5 ms) with
their type, with nothing found on the clean pair. Also: a jump of the time line, a pair that does not belong together (no
alignment, no defects looked for), the files and the report, the check against tone_blocks on a tone with losses, and the
LyraT counters read from the console log. Plays nothing (R15), no phone, no adb.
"""
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bench"))
import call_check  # noqa: E402
import phone_compare as pc  # noqa: E402

RATE = 16000
TMP = Path(tempfile.mkdtemp(prefix="t099_"))
START_S, DELAY, PPM = 4.3, 37.4, 80.0


def source(seconds, seed=1):
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    x = np.zeros(n)
    for a in range(0, n, int(0.4 * RATE)):
        f0 = rng.choice([196, 220, 262, 294, 330, 392, 440])
        seg = slice(a, min(n, a + int(0.4 * RATE)))
        for h, amp in ((1, 1500), (2, 700), (3, 350)):
            x[seg] += amp * np.sin(2 * np.pi * f0 * h * t[seg] + rng.uniform(0, 6.28))
    w = np.fft.rfft(rng.normal(0, 1, n))
    f = np.fft.rfftfreq(n, 1 / RATE)
    shape = np.exp(-((f - 500) / 250) ** 2) + 0.6 * np.exp(-((f - 1500) / 400) ** 2) + 0.2 * np.exp(-((f - 3000) / 800) ** 2)
    sp = np.fft.irfft(w * shape, n)
    env = np.clip(np.sin(2 * np.pi * 4 * t), 0, None) ** 2 * (np.sin(2 * np.pi * 0.23 * t) > -0.3)
    return x + 4000 * sp / sp.std() * env + rng.normal(0, 30, n)


def channel(ref, seconds=20.0, seed=2, cut_at=None, cut=0):
    """The phone side: phone[j] = g(t) * h * ref(START + DELAY + j (1 + PPM)) + codec noise; cut drops samples at cut_at."""
    rng = np.random.default_rng(seed)
    j = np.arange(int(seconds * RATE) + cut, dtype=np.float64)
    y = np.interp(START_S * RATE + DELAY + j * (1 + PPM * 1e-6), np.arange(len(ref)), ref)
    y = y * (0.5 + 0.3 * np.sin(2 * np.pi * 0.7 * j / RATE))
    y = np.convolve(y, [1.0, -0.6], mode="same")
    b = 120
    m = len(y) // b * b
    rms = np.sqrt((y[:m].reshape(-1, b) ** 2).mean(1)).repeat(b)
    rms = np.concatenate([rms, np.full(len(y) - m, rms[-1])])
    y = y + rng.normal(0, 1, len(y)) * rms * 10 ** (-25 / 20) + rng.normal(0, 3, len(y))
    if cut_at is not None:
        y = np.delete(y, np.arange(cut_at, cut_at + cut))
    return y


PLAN = ((2.1, "repeat", "маскированный кадр", 120), (5.37, "repeat2", "маскированный кадр", 240), (8.8, "zeros", "тишина", 120),
        (11.15, "click", "щелчок", 40), (14.4, "dip", "провал", 320), (17.03, "repeat", "маскированный кадр", 120))


def knock(y, plan):
    y = y.copy()
    for t, kind, _, _ in plan:
        s = int(t * RATE)
        if kind == "repeat":
            y[s:s + 120] = y[s - 120:s]
        elif kind == "repeat2":
            y[s:s + 240] = np.concatenate([y[s - 120:s], y[s - 120:s] * 0.7])
        elif kind == "zeros":
            y[s:s + 120] = 0
        elif kind == "click":
            y[s:s + 40] += 12000 * np.sign(np.sin(np.arange(40)))
        elif kind == "dip":
            y[s:s + 320] *= 0.03
    return y


def truth_centre(res, t, length):
    """Where the middle of a defect at phone sample t*RATE lies on the reference line of the result, in seconds."""
    s = t * RATE + length / 2.0
    return (s + START_S * RATE + DELAY + s * PPM * 1e-6 - res["n0"]) / RATE


class Alignment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = source(30)
        cls.clean = channel(cls.ref)

    def test_clean_pair_converges_and_finds_nothing(self):
        res = pc.compare(self.ref, self.clean)
        al = res["alignment"]
        self.assertTrue(al["converged"], al["why"])
        self.assertAlmostEqual(al["ppm"], PPM, delta=5.0)
        self.assertLessEqual(al["residual"], pc.MAX_RESIDUAL)
        self.assertEqual(res["events"], [])
        self.assertEqual(al["jumps"], [])

    def test_expected_offset_narrows_the_search(self):
        res = pc.compare(self.ref, self.clean, expected=START_S * RATE + 300)
        self.assertTrue(res["alignment"]["converged"])
        self.assertAlmostEqual(res["n0"], START_S * RATE + DELAY, delta=2)

    def test_jump_of_the_time_line(self):
        y = channel(self.ref, cut_at=10 * RATE, cut=40)
        res = pc.compare(self.ref, y)
        self.assertTrue(res["alignment"]["converged"], res["alignment"]["why"])
        self.assertEqual(len(res["alignment"]["jumps"]), 1)
        self.assertAlmostEqual(res["alignment"]["jumps"][0]["step"], 40, delta=2)
        shift = [e for e in res["events"] if e["kind"].startswith("сдвиг")]
        self.assertEqual(len(shift), 1, res["events"])
        self.assertEqual(shift[0]["kind"], "сдвиг (пропал кадр)")
        self.assertAlmostEqual(shift[0]["t"], truth_centre(res, 10.0, 0), delta=0.01)
        self.assertEqual(len(res["events"]), 1, res["events"])     # the step is one defect, not a step and a distortion

    def test_unrelated_pair_does_not_converge(self):
        other = np.random.default_rng(9).normal(0, 2000, 20 * RATE)
        res = pc.compare(self.ref, other)
        self.assertFalse(res["alignment"]["converged"])
        self.assertIsNone(res["events"])
        self.assertIn("не сошлось", pc.summary_line({**res, "seconds": 20.0}))


# Local offsets [phone sample, offset, clarity] of two bench runs of goal 099 (numbers only, no sound): the phone line
# stepped twice by one mSBC frame (-120 samples); a lone window at the start off by +120 among unclear ones.
BENCH_STEPS = [[8000, 13502.706, 41.1], [16000, 13502.717, 37.0], [24000, 13502.708, 42.4], [32000, 13502.695, 43.6], [40000, 13502.722, 45.1], [48000, 13502.721, 44.8], [56000, 13502.703, 41.7], [64000, 13502.698, 37.8], [72000, 13502.713, 39.3], [80000, 13502.718, 36.5], [88000, 13502.712, 24.3], [96000, 13502.729, 14.4], [104000, 13382.667, 26.5], [112000, 13382.704, 43.1], [120000, 13382.707, 43.4], [128000, 13382.743, 37.4], [136000, 13382.733, 39.1], [144000, 13382.732, 40.8], [152000, 13382.715, 38.9], [160000, 13382.72, 43.0], [168000, 13382.73, 40.8], [176000, 13382.719, 45.2], [184000, 13382.723, 46.2], [192000, 13382.713, 39.5], [200000, 13382.732, 42.9], [208000, 13382.732, 42.1], [216000, 13382.705, 36.6], [224000, 13382.723, 42.4], [232000, 13382.708, 41.5], [240000, 13382.719, 45.0], [248000, 13382.722, 43.1], [256000, 13382.724, 44.0], [264000, 13382.718, 40.3], [272000, 13382.732, 16.2], [280000, 13262.734, 33.1], [288000, 13262.719, 41.8], [296000, 13262.713, 43.3], [304000, 13262.728, 43.6], [312000, 13262.724, 44.0]]
BENCH_OUTLIER = [[8000, 13500.762, 2.7], [16000, 13680.857, 3.4], [24000, 13680.673, 3.1], [32000, 13669.992, 3.0], [40000, 13657.796, 4.3], [48000, 13657.728, 24.0], [56000, 13537.68, 12.4], [64000, 13537.711, 44.4], [72000, 13537.712, 43.6], [80000, 13537.713, 45.8], [88000, 13537.71, 44.6], [96000, 13537.72, 45.8], [104000, 13537.716, 44.0], [112000, 13537.712, 43.5], [120000, 13537.704, 42.8], [128000, 13537.716, 39.3], [136000, 13537.698, 28.4], [144000, 13537.706, 40.6], [152000, 13537.701, 24.8], [160000, 13537.709, 44.8], [168000, 13537.721, 45.2], [176000, 13537.724, 44.3], [184000, 13537.724, 43.5], [192000, 13537.73, 39.4], [200000, 13537.725, 42.4], [208000, 13537.727, 41.3], [216000, 13537.737, 39.2], [224000, 13537.704, 41.7], [232000, 13537.715, 36.8], [240000, 13537.72, 40.2], [248000, 13537.721, 42.7], [256000, 13537.732, 36.9], [264000, 13537.737, 40.3], [272000, 13537.732, 43.5], [280000, 13537.713, 38.3], [288000, 13537.722, 38.4], [296000, 13537.736, 39.3], [304000, 13537.719, 39.1], [312000, 13537.704, 43.3]]


class Segments(unittest.TestCase):
    def test_two_steps_of_one_frame(self):
        fit = pc.fit_alignment(BENCH_STEPS)
        self.assertTrue(fit["converged"], fit["why"])
        self.assertEqual([round(j["step"]) for j in fit["jumps"]], [-120, -120])
        self.assertEqual([j["phone_sample"] for j in fit["jumps"]], [100000, 276000])
        self.assertAlmostEqual(fit["ppm"], 0.0, delta=1.0)

    def test_lone_window_is_an_outlier(self):
        fit = pc.fit_alignment(BENCH_OUTLIER)
        self.assertTrue(fit["converged"], fit["why"])
        self.assertEqual(fit["jumps"], [])
        self.assertEqual(fit["outliers"], [48000])
        self.assertAlmostEqual(fit["ppm"], 0.0, delta=1.0)


class Defects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = source(30)
        cls.res = pc.compare(cls.ref, knock(channel(cls.ref), PLAN))

    def test_each_knocked_frame_found_in_time_with_its_type(self):
        events = self.res["events"]
        self.assertEqual(len(events), len(PLAN), [(e["centre"], e["kind"]) for e in events])
        for t, _, kind, length in PLAN:
            want = truth_centre(self.res, t, length)
            near = [e for e in events if abs(e["centre"] - want) <= pc.FRAME / RATE]
            self.assertEqual(len(near), 1, "%.3f: %s" % (want, [(e["centre"], e["kind"]) for e in events]))
            self.assertEqual(near[0]["kind"], kind)

    def test_report_and_files(self):
        out = TMP / "run"
        out.mkdir(exist_ok=True)
        pkt = [{"t": 100.0 + k, "rx_none": k // 5, "rx_err": 0, "tx_discarded": 0} for k in range(30)]
        rep = pc.analyse(out, self.ref, knock(channel(self.ref), PLAN), None, start_at=100.0, pkt_log=pkt)
        self.assertEqual(len(rep["events"]), len(PLAN))
        with wave.open(str(out / "compare_both.wav"), "rb") as w:
            self.assertEqual((w.getnchannels(), w.getframerate()), (2, RATE))
            self.assertAlmostEqual(w.getnframes() / RATE, rep["seconds"], delta=0.01)
        cuts = sorted(out.glob("cut_*.wav"))
        self.assertEqual(len(cuts), len(PLAN))
        self.assertTrue(all("pkt_near" in e for e in rep["events"]))
        md = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("| время (мм:сс.мс)", md)
        self.assertIn("маскированный кадр", md)
        self.assertIn("до шумоподавления", md)
        self.assertEqual(json.loads((out / "report.json").read_text(encoding="utf-8"))["summary"], rep["summary"])
        for name in ("up_s3.wav", "phone.wav"):
            self.assertTrue((out / name).exists())


class OtherRecording(unittest.TestCase):
    def test_any_rate_from_the_command_line(self):
        """The other party's recording of a call may come at 8 kHz in stereo: the command brings it to 16 kHz mono."""
        ref = source(30)
        y = channel(ref)
        y8 = np.fft.irfft(np.fft.rfft(y)[: len(y) // 4 + 1], len(y) // 2) * 0.5
        d = TMP / "other"
        d.mkdir(exist_ok=True)
        pc.write_wav(d / "ref.wav", ref)
        st = np.stack([y8, y8], axis=1)
        with wave.open(str(d / "their.wav"), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(np.clip(np.round(st), -32768, 32767).astype("<i2").tobytes())
        self.assertEqual(pc.main(["--ref", str(d / "ref.wav"), "--phone", str(d / "their.wav"), "--out", str(d)]), 0)
        rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(rep["alignment"]["converged"], rep["alignment"]["why"])
        self.assertAlmostEqual(rep["alignment"]["ppm"], PPM, delta=5.0)
        self.assertEqual(rep["events"], [])
        with self.assertRaises(ValueError):
            pc.read_wav(d / "their.wav")


class ToneCheck(unittest.TestCase):
    def test_detector_finds_the_losses_tone_blocks_finds(self):
        """A btloss-like window: 1800 Hz on the phone, two frames lost and concealed (a burst and a repeat)."""
        rng = np.random.default_rng(3)
        n = 20 * RATE
        x = 8000 * np.sin(2 * np.pi * 1800 * np.arange(n) / RATE + 0.4) + rng.normal(0, 20, n)
        x[: int(0.3 * RATE)] = 0
        for t in (4.26, 12.5):
            s = int(t * RATE)
            x[s:s + 120] = x[s - 120:s] * np.hanning(120) + rng.normal(0, 3000, 120)
        d = TMP / "btloss-window"
        d.mkdir(exist_ok=True)
        pc.write_wav(d / "phone.wav", x)
        import tone_blocks
        known = tone_blocks.analyse(list(np.round(x).astype(int)), RATE, [1800])["event_list"]
        (d / "events.json").write_text(json.dumps({"up": known, "down": []}), encoding="utf-8")
        row = pc.tone_check(d)
        self.assertEqual(row["btloss_events"], 2, known)
        self.assertEqual((row["matched"], row["extra"]), (2, 0), row)


class Counters(unittest.TestCase):
    def test_pkt_rows_from_the_console_log(self):
        log = TMP / "serial.log"
        log.write_text("    3.374 I (1) BT: SCO pkt: rx_total=190 rx_correct=186 rx_err=1 rx_none=3 rx_lost=0 tx_total=183 "
                       "tx_discarded=0\n    3.500 I (2) other line\n    4.579 I (3) BT: SCO pkt: rx_total=328 rx_correct=320 "
                       "rx_err=4 rx_none=4 rx_lost=0 tx_total=321 tx_discarded=1\n", encoding="utf-8")
        rows = call_check.pkt_rows_console(log, 1000.0)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[1]["t"], 1004.579)
        self.assertEqual((rows[1]["rx_none"], rows[1]["tx_discarded"]), (4, 1))
        ev = [{"centre": 4.0}]
        pc.pkt_near(ev, rows, 1000.0 - 0.0, seconds=0.5)
        self.assertEqual(ev[0]["pkt_near"]["rx_none"], 1)

    def test_compare_flag_moves_seconds(self):
        called = {}
        real = call_check.run_compare
        call_check.run_compare = lambda args: called.update(cs=args.compare_seconds, s=args.seconds) or {"result": call_check.PASS}
        try:
            self.assertEqual(call_check.main(["--compare", "--seconds", "15"]), 0)
        finally:
            call_check.run_compare = real
        self.assertEqual(called, {"cs": 15, "s": None})


if __name__ == "__main__":
    unittest.main()
