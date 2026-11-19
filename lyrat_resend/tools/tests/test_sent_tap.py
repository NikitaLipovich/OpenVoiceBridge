"""What the LyraT sends to the phone (goal 112): the firmware's mSBC codec on the PC (msbc.py, msbchost.dll) on a known
signal, against ffmpeg's independent SBC decoder when it is there; sent_tap.py placing frames on the up line's clock
(stamps of the S3, cross-correlation of the content), lost frames as gaps, muted spans, levels; the firmware's side read
from the source (command, frame type, payload layout, S3 forwarding) so the PC and both boards agree.
"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bench"))
import msbc  # noqa: E402
import sent_tap  # noqa: E402

MAIN = HERE.parent.parent / "main"
S3 = HERE.parent.parent.parent / "s3_eth_bridge" / "main" / "main.c"
RATE = 16000
SPF = 120


def tone(hz, seconds, amp=8000):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.int16)


def speechlike(seconds, seed=1):
    """Noise shaped by a syllable-rate envelope: what voice looks like to a cross-correlation."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    env = np.clip(np.sin(2 * np.pi * 3.3 * np.arange(n) / RATE) + 0.3 * rng.standard_normal(n // 400).repeat(400)[:n], 0, None)
    x = np.convolve(rng.standard_normal(n), np.ones(4) / 4, "same") * env
    return (x / np.max(np.abs(x)) * 9000).astype(np.int16)


def snr_db(ref, out):
    """SNR of out against ref after the best delay of the codec (0..200 samples)."""
    a = ref[2000:-2000].astype(float)
    best = max(range(0, 200), key=lambda d: np.dot(a, out[2000 + d:len(ref) - 2000 + d].astype(float)))
    e = a - out[2000 + best:len(ref) - 2000 + best]
    return 10 * np.log10(np.sum(a * a) / np.sum(e * e)), best


def packets_for(sent, first_idx, sco, mic_frame_of_idx0, batch=8, delays=None, drop=(), muted_idx=()):
    """The S3's UDP packets of a sent line: frames encoded, packed by `batch`, stamped with the mic frame at arrival."""
    frames = msbc.encode(sent)
    n = len(frames) // msbc.FRAME_BYTES
    out, seq = [], 0
    rng = np.random.default_rng(3)
    for k, b in enumerate(range(0, n, batch)):
        m = min(batch, n - b)
        idx = first_idx + b
        end = mic_frame_of_idx0 + (b + m) * SPF
        delay = delays[k] if delays is not None else int(rng.integers(160, 800))   # 10..50 ms through the UART
        muted = any(first_idx + b + j in muted_idx for j in range(m))
        if k not in drop:
            data = sent_tap.make_packet(seq, end + delay, 7, idx, sco, frames[b * msbc.FRAME_BYTES:(b + m) * msbc.FRAME_BYTES],
                                        muted=muted)
            out.append((0.0, data))
        seq += 1
    return out


class Codec(unittest.TestCase):
    def test_tone_round_trip(self):
        x = tone(1000, 1.0)
        f = msbc.encode(x)
        self.assertEqual(len(f), 134 * msbc.FRAME_BYTES)
        self.assertEqual(f[0], 0xAD)                      # mSBC syncword
        y, good = msbc.decode(f, reset_first=True)
        self.assertEqual(good, 134)
        snr, _ = snr_db(x, y)
        self.assertGreater(snr, 30)

    def test_speech_round_trip(self):
        x = speechlike(2.0)
        y, good = msbc.decode(msbc.encode(x), reset_first=True)
        snr, _ = snr_db(x, y)
        self.assertGreater(snr, 10)                       # wideband noise is the hard case for bitpool 26

    def test_decoder_goes_on_after_a_bad_frame(self):
        f = bytearray(msbc.encode(tone(400, 0.6)))
        f[3 * msbc.FRAME_BYTES] ^= 0xFF                   # syncword gone
        y, good = msbc.decode(bytes(f), reset_first=True)
        self.assertEqual(good, len(f) // msbc.FRAME_BYTES - 1)
        # The decoder goes on after it: the frames after the bad one still carry the tone.
        x = tone(400, 0.6)
        snr, _ = snr_db(x[8 * SPF:], y[8 * SPF:])
        self.assertGreater(snr, 25)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
    def test_against_ffmpeg(self):
        x = tone(1800, 1.0)
        f = msbc.encode(x)
        y, _ = msbc.decode(f, reset_first=True)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.msbc").write_bytes(f)
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "sbc", "-i", str(Path(d) / "a.msbc"), "-f", "s16le",
                                "-ac", "1", str(Path(d) / "a.raw")], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            z = np.fromfile(Path(d) / "a.raw", np.int16)
        n = min(len(y), len(z))
        self.assertGreater(n, RATE * 0.9)
        self.assertLess(np.max(np.abs(y[:n].astype(int) - z[:n])), 64)


