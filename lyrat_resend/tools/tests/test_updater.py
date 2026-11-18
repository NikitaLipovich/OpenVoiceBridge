"""The LyraT through its updater (goal 096) end to end against the simulator: ota.py and fw.py on the PC, the S3, the
UART and the LyraT of upd_sim.py (C receivers, the C rules of upd_mode.c).

Covered: an image over Wi-Fi straight to the updater, with the times of the phases; a new image that fails its self-test
is kept out by the updater ("rolled_back") and a good one goes in after it; the test without network ends with exit
code 5 and the UART way through the S3 then works in the same window; fw.py --via auto falls back by itself; the headset
firmware never takes an image over the UART (no_slot) and "upd" brings it to the updater; every start goes through the
updater (a "reboot" of the headset firmware shows mode=updater, then mode=app again).
Not covered here: radio, flash, the IDF bootloader (the bench, goal 096 RUNBOOK step 5).
"""
import re
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "bench"))
import images  # noqa: E402
import ota  # noqa: E402
import simproc  # noqa: E402
from simproc import last_json, run_tool  # noqa: E402

OUT = simproc.TMP / "updater"
TOKEN = {"BENCH_OTA_TOKEN": re.search(r'"(.+)"', simproc.TOKEN.read_text(encoding="utf-8")).group(1)}


def syn():
    d = OUT / "synthetic"
    if not (d / "lyrat-ota").is_dir():
        images.prepare(d)       # small images: the modelled UART takes minutes for a real one
    return d


def lyrat_ota_fields():
    rc, out, err, _ = run_tool("s3_uart.py", "cmd", "ota")
    m = re.search(r"ota state=\S+ (.*)", out)
    return dict(w.split("=", 1) for w in m.group(1).split() if "=" in w) if m else {}


def wait_mode(mode, timeout=30.0):
    end = time.monotonic() + timeout
    f = {}
    while time.monotonic() < end:
        f = lyrat_ota_fields()
        if f.get("mode") == mode:
            return f
        time.sleep(0.3)
    return f


def ota_lyrat(image, *args, timeout=300):
    return run_tool("ota.py", "lyrat", str(image), *args, "--label", "t096", timeout=timeout, extra_env=TOKEN)


class Updater(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The updater's clock 4 times faster: its 120 s window lasts 30 s, the 10 s one 2.5 s.
        cls.sim = simproc.Sim("--upd-scale", "4")

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()

    def setUp(self):
        self.assertEqual(wait_mode("app", 40).get("mode"), "app", "the LyraT must be in its headset firmware")

    def test_1_wifi_update(self):
        new = syn() / "lyrat-ota" / "lyrat_resend.bin"
        img = ota.Image.load(new)
        rc, out, err, secs = ota_lyrat(new)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(last_json(out)["result"], "done")
        self.assertIn("in the updater after", err)
        self.assertIn("upload (wifi): ok", err)
        f = lyrat_ota_fields()
        self.assertEqual((f.get("mode"), f.get("elf"), f.get("img"), f.get("wifi")), ("app", img.elf[:16], "valid", "none"))

    def test_2_bad_image_kept_out_then_good_one(self):
        base = syn() / "lyrat-base" / "lyrat_resend.bin"
        bad = ota.Image.load(base)
        rc, out, err, _ = ota_lyrat(base, "--test-rollback")
        self.assertEqual(rc, 0, out + err)                          # rolled back as asked
        self.assertEqual(last_json(out)["result"], "rolled_back")
        f = lyrat_ota_fields()
        self.assertEqual((f.get("mode"), f.get("app_st"), f.get("inv")), ("updater", "bad", bad.elf[:16]))
        # the updater keeps waiting (no 10 s start of the bad one) and takes the next image
        time.sleep(4.0)
        self.assertEqual(lyrat_ota_fields().get("mode"), "updater")
        # the same build again, now without FAILTEST: the old verdict about its ELF must not count (incident 005)
        rc, out, err, _ = ota_lyrat(base)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(last_json(out)["result"], "done")
        f = lyrat_ota_fields()
        self.assertEqual((f.get("mode"), f.get("elf"), f.get("img")), ("app", bad.elf[:16], "valid"))

    def test_3_no_wifi_then_uart_in_the_same_window(self):
        base = syn() / "lyrat-base" / "lyrat_resend.bin"
        rc, out, err, _ = ota_lyrat(base, "--test-no-wifi")
        self.assertEqual(rc, 5, out + err)
        self.assertEqual(last_json(out)["result"], "wifi_failed")
        f = lyrat_ota_fields()
        self.assertEqual(f.get("mode"), "updater")                  # still waiting: the UART way is next
        self.assertTrue(f.get("wifi", "").startswith("fail:"), f)
        rc, out, err, _ = ota_lyrat(base, "--via", "uart", timeout=600)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(last_json(out)["result"], "done")
        self.assertIn("upload (uart): ok", err)

    def test_4_fw_auto_falls_back_to_uart(self):
        rc, out, _, _ = run_tool("s3_uart.py", "cmd", "upd 8")
        self.assertIn("upd ok stay=2", out)
        self.assertEqual(wait_mode("updater").get("mode"), "updater")
        new = syn() / "lyrat-ota" / "lyrat_resend.bin"
        rc, out, err, _ = run_tool("fw.py", "flash", "--board", "lyrat", "--image", str(new), timeout=900,
                                   extra_env=TOKEN)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("(wifi):", out)
        self.assertIn("again through the S3's UART", out)
        self.assertIn("(uart):", out)
        self.assertIn('"result": "done"', out)

    def test_5_every_start_goes_through_the_updater(self):
        rc, out, _, _ = run_tool("s3_uart.py", "cmd", "reboot")
        self.assertIn("reboot in", out)
        f = wait_mode("updater", 10)
        self.assertEqual(f.get("mode"), "updater")
        self.assertEqual(f.get("app_st"), "ok")
        self.assertEqual(wait_mode("app", 30).get("mode"), "app")    # by itself after the 10 s window
        # the headset firmware has no slot: an image over the UART is refused before anything is written
        rc, out, _, _ = run_tool("s3_uart.py", "cmd", "upd 1")
        self.assertIn("upd refused: bad request", out)
        rc, out, _, _ = run_tool("s3_uart.py", "cmd", "tone 1")
        self.assertNotIn("not in the updater", out)


class HelloRace(unittest.TestCase):
    """Incident 003: a slow first answer to HELLO makes ota.py send HELLO again; the receiver keeps the nonce of the last
    one, so the tool must take the last answer, not the first."""

    def test_last_nonce_wins(self):
        import socket
        import threading
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        nonces = []

        def serve():
            srv.settimeout(5)
            got = []
            try:
                while len(got) < 2:
                    data, peer = srv.recvfrom(2048)
                    got.append((data, peer))
            except OSError:
                return
            for data, peer in got:          # both answers late, the second HELLO's nonce last
                n = bytes(range(16)) if not nonces else bytes(range(16, 32))
                nonces.append(n)
                srv.sendto(data[:4] + bytes([ota.OP_HELLO]) + bytes(9) + n.hex().encode(), peer)

        t = threading.Thread(target=serve)
        t.start()
        link = ota.Link("127.0.0.1", srv.getsockname()[1])
        try:
            got = link.hello(0)
        finally:
            link.close()
            t.join()
            srv.close()
        self.assertEqual(len(nonces), 2)
        self.assertEqual(got, nonces[-1])


if __name__ == "__main__":
    unittest.main()
