"""ota.py and udplog.py end to end against the S3 emulator (s3_emu.py) on 127.0.0.1. No bench: nothing leaves the PC.

Run from this folder after build_host.cmd, with OTA_TEST_BUILDS = a folder holding two builds of each board:
s3-base, s3-ota (s3_eth_bridge.bin) and lyrat-base, lyrat-ota (lyrat_resend.bin); "ota" is the newer one.
The token of the emulator is made up for the run and lives in a temporary folder. Made by goal 090.
"""
import os
import tempfile
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "bench"
sys.path.insert(0, str(BENCH))
import ota  # noqa: E402

BUILDS = Path(os.environ.get("OTA_TEST_BUILDS", HERE / "images"))
S3_BASE = BUILDS / "s3-base" / "s3_eth_bridge.bin"
S3_NEW = BUILDS / "s3-ota" / "s3_eth_bridge.bin"
LY_BASE = BUILDS / "lyrat-base" / "lyrat_resend.bin"
LY_NEW = BUILDS / "lyrat-ota" / "lyrat_resend.bin"
HAVE_IMAGES = all(p.exists() for p in (S3_BASE, S3_NEW, LY_BASE, LY_NEW))
TMP = Path(tempfile.mkdtemp(prefix="ota_test_"))
OUT = Path(os.environ.get("OTA_TEST_OUT", TMP / "runs"))
TOKEN = TMP / "token.h"
WRONG = TMP / "wrong.h"
TOKEN.write_text('#define OTA_TOKEN "%s"\n' % os.urandom(12).hex(), encoding="utf-8")
WRONG.write_text('#define OTA_TOKEN "%s"\n' % os.urandom(12).hex(), encoding="utf-8")
needs_images = unittest.skipUnless(HAVE_IMAGES, "set OTA_TEST_BUILDS to the folder of the builds")


class Emu:
    def __init__(self, *extra):
        self.p = subprocess.Popen([sys.executable, str(HERE / "s3_emu.py"), "--token-file", str(TOKEN),
                                   "--s3-image", str(S3_BASE), "--lyrat-image", str(LY_BASE), *extra],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ports = json.loads(self.p.stdout.readline())
        self.port, self.log_port = ports["port"], ports["log_port"]

    def close(self):
        self.p.kill()
        self.p.wait()
        self.p.stdout.close()
        self.p.stderr.close()


def run_tool(script, *args, timeout=600):
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(BENCH / script), *args], capture_output=True, text=True, timeout=timeout)
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
    try:
        out = json.loads(last)
    except ValueError:
        out = {"raw": r.stdout}
    out["_exit"], out["_s"], out["_err"] = r.returncode, round(time.monotonic() - t0, 1), r.stderr
    print("\n    %s %s -> exit %d, %s, %.1f s" % (script, " ".join(a for a in args if not a.startswith(("C:", "127"))),
                                                r.returncode, {k: v for k, v in out.items() if not k.startswith("_")},
                                                out["_s"]))
    return out


def ota_args(emu, what, image, *extra, token=TOKEN):
    return ["ota.py", what, str(image), "--host", "127.0.0.1", "--port", str(emu.port), "--token-file", str(token),
            "--no-record", "--quiet", *extra]


