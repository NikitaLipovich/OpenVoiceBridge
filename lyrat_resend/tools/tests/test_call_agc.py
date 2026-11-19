"""The call AGC toward the headphones (goal 113): the firmware's main/call_agc.c on the PC (agc.py, agchost.dll built on
first use). No sample above the guard on hostile input (full-scale steps after silence, square bursts, random levels),
the talker brought to the target level, gain held through pauses and steady noise, no step in the gain inside a frame,
the old fixed path clips where the AGC does not; the glue in main.c (default on, the command, the AEC reference after it).
"""
import re
import sys
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bench"))
import agc  # noqa: E402

MAIN = HERE.parent.parent / "main"
RATE, FR = agc.RATE, agc.FRAME


def speech(seconds, level_db, seed=1):
    """Noise through a syllable envelope (4 Hz) with word pauses, active RMS at level_db dBFS. -> (int16, mask)."""
    r = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    x = np.convolve(r.standard_normal(n), [1, 0.6, 0.2], "same")
    env = np.clip(np.sin(2 * np.pi * 4 * t), 0, None) * (r.random(int(seconds) + 1) > 0.25).repeat(RATE)[:n]
    y = x * env
    m = env > 0.05
    y = y / np.sqrt(np.mean(y[m] ** 2)) * 32768 * 10 ** (level_db / 20)
    return np.clip(np.round(y), -32768, 32767).astype(np.int16), m


def active_db(y, m, skip_s):
    s = int(skip_s * RATE)
    v = y[s:].astype(float)[m[s:len(y)]]
    return 10 * np.log10(np.mean(v ** 2) / 32768 ** 2)


class Agc(unittest.TestCase):
    def test_no_sample_over_the_guard_on_hostile_input(self):
        rng = np.random.default_rng(5)
        parts = [np.zeros(RATE, np.int16)]
        for _ in range(60):     # silence, then a full-scale burst starting anywhere in a frame
            k = int(rng.integers(1, 2 * FR))
            parts.append(np.zeros(k, np.int16))
            amp = int(rng.choice([300, 3000, 20000, 32767]))
            sq = np.where(np.arange(int(rng.integers(5, 800))) % 16 < 8, amp, -amp).astype(np.int16)
            parts.append(sq)
        parts.append(speech(5, -30)[0])
        parts.append(np.full(500, 32767, np.int16))     # a jump straight after quiet speech that raised the gain
        x = np.concatenate(parts)
        for g0 in (1, 2, 8):
            r = agc.run(x, init_gain=g0)
            self.assertLessEqual(int(np.max(np.abs(r["y"].astype(int)))), agc.LIMIT + 1, g0)
            self.assertEqual(r["clips"], 0)
        self.assertGreater(agc.fixed(x, 2)[1], 1000)     # the old path clips this

    def test_level_brought_to_target_and_loud_not_clipped(self):
        for lv in (-30, -24, -12):
            x, m = speech(30, lv, seed=-lv)
            r = agc.run(x)
            self.assertEqual(int(np.sum(np.abs(r["y"].astype(int)) >= 32767)), 0)
            self.assertLess(abs(active_db(r["y"], m, 10) - (-18)), 3, lv)

    def test_gain_held_in_silence_and_steady_noise(self):
        rng = np.random.default_rng(2)
        noise = np.clip(rng.standard_normal(20 * RATE) * 32768 * 10 ** (-50 / 20), -32768, 32767).astype(np.int16)
        x = np.concatenate([np.zeros(5 * RATE, np.int16), noise])
        r = agc.run(x, init_gain=2)
        self.assertLess(float(np.ptp(r["gain_db"])), 0.5)        # steady noise is not lifted
        self.assertFalse(r["speech"][int(6 * RATE / FR):].any())

    def test_gain_moves_slowly_and_without_steps(self):
        x = np.concatenate([speech(10, -30, 3)[0], speech(10, -14, 4)[0]])
        r = agc.run(x)
        d = np.diff(r["gain_db"])
        rel = np.abs(np.diff(r["y"].astype(float)))
        self.assertLess(float(np.max(d)), 12 * FR / RATE + 0.01)  # up: never faster than the learning rate
        self.assertEqual(r["clips"], 0)
        # the gain changes linearly over 240 samples: its part of one sample step is tiny
        g = 10 ** (r["gain_db"] / 20)
        self.assertLess(float(np.max(np.abs(np.diff(g)))) / FR * 32768, 400)
        self.assertGreater(float(np.max(rel)), 0)

    def test_output_aligned_and_tone_kept(self):
        t = np.arange(RATE * 3) / RATE
        x = (10000 * np.sin(2 * np.pi * 1000 * t)).astype(np.int16)
        r = agc.run(x, init_gain=2)
        self.assertTrue(np.allclose(r["y"][FR:].astype(float), 2 * x[FR:].astype(float), atol=2))


class Glue(unittest.TestCase):
    def test_main_c(self):
        src = (MAIN / "main.c").read_text(encoding="utf-8")
        self.assertIn('#include "call_agc.h"', src)
        self.assertRegex(src, r"\.agc_on\s*=\s*1")
        self.assertIn('strcmp(cmd, "agc") == 0', src)
        tx = src[src.index("static void i2s_tx_task"):src.index("static void mic_rx_task")]
        self.assertLess(tx.index("call_agc_process"), tx.index("lyrat_aec_write_reference"))
        self.assertIn("call_agc_drop_delay", tx)
        self.assertIn('"call_agc.c"', (MAIN / "CMakeLists.txt").read_text())
        self.assertNotRegex((MAIN / "call_agc.c").read_text() + (MAIN / "call_agc.h").read_text(),
                            re.compile(r"#include\s*[<\"](esp_|freertos)"))


if __name__ == "__main__":
    unittest.main()
