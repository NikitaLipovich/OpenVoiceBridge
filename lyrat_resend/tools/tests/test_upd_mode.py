"""The rules of the LyraT's updater in C (main/upd_mode.c through otahost.dll), goal 096.

What can go wrong in the boot cycle of factory (updater) and ota_0 (headset firmware), checked without a board:
the state of the installed firmware from the NVS marks, the window of the updater (10 s, 120 s after "upd", longer while
a session runs), the start of a new image at once, a bad firmware kept out and tried again later, and the console
command "upd" with its refusal during a call. The constants are read from upd_mode.h, not repeated here.
"""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ota_test"))
import otahost as H  # noqa: E402

HEADER = (HERE.parent.parent / "main" / "upd_mode.h").read_text(encoding="utf-8")
D = {k: int(v) for k, v in re.findall(r"#define\s+(UPD_\w+)\s+(\d+)\b", HEADER)}
T0 = 0xFFFFF000          # near the wrap of the 32-bit ms clock: every comparison must survive it


class AppState(unittest.TestCase):
    def test_states(self):
        self.assertEqual(H.upd_app_state(False, 0, 0), "none")
        self.assertEqual(H.upd_app_state(False, 1, 5), "none")
        self.assertEqual(H.upd_app_state(True, 0, 0), "ok")
        self.assertEqual(H.upd_app_state(True, 1, 0), "fresh")
        # a new image that started once and never passed its self-test is kept out at once
        self.assertEqual(H.upd_app_state(True, 1, 1), "bad")
        # an old one only after UPD_TRIES_MAX starts without Bluetooth
        self.assertEqual(H.upd_app_state(True, 0, D["UPD_TRIES_MAX"] - 1), "ok")
        self.assertEqual(H.upd_app_state(True, 0, D["UPD_TRIES_MAX"]), "bad")


class Window(unittest.TestCase):
    def test_normal_start_waits_the_window(self):
        w = H.CUpdWatch(T0, "ok")
        self.assertEqual(w.step(T0), "stay")
        self.assertEqual(w.step(T0 + D["UPD_WINDOW_MS"] - 1), "stay")
        self.assertEqual(w.left_s(T0 + D["UPD_WINDOW_MS"] - 1500), 2)
        self.assertEqual(w.step(T0 + D["UPD_WINDOW_MS"]), "boot_app")

    def test_after_upd_the_window_is_long(self):
        w = H.CUpdWatch(T0, "ok", stay=1)
        self.assertEqual(w.step(T0 + D["UPD_WINDOW_MS"] + 1), "stay")
        self.assertEqual(w.step(T0 + D["UPD_WINDOW_STAY_MS"]), "boot_app")
        w2 = H.CUpdWatch(T0, "fresh", stay=2)
        self.assertEqual(w2.step(T0 + D["UPD_WINDOW_STAY_MS"] - 1), "stay")

    def test_a_session_holds_the_window(self):
        w = H.CUpdWatch(T0, "ok")
        late = T0 + D["UPD_WINDOW_MS"] + 5000
        self.assertEqual(w.step(T0 + 9000, active=True), "stay")
        self.assertEqual(w.step(late, active=True), "stay")           # receiving past the window
        self.assertEqual(w.step(late + 1), "stay")                    # closed without END: the PC may try again
        self.assertEqual(w.left_s(late + 1), D["UPD_AFTER_SESSION_MS"] // 1000)
        self.assertEqual(w.step(late + D["UPD_AFTER_SESSION_MS"] - 1), "stay")
        self.assertEqual(w.step(late + D["UPD_AFTER_SESSION_MS"]), "boot_app")

    def test_new_image_starts_at_once(self):
        w = H.CUpdWatch(T0, "ok", stay=1)
        self.assertEqual(w.step(T0, done=7), "stay")       # the counters before: no image yet
        self.assertEqual(w.step(T0 + 100, active=True, done=7), "stay")
        self.assertEqual(w.step(T0 + 200, done=8), "boot_new")

    def test_no_firmware_waits_for_ever(self):
        w = H.CUpdWatch(T0, "none")
        self.assertEqual(w.left_s(T0), -1)
        for t in (D["UPD_WINDOW_MS"], D["UPD_WINDOW_STAY_MS"], D["UPD_BAD_RETRY_MS"] * 3):
            self.assertEqual(w.step(T0 + t), "stay")
        w.set_app("ok")                                    # an image came (the recheck saw it)
        self.assertEqual(w.step(T0 + D["UPD_BAD_RETRY_MS"] * 3 + 1), "boot_app")

    def test_bad_firmware_is_retried_later(self):
        w = H.CUpdWatch(T0, "bad", stay=1)
        self.assertEqual(w.step(T0 + D["UPD_WINDOW_STAY_MS"] + 1), "stay")
        self.assertEqual(w.step(T0 + D["UPD_BAD_RETRY_MS"] - 1), "stay")
        self.assertEqual(w.step(T0 + D["UPD_BAD_RETRY_MS"]), "boot_retry")


class Command(unittest.TestCase):
    def test_upd(self):
        self.assertEqual(H.upd_command("")[0], D["UPD_STAY_NORMAL"])
        self.assertEqual(H.upd_command("   ")[0], D["UPD_STAY_NORMAL"])
        stay, text = H.upd_command(" 8")
        self.assertEqual(stay, D["UPD_STAY_NOWIFI_TEST"])
        self.assertIn("upd ok", text)

    def test_refused_during_a_call_and_bad_args(self):
        stay, text = H.upd_command("", sco_on=True)
        self.assertEqual(stay, 0)
        self.assertIn("refused: call", text)
        self.assertEqual(H.upd_command("8", sco_on=True)[0], 0)
        for bad in ("1", "x", "8 8", "88"):
            stay, text = H.upd_command(bad)
            self.assertEqual(stay, 0, bad)
            self.assertIn("bad request", text)


class HeadsetHasNoWifi(unittest.TestCase):
    """R13 (goal 096 D8): no source of the headset firmware calls Wi-Fi or the network stack; the updater does."""

    def test_sources(self):
        main = HERE.parent.parent / "main"
        calls = re.compile(r"\b(esp_wifi_\w+|esp_netif_\w+|esp_event_loop_create_default)\s*\(")
        found = {f.name: calls.findall(f.read_text(encoding="utf-8")) for f in main.glob("*.c")}
        self.assertEqual({k: v for k, v in found.items() if v}, {})
        upd = (HERE.parent.parent / "updater" / "main" / "upd_net.c").read_text(encoding="utf-8")
        self.assertIn("esp_wifi_init(", upd)        # the check itself sees calls


if __name__ == "__main__":
    unittest.main()
