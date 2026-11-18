"""The LyraT of goal 096 for the bench simulator (s3_sim.py): the updater in factory and the headset firmware in ota_0.

What it repeats, each from the firmware, not from the documents:
  - every start goes through the updater; its window and the state of the installed firmware are the C rules of
    main/upd_mode.c (otahost.dll: upd_app_state, upd_watch_*), on a clock sped up by `scale` (default 20: the 10 s
    window lasts 0.5 s, the 120 s one 6 s, the 10 min retry of a bad firmware 30 s);
  - the headset firmware: "upd [8]" (upd_command of upd_mode.c) restarts into the updater, "reboot" too; the UART
    receiver has no slot (no_slot); a fresh image passes or fails its self-test (FAILTEST) and a failed one is kept out;
  - the updater: Wi-Fi up after WIFI_UP_S (never with the wrong password of "upd 8": wifi=fail:15), UDP receiver with the
    token on its own port (the board's port 5007; here any port, given in the status line as port=), the UART receiver
    of the S3's relay into ota_0, console s, ota, reboot, app;
  - the status text of lyrat_ota.c with lyrat_mode_text() of each firmware at its end.
Made by goal 096.
"""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ota_test"))
sys.path.insert(0, str(HERE.parent / "bench"))
import otahost as H  # noqa: E402
import ota  # noqa: E402
import s3_emu  # noqa: E402

APP_SLOT = 0x2E0000                 # ota_0 of partitions_ota.csv
UPDATER = SimpleNamespace(version="updater-sim", elf="f0" * 32)
WIFI_UP_S = 0.3
SELFTEST_FRESH_S = 2.0
SELFTEST_S = 0.2
WRONG_PASS_REASON = 15              # 4-way handshake timeout: what a wrong password gives


class UpdBoard(s3_emu.Board):
    def __init__(self, image: bytes, scale=20.0):
        super().__init__("lyrat", image)
        self.scale = scale
        self.mode = "app"
        self.app = self.run
        self.fresh = self.tries = self.stay = 0
        self.failtest_mark = self.failtest_now = False
        self.watch = None
        self.app_st = "ok"
        self.wifi = "off"
        self.wifi_at = None
        self.done = 0
        self.next_boot = None           # (time, "updater"|"app", stay)
        self.active = lambda: False     # set by the simulator: a session is open on either receiver
        self.selftest = "pass"
        self.log = lambda text, tag="LyraT": None

    def sim_ms(self):
        return int(time.monotonic() * 1000 * self.scale) & 0xFFFFFFFF

    # --- starts --------------------------------------------------------------------------------------------------------
    def restart(self, to, stay=0, delay=0.0):
        self.next_boot = (time.monotonic() + delay, to, stay)

    def _boot(self, to, stay):
        t = time.monotonic()
        self.boot = s3_emu.random.randrange(65536)
        self.restarts += 1
        self.down_until = t + 0.4
        self.pending = None
        self.check_at = None
        if to == "updater":
            self.mode, self.slot, self.run = "updater", "factory", UPDATER
            self.img, self.selftest = "valid", "off"
            self.stay = stay
            self.app_st = H.upd_app_state(self.app is not None, self.fresh, self.tries)
            self.watch = H.CUpdWatch(self.sim_ms(), self.app_st, stay)
            self.last_invalid = self.app if self.app_st == "bad" else None
            self.wifi, self.wifi_at = "connecting", t + WIFI_UP_S
        else:
            self.mode, self.slot, self.run = "app", "ota_0", self.app
            self.last_invalid = None
            self.wifi, self.wifi_at = "none", None
            self.failtest_now, self.failtest_mark = self.failtest_mark, False
            self.img, self.selftest = "pending", "wait"
            self.check_at = t + (SELFTEST_FRESH_S if self.fresh else SELFTEST_S)
        return to

    def activate(self, image: bytes, failtest: bool, delay=0.3):
        """A receiver of the updater took a new image into ota_0 (lyrat_ota.c port_finish): fresh, tries 0."""
        self.app = ota.Image(image)
        self.fresh, self.tries = 1, 1     # the start ahead is counted at once (lyrat_ota.c port_finish)
        self.failtest_mark = failtest
        self.done += 1
        self.last_invalid = None          # lyrat_inv_elf(): no verdict after an image came in this start (incident 005)

    def step(self):
        t = time.monotonic()
        if self.next_boot and t >= self.next_boot[0]:
            _, to, stay = self.next_boot
            self.next_boot = None
            return self._boot(to, stay)
        if self.mode == "updater":
            if self.wifi == "connecting" and t >= self.wifi_at:
                self.wifi = "fail:%d" % WRONG_PASS_REASON if self.stay == 2 else "up"
            st = self.watch.step(self.sim_ms(), self.active(), self.done)
            if st == "boot_new":
                self.log("new image in ota_0, starting it", "updater")
                self.restart("app", delay=0.3)
            elif st in ("boot_app", "boot_retry") and not self.next_boot:
                if st == "boot_retry":
                    self.fresh = self.tries = 0
                self.tries += 1
                self.log("starting the firmware %s" % self.app.version, "updater")
                self.restart("app", delay=0.1)
        elif self.check_at and t >= self.check_at:
            self.check_at = None
            if self.fresh and self.failtest_now:
                self.img, self.selftest = "invalid", "fail"
                self.log("self-test failed: back to the updater, it keeps this image out", "ota")
                self.restart("updater", delay=0.5)
            else:
                self.img, self.selftest = "valid", "pass"
                self.fresh = self.tries = 0
        return None

    # --- the end of the status line, lyrat_mode_text() ---------------------------------------------------------------
    def mode_text(self, host, port):
        if self.mode == "app":
            return "mode=app wifi=none"
        ip = host if self.wifi == "up" else "-"
        app = self.app
        return ("mode=updater app=%s app_elf=%s app_st=%s tries=%d wifi=%s ip=%s port=%d left=%d" % (
            app.version[:16] if app else "-", app.elf[:16] if app else "-", self.app_st, self.tries, self.wifi, ip, port,
            self.watch.left_s(self.sim_ms()) if self.watch else -1))

    def command(self, word, arg, sco):
        """The console of the updater, or "upd" of the headset firmware. (status, text) or None for the others."""
        if self.mode == "app":
            if word != "upd":
                return None
            stay, text = H.upd_command(arg, sco)
            if stay:
                self.restart("updater", stay, delay=1.0)
            return 0, text
        if word in ("s", "status"):
            return 0, "=== Updater ===\n  %s" % self.mode_text("-", 0)
        if word == "reboot":
            self.restart("updater", 0, delay=1.0)
            return 0, "reboot in 1000 ms (the updater again)"
        if word == "app":
            self.fresh = self.tries = 0
            self.app_st = H.upd_app_state(self.app is not None, 0, 0)
            self.watch.set_app(self.app_st)
            if self.app is not None:
                self.tries = 1
                self.restart("app", delay=0.1)
            return 0, "app: %s" % ("starting the firmware" if self.app is not None else "no firmware in ota_0")
        if word == "upd":
            return 0, "upd: already in the updater"
        if word in ("ota", "ulog", "ustat", "help", "h", "?"):
            return None                 # the simulator's own answers
        return 1, "%s: not in the updater (no Bluetooth, no sound here)" % word


