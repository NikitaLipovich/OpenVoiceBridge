"""The LyraT's microphone mute (goal 111): the C rules of main/mic_mute.c through otahost.dll (key debounce against the
ESP32 GPIO36 erratum, short press toggles on release, long and stuck presses do not; the command "mute"; the ramp to
silence and back without a click, the frames keep their size), the glue in main.c read from the source (key, LED,
heartbeat bit, log line), and the PC side: bench_events, benchd status, call_check mute intervals and s3_uart.
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "ota_test"))
sys.path.insert(0, str(HERE.parent / "bench"))
import otahost as H  # noqa: E402

MAIN = HERE.parent.parent / "main"
SRC = (MAIN / "main.c").read_text(encoding="utf-8")
HDR = (MAIN / "mic_mute.h").read_text(encoding="utf-8")


def const(name):
    return int(re.search(r"#define\s+%s\s+(\d+)" % name, HDR).group(1))


POLL, DEB, SHORT, RAMP = const("MM_POLL_MS"), const("MM_DEBOUNCE"), const("MM_SHORT_MAX_MS"), const("MM_RAMP_SAMPLES")


def press(m, t, hold_ms, polls_after=10):
    """Key down for hold_ms then up, sampled every POLL ms from t. Returns (results, t after)."""
    out = []
    end = t + hold_ms
    while t < end:
        out.append(m.key(True, t))
        t += POLL
    for _ in range(polls_after):
        out.append(m.key(False, t))
        t += POLL
    return [r for r in out if r != "none"], t


class Key(unittest.TestCase):
    def test_short_press_toggles_on_release(self):
        m = H.CMicMute()
        got, t = press(m, 0, 200)
        self.assertEqual(got, ["toggle"])
        got, t = press(m, t, 1500)
        self.assertEqual(got, ["toggle"])

    def test_nothing_while_held(self):
        m = H.CMicMute()
        t = 0
        for _ in range(100):
            self.assertEqual(m.key(True, t), "none")
            t += POLL

    def test_long_press_does_not_toggle(self):
        m = H.CMicMute()
        got, _ = press(m, 0, SHORT + 100)
        self.assertEqual(got, ["long"])

    def test_stuck_low_never_toggles(self):
        m = H.CMicMute()
        t = 0
        res = set()
        for _ in range(10 * 60 * 100):   # 10 min stuck pressed from boot
            res.add(m.key(True, t))
            t += POLL
        self.assertEqual(res, {"none"})

    def test_erratum_glitches_ignored(self):
        """GPIO36 reads 0 for one sample now and then (the ~80 ns pull of the erratum caught by a poll): no press."""
        m = H.CMicMute()
        t = 0
        run = 0
        for i in range(20000):
            pressed = i % 7 == 0 or i % 11 == 0 or (i % 50 in (24, 25))
            run = run + 1 if pressed else 0
            if run > DEB - 1:              # at most DEB-1 low samples in a row: more is a real press
                pressed, run = False, 0
            self.assertEqual(m.key(pressed, t), "none", "sample %d" % i)
            t += POLL

    def test_bounce_on_press_and_release(self):
        m = H.CMicMute()
        t = 0
        seq = [1, 0, 1, 0, 1, 1, 1, 1] + [1] * 20 + [0, 1, 0, 1, 0, 0, 0, 0] + [0] * 5
        res = []
        for v in seq:
            res.append(m.key(bool(v), t))
            t += POLL
        self.assertEqual([r for r in res if r != "none"], ["toggle"])

    def test_debounce_needs_all_samples(self):
        m = H.CMicMute()
        t = 0
        for _ in range(DEB - 1):
            m.key(True, t)
            t += POLL
        m.key(False, t)               # the run breaks: not a press
        got, _ = press(m, t + POLL, 0, 10)
        self.assertEqual(got, [])

    def test_clock_wrap(self):
        m = H.CMicMute()
        got, _ = press(m, 0xFFFFFFFF - 50, 200)
        self.assertEqual(got, ["toggle"])


class Command(unittest.TestCase):
    def test_words(self):
        m = H.CMicMute()
        self.assertEqual(m.command(""), (0, "mute=off"))
        self.assertEqual(m.command("?"), (0, "mute=off"))
        self.assertEqual(m.command("on"), (1, "mute=on (was off)"))
        self.assertTrue(m.muted)
        self.assertEqual(m.command("on"), (0, "mute=on (was on)"))
        self.assertEqual(m.command("toggle"), (1, "mute=off (was on)"))
        self.assertEqual(m.command(" toggle"), (1, "mute=on (was off)"))
        self.assertEqual(m.command("off"), (1, "mute=off (was on)"))
        self.assertEqual(m.command("1")[0], -1)
        self.assertIn("usage", m.command("onn")[1])
        self.assertFalse(m.muted)

    def test_set(self):
        m = H.CMicMute()
        self.assertFalse(m.set(H.MM_OFF))
        self.assertTrue(m.set(H.MM_TOGGLE))
        self.assertTrue(m.muted)
        self.assertFalse(m.set(H.MM_ON))


class Ramp(unittest.TestCase):
    N = 120     # samples per frame of the mic task (7.5 ms at 16 kHz)

    def tone(self, frames, amp=16000):
        return [[int(amp * math.sin(2 * math.pi * 1000 * (f * self.N + i) / 16000)) for i in range(self.N)]
                for f in range(frames)]

    def test_unmuted_is_bit_exact(self):
        m = H.CMicMute()
        for fr in self.tone(5):
            self.assertEqual(m.apply(fr), fr)

    def test_mute_ramps_to_zero_then_zeros(self):
        m = H.CMicMute()
        m.set(H.MM_ON)
        frames = self.tone(10)
        out = [m.apply(fr) for fr in frames]
        flat = sum(out, [])
        self.assertTrue(all(len(o) == self.N for o in out))            # the frames keep coming, same size
        self.assertTrue(all(v == 0 for v in flat[RAMP:]))               # silence after the ramp
        self.assertEqual(m.gain, 0)
        # no click: |out| never exceeds |in|, and the envelope falls monotonically
        src = sum(frames, [])
        self.assertTrue(all(abs(o) <= abs(s) for o, s in zip(flat, src)))
        env = [abs(o) / max(1, abs(s)) for o, s in zip(flat[:RAMP], src[:RAMP]) if abs(s) > 2000]
        self.assertTrue(all(b <= a + 0.01 for a, b in zip(env, env[1:])))

    def test_unmute_ramps_back(self):
        m = H.CMicMute()
        m.set(H.MM_ON)
        for fr in self.tone(4):
            m.apply(fr)
        m.set(H.MM_OFF)
        frames = self.tone(6, amp=20000)
        flat = sum([m.apply(fr) for fr in frames], [])
        src = sum(frames, [])
        self.assertEqual(flat[RAMP + 1:], src[RAMP + 1:])
        self.assertEqual(m.gain, H.CMicMute().gain)
        # largest step between neighbours stays that of the tone itself (no jump from the ramp)
        max_src = max(abs(a - b) for a, b in zip(src, src[1:]))
        self.assertLessEqual(max(abs(a - b) for a, b in zip(flat, flat[1:])), max_src + 1)

    def test_ramp_is_10ms(self):
        self.assertEqual(RAMP, 160)        # 10 ms at 16 kHz

    def test_full_scale_no_overflow(self):
        m = H.CMicMute()
        m.set(H.MM_ON)
        out = m.apply([32767, -32768] * 100)
        self.assertTrue(all(-32768 <= v <= 32767 for v in out))
        self.assertTrue(all(abs(v) <= 32768 for v in out))


class Glue(unittest.TestCase):
    """What main.c must keep: the key polled (no interrupt), the LED, the mute after the tone, the heartbeat bit."""

    def test_key_and_led_pins(self):
        self.assertRegex(SRC, r"#define MM_KEY_PIN\s+GPIO_NUM_36")
        self.assertRegex(SRC, r"#define MM_LED_PIN\s+GPIO_NUM_22")
        self.assertIn(".intr_type = GPIO_INTR_DISABLE", SRC)
        self.assertNotIn("gpio_isr_handler_add(MM_KEY_PIN", SRC)
        self.assertNotRegex(SRC, r"GPIO_NUM_39")       # Mode stays free for the mode switch

    def test_mute_after_tone_before_send(self):
        task = SRC[SRC.index("static void mic_rx_task"):SRC.index("// HFP Client callback")]
        a, b, c = task.index("tone_fill("), task.index("mm_apply(&s_mm"), task.index("xRingbufferSend(s_rb_mic_tx")
        self.assertLess(a, b)
        self.assertLess(b, c)

    def test_heartbeat_bit(self):
        self.assertIn("(s_mm.muted ? 4 : 0)", SRC)

    def test_log_line(self):
        self.assertIn('"MIC MUTE %s src=%s sco=%d"', SRC)


class LogLine(unittest.TestCase):
    """The firmware's line, as the PC tools read it."""
    LINE = "I (123456) AUDIO: MIC MUTE on src=key sco=1"

    def test_event(self):
        import bench_events
        ev = bench_events.EventIndex().feed({"board": 1, "boot": 1, "seq": 1, "text": self.LINE})
        kinds = [e["type"] for e in ev]
        self.assertIn("mic_mute", kinds)
        e = [e for e in ev if e["type"] == "mic_mute"][0]
        self.assertEqual((e["state"], e["src"]), ("on", "key"))

    def test_parse(self):
        import call_record
        self.assertEqual(call_record.mic_mute_of(self.LINE), ("on", "key"))
        self.assertEqual(call_record.mic_mute_of("I (1) AUDIO: MIC MUTE off src=cmd sco=0"), ("off", "cmd"))
        self.assertEqual(call_record.mic_mute_of("I (1) AUDIO: MIC MUTE off src=boot key=GPIO36(1) led=GPIO22"),
                         ("off", "boot"))
        self.assertIsNone(call_record.mic_mute_of("I (1) AUDIO: MIC f=50 pk=10"))


