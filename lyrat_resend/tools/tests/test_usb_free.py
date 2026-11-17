"""The bench without USB, against the S3 simulator on 127.0.0.1 (s3_sim.py). Goal 093.

What is proven here, and only on the PC:
  Secrets, Variant   first_flash.ensure_secrets() makes ota_secrets.h from wifi_secrets.h without printing a value;
                     fw.current_variant() reads back the variant the LyraT is built with (mac_b1 keeps the pairing)
  FirstFlash         first_flash.py --dry-run: touches no port, file or build; the network half passes against the
                     simulator, and fails with the reason when the S3 or the LyraT is not there
  NetConsole         console.NetWatch: lines over the UDP log, a reply written as the console prints it, read by the
                     parsers of round.py; reboot through the relay is a reset with its reason (SW); a lost listener
                     comes back; the S3's console and its restart by UX; open_watch() picks USB only when asked
  Link               round.reboot_trial and round.pair_trial (fw.py erase-bonds over the network) with the phone
                     replaced by stubs: the trials pass with the console over the network
  Stages             round.py power over the network PASS, and FAIL with the reset it saw (reason in serial.log as a
                     ROM line); fw.py flash --image and erase-bonds over the network; s3.py console and reset;
                     s3_uart.py power (txp= in the stat line) and restart; owner_check.py ports without COM;
                     call_record.py over the network with losses: silence by frame in the WAV, loss counters in its report
  Static             no bench tool opens a COM port unless asked (--usb / --port), no default COM port in the tools
BENCH_NO_ADB=1 for all of it: no test reaches the phone. What only the bench can show (Wi-Fi, the UART wires, the real
firmware's reboot, unpair and UX) is in catalogs/pc_tests_coverage.md as not covered.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "bench"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BENCH))
os.environ["BENCH_NO_ADB"] = "1"
os.environ.pop("BENCH_USB", None)
import simproc  # noqa: E402
from simproc import last_json, run_tool  # noqa: E402

OUT = Path(tempfile.mkdtemp(prefix="t093_"))


def with_sim_addr():
    os.environ["BENCH_S3_ADDR"] = "127.0.0.1"


class Secrets(unittest.TestCase):
    def setUp(self):
        import first_flash
        self.ff = first_flash
        self.d = Path(tempfile.mkdtemp(prefix="t093_sec_", dir=OUT))
        self.ota, self.wifi = self.d / "ota_secrets.h", self.d / "wifi_secrets.h"

    def test_made_from_wifi_with_a_new_token(self):
        self.wifi.write_text('#define WIFI_SSID "bench-net"\n#define WIFI_PASS "pa\\"ss"\n', encoding="utf-8")
        r = self.ff.ensure_secrets(self.ota, self.wifi)
        self.assertEqual((r["ok"], r["state"]), (True, "created"))
        self.assertNotIn("bench-net", json.dumps(r))
        d = self.ff.defines(self.ota)
        self.assertEqual((d["WIFI_SSID"], d["WIFI_PASS"]), ("bench-net", 'pa\\"ss'))
        self.assertGreaterEqual(len(d["OTA_TOKEN"]), 32)
        again = self.ff.ensure_secrets(self.ota, self.wifi)
        self.assertEqual(again["state"], "present")
        self.assertEqual(self.ff.defines(self.ota)["OTA_TOKEN"], d["OTA_TOKEN"])     # never replaced

    def test_dry_run_creates_nothing(self):
        self.wifi.write_text('#define WIFI_SSID "n"\n#define WIFI_PASS "p"\n', encoding="utf-8")
        r = self.ff.ensure_secrets(self.ota, self.wifi, create=False)
        self.assertEqual(r["state"], "would_create")
        self.assertFalse(self.ota.exists())

    def test_template_values_are_missing(self):
        self.wifi.write_text('#define WIFI_SSID "YOUR_SSID"\n#define WIFI_PASS "YOUR_PASSWORD"\n', encoding="utf-8")
        r = self.ff.ensure_secrets(self.ota, self.wifi)
        self.assertEqual((r["ok"], r["state"], r["problems"]), (False, "missing", ["WIFI_SSID", "WIFI_PASS"]))
        self.assertFalse(self.ota.exists())
        r = self.ff.ensure_secrets(self.ota, self.d / "absent.h")
        self.assertFalse(r["ok"])

    def test_short_token_is_a_problem(self):
        self.ota.write_text('#define OTA_TOKEN "short"\n#define WIFI_SSID "n"\n#define WIFI_PASS "p"\n', encoding="utf-8")
        r = self.ff.ensure_secrets(self.ota, self.wifi)
        self.assertEqual((r["ok"], r["problems"]), (False, ["OTA_TOKEN"]))


class Variant(unittest.TestCase):
    def check(self, header, conf, want):
        import fw
        d = Path(tempfile.mkdtemp(prefix="t093_var_", dir=OUT))
        h, c = d / "bench_variant.h", d / "sdkconfig"
        if header is not None:
            h.write_text(header, encoding="utf-8")
        c.write_text(conf, encoding="utf-8")
        self.assertEqual(fw.current_variant(h, c), want)

    def test_variants(self):
        self.check(None, "CONFIG_BT_HFP_WBS_ENABLE=y\n", "base")
        self.check("#pragma once\n#define BENCH_MAC_PATCH 257\n", "CONFIG_BT_HFP_WBS_ENABLE=y\n", "mac_b1")
        self.check("#define BENCH_MAC_PATCH 517\n#define BENCH_FRAME_DROP 1\n", "", "mac_b2p5+fdrop")
        self.check("#define BENCH_MAC_PATCH 257\n", "# CONFIG_BT_HFP_WBS_ENABLE is not set\n", "mac_b1+no_wbs")
        self.check("#define BENCH_AFH_BLOCK 5\n", "", "afh_5")

    def test_round_trip_through_set_variant_names(self):
        import fw
        for name in ("mac_b1", "mac_b3p7", "bredr_n12", "esco_lat_9", "nsinj"):
            macro = fw.macro_of(name)
            d = Path(tempfile.mkdtemp(prefix="t093_rt_", dir=OUT))
            (d / "h").write_text("#define %s %d\n" % macro, encoding="utf-8")
            self.assertEqual(fw.current_variant(d / "h", d / "none"), name)


class FirstFlash(unittest.TestCase):
    def run_ff(self, *args, sim_args=None, timeout=240):
        if sim_args is None:
            return run_tool("first_flash.py", "--dry-run", *args, timeout=timeout)
        with simproc.Sim(*sim_args):
            return run_tool("first_flash.py", "--dry-run", *args, timeout=timeout)

    def record(self, out):
        d = Path(re.search(r"Запись прогона: (.+)", out).group(1).strip())
        return json.loads((d / "first_flash.json").read_text(encoding="utf-8"))

    def test_dry_run_passes_and_touches_nothing(self):
        import first_flash
        watched = [p / n for p, _ in first_flash.PROJECTS.values() for n in ("sdkconfig", "sdkconfig.old")]
        watched += [first_flash.OTA_SECRETS]
        before = {str(p): p.stat().st_mtime_ns if p.exists() else None for p in watched}
        rc, out, err, _ = self.run_ff("--log-seconds", "4", "--label", "test", sim_args=())
        self.assertEqual(rc, 0, out + err)
        self.assertIn("USB-шаги только показаны", out)
        rec = self.record(out)
        self.assertTrue(rec["dry_run"] and rec["usb_can_go"])
        steps = {s["name"]: s["ok"] for s in rec["steps"]}
        self.assertEqual(steps, {"секреты": True, "порты": None, "sdkconfig": None, "сборка": None, "прошивка USB": None,
                                 "сеть": True, "ota.py query": True, "журнал по сети": True})
        self.assertGreater(rec["udplog"]["lines"]["lyrat"], 0)
        self.assertNotIn("127.0.0.1", json.dumps(rec))          # the S3's address is not written
        after = {str(p): p.stat().st_mtime_ns if p.exists() else None for p in watched}
        self.assertEqual(before, after)

    def test_no_s3_on_the_network(self):
        rc, out, err, _ = self.run_ff("--wifi-timeout", "4", "--label", "test-nos3", sim_args=None)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("USB пока не отключать", out)
        rec = self.record(out)
        self.assertFalse(rec["usb_can_go"])
        self.assertEqual([s["name"] for s in rec["steps"] if s["ok"] is False], ["сеть"])

    def test_lyrat_silent_behind_the_s3(self):
        rc, out, err, _ = self.run_ff("--wifi-timeout", "6", "--label", "test-silent", sim_args=("--lyrat-silent",))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("LyraT за ней не отвечает", out)


class NetConsole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with_sim_addr()
        cls.sim = simproc.Sim()

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def watch(self, name, board="lyrat"):
        import console
        w = console.NetWatch(OUT / name, board=board)
        self.addCleanup(w.disconnect)          # a failed test must not leave its listener to the next one
        w.pump(time.monotonic() + 5, until=lambda: w.sp is not None)
        self.assertIsNotNone(w.sp)
        w.pump(time.monotonic() + 3, until=lambda: w.lines > 0)
        return w

    def test_lines_reply_and_status_parsers(self):
        import round as rnd
        w = self.watch("nc_status")
        w.pump(time.monotonic() + 1.5)
        self.assertGreater(w.lines, 5)
        self.assertEqual(rnd.board_status(w), (1, "3c:7a"))
        s = w.finish(1)
        self.assertEqual((s["transport"], s["port"], s["board_resets"]), ("net", "net:lyrat", 0))
        self.assertGreater(s["net"]["records"], 0)
        self.assertEqual(s["net"]["lost_way"], 0)
        text = (OUT / "nc_status" / "serial.log").read_text(encoding="utf-8")
        self.assertIn("CMD:   bonds=1", text)
        self.assertNotIn("11:22:33:44", text)                   # MAC prefixes hidden as on USB

    def test_reboot_is_a_reset_with_its_reason_and_slc_returns(self):
        w = self.watch("nc_reboot")
        w.pump(time.monotonic() + 3, until=lambda: w.slc_up)
        ups = w.slc_ups
        self.assertTrue(w.pulse_reset())
        w.pump(time.monotonic() + 8, until=lambda: w.slc_ups > ups)
        self.assertEqual(len(w.resets), 1)
        self.assertEqual(w.resets[0]["reason"], "SW_RESET")
        self.assertEqual(w.fw_reasons["SW"], 1)
        s = w.finish(1)
        self.assertEqual((s["board_resets"], s["rom_reset_reasons"]), (1, {"SW_RESET": 1}))
        self.assertTrue(s["slc_up_at_end"])
        log = (OUT / "nc_reboot" / "serial.log").read_text(encoding="utf-8")
        self.assertRegex(log, r"rst:0x3 \(SW_RESET\) \[net: new boot id")

    def test_lost_listener_comes_back(self):
        w = self.watch("nc_lost")
        w.sp.close()
        self.assertIsNone(w.sp)
        w.pump(time.monotonic() + 5, until=lambda: w.sp is not None)
        n = w.lines
        w.pump(time.monotonic() + 1.5)
        s = w.finish(1)
        self.assertEqual((s["port_opens"], s["port_drops"]), (2, 1))
        self.assertGreater(s["lines"], n)

    def test_s3_console_and_restart(self):
        import s3
        w = s3.watch(OUT / "nc_s3")
        self.addCleanup(w.disconnect)
        self.assertEqual(w.transport, "net")
        w.pump(time.monotonic() + 5, until=lambda: w.sp is not None)
        w.pump(time.monotonic() + 2.5)
        self.assertTrue(any(s3.parse_stat(line) for _, line in w.recent))
        self.assertTrue(s3.restart(w))
        w.pump(time.monotonic() + 6, until=lambda: w.resets)
        self.assertEqual(len(w.resets), 1)
        w.finish(1)

    def test_open_watch_picks_usb_only_when_asked(self):
        import console
        from serial_watch import SerialWatch
        net = console.open_watch(None, OUT / "ow_net")
        self.assertIsInstance(net, console.NetWatch)
        net.finish(0)
        usb = console.open_watch("COM250", OUT / "ow_usb")      # never opened: nothing is pumped
        self.assertIs(type(usb), SerialWatch)
        usb.finish(0)
        os.environ["BENCH_USB"] = "1"
        try:
            env_usb = console.open_watch(None, OUT / "ow_env")
            self.assertIs(type(env_usb), SerialWatch)
            env_usb.finish(0)
        finally:
            os.environ.pop("BENCH_USB")
        import call_record
        mixed = console.open_watch(None, OUT / "ow_mix", mixin=call_record.CallMixin)
        self.assertIsInstance(mixed, console.NetWatch)
        self.assertIsNone(mixed.sco_open)
        mixed.finish(0)


class Link(unittest.TestCase):
    """round.py link's trials with the phone replaced: what they read of the board comes over the network."""

    @classmethod
    def setUpClass(cls):
        with_sim_addr()
        cls.sim = simproc.Sim()
        import round as rnd
        import phone
        cls.rnd, cls.phone = rnd, phone
        cls.saved = {k: getattr(phone, k) for k in ("hfp_connected", "forget", "pair")}
        phone.hfp_connected = lambda dump=None: True
        phone.forget = lambda timeout=30.0: {"forgot": True}

        def pair(timeout=90.0):
            import s3_uart
            ch = s3_uart.Channel("127.0.0.1")
            try:
                ch.command("_pair 654321")
            finally:
                ch.close()
            return {"passkey": "654321"}
        phone.pair = pair

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.saved.items():
            setattr(cls.phone, k, v)
        cls.sim.close()

    def test_reboot_and_pair_trials_over_the_network(self):
        import console
        out = OUT / "link"
        w = console.NetWatch(out)
        self.addCleanup(w.disconnect)
        w.pump(time.monotonic() + 5, until=lambda: w.slc_up)
        trial = self.rnd.reboot_trial(w, 1, 15.0)
        self.assertTrue(trial["pass"], trial)
        self.assertEqual((trial["resets_seen"], trial["reset_reason"], trial["peer_at_boot"]), (1, "SW_RESET", "3c:7a"))
        args = type("A", (), {"pair_timeout": 20.0, "slc_timeout": 15.0, "port": None})()
        trial = self.rnd.pair_trial(w, 1, args, out)
        self.assertEqual(trial["erase_rc"], 0, (out / "erase-1.txt").read_text(encoding="utf-8"))
        self.assertTrue(trial["passkey_match"], trial)
        self.assertTrue(trial["pass"], trial)
        erase = (out / "erase-1.txt").read_text(encoding="utf-8")
        self.assertIn("unpair removed=1", erase)
        s = w.finish(2)
        self.assertEqual(s["panics"], 0)


