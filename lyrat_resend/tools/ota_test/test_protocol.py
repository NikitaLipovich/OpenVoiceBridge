"""Tests of the image protocol, the frames and the log ring on the PC.

The C code is the firmware's own (otahost.dll, built from lyrat_resend/main with MSVC); the Python model is ota.py and
udplog.py from lyrat_resend/tools/bench. Each pair (C or Python sender) x (C or Python receiver) runs over a simulated
channel with loss, repeats, corruption and a cut in the middle.

Run from this folder: build_host.cmd, then
  set OTA_TEST_BUILDS=<dir with s3-base\s3_eth_bridge.bin and lyrat-base\lyrat_resend.bin>
  python -m unittest -v test_protocol
Without OTA_TEST_BUILDS the cases that need real images are skipped. Made by goal 090.
"""
import hashlib
import heapq
import os
import random
import struct
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "bench"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BENCH))

import otahost as H  # noqa: E402
import ota  # noqa: E402
import udplog  # noqa: E402

BUILDS = Path(os.environ.get("OTA_TEST_BUILDS", HERE / "images"))
S3_BIN = BUILDS / "s3-base" / "s3_eth_bridge.bin"
LYRAT_BIN = BUILDS / "lyrat-base" / "lyrat_resend.bin"
HAVE_IMAGES = S3_BIN.exists() and LYRAT_BIN.exists()
needs_images = unittest.skipUnless(HAVE_IMAGES, "set OTA_TEST_BUILDS to the folder of the builds")
ST = ota.ST


def small_image(real: Path, size=40_000, seed=1):
    """The real header and app description, then random bytes: valid for the head check, quick to send."""
    head = real.read_bytes()[:4096]
    rnd = random.Random(seed)
    return head + bytes(rnd.randrange(256) for _ in range(size - len(head)))


# --- adapters: one interface for C and Python ends, times in ms -------------------------------------------------------

class PySenderMs:
    def __init__(self, data, session, flags=0, baud=0, auth=None, window=2, rto=600, max_restarts=3, **kw):
        self.s = ota.Sender(data, session, flags, baud, auth=auth, window=window, rto=rto / 1000.0, begin_rto=2.0,
                            max_restarts=max_restarts, **kw)

    def poll(self, now):
        return self.s.poll(now / 1000.0)

    def on_reply(self, rep, now):
        self.s.on_reply(rep, now / 1000.0)

    @property
    def state(self):
        return self.s.state

    @property
    def fail(self):
        return ota.status_name(self.s.fail) if self.s.fail is not None else None


class CSenderMs:
    def __init__(self, data, session, flags=0, baud=0, auth=None, window=2, rto=600, max_restarts=3):
        self.c = H.CSender(data, window=window, rto=rto, max_restarts=max_restarts)
        self.c.start(session, hashlib.sha256(data).digest(), flags, baud, 0, auth)

    def poll(self, now):
        return self.c.poll(now)

    def on_reply(self, rep, now):
        self.c.on_reply(rep, now)

    @property
    def state(self):
        return self.c.state

    @property
    def fail(self):
        i = self.c.info()
        return i["fail_name"] if i["state_name"] == "failed" else None


class PyReceiverMs:
    def __init__(self, capacity, chip=None, project=None, token=None):
        self.r = ota.Receiver(capacity, chip, project, token)

    def handle(self, req, now):
        return self.r.handle(req, now / 1000.0)

    def tick(self, now):
        self.r.tick(now / 1000.0)

    def __getattr__(self, name):        # slc, sco, nonce, active ... live on the model
        return getattr(self.r, name)

    def __setattr__(self, name, value):
        if name == "r":
            object.__setattr__(self, name, value)
        else:
            setattr(self.r, name, value)

    def info(self):
        r = self.r
        return {"state_name": r.state, "err_name": ota.status_name(r.last_err), "done": r.count["done"], "next": r.next}


class CReceiverMs(H.CReceiver):
    pass


SENDERS = {"C": CSenderMs, "Py": PySenderMs}
RECEIVERS = {"C": CReceiverMs, "Py": PyReceiverMs}


# --- channel ---------------------------------------------------------------------------------------------------------