class Intervals(unittest.TestCase):
    """call_check: the mute intervals and their effect on the up line."""

    def test_intervals_from_marks(self):
        import call_record as cr
        marks = [(10.0, True, "key"), (12.0, True, "key"), (15.0, False, "cmd"), (20.0, True, "key")]
        self.assertEqual(cr.mute_spans(marks, 30.0), [[10.0, 15.0], [20.0, 30.0]])
        self.assertEqual(cr.mute_spans([(1.0, False, "x"), (5.0, False, "x")], 9.0), [])
        self.assertEqual(cr.mute_spans([], 9.0), [])

    def test_live_mark(self):
        import call_check as cc
        self.assertEqual(cc.mute_mark(True), " | МИКРОФОН ВЫКЛ")
        self.assertEqual(cc.mute_mark(False), "")
        self.assertEqual(cc.mute_mark(None), "")

    def test_up_zeroed_and_not_a_defect(self):
        import numpy as np
        import call_check as cc
        import call_quality as cq
        rate = cc.RATE
        t = np.arange(rate * 10) / rate
        up = (8000 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)
        muted = [[3.0, 6.0]]
        z = cc.zero_muted(up, muted)
        self.assertTrue(np.all(z[3 * rate:6 * rate] == 0))
        self.assertTrue(np.array_equal(z[:3 * rate], up[:3 * rate]))
        gaps = cc.mute_gaps(muted)
        plain = cq.line_metrics(z, [])
        judged = cq.line_metrics(z, gaps)
        self.assertGreater(plain["dropouts"], 0)          # zeros read as a drop-out without the marks
        self.assertEqual(judged["dropouts"], 0)           # with them the muted part is no defect
        cut, g = cc.cut_muted(z, [[int(1.0 * rate), 160], [int(4.0 * rate), 160], [int(5.9 * rate), 3200]], muted)
        self.assertEqual(len(cut), 7 * rate)
        self.assertEqual(g, [[rate, 160], [3 * rate, 1600]])   # the one inside goes, the one across is clipped

    def report(self, muted, seconds=10):
        import numpy as np
        import call_check as cc
        rate = cc.RATE
        t = np.arange(rate * seconds) / rate
        env = (np.sin(2 * np.pi * 0.7 * t) > 0).astype(float) * 0.9 + 0.1      # bursts and pauses, like speech
        x = (6000 * env * np.sin(2 * np.pi * 300 * t)).astype(np.int16)
        files = {"down": x, "up": cc.zero_muted(x, muted), "gaps": {"down": [], "up": []}, "sco": [[0.0, float(seconds)]],
                 "muted": muted, "paths": {"down": "d.wav", "up": "u.wav", "both": "b.wav"}, "seconds": seconds,
                 "shift_ms": 0, "counter_offset": 0}
        r = cc.build_report({}, {"call": {}, "mic": {}}, files, {"align_frames": 0}, {}, {}, [])
        return r, cc.render_md(r)

    def test_report_marks_mute_and_no_defect(self):
        r, md = self.report([[3.0, 6.0]])
        self.assertEqual((r["mute"]["count"], r["mute"]["seconds"], r["mute"]["whole"]), (1, 3.0, False))
        self.assertEqual(r["lines"]["up"]["dropouts"], 0)
        self.assertAlmostEqual(r["lines"]["up"]["seconds"], 7.0, delta=0.01)
        self.assertFalse([p for p in r["verdict"]["problems"] if "микрофон" in p], r["verdict"])
        self.assertIn("Микрофон выключался", md)
        self.assertIn("3.0–6.0 с", md)

    def test_report_whole_call_muted(self):
        r, _ = self.report([[0.0, 10.0]])
        self.assertTrue(r["mute"]["whole"])
        self.assertFalse([p for p in r["verdict"]["problems"] if "микрофон" in p], r["verdict"])

    def test_report_without_mute_unchanged(self):
        r, md = self.report([])
        self.assertEqual(r["mute"]["count"], 0)
        self.assertNotIn("Микрофон выключался", md)