class V2Rx(s3_emu.LyratRx):
    """The UART receiver of lyrat_ota.c: no slot in the headset firmware, ota_0 in the updater."""

    sim = None

    def _cap(self, ctx):
        return APP_SLOT if self.board.mode == "updater" else 0

    def _stext(self, ctx, out, cap):
        text = self.sim.lyrat_status_text().encode()[:cap - 1]
        import ctypes
        ctypes.memmove(out, text, len(text))
        return len(text)


class NetRx:
    """The updater's UDP receiver (upd_net.c): the C receiver with the token, one nonce per BEGIN."""

    def __init__(self, sim):
        self.sim = sim
        self.new()

    def new(self):
        self.rx = H.CReceiver(APP_SLOT, ota.CHIP_ESP32, "lyrat_resend", token=self.sim.token or b"")
        self.nonce = None
        self.nonce_t = 0.0

    def handle(self, data, peer, sock):
        b = self.sim.lyrat
        if b.mode != "updater" or not b.up() or b.wifi != "up":
            return                      # no network: nobody answers
        if len(data) < 5 or data[:2] != b"OT" or data[2] != 1 or data[3] != 0:
            return
        op, head = data[4], data[:4]
        if op == ota.OP_HELLO:
            self.nonce, self.nonce_t = os.urandom(16), time.monotonic()
            rep = bytes([op]) + bytes(9) + self.nonce.hex().encode()
        elif op == ota.OP_QUERY:
            import json
            i = self.rx.info()
            rep = bytes([op]) + bytes(9) + json.dumps({"fw": "lyrat_updater", "ip": self.sim.host, "rx": {
                "state": i["state_name"], "size": i["size"], "next": i["next"], "err": i["err_name"], "done": i["done"]},
                "status": self.sim.lyrat_status_text()}).encode()
        else:
            r = self.rx
            fresh = self.nonce is not None and time.monotonic() - self.nonce_t < 60
            r.nonce = self.nonce if (fresh and self.sim.token) else None
            if op == ota.OP_BEGIN:
                same = r.info()["state_name"] == "recv" and int.from_bytes(data[5:9], "little") == r.info()["session"]
                if not same:
                    self.nonce = None
                    if self.sim.ly_rx.info()["state_name"] == "recv":     # the S3 is sending one over the UART
                        sock.sendto(head + bytes([op, ota.ST["refused"]]) + data[5:9] + bytes(4), peer)
                        return
            rep = r.handle(data[4:], s3_emu.now_ms())
            if r.events() & H.EV_DONE:
                b.activate(r.active, bool(r.info()["flags"] & ota.F_FAILTEST))
        sock.sendto(head + rep, peer)
