"""The LyraT's two modes against models of a phone and of Windows, on a model clock. Goal 102.

What the bench would show about connecting, keeping and dropping links, without a board: the rules are the firmware's
own C (main/hf_mode.c through otahost.dll); this file models what is around them:
  Stack    Bluedroid's HF client: ONE link at a time (one control block), outgoing page with its timeout, incoming
           connections refused while the slot is taken, SLC after ACL, SCO on the AG's request, disconnects, link loss.
  Phone    connects by itself when its Bluetooth comes on and after a link loss (a few retries, or for ever when
           aggressive); audio on a call (RING -> the LyraT answers -> SCO) or from the probe app; accepts pages.
  Windows  never connects by itself (bench 2026-08-25, harness/GOAL.md) except right after pairing and on "Connect" in
           its settings; audio only while an app holds the Hands-Free endpoint, opened and closed often; accepts pages.
           hfp_service=False: the radio link comes up, HFP does not (the service of the device switched off).
  Lyrat    main.c's glue: hf_peers_load, hfm_load, hf_reconnect_task (first try after 4 s, then every 8 s or at once
           when kicked), hf_client_cb, gap_cb (paired), the commands "mode" and "unpair", NVS as a dict that survives
           a reboot. Log lines use the format strings of main.c (FMT, checked against the source by the tests).
OldLyrat is the reconnect logic of phone-bringup (before goal 102), for the regression check of the phone mode.
"""
import heapq
import itertools
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAIN_C = HERE.parent.parent / "main" / "main.c"
sys.path.insert(0, str(HERE.parent / "ota_test"))
import otahost as H  # noqa: E402

# Times of the radio, ms (typical figures; the page timeout is Bluedroid's default 0x2000 slots = 5.12 s)
PAGE_MS = 1200
PAGE_TIMEOUT_MS = 5120
IN_ACL_MS = 300
SLC_MS = 400
SCO_MS = 150
DISC_MS = 100

# Format strings of main.c that this model prints (the tests check that each one is in the source as it is here)
FMT = {
    "boot": "BOOT reset_reason=%s code=%d",
    "bt_ready": "BT ready: '%s' MAC=%02x:%02x:%02x:%02x:%02x:%02x",
    "mode_boot": "mode boot: mode=%s src=%s phone=%s pc=%s%s",
    "try_old": "HFP connect try peer[%u] %02x:%02x:%02x:%02x:%02x:%02x",
    "try": "mode connect try %s %s in %s mode",
    "conn": "HFP conn state=%d",
    "judge": "mode slc %s %s in %s mode: %s",
    "slc_up": "*** SLC CONNECTED ***",
    "slc_down": "SLC DOWN after %\"PRId64\" s, last_acl_reason=0x%x",
    "acl_up": "ACL UP stat=0x%x handle=0x%x",
    "acl_down": "ACL DOWN reason=0x%x handle=0x%x",
    "paired": "Paired: '%s'",
    "sco_state": "SCO audio state=%d codec=%s",
    "sco_open": "*** SCO OPEN ***",
    "sco_closed": "*** SCO CLOSED ***",
    "ring": "RING — auto-answer",
    "mode_cmd": "mode cmd: %s",
}
CONN_DISCONNECTED, CONN_CONNECTING, CONN_CONNECTED, CONN_SLC = 0, 1, 2, 3
AUDIO_DISCONNECTED, AUDIO_CVSD, AUDIO_MSBC = 0, 2, 3
LYRAT_MAC = bytes([0x24, 0x0a, 0xc4, 0x00, 0x10, 0x20])


def fmt(key, *args):
    return FMT[key].replace('%"PRId64"', "%d") % args


def task_timing():
    """(first try ms, period ms) of hf_reconnect_task in main.c."""
    src = MAIN_C.read_text(encoding="utf-8")
    body = src[src.index("static void hf_reconnect_task"):]
    body = body[:body.index("\n}\n")]
    first = int(re.search(r"vTaskDelay\(pdMS_TO_TICKS\((\d+)\)\)", body).group(1))
    period = int(re.search(r"ulTaskNotifyTake\(pdTRUE, pdMS_TO_TICKS\((\d+)\)\)", body).group(1))
    return first, period


