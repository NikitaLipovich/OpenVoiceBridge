"""Pieces of the bench tools that need no network and no board: bench variants of fw.py, the sound analysis on synthetic
WAV files (tone, silence, noise), phone_mic.level, the floor of udp_record.py, and that every bench module loads.
Plays nothing (R15). Made by goal 092.
"""
import importlib
import math
import random
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "bench"
sys.path.insert(0, str(BENCH))
import audio_check  # noqa: E402
import fw  # noqa: E402
import udp_record  # noqa: E402

RATE = 16000
TMP = Path(tempfile.mkdtemp(prefix="t092_unit_"))


def write_wav(path, samples, channels=1, rate=RATE):
    a = np.asarray(samples, dtype="<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a.tobytes())
    return path


def tone(hz, seconds, level=8000, rate=RATE):
    n = int(seconds * rate)
    return (level * np.sin(2 * np.pi * hz * np.arange(n) / rate)).astype(int)


def noise(seconds, level=1000, seed=3):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * RATE)) * level).astype(int)


class FwVariants(unittest.TestCase):
    def test_known_names(self):
        self.assertEqual(fw.macro_of("bredr_p6"), ("BENCH_BREDR_POWER", 6))
        self.assertEqual(fw.macro_of("mac_b3p2"), ("BENCH_MAC_PATCH", 3 * 256 + 2))
        self.assertEqual(fw.macro_of("esco_pkt_x3c8"), ("BENCH_ESCO_PKT", 0x3C8))
        self.assertEqual(fw.macro_of("esco_lat_10"), ("BENCH_ESCO_LAT", 10))

    def test_hex_without_x_is_unknown_not_a_crash(self):      # goal 092 #003
        for name in ("esco_lat_1f", "esco_pkt_3c8", "esco_lat_", "esco_pkt_x", "mac_b6p1", "nothing"):
            self.assertIsNone(fw.macro_of(name), name)