class Status(unittest.TestCase):
    """The heartbeat bit in benchd's /status, the phone's direct mode (s3_direct.py = S3Direct.java), the panel."""

    def test_flag(self):
        import benchd
        self.assertEqual([benchd.flag_muted(f) for f in (5, 7, 4, 1, 3, 0, -1, None)],
                         [True, True, True, False, False, False, None, None])

    def test_direct(self):
        java = (HERE.parent / "android" / "sco_probe" / "src" / "com" / "openvoicebridge" / "scoprobe" /
                "S3Direct.java").read_text(encoding="utf-8")
        self.assertIn('ly.put("mic_muted", flags instanceof Long && (Long) flags >= 0 ? ((Long) flags & 4) != 0 : null)', java)
        src = (HERE.parent / "bench" / "s3_direct.py").read_text(encoding="utf-8")
        self.assertIn('"mic_muted": bool(flags & 4) if isinstance(flags, int) and flags >= 0 else None', src)

    def test_action_and_ui(self):
        import benchd
        self.assertEqual(benchd.ACTIONS["mic_mute"]["params"], {"mode": ["on", "off", "toggle"]})
        spec = json.loads((HERE.parent / "bench" / "bench_ui_spec.json").read_text(encoding="utf-8"))
        acts = {a["id"]: a for a in spec["actions"]}
        self.assertEqual(acts["mic_off"]["body"], {"name": "mic_mute", "mode": "on"})
        self.assertEqual(acts["mic_on"]["body"], {"name": "mic_mute", "mode": "off"})
        self.assertEqual(acts["mic_off"]["direct"], {"op": "uc", "text": "mute on"})
        self.assertEqual(acts["mic_on"]["direct"], {"op": "uc", "text": "mute off"})

    def test_indicator(self):
        """A visible state, not only buttons: red when muted, green when on, a dash with no heartbeat."""
        import gen_bench_ui as g
        spec = json.loads((HERE.parent / "bench" / "bench_ui_spec.json").read_text(encoding="utf-8"))
        f = [f for p in spec["panels"] for f in p["fields"] if f["id"] == "mic"][0]

        def show(v):
            for c in f["cases"]:
                if g.holds(c.get("if", []), {"lyrat": {"mic_muted": v}}):
                    return c["text"], c.get("color")
        self.assertEqual(show(True)[1], "bad")
        self.assertIn("ВЫКЛЮЧЕН", show(True)[0])
        self.assertEqual(show(False), ("включён", "ok"))
        self.assertEqual(show(None)[0], "—")