class Clock:
    def __init__(self):
        self.now = 0
        self.q = []
        self.seq = itertools.count()

    def at(self, delay, fn, *args):
        heapq.heappush(self.q, (self.now + delay, next(self.seq), fn, args))

    def run_until(self, t):
        while self.q and self.q[0][0] <= t:
            when, _, fn, args = heapq.heappop(self.q)
            self.now = when
            fn(*args)
        self.now = max(self.now, t)

    def run_for(self, ms):
        self.run_until(self.now + ms)

    def run_until_cond(self, cond, limit_ms):
        """Run until cond() holds (checked after each event); returns the time it took, or None."""
        start, end = self.now, self.now + limit_ms
        while not cond():
            if not self.q or self.q[0][0] > end:
                self.now = end
                return None
            when, _, fn, args = heapq.heappop(self.q)
            self.now = when
            fn(*args)
        return self.now - start


class AG:
    kind = "ag"
    codec = "mSBC"

    def __init__(self, world, bda, name):
        self.world, self.bda, self.name = world, bytes(bda), name
        self.bt_on = True
        self.paired = True              # its own side of the pairing
        self.hfp_service = True
        self.rejected = 0               # incoming tries refused by the headset's stack
        self.dropped = 0                # links the headset took down after SLC
        self.links = 0

    @property
    def stack(self):
        return self.world.stack

    def accepts_page(self):
        return self.bt_on and self.paired

    def connect(self):
        """The AG starts a link to the headset."""
        if self.bt_on and self.paired:
            self.stack.incoming(self)

    def on_link_up(self):
        self.links += 1

    def on_link_down(self, by_headset, loss):
        if by_headset:
            self.dropped += 1

    def on_refused(self):
        self.rejected += 1

    def open_audio(self, codec=None):
        self.stack.ag_open_sco(self, codec or self.codec)

    def close_audio(self):
        self.stack.ag_close_sco(self)

    def leave(self):
        """Away, asleep or Bluetooth off: the link is lost."""
        self.bt_on = False
        self.stack.link_loss(self)


class Phone(AG):
    kind = "phone"

    def __init__(self, world, bda, name="Phone", retries=3, retry_ms=3000, aggressive=False):
        super().__init__(world, bda, name)
        self.retries, self.retry_ms, self.aggressive = retries, retry_ms, aggressive
        self.left = 0
        self.in_call = False

    def come_back(self, delay=1000):
        """Bluetooth on: the phone connects its headsets by itself."""
        self.bt_on = True
        self.left = self.retries
        self.world.clock.at(delay, self._try)

    def _try(self):
        if not self.bt_on or self.stack.linked_to(self):
            return
        self.connect()
        self.left -= 1
        if self.aggressive or self.left > 0:
            self.world.clock.at(self.retry_ms, self._try)

    def on_link_down(self, by_headset, loss):
        super().on_link_down(by_headset, loss)
        self.in_call = False
        if loss and self.bt_on:
            self.come_back(self.retry_ms)
        elif by_headset and self.aggressive:
            self.world.clock.at(self.retry_ms, self._try)

    def on_refused(self):
        super().on_refused()

    def call(self):
        """An incoming call: RING to the headset, which answers; then the phone opens the audio."""
        if self.stack.linked_to(self) == "slc":
            self.in_call = True
            self.world.lyrat.on_ring()

    def answered(self):
        if self.in_call:
            self.world.clock.at(300, self.open_audio)

    def hang_up(self):
        self.in_call = False
        self.close_audio()


class Windows(AG):
    kind = "pc"
    codec = "CVSD"

    def __init__(self, world, bda, name="PC", codec="CVSD", connect_after_pair=True):
        super().__init__(world, bda, name)
        self.codec, self.connect_after_pair = codec, connect_after_pair

    def pair(self):
        """Settings > Bluetooth > Add device > LyraT_Phone (the headset must be visible)."""
        self.paired = True
        self.world.lyrat.bonds_add(self.bda)
        self.world.lyrat.on_paired(self.bda, self.name)
        if self.connect_after_pair:
            self.world.clock.at(500, self.connect)

    def user_connect(self):
        self.connect()

    def come_back(self):
        self.bt_on = True            # no connect of its own: the headset has to call


