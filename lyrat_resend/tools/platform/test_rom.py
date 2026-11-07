"""Tests of the platform's ROM flasher (rom_flasher.c) and bundle check (rom_bundle.c) against rom_emu.py. Goal 095.

  build_host.cmd, then: python test_rom.py [-v]
Images: the real builds of u1_hf and u2_ag (env PLAT_BUILDS, default the sandbox of goal 095: <dir>/u1_hf-cur,
<dir>/u2_ag-cur); without them a synthetic bundle of the same shape is used and the test says so.
"""
import json
import os
import random
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rom_emu as E  # noqa: E402
import rombundle as RB  # noqa: E402
import romhost as H  # noqa: E402

REPO = HERE.parents[2]
BUILDS = Path(os.environ.get("PLAT_BUILDS", REPO / "goals_harness" / "phone-then-pc" / "095-platform-ota-relay" /
                             "sandbox" / "build"))
RESULTS = {}


def synthetic(seed=3, app=200_000):
    r = random.Random(seed)
    rnd = lambda n: bytes(r.getrandbits(8) for _ in range(n))  # noqa: E731
    boot = bytearray(rnd(26_000))
    boot[0] = 0xE9
    return RB.make([(0x1000, bytes(boot)), (0x8000, rnd(3072)), (0xF000, b"\xff" * 8192), (0x20000, rnd(app))],
                   0x400000)


def real(project):
    d = BUILDS / ("%s-cur" % project)
    if (d / "flasher_args.json").exists():
        return RB.from_build(d, project)[0], True
    return synthetic(), False


def run(bundle, baud_high=0, **faults):
    clk = E.Clock()
    rom = E.Esp32Rom(clk, **faults)
    nvs_before = rom.region(0x9000, 0x6000)
    f = H.CRomFlasher(rom, bundle, baud_high=baud_high)
    r = f.flash()
    rom.advance_to(clk.ms + 50)         # the target starts after the last EN pulse
    info = f.info()
    info["result"] = H.err_name(r)
    segs = RB.parse(bundle)["segments"]
    info["flash_ok"] = all(rom.region(s["offset"], s["size"]) == bundle[s["data_at"]:s["data_at"] + s["size"]]
                           for s in segs)
    info["nvs_kept"] = rom.region(0x9000, 0x6000) == nvs_before
    info["last_event"] = rom.events[-1][0] if rom.events else None
    info["events"] = [e[0] for e in rom.events]
    info["counts"] = dict(rom.counts)
    f.close()
    return r, info, rom, f


def offset_in_app(bundle, frac):
    """Host byte count at which the flasher is about frac into the app segment (for cut/corrupt faults)."""
    segs = RB.parse(bundle)["segments"]
    before = sum(s["size"] for s in segs[:-1])
    blocks = before // 1024 + len(segs) + int(segs[-1]["size"] * frac) // 1024
    return 400 + blocks * (1024 + 16 + 10 + 20)


class Bundle(unittest.TestCase):
    """C check (rom_bundle.c) and Python builder/parser agree, on good and on broken headers."""

    def cases(self):
        good = synthetic(app=10_000)
        out = {"good": good}

        def patched(at, value, fmt="<I"):
            import struct
            b = bytearray(good)
            struct.pack_into(fmt, b, at, value)
            return bytes(b)
        out["short"] = good[:200]
        out["magic"] = b"XBND" + good[4:]
        out["version"] = good[:4] + b"\x02" + good[5:]
        out["chip"] = good[:5] + b"\x09" + good[6:]
        out["count"] = good[:6] + b"\x00" + good[7:]
        out["flash_size"] = patched(8, 3 * 0x100000)
        out["align"] = patched(16 + 24 * 3, 0x20100)
        out["range"] = patched(16, 0)
        out["overlap"] = patched(16 + 24 * 1, 0x1000)
        out["protected"] = RB.make([(0x1000, b"\xe9" * 100), (0xA000, b"x" * 100)], 0x400000)
        out["size"] = good + b"\x00"
        return out

    def test_agree(self):
        seen = {}
        for want, data in self.cases().items():
            name, b, _ = H.bundle_parse(data)
            if b:
                H.lib.host_free(b)
            try:
                RB.parse(data)
                py = "ok"
            except ValueError as e:
                py = str(e)
            seen[want] = [name, py]
            self.assertEqual(name, "ok" if want == "good" else want, want)
            self.assertEqual(py, name, want)
        RESULTS["bundle_checks"] = seen

    def test_real_layout(self):
        for p in ("u1_hf", "u2_ag"):
            data, is_real = real(p)
            name, b, info = H.bundle_parse(data)
            self.assertEqual(name, "ok")
            H.lib.host_free(b)
            self.assertEqual([s["offset"] for s in info["segments"]], [0x1000, 0x8000, 0xF000, 0x20000])
            RESULTS["layout_" + p] = {"real_build": is_real, "total": info["total"],
                                      "segments": [[hex(s["offset"]), s["size"]] for s in info["segments"]]}


