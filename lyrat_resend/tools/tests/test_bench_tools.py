"""The bench tools end to end against the S3 simulator on 127.0.0.1 (s3_sim.py), as processes, the way a person runs them.

s3_uart.py (query, cmd with its retries, tx), ota.py (query, s3 and lyrat --dry-run), udplog.py (--seconds, the stat line
joined and parsed), udp_record.py --tap (WAV files, slots, loss and duplicate counting). BENCH_S3_ADDR=127.0.0.1 points
them at the simulator; nothing leaves the PC. Made by goal 092.
"""
import json
import time
import sys
import tempfile
import unittest
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "bench"))
import simproc  # noqa: E402
from simproc import last_json, run_tool  # noqa: E402
import s3_sim  # noqa: E402
import audio_check  # noqa: E402
import s3  # noqa: E402

OUT = Path(tempfile.mkdtemp(prefix="t092_tools_"))


class Uart(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim()

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def test_query(self):
        rc, out, err, _ = run_tool("s3_uart.py", "query")
        self.assertEqual(rc, 0, err)
        self.assertEqual(s3_sim.flat_keys(last_json(out)), s3_sim.F["uq_keys"])

    def test_cmd_known_and_unknown(self):
        rc, out, err, _ = run_tool("s3_uart.py", "cmd", "tone 1")
        self.assertEqual(rc, 0, err)
        self.assertIn("tone = 1", out)
        self.assertEqual(last_json(out)["status"], 0)
        rc, out, _, _ = run_tool("s3_uart.py", "cmd", "nosuchcmd")
        self.assertEqual((rc, last_json(out)["status"]), (1, 1))

    def test_tx_off_refuses_then_auto(self):
        rc, out, _, _ = run_tool("s3_uart.py", "tx", "0")
        self.assertIn("UT 0", out)
        rc, out, _, _ = run_tool("s3_uart.py", "cmd", "tone 0")
        self.assertEqual((rc, last_json(out)["status"]), (1, 3))
        rc, out, _, _ = run_tool("s3_uart.py", "tx", "A")
        self.assertIn("UT 2", out)
        rc, out, _, _ = run_tool("s3_uart.py", "cmd", "tone 0")
        self.assertEqual(rc, 0)


class UartNoisy(unittest.TestCase):
    """Bit errors on the modelled UART: the tool repeats with the same seq and the command runs only once."""

    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim("--uart-ber", "3e-4")

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def test_commands_get_through_once_each(self):
        sys.path.insert(0, str(HERE.parent / "bench"))
        import os
        os.environ["BENCH_S3_ADDR"] = "127.0.0.1"
        import s3_uart
        ch = s3_uart.Channel()
        try:
            self.assertTrue(ch.find())
            for _ in range(30):             # the S3 drives its TX once a heartbeat of the LyraT came through
                if (ch.query() or {}).get("tx_on"):
                    break
                time.sleep(0.1)
            got = [ch.command("tone %d" % (i % 2)) for i in range(30)]
            self.assertTrue(all(g["status"] == 0 for g in got), got)
            st = ch.command("ustat")
            u = s3_uart.parse_ustat(st["text"])
        finally:
            ch.close()
        self.assertGreater(ch.retries, 0, "no retry at this error rate: the test proves nothing")
        self.assertEqual(u["cmds"], 31)          # 30 + this ustat: no command ran twice
        self.assertGreaterEqual(u["dups"], 0)


class Ota(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim()
        cls.common = ["--host", "127.0.0.1", "--port", str(cls.sim.ports["port"]), "--token-file", str(simproc.TOKEN)]

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def test_query(self):
        rc, out, err, _ = run_tool("ota.py", "query", *self.common)
        self.assertEqual(rc, 0, err)
        q = json.loads(out)
        self.assertEqual((q["fw"], q["token"], len(q["rx"])), ("s3tap", 1, 2))

    def test_query_through_env_address(self):
        rc, out, err, _ = run_tool("ota.py", "query", "--port", str(self.sim.ports["port"]))
        self.assertEqual(rc, 0, err)

    def test_s3_dry_run(self):
        rc, out, err, _ = run_tool("ota.py", "s3", str(simproc.image("s3", "ota")), *self.common, "--dry-run",
                                   "--no-record", "--quiet")
        self.assertEqual((rc, last_json(out)["result"]), (0, "dry_run_ok"), err)

    def test_lyrat_dry_run(self):
        rc, out, err, _ = run_tool("ota.py", "lyrat", str(simproc.image("lyrat", "ota")), *self.common, "--dry-run",
                                   "--no-record", "--quiet", timeout=600)
        self.assertEqual((rc, last_json(out)["result"]), (0, "dry_run_ok"), err)

    def test_wrong_board_image_refused_before_sending(self):
        rc, out, err, _ = run_tool("ota.py", "s3", str(simproc.image("lyrat", "ota")), *self.common, "--dry-run",
                                   "--no-record", "--quiet")
        self.assertEqual((rc, last_json(out)["result"]), (1, "bad_image"))


class Log(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim()

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def test_udplog_seconds_and_the_stat_line(self):
        out = OUT / "udplog"
        rc, text, err, _ = run_tool("udplog.py", "--seconds", "5", "--out", str(out))
        self.assertEqual(rc, 0, err)
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertGreater(summary["boards"]["s3"]["lines"], 0)
        self.assertGreater(summary["boards"]["lyrat"]["lines"], 0)
        self.assertEqual(s3_sim.flat_keys(summary["s3"]), s3_sim.F["log_keys"])
        stats = [s3.parse_stat(line) for line in (out / "udplog.log").read_text(encoding="utf-8").splitlines()
                 if " stat up=" in line]
        self.assertTrue(stats, "no stat line in 5 s")
        # a confirmed image runs no self-test after a plain restart: "off" (s3_ota_start)
        self.assertEqual((stats[0]["net"], stats[0]["selftest"]), ("wifi", "off"))


class Tap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim()

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def test_record_wav(self):
        out = OUT / "tap"
        rc, text, err, _ = run_tool("udp_record.py", "--tap", "--seconds", "3", "--slots", "L,LR", "--out", str(out))
        self.assertEqual(rc, 0, err)
        tap = json.loads((out / "tap.json").read_text(encoding="utf-8"))
        for name, ch in (("call", 1), ("mic", 2)):
            with wave.open(str(out / (name + ".wav"))) as w:
                self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()), (ch, 16000, 2))
                self.assertGreater(w.getnframes(), 16000 * 2)
            self.assertEqual((tap[name]["lost_packets"], tap[name]["duplicates"]), (0, 0))
            self.assertEqual(tap[name]["boots"], 1)
        samples, rate = audio_check.read_wav(out / "call.wav")
        r = audio_check.check(samples, rate, [1000], min_seconds=1.0)
        self.assertTrue(r["pass"], r)


class TapLossy(unittest.TestCase):
    """Losses and repeats on the way: counted, and the time axis of the WAV stays real."""

    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim("--stream-loss", "0.05", "--stream-dup", "0.05")

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def test_loss_and_duplicates_counted(self):
        out = OUT / "tap_lossy"
        rc, text, err, _ = run_tool("udp_record.py", "--tap", "--seconds", "4", "--slots", "LR,LR", "--out", str(out))
        self.assertEqual(rc, 0, err)
        tap = json.loads((out / "tap.json").read_text(encoding="utf-8"))
        for name in ("call", "mic"):
            s = tap[name]
            self.assertGreater(s["lost_packets"], 0, name)
            self.assertGreater(s["duplicates"], 0, name)
            self.assertTrue(s["gaps"])
            # silence in place of each loss: the file is as long as the stream was
            self.assertAlmostEqual(s["seconds"], s["wall_s"], delta=0.1)


if __name__ == "__main__":
    unittest.main()