class Stack:
    """Bluedroid HF client: one control block."""

    def __init__(self, world):
        self.world = world
        self.slot = None                # {"ag", "state": "connecting"|"slc", "out"}
        self.sco = None
        self.ignored = 0                # connects from the headset while the slot was taken
        self.handle = 0x80

    @property
    def lyrat(self):
        return self.world.lyrat

    @property
    def clock(self):
        return self.world.clock

    def linked_to(self, ag):
        return self.slot["state"] if self.slot and self.slot["ag"] is ag else None

    def connect(self, bda):
        if self.slot:
            self.ignored += 1
            return
        ag = self.world.ag_by_addr(bda)
        self.slot = {"ag": ag, "state": "connecting", "out": True, "gen": object()}
        gen = self.slot["gen"]
        self.lyrat.hf_conn_state(CONN_CONNECTING, bda)
        if ag and ag.accepts_page():
            self.clock.at(PAGE_MS, self._acl_up, gen)
        else:
            self.clock.at(PAGE_TIMEOUT_MS, self._page_failed, gen)

    def _alive(self, gen):
        return self.slot is not None and self.slot["gen"] is gen

    def _page_failed(self, gen):
        if self._alive(gen):
            bda = self.slot["ag"].bda if self.slot["ag"] else None
            self.slot = None
            self.lyrat.hf_conn_state(CONN_DISCONNECTED, bda)

    def _acl_up(self, gen):
        if not self._alive(gen):
            return
        self.lyrat.gap_acl_up(self.handle)
        ag = self.slot["ag"]
        if ag.hfp_service:
            self.clock.at(SLC_MS, self._slc, gen)
        else:
            self.clock.at(SLC_MS, self._no_service, gen)

    def _no_service(self, gen):
        if self._alive(gen):
            bda = self.slot["ag"].bda
            self.slot = None
            self.lyrat.hf_conn_state(CONN_DISCONNECTED, bda)
            self.lyrat.gap_acl_down(0x13, self.handle)

    def _slc(self, gen):
        if not self._alive(gen):
            return
        ag = self.slot["ag"]
        self.lyrat.hf_conn_state(CONN_CONNECTED, ag.bda)
        self.slot["state"] = "slc"
        ag.on_link_up()
        self.lyrat.hf_conn_state(CONN_SLC, ag.bda)

    def incoming(self, ag):
        if self.slot or ag.bda not in self.lyrat.bonds:
            ag.on_refused()
            return
        self.slot = {"ag": ag, "state": "connecting", "out": False, "gen": object()}
        self.clock.at(IN_ACL_MS, self._acl_up, self.slot["gen"])

    def _down(self, reason, by_headset, loss):
        ag = self.slot["ag"]
        if self.sco:
            self._sco_closed()
        self.slot = None
        self.lyrat.hf_conn_state(CONN_DISCONNECTED, ag.bda)
        self.lyrat.gap_acl_down(reason, self.handle)
        ag.on_link_down(by_headset, loss)

    def disconnect(self, bda):
        if self.slot and self.slot["ag"] and self.slot["ag"].bda == bytes(bda):
            gen = self.slot["gen"]
            self.clock.at(DISC_MS, lambda: self._alive(gen) and self._down(0x16, True, False))

    def link_loss(self, ag):
        if self.slot and self.slot["ag"] is ag:
            if self.slot["state"] == "slc":
                self._down(0x08, False, True)
            # a page in flight to it simply times out

    def ag_open_sco(self, ag, codec):
        if self.linked_to(ag) == "slc" and not self.sco:
            self.sco = codec
            self.clock.at(SCO_MS, self._sco_open, ag, codec)

    def _sco_open(self, ag, codec):
        if self.linked_to(ag) == "slc" and self.sco == codec:
            self.lyrat.audio_state(AUDIO_MSBC if codec == "mSBC" else AUDIO_CVSD)

    def ag_close_sco(self, ag):
        if self.linked_to(ag) == "slc" and self.sco:
            self._sco_closed()

    def _sco_closed(self):
        self.sco = None
        self.lyrat.audio_state(AUDIO_DISCONNECTED)