class Stages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim()

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def round_json(self, out):
        d = Path(json.loads(out[out.index("{"):])["dir"]) if out.strip().startswith("{") else None
        return d

    def test_round_power_over_the_network(self):
        rc, out, err, _ = run_tool("round.py", "power", "--power", "adapter", "--seconds", "6", "--label", "t093")
        res = json.loads(out[out.index("{"):])
        self.assertEqual((rc, res["verdict"]), (0, "PASS"), err)
        self.assertEqual((res["transport"], res["port"], res["board_resets"], res["port_drops"]), ("net", "net:lyrat", 0, 0))
        self.assertGreater(res["lines"], 0)

    def test_round_power_sees_a_reboot(self):
        def reboot_later():
            time.sleep(3.0)
            run_tool("s3_uart.py", "reboot")
        t = threading.Thread(target=reboot_later)
        t.start()
        rc, out, err, _ = run_tool("round.py", "power", "--power", "adapter", "--seconds", "9", "--label", "t093-reboot")
        t.join()
        res = json.loads(out[out.index("{"):])
        self.assertEqual((rc, res["verdict"], res["board_resets"]), (1, "FAIL", 1), err)
        self.assertEqual(res["rom_reset_reasons"], {"SW_RESET": 1})
        self.assertFalse(res["checks"]["board_resets_zero"])

    def test_fw_erase_bonds_and_flash_over_the_network(self):
        rc, out, err, _ = run_tool("fw.py", "erase-bonds", timeout=120)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("unpair removed=1", out)
        self.assertIn("lyrat back: up", out)
        rc, out, _, _ = run_tool("s3_uart.py", "cmd", "s")
        self.assertIn("bonds=0", out)
        run_tool("s3_uart.py", "cmd", "_pair 111111")
        import images
        syn = OUT / "synthetic"
        images.prepare(syn)            # small images: the real 1.25 MB one takes minutes over the modelled UART
        token = {"BENCH_OTA_TOKEN": re.search(r'"(.+)"', simproc.TOKEN.read_text(encoding="utf-8")).group(1)}
        rc, out, err, _ = run_tool("fw.py", "flash", "--board", "lyrat", "--image", str(syn / "lyrat-ota" / "lyrat_resend.bin"),
                                   timeout=900, extra_env=token)
        self.assertEqual(rc, 0, out + err)
        self.assertIn('"result": "done"', out)
        rc, out, err, _ = run_tool("fw.py", "flash", "--board", "s3", "--image", str(syn / "s3-ota" / "s3_eth_bridge.bin"),
                                   timeout=600, extra_env=token)
        self.assertEqual(rc, 0, out + err)
        self.assertIn('"result": "done"', out)

    def test_wifi_power_by_command_and_in_stat(self):
        import s3
        rc, out, _, _ = run_tool("s3_uart.py", "power", "40")
        self.assertEqual((rc, last_json(out)), (0, {"txp": 40, "err": 0}))
        rc, out, _, _ = run_tool("s3_uart.py", "power", "200")
        self.assertEqual(rc, 1)
        self.assertEqual(last_json(out)["txp"], 40)
        d = OUT / "udplog_txp"
        rc, _, err, _ = run_tool("udplog.py", "--seconds", "4", "--board", "s3", "--out", str(d))
        self.assertEqual(rc, 0, err)
        stats = [s3.parse_stat(line) for line in (d / "udplog.log").read_text(encoding="utf-8").splitlines()
                 if " stat up=" in line]
        self.assertTrue(stats)
        self.assertEqual(stats[-1]["txp"], 40)

    def test_s3_console_reset_and_restart_over_the_network(self):
        rc, out, err, _ = run_tool("s3.py", "console", "--seconds", "3", "--out", str(OUT / "s3con"))
        self.assertEqual(rc, 0, out + err)
        self.assertGreater(last_json(out)["lines"], 0)
        rc, out, err, _ = run_tool("s3.py", "reset", "--out", str(OUT / "s3reset"))
        self.assertEqual(rc, 0, out + err)
        time.sleep(1.5)
        rc, out, err, _ = run_tool("s3_uart.py", "restart")
        self.assertEqual((rc, last_json(out)["restarted"]), (0, True), err)

    def test_owner_check_ports_needs_no_com(self):
        rc, out, err, _ = run_tool("owner_check.py", "ports", timeout=180)
        self.assertIn("Порты USB", out, out + err)
        self.assertNotIn("порта LyraT нет", out)
        self.assertNotIn("порта S3 нет", out)
        self.assertIn("S3 по сети", out)
        self.assertNotIn("S3 не отвечает", out)

    def test_bench_busy_over_the_network(self):
        with_sim_addr()
        import owner_common as oc
        self.assertIsNone(oc.lyrat_over_network())
        os.environ["BENCH_S3_ADDR"] = "127.0.0.9"        # nothing there
        try:
            self.assertIn("S3 не отвечает", oc.lyrat_over_network(timeout=1.0))
        finally:
            os.environ["BENCH_S3_ADDR"] = "127.0.0.1"


