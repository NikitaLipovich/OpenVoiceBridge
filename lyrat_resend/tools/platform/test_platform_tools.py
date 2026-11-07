"""platform_ota.py and udplog.py against u3_emu.py (goal 095). Based on lyrat_resend/tools/ota_test/test_tools.py.

  build_host.cmd here and in ../ota_test, then: python test_platform_tools.py [-v]
Images: env PLAT_BUILDS (default the sandbox of goal 095) with u3_relay-cur/, u1_hf-cur/, u2_ag-cur/ of idf.py builds.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
BUILDS = Path(os.environ.get("PLAT_BUILDS", REPO / "goals_harness" / "phone-then-pc" / "095-platform-ota-relay" /
                             "sandbox" / "build"))
U3 = BUILDS / "u3_relay-cur" / "u3_relay.bin"
U1 = BUILDS / "u1_hf-cur"
U2 = BUILDS / "u2_ag-cur"
TOKEN = "emu-token-0123456789abc"
RESULTS = {}
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "bench"))


class Emu:
    def __init__(self, tmp, *extra):
        self.token = Path(tmp) / "secrets.h"
        self.token.write_text('#define OTA_TOKEN "%s"\n' % TOKEN, encoding="utf-8")
        self.p = subprocess.Popen([sys.executable, str(HERE / "u3_emu.py"), "--token-file", str(self.token),
                                   "--u3-image", str(U3), "--u1-image", str(U1 / "u1_hf.bin"),
                                   "--u2-image", str(U2 / "u2_ag.bin"), *extra],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ports = json.loads(self.p.stdout.readline())
        self.port, self.log_port = ports["port"], ports["log_port"]

    def args(self, token=True):
        a = ["--host", "127.0.0.1", "--port", str(self.port), "--log-port", str(self.log_port), "--no-record",
             "--quiet", "--timeout", "120"]
        return a + (["--token-file", str(self.token)] if token else [])

    def close(self):
        self.p.kill()
        self.p.wait()
        self.p.stdout.close()
        self.p.stderr.close()


def variant(src, tmp, tag):
    """A copy of an app image with another version and ELF SHA-256: an update the emulator can tell from the running
    image. Only the emulator takes it: the image's own hash is not recomputed, a real board would refuse it."""
    data = bytearray(Path(src).read_bytes())
    data[48:80] = tag.encode().ljust(32, b"\0")
    data[176:208] = bytes((b ^ 0xA5) for b in data[176:208])
    out = Path(tmp) / ("%s-%s" % (tag, Path(src).name))
    out.write_bytes(bytes(data))
    return out


def tool(script, *args, timeout=300):
    t = time.monotonic()
    r = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True, timeout=timeout)
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    return r.returncode, last, round(time.monotonic() - t, 1), r


def ota_tool(*args):
    return tool(HERE / "platform_ota.py", *args)


@unittest.skipUnless(U3.exists() and (U1 / "flasher_args.json").exists() and (U2 / "flasher_args.json").exists(),
                     "builds of goal 095 missing: set PLAT_BUILDS")