class Simulator(unittest.TestCase):
    """End to end against the S3 simulator: s3_uart.py mute, the heartbeat bit, call_check over a muted stretch."""

    @classmethod
    def setUpClass(cls):
        import simproc
        cls.simproc = simproc
        cls.tmp = Path(tempfile.mkdtemp(prefix="t111_mute_"))

    def test_s3_uart_mute_and_flag(self):
        from simproc import last_json, run_tool
        with self.simproc.Sim():
            rc, out, err, _ = run_tool("s3_uart.py", "mute")
            self.assertEqual(rc, 0, err)
            self.assertIn("mute=off", out)
            rc, out, _, _ = run_tool("s3_uart.py", "mute", "on")
            self.assertEqual(rc, 0, out)
            self.assertIn("mute=on (was off)", out)
            flags = None
            for _ in range(20):
                rc, out, _, _ = run_tool("s3_uart.py", "query")
                flags = (last_json(out) or {}).get("lyrat_flags")
                if isinstance(flags, int) and flags & 4:
                    break
                time.sleep(0.3)
            self.assertTrue(isinstance(flags, int) and flags & 4, flags)
            rc, out, _, _ = run_tool("s3_uart.py", "mute", "toggle")
            self.assertIn("mute=off (was on)", out)

    def test_call_check_marks_the_muted_stretch(self):
        import s3_uart
        import call_check as cc
        out = self.tmp / "call"
        os.environ["BENCH_S3_ADDR"] = "127.0.0.1"
        with self.simproc.Sim():
            env = dict(self.simproc.env(), PYTHONIOENCODING="utf-8")
            p = subprocess.Popen([sys.executable, str(HERE.parent / "bench" / "call_check.py"), "--out", str(out),
                                  "--no-phone", "--grace", "2", "--pcm-port", "0"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.PIPE, text=True, encoding="utf-8", env=env)
            lines = []
            reader = threading.Thread(target=lambda: lines.extend(p.stdout), daemon=True)
            reader.start()
            end = time.monotonic() + 60
            while not any("ЗВОНИТЕ" in x for x in lines) and p.poll() is None and time.monotonic() < end:
                time.sleep(0.2)
            self.assertTrue(any("ЗВОНИТЕ" in x for x in lines), "".join(lines))
            ch = s3_uart.Channel()
            ch.find(timeout=3)
            time.sleep(1.0)
            self.assertEqual(ch.command("simsco 1", timeout=0.5, retries=5)["status"], 0)
            time.sleep(1.5)
            self.assertEqual(ch.command("mute on", timeout=0.5, retries=5)["status"], 0)
            time.sleep(5.5)
            self.assertEqual(ch.command("mute off", timeout=0.5, retries=5)["status"], 0)
            time.sleep(3.0)
            self.assertEqual(ch.command("simsco 0", timeout=0.5, retries=5)["status"], 0)
            ch.close()
            p.wait(timeout=90)
            reader.join(timeout=5)
            p.stdout.close()
            p.stdin.close()
        text = "".join(lines)
        self.assertNotIn("Traceback", text)
        self.assertIn("микрофон LyraT ВЫКЛЮЧЕН", text)
        self.assertIn("микрофон LyraT включён", text)
        self.assertIn("МИКРОФОН ВЫКЛ", text)                # the live line
        self.assertIn("Микрофон выключен", text)            # the summary
        r = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(r["mute"]["count"], 1, r["mute"])
        self.assertAlmostEqual(r["mute"]["seconds"], 5.5, delta=1.2)
        self.assertEqual(r["lines"]["up"]["dropouts"], 0)
        up = cc._read(r["files"]["up"])[:, 0]
        a, b = r["mute"]["intervals"][0]
        mid = up[int((a + 0.5) * cc.RATE):int((b - 0.5) * cc.RATE)]
        self.assertTrue(len(mid) > cc.RATE and not mid.any())     # zeroed: the phone got silence there
        self.assertTrue(up[int((b + 0.5) * cc.RATE):int((b + 1.5) * cc.RATE)].any())
        self.assertIn("Микрофон выключался", (out / "report.md").read_text(encoding="utf-8"))
        s = json.loads((out / "call_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(len(s["mic_mute"]), 1)


if __name__ == "__main__":
    unittest.main()