class LossyCall(unittest.TestCase):
    """A conversation's streams over a lossy network: each loss is silence of its length at its frame, and counted."""

    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim("--stream-loss", "0.05")

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def test_wav_silence_by_frame(self):
        import udp_record
        out = OUT / "lossy_tap"
        rc, _, err, _ = run_tool("udp_record.py", "--tap", "--seconds", "4", "--slots", "L,LR", "--out", str(out))
        self.assertEqual(rc, 0, err)
        tap = json.loads((out / "tap.json").read_text(encoding="utf-8"))
        for name in ("call", "mic"):
            s = tap[name]
            self.assertGreater(s["lost_packets"], 0)
            with wave.open(str(out / (name + ".wav"))) as w:
                ch = w.getnchannels()
                import array
                pcm = array.array("h")
                pcm.frombytes(w.readframes(w.getnframes()))
            total = 0
            for first, frames in s["gaps"]:
                self.assertEqual(frames % udp_record.SAMPLES_PER_FRAME, 0, (name, first, frames))
                self.assertFalse(any(pcm[first * ch:(first + frames) * ch]), (name, first))
                # the tone is there right before and right after: the gap is in its place, not shifted
                self.assertTrue(any(pcm[max(0, first - 40) * ch:first * ch]) or first == 0)
                self.assertTrue(any(pcm[(first + frames) * ch:(first + frames + 40) * ch]))
                total += frames
            self.assertEqual(total, s["lost_packets"] * udp_record.SAMPLES_PER_FRAME)
            self.assertAlmostEqual(s["seconds"], s["wall_s"], delta=0.1)

    def test_call_record_over_the_network_reports_losses(self):
        out = OUT / "call"
        rc, text, err, _ = run_tool("call_record.py", "--seconds", "6", "--out", str(out), timeout=120)
        s = json.loads((out / "call_summary.json").read_text(encoding="utf-8"))
        for name in ("call", "mic"):
            f = s["files"][name]
            self.assertGreater(f["lost_packets"], 0)
            self.assertGreater(f["loss_pct"], 0)
            self.assertGreater(f["gaps_s"], 0)
            self.assertAlmostEqual(f["seconds"], f["wall_s"], delta=0.15)
        self.assertEqual((s["console_port"], s["console_port_opens"], s["console_port_drops"]), (None, 1, 0))
        self.assertIn("потери S3 -> ПК", text)
        self.assertTrue((out / "serial.log").read_text(encoding="utf-8"))