@needs_images
class OtaTool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emu = Emu()

    @classmethod
    def tearDownClass(cls):
        cls.emu.close()

    def query(self):
        link = ota.Link("127.0.0.1", self.emu.port)
        try:
            return link.query()
        finally:
            link.close()

    def test_1_s3_dry_run(self):
        before = self.query()
        r = run_tool(*ota_args(self.emu, "s3", S3_NEW, "--dry-run"))
        self.assertEqual((r["_exit"], r["result"]), (0, "dry_run_ok"), r["_err"])
        after = self.query()
        self.assertEqual(after["elf"], before["elf"])          # nothing activated
        self.assertEqual(after["boot"], before["boot"])

    def test_2_s3_update(self):
        r = run_tool(*ota_args(self.emu, "s3", S3_NEW))
        self.assertEqual((r["_exit"], r["result"]), (0, "done"), r["_err"])
        q = self.query()
        self.assertEqual(q["elf"], ota.Image.load(S3_NEW).elf[:16])
        self.assertEqual(q["img"], "valid")

    def test_3_s3_rollback(self):
        running = self.query()["elf"]
        r = run_tool(*ota_args(self.emu, "s3", S3_BASE, "--test-rollback"))
        self.assertEqual((r["_exit"], r["result"]), (0, "rolled_back"), r["_err"])
        q = self.query()
        self.assertEqual(q["elf"], running)
        self.assertEqual(q["last_invalid"]["elf"], ota.Image.load(S3_BASE).elf[:16])

    def test_4_wrong_token(self):
        r = run_tool(*ota_args(self.emu, "s3", S3_BASE, token=WRONG))
        self.assertEqual((r["_exit"], r["result"]), (1, "auth"), r["_err"])

    def test_5_wrong_board(self):
        r = run_tool(*ota_args(self.emu, "s3", LY_NEW))
        self.assertEqual((r["_exit"], r["result"]), (1, "bad_image"))

    def test_6_lyrat_dry_run(self):
        before = self.query()["lyrat"]["status"]
        r = run_tool(*ota_args(self.emu, "lyrat", LY_NEW, "--dry-run"))
        self.assertEqual((r["_exit"], r["result"]), (0, "dry_run_ok"), r["_err"])
        time.sleep(2.5)
        self.assertIn("elf=" + ota.Image.load(LY_BASE).elf[:16], self.query()["lyrat"]["status"] or before)

    def test_7_lyrat_update_then_rollback(self):
        r = run_tool(*ota_args(self.emu, "lyrat", LY_NEW))
        self.assertEqual((r["_exit"], r["result"]), (0, "done"), r["_err"])
        st = self.query()["lyrat"]["status"]
        self.assertIn("elf=" + ota.Image.load(LY_NEW).elf[:16], st)
        r = run_tool(*ota_args(self.emu, "lyrat", LY_BASE, "--test-rollback"))
        self.assertEqual((r["_exit"], r["result"]), (0, "rolled_back"), r["_err"])


@needs_images
class OtaCallRule(unittest.TestCase):
    def test_refused_with_slc_then_forced(self):
        emu = Emu("--lyrat-slc", "--uart-ber", "2e-6")
        try:
            r = run_tool(*ota_args(emu, "lyrat", LY_NEW))
            self.assertEqual(r["_exit"], 1, r["_err"])
            self.assertEqual(r["result"], "failed")
            link = ota.Link("127.0.0.1", emu.port)
            self.assertEqual(link.query()["lyrat"]["err"], "refused")
            link.close()
            r = run_tool(*ota_args(emu, "lyrat", LY_NEW, "--force"))
            self.assertEqual((r["_exit"], r["result"]), (0, "done"), r["_err"])
        finally:
            emu.close()


@needs_images
class UdpLog(unittest.TestCase):
    def test_both_boards_counted_and_filtered(self):
        emu = Emu("--log-loss", "0.1")
        try:
            out = OUT / "udplog_all"
            r = run_tool("udplog.py", "--host", "127.0.0.1", "--port", str(emu.log_port), "--seconds", "4", "--out",
                         str(out))
            self.assertEqual(r["_exit"], 0, r["_err"])
            b = r["boards"]
            self.assertGreater(b["s3"]["lines"], 30)
            self.assertGreater(b["lyrat"]["lines"], 30)
            self.assertGreater(b["s3"]["lost_way"] + b["lyrat"]["lost_way"], 0)      # 10 % of datagrams dropped
            self.assertEqual(b["s3"]["lost_board"], 0)
            self.assertEqual(b["s3"]["first"][0]["seq"], 0)                         # the start waited in the ring
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            dropped = summary["s3"]["dropped_datagrams"]
            self.assertGreater(dropped, 0)
            text = (out / "udplog.log").read_text(encoding="utf-8")
            self.assertIn("boot line 0", text)
            self.assertNotIn("\x1b", text)
            out2 = OUT / "udplog_lyrat_w"
            r = run_tool("udplog.py", "--host", "127.0.0.1", "--port", str(emu.log_port), "--seconds", "2",
                         "--board", "lyrat", "--level", "W", "--out", str(out2))
            lines = (out2 / "udplog.log").read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines)
            self.assertTrue(all(" lyrat " in ln and " W (" in ln for ln in lines), lines[:3])
        finally:
            emu.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