class Channel:
    """Delivers payloads after a latency; may drop, repeat, reorder or corrupt them. corrupt_data=k flips one byte in
    the k-th DATA request after the transport check (as if a corrupted chunk slipped through a checksum)."""

    def __init__(self, seed=0, loss=0.0, dup=0.0, reorder=0.0, latency=3, corrupt_data=None, frames_ber=0.0):
        self.rnd = random.Random(seed)
        self.loss, self.dup, self.reorder, self.latency = loss, dup, reorder, latency
        self.corrupt_data = corrupt_data
        self.frames_ber = frames_ber          # bit error rate on a real ctl frame byte stream, C parser on the far end
        self.data_seen = 0
        self.q = []
        self.n = 0
        self.parsers = {}
        self.stats = {"sent": 0, "dropped": 0, "duplicated": 0, "frames_lost": 0, "corrupted": 0}

    def _through_frames(self, payload, direction, now):
        """Wraps the payload in a ctl frame, flips bits, parses with the C parser: returns the payload or None."""
        kind = H.CTL_T_OTA if direction == "fwd" else H.CTL_T_OTA_ACK
        raw = bytearray(H.ctl_frame(kind, self.n & 0xFFFF, payload))
        for i in range(len(raw)):
            for bit in range(8):
                if self.rnd.random() < self.frames_ber:
                    raw[i] ^= 1 << bit
        p = self.parsers.setdefault(direction, H.CtlParser())
        # A gap of silence between frames: a frame cut by noise is dropped by the time-out, like on the line.
        got = p.feed(bytes(raw), now) + p.feed(b"", now + 300)
        good = [pl for (t, _s, pl) in got if t == kind]
        if not good:
            self.stats["frames_lost"] += 1
            return None
        return good[0]

    def send(self, direction, payload, now):
        self.stats["sent"] += 1
        self.n += 1
        if direction == "fwd" and payload[0] == ota.OP_DATA:
            self.data_seen += 1
            if self.corrupt_data == self.data_seen:
                b = bytearray(payload)
                b[-1] ^= 0x5A
                payload = bytes(b)
                self.stats["corrupted"] += 1
        if self.frames_ber:
            payload = self._through_frames(payload, direction, now)
            if payload is None:
                return
        if self.rnd.random() < self.loss:
            self.stats["dropped"] += 1
            return
        copies = 2 if self.rnd.random() < self.dup else 1
        self.stats["duplicated"] += copies - 1
        for _ in range(copies):
            delay = self.latency + (self.rnd.randrange(20) if self.rnd.random() < self.reorder else 0)
            heapq.heappush(self.q, (now + delay, self.n, self.rnd.random(), direction, payload))
            self.n += 1

    def due(self, now):
        out = []
        while self.q and self.q[0][0] <= now:
            _, _, _, d, p = heapq.heappop(self.q)
            out.append((d, p))
        return out


def run(sender, receiver, ch, max_ms=600_000, step=2, cut_at=None, hooks=None):
    """Runs until the sender is done or failed. cut_at: stop sending once this many bytes were acked (sender dies)."""
    now = 0
    while now < max_ms:
        if hooks:
            hooks(now, sender, receiver)
        if sender.state in ("done", "failed"):
            break
        alive = cut_at is None or getattr(receiver, "info")().get("next", 0) < cut_at
        if alive:
            while True:
                req = sender.poll(now)
                if req is None:
                    break
                ch.send("fwd", req, now)
        for d, p in ch.due(now):
            if d == "fwd":
                rep = receiver.handle(p, now)
                if rep:
                    ch.send("back", rep, now)
            elif alive:
                sender.on_reply(p, now)
        receiver.tick(now)
        if not alive and not ch.q:
            now += 1000                       # nobody talks any more: let the receiver's idle time pass
        now += step
    return now


def make_pair(s_kind, r_kind, data, chip=None, project=None, flags=0, token=None, nonce=None, **skw):
    r = RECEIVERS[r_kind](capacity=0x300000, chip=chip, project=project, token=token)
    auth = None
    session = random.randrange(1, 1 << 31)
    if token is not None:
        r.nonce = nonce or os.urandom(16)
        begin = ota.build_begin(session, len(data), hashlib.sha256(data).digest(), flags, 0)
        auth = ota.auth_make(token, r.nonce, begin)
    s = SENDERS[s_kind](data, session, flags, 0, auth, **skw)
    return s, r


PAIRS = [("C", "C"), ("Py", "C"), ("C", "Py"), ("Py", "Py")]