class Flash(unittest.TestCase):
    def check_ok(self, info):
        self.assertEqual(info["result"], "ok", info)
        self.assertTrue(info["flash_ok"])
        self.assertTrue(info["nvs_kept"])
        self.assertEqual(info["last_event"], "run")     # never left in download mode

    def test_real_images(self):
        for p in ("u1_hf", "u2_ag"):
            for baud in (0, 460800):
                data, is_real = real(p)
                r, info, rom, f = run(data, baud_high=baud)
                self.check_ok(info)
                RESULTS["%s_%d" % (p, baud or 115200)] = {k: info[k] for k in ("result", "ms", "blocks", "syncs",
                                                                               "session_restarts", "flash_ok",
                                                                               "nvs_kept", "last_event")}
                RESULTS["%s_%d" % (p, baud or 115200)]["real_build"] = is_real

    def test_pin_order(self):
        data, _ = real("u1_hf")
        r, info, rom, f = run(data)
        log = f.pin_log
        # Download: IO0 low before EN low; EN up while IO0 still low; IO0 let go >= 200 ms later.
        self.assertEqual(log[0][1:], (0, 1))
        self.assertEqual(log[1][1:], (1, 1))
        self.assertEqual(log[2][1:], (0, 1))
        self.assertGreaterEqual(log[2][0] - log[1][0], 100)
        self.assertEqual(log[3][1:], (0, 0))
        self.assertGreaterEqual(log[3][0] - log[2][0], 200)
        # Run: EN pulse with IO0 released.
        self.assertEqual([x[1:] for x in log[-3:]], [(0, 0), (1, 0), (0, 0)])
        self.assertEqual(info["events"][:2], ["reset", "download"])
        RESULTS["pin_order"] = log

    def test_partial_block_padding(self):
        data = RB.make([(0x1000, b"\xe9" + b"\x11" * 999), (0x20000, b"\x22" * 1500)], 0x400000)
        r, info, rom, f = run(data)
        self.check_ok(info)
        self.assertEqual(rom.region(0x1000 + 1000, 3096), b"\xff" * 3096)     # rest of the sector erased, not old
        self.assertEqual(rom.region(0x20000 + 1500, 2596), b"\xff" * 2596)

    def test_sync_retries(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, sync_ignore=4)
        self.check_ok(info)
        self.assertEqual(info["syncs"], 5)
        RESULTS["sync_ignore_4"] = {"result": info["result"], "syncs": info["syncs"]}

    def test_no_download(self):
        data, _ = real("u1_hf")
        r, info, rom, f = run(data, no_download=True)
        self.assertEqual(info["result"], "sync")
        self.assertEqual(info["syncs"], 20)              # 10 per session, 2 sessions
        self.assertEqual(info["session_restarts"], 1)
        self.assertNotIn(E.OP_FLASH_BEGIN, info["counts"])
        self.assertEqual(info["last_event"], "run")
        RESULTS["no_download"] = {"result": info["result"], "syncs": info["syncs"], "ms": info["ms"]}

    def test_wrong_chip(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, magic=0x09)
        self.assertEqual(info["result"], "chip")
        self.assertEqual(info["session_restarts"], 0)
        self.assertNotIn(E.OP_FLASH_BEGIN, info["counts"])
        self.assertEqual(info["last_event"], "run")

    def test_cut_transient(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, cut_at=offset_in_app(data, 0.5), cut_ms=3000)
        self.check_ok(info)
        self.assertEqual(info["session_restarts"], 1)
        self.assertIn("cut", info["events"])
        RESULTS["cut_3s"] = {k: info[k] for k in ("result", "ms", "session_restarts", "blocks", "flash_ok")}

    def test_cut_forever(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, cut_at=offset_in_app(data, 0.5), cut_ms=None)
        self.assertIn(info["result"], ("timeout", "sync"))
        self.assertFalse(info["flash_ok"])
        self.assertEqual(info["session_restarts"], 1)
        self.assertLess(info["ms"], 60_000)              # gives up in bounded time
        RESULTS["cut_forever"] = {k: info[k] for k in ("result", "ms", "session_restarts", "phase_name")}

    def test_corrupt_on_wire(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, corrupt_at=offset_in_app(data, 0.3))
        self.check_ok(info)
        self.assertEqual(info["segment_restarts"], 1)    # the ROM said checksum: the segment again, same session
        self.assertEqual(info["session_restarts"], 0)
        RESULTS["corrupt_on_wire"] = {k: info[k] for k in ("result", "segment_restarts", "session_restarts",
                                                          "rom_error")}

    def test_reply_lost(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, drop_reply={E.OP_FLASH_DATA: 1})
        self.check_ok(info)
        self.assertEqual(info["session_restarts"], 1)
        RESULTS["reply_lost"] = {k: info[k] for k in ("result", "session_restarts", "ms")}

    def test_bitflip_md5(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, bitflip_block=100)
        self.check_ok(info)
        self.assertEqual(info["session_restarts"], 1)    # MD5 said no: written again
        self.assertEqual(info["bad_seg"], 3)
        RESULTS["bitflip_md5"] = {k: info[k] for k in ("result", "session_restarts", "bad_seg")}

    def test_md5_mismatch_every_time(self):
        data = bytearray(synthetic(app=20_000))
        data[24 + 24 * 3] ^= 0xFF                         # the bundle's own MD5 of the app is wrong
        r, info, *_ = run(bytes(data))
        self.assertEqual(info["result"], "md5")
        self.assertEqual(info["bad_seg"], 3)

    def test_baud_stuck(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, baud_high=460800, baud_stuck=True)
        self.check_ok(info)
        self.assertEqual(info["session_restarts"], 1)    # the next session stayed at 115200
        self.assertEqual(info["baud"], 115200)
        RESULTS["baud_stuck"] = {k: info[k] for k in ("result", "session_restarts", "ms")}

    def test_erase_slow_but_in_time(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, erase_ms_per_sector=100.0)
        self.check_ok(info)

    def test_erase_too_slow(self):
        data, _ = real("u1_hf")
        r, info, *_ = run(data, erase_ms_per_sector=1000.0)
        self.assertEqual(info["result"], "timeout")
        self.assertEqual(info["last_event"], "run")


if __name__ == "__main__":
    prog = unittest.main(exit=False, verbosity=2 if "-v" in sys.argv else 1)
    print("RESULTS " + json.dumps(RESULTS, default=str))
    sys.exit(0 if prog.result.wasSuccessful() else 1)