class Sound(unittest.TestCase):
    def test_tone_sequence_passes(self):
        x = np.concatenate([tone(400, 1.5), tone(1000, 1.5), tone(1800, 1.5)])
        r = audio_check.check(list(x), RATE, [400, 1000, 1800])
        self.assertTrue(r["pass"], r)
        self.assertLess(abs(r["tones"]["1000"]["shift_pct"]), 0.2)
        self.assertGreater(r["tones"]["1000"]["snr_db"], 40)

    def test_silence_fails(self):
        r = audio_check.check([0] * RATE * 3, RATE, [1000])
        self.assertFalse(r["pass"])

    def test_noise_fails(self):
        r = audio_check.check(list(noise(3, 3000)), RATE, [1000])
        self.assertFalse(r["pass"])

    def test_wrong_rate_reads_as_missing_or_shifted(self):
        x = tone(1000 * 1.02, 3)        # a 2 % rate error
        r = audio_check.check(list(x), RATE, [1000], max_shift=1.0)
        self.assertFalse(r["pass"])

    def test_tone_in_noise_snr(self):
        x = tone(1000, 3, 4000) + noise(3, 400)
        r = audio_check.check(list(x), RATE, [1000], min_snr=15)
        self.assertTrue(r["pass"])
        self.assertLess(r["tones"]["1000"]["snr_db"], 30)

    def test_read_wav_takes_the_first_channel(self):
        left, right = tone(1000, 1), tone(400, 1)
        path = write_wav(TMP / "st.wav", np.column_stack([left, right]).ravel(), channels=2)
        samples, rate = audio_check.read_wav(path)
        self.assertEqual((rate, len(samples)), (RATE, RATE))
        self.assertEqual(samples[:50], list(left[:50]))

    def test_cli_exit_codes(self):
        import subprocess
        ok = write_wav(TMP / "ok.wav", np.concatenate([tone(400, 1.5), tone(1000, 1.5)]))
        bad = write_wav(TMP / "bad.wav", [0] * RATE * 2)
        for path, code in ((ok, 0), (bad, 1), (TMP / "missing.wav", 2)):
            r = subprocess.run([sys.executable, str(BENCH / "audio_check.py"), str(path), "--tones", "400,1000"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, code, (path, r.stdout[-300:], r.stderr[-300:]))


class NoiseMeasures(unittest.TestCase):
    def test_voice_check_noise_and_rms(self):
        import voice_check as vc
        steady = vc.analyse_noise(noise(2, 300).astype(float))
        self.assertAlmostEqual(steady["rms"], 300, delta=20)
        self.assertLess(steady["step_std_db"], 2.0)
        bursts = np.concatenate([noise(0.25, 30 + 1500 * (i % 2), seed=i) for i in range(8)]).astype(float)
        self.assertGreater(vc.analyse_noise(bursts)["step_std_db"], steady["step_std_db"])
        self.assertEqual(vc.rms([3, -3, 3, -3]), 3.0)
        self.assertIsNone(vc.rms([]))

    def test_phone_mic_level(self):
        import phone_mic
        speak, quiet = 4, 3
        x = np.concatenate([tone(300, speak, 6000), noise(1, 50), noise(quiet, 50)])
        path = write_wav(TMP / "phone.wav", x)
        r = phone_mic.level(path, speak)
        self.assertAlmostEqual(r["speech_rms"], 6000 / math.sqrt(2), delta=60)
        self.assertAlmostEqual(r["quiet_rms"], 50, delta=5)
        self.assertLess(r["quiet_floor10"], r["quiet_rms"] + 1)

    def test_phone_mic_level_short_file(self):
        import phone_mic
        path = write_wav(TMP / "short.wav", tone(300, 2, 1000))
        r = phone_mic.level(path, 15)       # nothing after the speech part: no crash, no quiet level
        self.assertIsNone(r["quiet_rms"])

    def test_tap_floor(self):
        quiet = noise(2, 100)
        self.assertAlmostEqual(udp_record.floor_rms(list(quiet), 1)[0], 100, delta=15)
        stereo = np.column_stack([noise(2, 100, 1), noise(2, 600, 2)]).ravel()
        f = udp_record.floor_rms(list(stereo), 2)
        self.assertTrue(f[0] < 150 < f[1])
        self.assertTrue(udp_record.floor_too_high(f))
        self.assertEqual(udp_record.floor_rms([0] * 4000, 1), [None])     # losses written as silence do not count


class FakeWatch:
    """Enough of NetWatch for round.apply_mode: the board answers bpow and bod as the firmware of goal 098 prints."""
    def __init__(self):
        self.t, self.recent, self.level, self.trips, self.sent = 0.0, [], (4, 5), 0, []
        self.channel = self

    def now(self):
        self.t += 0.01
        return self.t

    def send(self, text):
        self.sent.append(text)
        words = text.split()
        if words[0] == "bpow" and len(words) > 1:
            n = int(words[1])
            self.level = (4, 5) if n == 8 else (n, n)
        if words[0] == "bpow":
            self.recent.append((self.now(), "I (1) CMD:   bredr tx power min=%d max=%d err=0 build=4..5" % self.level))
        elif words[0] == "bod":
            self.recent.append((self.now(), "I (1) CMD:   bod on=1 level=7 trips=%d last_ms=-1" % self.trips))

    def pump(self, end, until=None):
        pass

    def power(self, q):
        return (q, 0)

    def query(self):
        return {"boot": 7}


class BtlossModes(unittest.TestCase):    # goal 098
    def setUp(self):
        import round as rnd
        self.rnd = rnd

    def test_parse_modes(self):
        modes = self.rnd.parse_modes(["D-H=bpow 8;s3:power 80", "P9-L=bpow 7; s3:power 44"])
        self.assertEqual(modes, {"D-H": ["bpow 8", "s3:power 80"], "P9-L": ["bpow 7", "s3:power 44"]})
        with self.assertRaises(SystemExit):
            self.rnd.parse_modes(["nothing"])

    def test_apply_mode_confirms_each_command(self):
        w = FakeWatch()
        done = self.rnd.apply_mode(w, ["bpow 7", "s3:power 44"])
        self.assertTrue(all(c["ok"] for c in done), done)
        self.assertEqual(w.level, (7, 7))
        done = self.rnd.apply_mode(w, ["bpow 8"])
        self.assertTrue(done[0]["ok"])
        self.assertEqual(w.level, (4, 5))
        w.trips = 3
        self.assertEqual(self.rnd.bod_trips(w)["trips"], 3)

    def test_apply_mode_sees_a_level_not_taken(self):
        w = FakeWatch()
        w.send = lambda text: w.recent.append((w.now(), "bredr tx power min=4 max=5 err=0 build=4..5"))
        self.assertFalse(self.rnd.apply_mode(w, ["bpow 7"])[0]["ok"])

    def test_mode_table(self):
        def win(mode, down, up, complete=True, bod=0):
            return {"mode": mode, "tag": mode, "complete": complete, "board_resets": 0, "slc_drops": 0, "bod_delta": bod,
                    "s3_restarted": False, "sco": {"rx_none_pct": 1.0}, "tap": {"loss_pct": 0.5},
                    "down": {"events": down, "seconds_measured": 60.0, "snr_per_second": {"worst_db": 20.0}},
                    "up": {"events": up, "seconds_measured": 60.0, "snr_per_second": {"worst_db": 10.0}}}
        table = self.rnd.mode_table([win("A", 1, 2), win("A", 3, 0), win("B", 0, 1), win("B", 9, 9, complete=False, bod=2)])
        self.assertEqual(table["A"]["total_rate_per_min"], 3.0)
        self.assertEqual(table["A"]["total_median"], 3.0)
        self.assertEqual(table["A"]["total_min_max"], [3.0, 3.0])
        self.assertEqual(table["B"]["windows"], 2)
        self.assertEqual(table["B"]["complete"], 1)
        self.assertEqual(table["B"]["total_rate_per_min"], 1.0)
        self.assertEqual(table["B"]["bod_trips"], 2)
        self.assertEqual(table["A"]["up_worst_db_min"], 10.0)


class Imports(unittest.TestCase):
    MODULES = ("api_check", "audio_check", "bench", "bench_events", "benchd", "benchd_client", "benchlib", "btlog", "call_check", "call_listen", "call_quality", "call_record", "call_stream", "console", "first_flash", "fw", "gen_bench_ui",
               "hfp_states", "msbc", "ota", "owner_check", "owner_common", "pc_link", "phone", "phone_compare", "phone_mic", "round", "s3", "s3_direct", "s3_uart", "sent_tap", "serial_watch", "tap_check", "tone_blocks", "udp_record",
               "udplog", "voice_check")

    def test_every_bench_module_loads(self):
        for name in self.MODULES:
            with self.subTest(name):
                importlib.import_module(name)

    def test_every_module_is_listed(self):
        # break_shape.py is a script of goal 062 that runs at import (sys.argv): left out on purpose, see REPORT.
        found = {p.stem for p in BENCH.glob("*.py")} - {"break_shape"}
        self.assertEqual(found, set(self.MODULES))


if __name__ == "__main__":
    random.seed(1)
    unittest.main()