class Primitives(unittest.TestCase):
    def test_sha256_and_hmac_match_python(self):
        rnd = random.Random(5)
        for n in list(range(0, 130)) + [1000, 4096, 100_003]:
            data = bytes(rnd.randrange(256) for _ in range(n))
            self.assertEqual(H.sha256(data), hashlib.sha256(data).digest(), n)
        import hmac
        for klen in (0, 1, 16, 64, 65, 200):
            key = bytes(rnd.randrange(256) for _ in range(klen))
            msg = bytes(rnd.randrange(256) for _ in range(rnd.randrange(300)))
            self.assertEqual(H.hmac_sha256(key, msg), hmac.new(key, msg, hashlib.sha256).digest(), klen)

    def test_auth_same_in_c_and_python(self):
        token, nonce = b"bench-token-0123456789", os.urandom(16)
        begin = ota.build_begin(7, 1234, hashlib.sha256(b"x").digest(), 1, 0)
        self.assertEqual(H.auth_make(token, nonce, begin), ota.auth_make(token, nonce, begin))
        signed = begin[:46] + ota.auth_make(token, nonce, begin)
        self.assertEqual(H.lib.ota_auth_check(token, len(token), nonce, signed), 1)
        self.assertEqual(H.lib.ota_auth_check(b"wrong-token", 11, nonce, signed), 0)
        self.assertEqual(H.lib.ota_auth_check(token, len(token), os.urandom(16), signed), 0)

    def test_frames_same_as_s3_uart(self):
        import s3_uart
        rnd = random.Random(3)
        for kind in (1, 2, 3, 4, 5, 6, 7):
            payload = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 1025)))
            seq = rnd.randrange(65536)
            self.assertEqual(H.ctl_frame(kind, seq, payload), s3_uart.frame(kind, seq, payload))
        p = H.CtlParser()
        frame = H.ctl_frame(6, 9, b"abc")
        self.assertEqual(p.feed(b"\x00\xa5" + frame, 0), [(6, 9, b"abc")])
        bad = bytearray(frame)
        bad[9] ^= 1
        self.assertEqual(p.feed(bytes(bad), 10), [])
        self.assertEqual(p.counts()["bad_crc"], 1)

    @needs_images
    def test_image_check_on_the_real_builds(self):
        for path, chip, project, other_chip, other_project in (
                (S3_BIN, ota.CHIP_ESP32S3, "s3_eth_bridge", ota.CHIP_ESP32, "lyrat_resend"),
                (LYRAT_BIN, ota.CHIP_ESP32, "lyrat_resend", ota.CHIP_ESP32S3, "s3_eth_bridge")):
            head = path.read_bytes()[:4096]
            img = ota.Image.load(path)
            self.assertTrue(img.magic_ok)
            self.assertEqual((img.chip, img.project), (chip, project))
            self.assertEqual(H.image_check(head, chip, project), ST["ok"])
            self.assertEqual(H.image_check(head, other_chip, project), ST["bad_image"])
            self.assertEqual(H.image_check(head, chip, other_project), ST["bad_image"])
            self.assertEqual(H.image_check(head[:207], chip, project), ST["bad_image"])
            self.assertEqual(H.image_elf(head), img.elf)
            self.assertIsNone(img.problem("s3" if chip == ota.CHIP_ESP32S3 else "lyrat"))
            self.assertIsNotNone(img.problem("lyrat" if chip == ota.CHIP_ESP32S3 else "s3"))
        self.assertEqual(H.image_check(b"\x00" * 300, 0, None), ST["bad_image"])