class Placement(unittest.TestCase):
    M0 = 1_000_000          # mic frame of sample 0 of mic.wav
    SM = 8000               # sample of mic.wav that is sample 0 of the files

    def build(self, packets, up, d):
        return sent_tap.build(d, "t", packets, self.M0, self.SM, len(up), up)

    def test_voice_aligned_by_content_with_a_lost_packet(self):
        lag = int(0.060 * RATE)                       # the board's ring: what is sent is 60 ms behind the mic
        up = speechlike(6.0)
        sent = np.zeros_like(up)
        sent[lag:] = up[:-lag] // 2
        # Frame idx 5000 is encoded when the file is at sample 0.5 s.
        idx0_mic = self.M0 + self.SM + int(0.5 * RATE) - 5000 * SPF
        part = sent[int(0.5 * RATE):]
        pk = packets_for(part, 5000, 3, idx0_mic + 5000 * SPF, drop=(10,))
        with tempfile.TemporaryDirectory() as d:
            r = self.build(pk, up, d)
            self.assertTrue(Path(r["paths"]["sent"]).exists() and Path(r["paths"]["sent_both"]).exists())
        self.assertEqual(r["stats"]["lost_frames"], 8)
        self.assertEqual(len(r["gaps"]), 8)
        self.assertEqual(r["align"]["method"], "xcorr")
        # The stamps put the frames late by the least UART delay plus the codec; the content brings them back to up.
        out = r["pcm"]
        a, b = int(1.0 * RATE), int(5.0 * RATE)
        c = np.corrcoef(up[a:b].astype(float), out[a:b].astype(float))[0, 1]
        self.assertGreater(c, 0.6, r["align"])
        rest, _ = sent_tap._xcorr_lag(up, out)
        self.assertLessEqual(abs(rest), 16, r["align"])   # 1 ms
        self.assertGreater(r["align"]["lag_ms"], 60)     # the ring, the least UART delay and the codec
        self.assertGreater(r["levels"]["rms_dbfs"], -40)

    def test_tone_placed_by_stamps(self):
        up = (np.random.default_rng(5).standard_normal(int(4 * RATE)) * 30).astype(np.int16)   # room floor, no tone
        start = int(1.0 * RATE)
        sent = tone(1000, 2.0)
        idx0_mic = self.M0 + self.SM + start
        pk = packets_for(sent, 100, 1, idx0_mic, delays=[200] * 40)
        with tempfile.TemporaryDirectory() as d:
            r = self.build(pk, up, d)
        self.assertEqual(r["align"]["method"], "stamps")
        nz = np.nonzero(r["pcm"])[0]
        # Placed within the least delay (200 samples, 12.5 ms) of where it was encoded.
        self.assertLessEqual(abs(int(nz[0]) - (start + 200)), SPF)
        self.assertAlmostEqual(r["seconds"], 2.0, delta=0.05)
        self.assertEqual(r["stats"]["lost_frames"], 0)

    def test_muted_frames_give_spans(self):
        up = np.zeros(int(3 * RATE), np.int16)
        sent = tone(400, 2.0)
        muted = set(range(100 + 40, 100 + 120))           # 0.3 .. 0.9 s of the sent line, in whole batches
        pk = packets_for(sent, 100, 1, self.M0 + self.SM, delays=[0] * 40, muted_idx=muted)
        with tempfile.TemporaryDirectory() as d:
            r = self.build(pk, up, d)
        self.assertEqual(len(r["muted"]), 1)
        a, b = r["muted"][0]
        self.assertAlmostEqual(a, 0.3, delta=0.07)
        self.assertAlmostEqual(b, 0.9, delta=0.07)

    def test_other_boot_and_garbage_ignored(self):
        up = np.zeros(int(2 * RATE), np.int16)
        pk = packets_for(tone(1000, 1.0), 0, 1, self.M0 + self.SM, delays=[0] * 20)
        pk.append((0.0, b"S3\x01\x02garbage"))
        bad = bytearray(pk[0][1])
        bad[12] = 99                                       # another boot id of the S3
        pk.append((0.0, bytes(bad)))
        with tempfile.TemporaryDirectory() as d:
            r = self.build(pk, up, d)
        self.assertEqual(r["stats"]["boots"], 2)
        self.assertEqual(r["stats"]["duplicates"], 0)
        self.assertAlmostEqual(r["seconds"], 1.0, delta=0.05)

    def test_journal_round_trip(self):
        pk = packets_for(tone(1000, 0.5), 0, 1, 0, delays=[0] * 10)
        with tempfile.TemporaryDirectory() as d:
            import struct
            with open(Path(d) / "sent.bin", "wb") as f:
                for at, data in pk:
                    f.write(struct.pack("<dI", at, len(data)) + data)
            back = sent_tap.read_journal(Path(d) / "sent.bin")
        self.assertEqual([x[1] for x in back], [x[1] for x in pk])


class Firmware(unittest.TestCase):
    """The layout both boards and the PC share, read from the source."""

    @classmethod
    def setUpClass(cls):
        cls.main = (MAIN / "main.c").read_text(encoding="utf-8")
        cls.hdr = (MAIN / "ctl_frame.h").read_text(encoding="utf-8")
        cls.s3 = S3.read_text(encoding="utf-8")

    def define(self, text, name):
        return int(re.search(r"#define\s+%s\s+(\d+)" % name, text).group(1))

    def test_frame_type_and_payload(self):
        self.assertRegex(self.hdr, r"CTL_T_SENT\s*=\s*10")
        self.assertEqual(self.define(self.main, "ST_HEAD_BYTES"), sent_tap.LY_HEAD)
        batch = self.define(self.main, "ST_BATCH_MAX")
        self.assertLessEqual(sent_tap.LY_HEAD + batch * msbc.FRAME_BYTES, self.define(self.hdr, "CTL_MAX_PAYLOAD"))
        self.assertLess(batch, 256)                       # u8 count
        self.assertIn("senttap_push(frame);", self.main)
        self.assertIn('strcmp(cmd, "senttap") == 0', self.main)
        self.assertIn("senttap_drain();", self.main)

    def test_s3_forwards_on_5009_with_mic_stamp(self):
        self.assertEqual(self.define(self.s3, "PORT_SENT"), sent_tap.PORT)
        self.assertIn("type == CTL_T_SENT", self.s3)
        self.assertIn("at = s_mic.total_frames", self.s3)
        self.assertIn("packet[3] = 2;", self.s3)
        self.assertEqual(sent_tap.HEAD, 16)
        self.assertIn("memcpy(packet + 16, ctl_frame_payload(p), len);", self.s3)


if __name__ == "__main__":
    unittest.main()