class Lyrat:
    """main.c's glue around hf_mode.c. nvs: dict kept by the caller across reboots; bonds: the stack's bond list."""

    def __init__(self, world, nvs, bonds, default_mode="phone", sco_codec_names=("CVSD(8kHz)", "mSBC(16kHz)")):
        self.world, self.nvs, self.bonds = world, nvs, bonds
        self.default_mode = default_mode
        self.log = []
        self.first_ms, self.period_ms = task_timing()
        self.slc_up = False
        self.slc_bda = None
        self.slc_up_ms = 0
        self.sco_active = False
        self.kick_on_down = False
        self.acl_reason = 0xFFFFFFFF
        self.task_gen = None
        self.task_started = False
        self.kick_pending = False
        self.sco_names = sco_codec_names
        self.peers = []
        self.hfm = None

    # --- log
    def say(self, key, *args, tag="BT"):
        self.log.append("I (%d) %s: %s" % (self.world.clock.now, tag, fmt(key, *args)))

    # --- NVS helpers (hf_peers_save / hfm_save_locked)
    def peers_save(self):
        self.nvs["peers"] = list(self.peers)

    def hfm_save(self):
        if self.hfm.dirty:
            saved = self.hfm.saved()
            self.nvs["mode"] = saved["mode"]
            for k, key in (("phone", "peer_ph"), ("pc", "peer_pc")):
                if saved[k]:
                    self.nvs[key] = saved[k]
                else:
                    self.nvs.pop(key, None)

    # --- start
    def boot(self):
        self.say("boot", "SW", 3, tag="MAIN")
        # hf_peers_load
        self.peers = list(self.nvs.get("peers", []))[:2]
        self.peers_from_nvs = bool(self.peers)
        if not self.peers and self.bonds:
            self.peers = list(self.bonds)[:2]
            self.peers_save()
        self.hfm_load()
        self.say("bt_ready", "LyraT_Phone", *LYRAT_MAC)
        self.task_gen = object()
        self.world.clock.at(self.first_ms, self._task_first, self.task_gen)

    def hfm_load(self):
        self.hfm = H.CHfm(self.nvs.get("mode"), self.default_mode, self.nvs.get("peer_ph"), self.nvs.get("peer_pc"),
                          self.peers[0] if self.peers_from_nvs and self.peers else None)
        self.hfm_save()
        self.say("mode_boot", self.hfm.mode, self.hfm.src, H.hfm_short(self.hfm.slot("phone")),
                 H.hfm_short(self.hfm.slot("pc")), " migrated" if self.hfm.s.migrated else "")

    # --- hf_reconnect_task
    def _task_first(self, gen):
        if gen is self.task_gen:
            self.task_started = True
            pending, self.kick_pending = self.kick_pending, False
            self._task_loop(gen)
            if pending:
                self.kick()

    def _task_loop(self, gen):
        if gen is not self.task_gen:
            return
        if not self.slc_up:
            target = self.next_target()
            if target:
                i = next((n for n, p in enumerate(self.peers) if p == target), len(self.peers))
                self.say("try_old", i, *target)
                self.try_line(target)
                self.world.stack.connect(target)
        self.task_gen = gen = object()
        self.world.clock.at(self.period_ms, self._task_loop, gen)

    def next_target(self):
        return self.hfm.next_target(list(self.bonds))

    def try_line(self, target):
        self.say("try", self.hfm.role(target), H.hfm_short(target), self.hfm.mode)

    def kick(self):
        if not self.task_started:
            self.kick_pending = True          # xTaskNotifyGive before the first wait: the first take returns at once
            return
        self.task_gen = gen = object()
        self.world.clock.at(0, self._task_loop, gen)

    # --- hf_client_cb
    def hf_conn_state(self, state, bda):
        self.say("conn", state)
        if state == CONN_DISCONNECTED:
            if self.slc_up:
                self.say("slc_down", (self.world.clock.now - self.slc_up_ms) // 1000, self.acl_reason)
            self.slc_up = False
            if self.kick_on_down:
                self.kick_on_down = False
                self.kick()
        if state == CONN_SLC:
            if not self.slc_keep(bda):
                return
            self.slc_bda = bytes(bda)
            self.slc_up = True
            self.slc_up_ms = self.world.clock.now
            self.peer_add(bda)
            self.say("slc_up")

    def slc_keep(self, bda):
        role = self.hfm.role(bda)
        r = self.hfm.on_slc(bda)
        self.hfm_save()
        self.say("judge", role, H.hfm_short(bda), self.hfm.mode, r)
        keep = r in ("keep", "adopt")
        if not keep:
            self.kick_on_down = True
            self.world.stack.disconnect(bda)
        return keep

    def peer_add(self, bda):
        self.peers = [bytes(bda)] + [p for p in self.peers if p != bytes(bda)][:1]
        self.peers_save()

    def audio_state(self, st):
        msbc = st == AUDIO_MSBC
        self.say("sco_state", st, self.sco_names[1] if msbc else self.sco_names[0])
        if st in (AUDIO_CVSD, AUDIO_MSBC):
            self.sco_active = True
            self.say("sco_open")
        else:
            self.sco_active = False
            self.say("sco_closed")

    def on_ring(self):
        self.say("ring")
        ag = self.world.stack.slot["ag"]
        ag.answered()

    # --- gap_cb
    def gap_acl_up(self, handle):
        self.say("acl_up", 0, handle)

    def gap_acl_down(self, reason, handle):
        self.acl_reason = reason
        self.log.append("W (%d) BT: %s" % (self.world.clock.now, fmt("acl_down", reason, handle)))

    def on_paired(self, bda, name):
        self.say("paired", name)
        self.hfm.on_paired(bda)

    def bonds_add(self, bda):
        if bytes(bda) not in self.bonds:
            self.bonds.append(bytes(bda))

    # --- console
    def command(self, line):
        cmd, _, rest = line.partition(" ")
        if cmd == "mode":
            act, text = self.hfm.command(rest, self.sco_active, self.slc_bda if self.slc_up else None)
            if act & H.HFM_DO_SAVE:
                self.hfm_save()
            if rest.strip():
                self.say("mode_cmd", text)
            if act & H.HFM_DO_DROP:
                self.kick_on_down = True
                self.world.stack.disconnect(self.slc_bda)
            elif act & H.HFM_DO_KICK:
                self.kick()
            return text
        if cmd == "unpair":
            self.bonds.clear()
            self.peers = []
            self.peers_save()
            self.hfm.forget_all()
            self.hfm_save()
            return "unpair"
        raise ValueError(line)


class OldLyrat(Lyrat):
    """The reconnect logic of phone-bringup: the MRU list round robin, every SLC kept, no mode."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.idx = 0

    def hfm_load(self):
        self.hfm = None

    def next_target(self):
        if not self.peers:
            return None
        t = self.peers[self.idx % len(self.peers)]
        self.idx += 1
        return t

    def try_line(self, target):
        pass

    def slc_keep(self, bda):
        return True

    def on_paired(self, bda, name):
        self.say("paired", name)


class World:
    """One bench: a LyraT, its stack and the AGs around it."""

    def __init__(self, nvs=None, bonds=None, default_mode="phone", lyrat_cls=Lyrat):
        self.clock = Clock()
        self.stack = Stack(self)
        self.ags = []
        self.nvs = {} if nvs is None else nvs
        self.bonds = [] if bonds is None else bonds
        self.default_mode, self.lyrat_cls = default_mode, lyrat_cls
        self.lyrat = None

    def add(self, ag):
        self.ags.append(ag)
        return ag

    def ag_by_addr(self, bda):
        return next((a for a in self.ags if a.bda == bytes(bda)), None)

    def start(self):
        self.lyrat = self.lyrat_cls(self, self.nvs, self.bonds, self.default_mode)
        self.lyrat.boot()
        return self.lyrat

    def reboot(self):
        """Restart of the LyraT: the link drops at the AG, NVS and bonds stay."""
        old_log = self.lyrat.log if self.lyrat else []
        if self.stack.slot:
            ag = self.stack.slot["ag"]
            self.stack.slot, self.stack.sco = None, None
            if ag:
                ag.on_link_down(False, True)
        self.lyrat.task_gen = None
        self.start()
        self.lyrat.log[:0] = old_log
        return self.lyrat

    def link(self):
        """(role, state) of the live link, None"""
        s = self.stack.slot
        if not s or not s["ag"]:
            return None
        return s["ag"].kind, s["state"]