@needs_images
class Transfer(unittest.TestCase):
    """Every case on all four pairs of ends."""

    def setUp(self):
        self.data = small_image(S3_BIN)

    def each(self, fn):
        for s_kind, r_kind in PAIRS:
            with self.subTest(sender=s_kind, receiver=r_kind):
                fn(s_kind, r_kind)

    def test_clean(self):
        def case(sk, rk):
            s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32S3, "s3_eth_bridge")
            run(s, r, Channel(1))
            self.assertEqual(s.state, "done")
            self.assertEqual(r.active, self.data)
        self.each(case)

    def test_loss_repeat_reorder(self):
        def case(sk, rk):
            for seed in range(4):
                s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32S3, "s3_eth_bridge")
                ch = Channel(seed, loss=0.15, dup=0.15, reorder=0.2)
                run(s, r, ch)
                self.assertEqual(s.state, "done", (seed, s.fail, ch.stats))
                self.assertEqual(r.active, self.data)
                self.assertGreater(ch.stats["dropped"], 0)
        self.each(case)

    def test_bit_errors_on_the_uart_frames(self):
        def case(sk, rk):
            s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32S3, "s3_eth_bridge")
            ch = Channel(11, frames_ber=2e-5)
            run(s, r, ch)
            self.assertEqual(s.state, "done", (s.fail, ch.stats))
            self.assertEqual(r.active, self.data)
            self.assertGreater(ch.stats["frames_lost"], 0)
        self.each(case)

    def test_corruption_past_the_checksum_is_refused(self):
        def case(sk, rk):
            s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32S3, "s3_eth_bridge")
            ch = Channel(2, corrupt_data=7)
            run(s, r, ch)
            self.assertEqual((s.state, s.fail), ("failed", "sha"))
            self.assertIsNone(r.active)
            self.assertEqual(r.info()["state_name"], "idle")
        self.each(case)

    def test_wrong_sha_announced_is_refused(self):
        def case(sk, rk):
            r = RECEIVERS[rk](capacity=0x300000, chip=ota.CHIP_ESP32S3, project="s3_eth_bridge")
            s = SENDERS[sk](self.data, 77)
            if sk == "C":            # announce the SHA-256 of other bytes
                s.c.start(77, hashlib.sha256(b"other").digest(), 0, 0, 0)
            else:
                s.s.sha = hashlib.sha256(b"other").digest()
            run(s, r, Channel(3))
            self.assertEqual((s.state, s.fail), ("failed", "sha"))
            self.assertIsNone(r.active)
        self.each(case)

    def test_cut_in_the_middle_then_new_session(self):
        def case(sk, rk):
            s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32S3, "s3_eth_bridge")
            run(s, r, Channel(4), max_ms=200_000, cut_at=len(self.data) // 2)
            i = r.info()
            self.assertEqual((i["state_name"], i["err_name"]), ("idle", "timeout"))
            self.assertLess(i["next"], len(self.data))
            self.assertIsNone(r.active)
            s2, _ = make_pair(sk, rk, self.data)
            run(s2, r, Channel(5))
            self.assertEqual(s2.state, "done")
            self.assertEqual(r.active, self.data)
        self.each(case)

    def test_receiver_restart_midway_opens_a_new_session(self):
        def case(sk, rk):
            if rk != "C":
                return
            s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32S3, "s3_eth_bridge")
            restarted = []

            def hook(now, snd, rcv):
                if not restarted and rcv.info()["next"] > len(self.data) // 3:
                    rcv.reset()
                    restarted.append(now)
            run(s, r, Channel(6), hooks=hook)
            self.assertTrue(restarted)
            self.assertEqual(s.state, "done", s.fail)
            self.assertEqual(r.active, self.data)
        self.each(case)

    def test_wrong_token_and_replayed_begin(self):
        token = b"bench-token-0123456789"

        def case(sk, rk):
            r = RECEIVERS[rk](capacity=0x300000, chip=ota.CHIP_ESP32S3, project="s3_eth_bridge", token=token)
            r.nonce = os.urandom(16)
            begin = ota.build_begin(5, len(self.data), hashlib.sha256(self.data).digest(), 0, 0)
            bad = ota.auth_make(b"not-the-token", r.nonce, begin)
            s = SENDERS[sk](self.data, 5, auth=bad)
            run(s, r, Channel(7))
            self.assertEqual((s.state, s.fail), ("failed", "auth"))
            self.assertIsNone(r.active)
            # A good BEGIN, recorded and sent again with the old nonce after the board issued a new one.
            nonce = os.urandom(16)
            r.nonce = nonce
            good = begin[:46] + ota.auth_make(token, nonce, begin)
            rep = ota.parse_reply(r.handle(good, 0))
            self.assertEqual(rep["status"], ST["ok"])
            r.handle(ota.build_short(ota.OP_ABORT, 5), 1)
            r.nonce = os.urandom(16)
            replay = begin[:46] + ota.auth_make(token, nonce, begin)
            self.assertEqual(ota.parse_reply(r.handle(replay, 2))["status"], ST["auth"])
        self.each(case)

    def test_token_good(self):
        token = b"bench-token-0123456789"

        def case(sk, rk):
            s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32S3, "s3_eth_bridge", token=token)
            run(s, r, Channel(8, loss=0.1))
            self.assertEqual(s.state, "done", s.fail)
            self.assertEqual(r.active, self.data)
        self.each(case)

    def test_wrong_board_and_too_big(self):
        def case(sk, rk):
            ly = small_image(LYRAT_BIN)
            s, r = make_pair(sk, rk, ly, ota.CHIP_ESP32S3, "s3_eth_bridge")
            run(s, r, Channel(9))
            self.assertEqual((s.state, s.fail), ("failed", "bad_image"))
            self.assertIsNone(r.active)
            s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32S3, "s3_eth_bridge")
            r.capacity = len(self.data) - 1
            run(s, r, Channel(9))
            self.assertEqual((s.state, s.fail), ("failed", "too_big"))
            r.capacity = 0
            s, _ = make_pair(sk, rk, self.data)
            run(s, r, Channel(9))
            self.assertEqual((s.state, s.fail), ("failed", "no_slot"))
        self.each(case)

    def test_call_rule(self):
        def case(sk, rk):
            s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32, None)
            r.chip = None
            r.slc = True
            run(s, r, Channel(10))
            self.assertEqual((s.state, s.fail), ("failed", "refused"))
            # FORCE starts with SLC up; a call during the transfer pauses it, the end of the call resumes it.
            s, _ = make_pair(sk, rk, self.data, flags=ota.F_FORCE)
            call = {}

            def hook(now, snd, rcv):
                # The call starts once a quarter of the image is in, and lasts 4 s.
                if "start" not in call and rcv.info()["next"] > len(self.data) // 4:
                    call["start"] = now
                rcv.sco = "start" in call and now - call["start"] < 4000
            run(s, r, Channel(10), hooks=hook)
            self.assertEqual(s.state, "done", s.fail)
            self.assertEqual(r.active, self.data)
            busy = r.info()["busy"] if rk == "C" else r.r.count["busy"]
            self.assertGreater(busy, 0)            # the call really paused the writes
            self.assertIn("start", call)
        self.each(case)

    def test_dry_run_activates_nothing(self):
        def case(sk, rk):
            s, r = make_pair(sk, rk, self.data, ota.CHIP_ESP32S3, "s3_eth_bridge", flags=ota.F_DRYRUN)
            run(s, r, Channel(12))
            self.assertEqual(s.state, "done")
            self.assertIsNone(r.active)
            self.assertEqual(r.info()["state_name"], "idle")
        self.each(case)

    def test_end_reply_lost_is_answered_again(self):
        for rk in ("C", "Py"):
            r = RECEIVERS[rk](capacity=0x300000)
            data = self.data[:5000]
            sha = hashlib.sha256(data).digest()
            self.assertEqual(ota.parse_reply(r.handle(ota.build_begin(3, len(data), sha), 0))["status"], 0)
            for off in range(0, len(data), 1008):
                r.handle(ota.build_data(3, off, data[off:off + 1008]), 1)
            first = ota.parse_reply(r.handle(ota.build_short(ota.OP_END, 3), 2))
            again = ota.parse_reply(r.handle(ota.build_short(ota.OP_END, 3), 3))
            self.assertEqual((first["status"], again["status"]), (0, 0), rk)
            other = ota.parse_reply(r.handle(ota.build_begin(4, len(data), sha), 4))
            self.assertEqual(other["status"], ST["refused"], rk)     # a reboot is pending

    def test_c_receiver_events_for_the_uart_baud(self):
        r = H.CReceiver(capacity=0x300000)
        data = self.data[:3000]
        r.handle(ota.build_begin(9, len(data), hashlib.sha256(data).digest(), 0, 460800), 0)
        self.assertEqual(r.events(), H.EV_BEGUN)
        self.assertEqual(r.info()["baud"], 460800)
        r.handle(ota.build_short(ota.OP_ABORT, 9), 1)
        self.assertEqual(r.events(), H.EV_CLOSED)

    def test_full_size_lyrat_image_over_uart_frames(self):
        """The real LyraT build, C sender to C receiver, through ctl frames with bit errors, window 2."""
        data = LYRAT_BIN.read_bytes()
        s, r = make_pair("C", "C", data, ota.CHIP_ESP32, "lyrat_resend")
        ch = Channel(13, frames_ber=1e-6, latency=45)
        end = run(s, r, ch, max_ms=3_600_000, step=5)
        self.assertEqual(s.state, "done", (s.fail, ch.stats))
        self.assertEqual(r.active, data)
        print("\n  lyrat %d bytes: %d requests, %d frames lost, sim %.1f s" % (len(data), ch.stats["sent"],
                                                                            ch.stats["frames_lost"], end / 1000))