class Static(unittest.TestCase):
    """No bench tool opens a COM port unless asked: USB comes from --usb, --port or BENCH_USB=1 only."""

    # Where a SerialWatch may still be built: the USB transport itself, and the line check of the UART (its witness
    # must not be the line it checks).
    ALLOWED = {"serial_watch.py", "console.py", "s3.py", "call_record.py", "s3_uart.py"}
    USB_TOOLS = {"serial_watch.py", "s3_uart.py"}     # the USB console itself, and the line check of the UART

    def test_serial_watch_only_behind_the_transport_choice(self):
        for path in BENCH.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                if path.name not in self.ALLOWED:
                    self.assertNotRegex(text, r"\bSerialWatch\(")
                    self.assertNotRegex(text, r"\bCallWatch\(")

    def test_no_default_com_port(self):
        for path in BENCH.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                defaults = re.findall(r'add_argument\("--port", default=(DEFAULT_PORT|"COM\d+")', text)
                # serial_watch.py is the USB console; s3_uart.py linecheck checks the UART the network rides on.
                self.assertEqual(len(defaults), 1 if path.name in self.USB_TOOLS else 0, defaults)

    def test_no_control_characters_in_the_tools(self):       # goal 093 #001: a backspace hid in a pattern
        for path in BENCH.glob("*.py"):
            with self.subTest(path.name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual([hex(ord(c)) for c in text if ord(c) < 32 and c not in "\n\r\t"], [])

    def test_peer_patterns_read_the_firmware_lines(self):
        import round as rnd
        cases = ((rnd.PEER_BOOT, "I (402) BT: peer[0] = xx:xx:xx:xx:3c:7a"),
                 (rnd.PEER_TRY, "I (900) BT: HFP connect try peer[0] xx:xx:xx:xx:3c:7a"),
                 (rnd.PEER_STATUS, "I (5) CMD:   peer[0] = xx:xx:xx:xx:3c:7a"))
        for rx, line in cases:
            m = rx.search(line)
            self.assertTrue(m, line)
            self.assertEqual(m.group(1), "3c:7a")

    def test_no_test_reaches_adb(self):
        import round as rnd
        import phone
        self.assertEqual(rnd.adb("devices"), "")
        with self.assertRaises(phone.PhoneError):
            phone.adb("devices")


if __name__ == "__main__":
    unittest.main()