class Tools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.emu = Emu(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.emu.close()
        cls.tmp.cleanup()

    def run_ok(self, name, *args, want=0, result=None):
        code, last, secs, r = ota_tool(*args, *self.emu.args())
        RESULTS[name] = {"exit": code, "out": last, "s": secs}
        self.assertEqual(code, want, (last, r.stderr[-600:]))
        if result:
            self.assertEqual(json.loads(last)["result"], result)
        return last

    def test_01_query(self):
        out = self.run_ok("query", "query")
        self.assertIn('"fw": "u3relay"', out + ota_tool("query", *self.emu.args())[3].stdout)
        q = json.loads(ota_tool("query", "u2", *self.emu.args())[3].stdout)
        self.assertEqual(q["node"], "u2")

    def test_02_u3(self):
        self.run_ok("u3_dry_run", "u3", U3, "--dry-run", result="dry_run_ok")
        self.run_ok("u3", "u3", U3, result="done")
        self.run_ok("u3_test_rollback", "u3", variant(U3, self.tmp.name, "u3-b"), "--test-rollback",
                    result="rolled_back")

    def test_03_nodes_app(self):
        self.run_ok("u1_app", "u1", U1 / "u1_hf.bin", result="done")
        self.run_ok("u2_app_dry_run", "u2", U2 / "u2_ag.bin", "--dry-run", result="dry_run_ok")
        self.run_ok("u2_app_test_rollback", "u2", variant(U2 / "u2_ag.bin", self.tmp.name, "u2-b"), "--test-rollback",
                    result="rolled_back")
        self.run_ok("u2_app_new_version", "u2", variant(U2 / "u2_ag.bin", self.tmp.name, "u2-c"), result="done")
        # The wrong node's image: refused by the tool, and by U3 itself when sent past the tool.
        self.run_ok("u1_gets_u2_image_tool", "u1", U2 / "u2_ag.bin", want=1, result="bad_image")
        import ota
        link = ota.Link("127.0.0.1", self.emu.port)
        try:
            up = ota.upload(link, 1, ota.Image.load(U2 / "u2_ag.bin"), TOKEN.encode(), quiet=True, timeout=30)
        finally:
            link.close()
        RESULTS["u1_gets_u2_image_u3"] = up["status"]
        self.assertEqual(up["status"], "bad_image")

    def test_04_rom(self):
        self.run_ok("u1_rom_dry_run", "u1-rom", U1, "--dry-run", result="dry_run_ok")
        self.run_ok("u1_rom_build_dir", "u1-rom", U1, "--baud", "460800", result="done")
        q = json.loads(ota_tool("query", "u1", *self.emu.args())[3].stdout)
        RESULTS["u1_rom_query"] = q["rom"]
        self.assertEqual(q["rom"]["job"], "done")
        self.assertEqual(q["run_project"], "u1_hf")
        out = Path(self.tmp.name) / "u2.rbnd"
        code, last, _, _ = tool(HERE / "platform_ota.py", "bundle", U2, "-o", out)
        self.assertEqual(code, 0)
        self.run_ok("u2_rom_bundle_file", "u2-rom", out, result="done")
        # A bundle of U2 for U1: the tool refuses it from a build dir; U3 refuses the file (project in the bundle).
        self.run_ok("u1_rom_gets_u2_build", "u1-rom", U2, want=1, result="bad_image")
        import ota
        link = ota.Link("127.0.0.1", self.emu.port)
        try:
            from platform_ota import Blob
            up = ota.upload(link, 0x11, Blob(out.read_bytes()), TOKEN.encode(), quiet=True, timeout=30)
        finally:
            link.close()
        RESULTS["u1_rom_gets_u2_bundle_u3"] = up["status"]
        self.assertEqual(up["status"], "bad_image")

    def test_05_no_token(self):
        code, last, _, _ = ota_tool("u1", U1 / "u1_hf.bin", *self.emu.args(token=False), "--token-file",
                                    str(Path(self.tmp.name) / "none.h"))
        RESULTS["no_token"] = {"exit": code, "out": last}
        self.assertEqual(code, 1)

    def test_06_text_requests(self):
        for name, args, want in (("cmd_ver", ("cmd", "u1", "ver"), "UR u1 0 u1_hf"),
                                 ("mode_reserved", ("mode", "u2", "3"), "MA u2 2 3"),
                                 ("con_u2", ("con", "u2"), '"listening": "u2"')):
            code, last, _, r = ota_tool(*args, *self.emu.args())
            RESULTS[name] = last
            self.assertEqual(code, 0)
            self.assertIn(want, last)
        ota_tool("con", "auto", *self.emu.args())

    def test_07_udplog_three_boards(self):
        out = Path(self.tmp.name) / "ul_all"
        code, last, secs, r = tool(REPO / "lyrat_resend" / "tools" / "bench" / "udplog.py", "--host", "127.0.0.1",
                                   "--port", self.emu.log_port, "--seconds", 4, "--out", out)
        self.assertEqual(code, 0, r.stderr)
        boards = json.loads(last)["boards"]
        RESULTS["udplog_all"] = boards
        for b in ("u3", "u1", "u2", "u1con"):
            self.assertIn(b, boards)
            self.assertGreater(boards[b]["lines"], 0)
        lines = (out / "udplog.log").read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(" u1con " in x for x in lines))
        out2 = Path(self.tmp.name) / "ul_u2"
        code, last, secs, r = tool(REPO / "lyrat_resend" / "tools" / "bench" / "udplog.py", "--host", "127.0.0.1",
                                   "--port", self.emu.log_port, "--seconds", 2, "--board", "u2", "--level", "W",
                                   "--u2-level", "2", "--out", out2)
        self.assertEqual(code, 0)
        kept = (out2 / "udplog.log").read_text(encoding="utf-8").splitlines()
        RESULTS["udplog_u2_w"] = {"written": len(kept), "boards": sorted(json.loads(last)["boards"])}
        self.assertTrue(kept and all(" u2 " in x for x in kept))


@unittest.skipUnless(U3.exists() and (U1 / "flasher_args.json").exists(), "builds of goal 095 missing")
class RomFaults(unittest.TestCase):
    """The ROM job through U3 with a line cut for 3 s in the middle, and a node whose IO0 is not wired."""

    def run_emu(self, faults, want_exit, want_result):
        with tempfile.TemporaryDirectory() as tmp:
            emu = Emu(tmp, "--rom-faults", json.dumps(faults))
            try:
                code, last, secs, r = ota_tool("u1-rom", U1, *emu.args())
                q = json.loads(ota_tool("query", "u1", *emu.args())[3].stdout)
            finally:
                emu.close()
        self.assertEqual(code, want_exit, last)
        self.assertEqual(json.loads(last)["result"], want_result)
        return q["rom"]

    def test_cut(self):
        rom = self.run_emu({"cut_at": 150_000, "cut_ms": 3000}, 0, "done")
        RESULTS["rom_cut_3s"] = rom
        self.assertEqual(rom["session_restarts"], 1)

    def test_no_download(self):
        rom = self.run_emu({"no_download": True}, 1, "failed")
        RESULTS["rom_no_download"] = rom
        self.assertEqual(rom["err"], "sync")


@unittest.skipUnless((U1 / "u1_hf.bin").exists() and U3.exists(), "builds of goal 095 missing")
class LinkFaults(unittest.TestCase):
    """The app of U1 through U3 over a control UART with bit errors: lost frames are sent again (go-back-N)."""

    def test_ber(self):
        with tempfile.TemporaryDirectory() as tmp:
            emu = Emu(tmp, "--uart-ber", "2e-5")
            try:
                code, last, secs, r = ota_tool("u1", U1 / "u1_hf.bin", *emu.args())
                q = json.loads(ota_tool("query", "u1", *emu.args())[3].stdout)
            finally:
                emu.close()
        RESULTS["u1_app_ber_2e-5"] = {"exit": code, "s": secs, "resent": q["resent"], "sent": q["sent"]}
        self.assertEqual(code, 0, last)
        self.assertGreater(q["resent"], 0)


if __name__ == "__main__":
    prog = unittest.main(exit=False, verbosity=2 if "-v" in sys.argv else 1)
    print("RESULTS " + json.dumps(RESULTS, default=str))
    sys.exit(0 if prog.result.wasSuccessful() else 1)