class LogRing(unittest.TestCase):
    def test_overflow_counts_and_numbers(self):
        ring = H.Ring(8, 1, 0xBEEF)
        for i in range(20):
            ring.log(i, "\x1b[0;32mI (%d) tag: line %d\x1b[0m\n" % (i, i))
        info = ring.info()
        self.assertEqual((info["count"], info["lost"], info["seq"]), (8, 12, 20))
        data, n = ring.pack()
        recs = udplog.parse_records(data)
        self.assertEqual(n, 8)
        self.assertEqual([r["seq"] for r in recs], list(range(8)))      # the oldest stay: the start of the log
        self.assertEqual(recs[0]["text"], "I (0) tag: line 0")
        self.assertEqual(recs[0]["level"], 3)
        self.assertEqual(recs[0]["boot"], 0xBEEF)
        self.assertEqual(recs[0]["board"], 1)
        ring.log(30, "W (30) tag: after")
        recs2 = udplog.parse_records(ring.pack()[0])
        self.assertEqual((recs2[0]["seq"], recs2[0]["lost"], recs2[0]["level"]), (20, 12, 2))
        t = udplog.Tracker()
        for r in recs + recs2:
            t.add(r)
        b = t.summary()["lyrat"]
        self.assertEqual((b["lines"], b["lost_board"], b["lost_way"]), (9, 12, 0))

    def test_way_loss_told_apart_from_board_loss(self):
        ring = H.Ring(64, 0, 1)
        for i in range(30):
            ring.log(i, "I (%d) x: %d" % (i, i))
        recs = udplog.parse_records(ring.pack()[0])
        t = udplog.Tracker()
        for r in recs:
            if r["seq"] not in (5, 6, 17):          # three lost on the way
                t.add(r)
        s = t.summary()["s3"]
        self.assertEqual((s["lost_way"], s["lost_board"], s["lines"]), (3, 0, 27))
        r = dict(recs[-1], boot=2, seq=0, lost=0)
        t.add(r)
        self.assertEqual(t.summary()["s3"]["restarts"], 1)

    def test_long_line_cut_and_levels(self):
        ring = H.Ring(4, 0, 1)
        ring.log(0, "E (1) big: " + "x" * 400)
        rec = udplog.parse_records(ring.pack()[0])[0]
        self.assertEqual(len(rec["text"]), 159)
        self.assertTrue(rec["text"].endswith("~"))
        self.assertEqual(rec["level"], 1)
        for text, lvl in (("W (5) a: b", 2), ("\x1b[0;31mE (5) a: b", 1), ("stat up=3 eth=ok", 0), ("D (1) x", 4),
                          ("V (1) x", 5), ("I", 0), ("", 0)):
            self.assertEqual(H.line_level(text), lvl, text)

    def test_relay_ring_keeps_the_lyrat_records(self):
        lyrat = H.Ring(16, 1, 0x1234)
        for i in range(5):
            lyrat.log(100 + i, "I (%d) BT: %d" % (i, i))
        frame_payload, n = lyrat.pack(600)
        relay = H.Ring(3, 1, 0)
        used_total, pushed, full = 0, 0, 0
        while used_total < len(frame_payload):
            rc, used = relay.push_record(frame_payload[used_total:])
            self.assertGreater(used, 0)
            used_total += used
            pushed += rc == 1
            full += rc == -1
        self.assertEqual((pushed, full), (3, 2))
        out = udplog.parse_records(relay.pack()[0])
        self.assertEqual([(r["board"], r["boot"], r["seq"], r["t_ms"]) for r in out],
                         [(1, 0x1234, i, 100 + i) for i in range(3)])
        self.assertEqual(relay.info()["lost"], 2)

    def test_datagram(self):
        ring = H.Ring(8, 0, 7)
        ring.log(1, "I (1) a: b")
        body, n = ring.pack()
        dgram = b"UL" + bytes([1, n]) + body
        recs = udplog.parse_datagram(dgram)
        self.assertEqual(recs[0]["text"], "I (1) a: b")
        self.assertIsNone(udplog.parse_datagram(b"XX\x01\x01" + body))
        flt = udplog.make_filter("lyrat", None, None)
        self.assertFalse(flt(recs[0]))
        self.assertTrue(udplog.make_filter("s3", "W", None)(dict(recs[0], level=2)))
        self.assertFalse(udplog.make_filter("s3", "W", None)(recs[0]))
        self.assertTrue(udplog.make_filter(None, None, "a: b")(recs[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
