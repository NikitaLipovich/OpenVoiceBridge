"""benchd, the bench service (goal 103), against the S3 simulator on 127.0.0.1. No board, no phone, no sound.

What is proven here, and only on the PC:
  Events       bench_events: the firmware's lines (start and its reason, SLC up/down, ACL, SCO and codec, OTA of both
               boards, rollback, panic, supply dips, the S3's Wi-Fi) give their events; time words of the API
  StoreUnit    rotation by size, by time and by day; the disk limit deletes the oldest logs, never the open one; queries
               by time, board and pattern; the lock survives a restart of the service (state.json)
  Service      benchd against the simulator: the log of both boards in files (MAC prefixes hidden), status, events of a
               start (reboot 1: reason SW, SLC back), SCO open/close, commands with their event; the log relay: two
               udplog.py at once and a NetWatch get the lines while benchd keeps its own; s3_uart.py finds the S3
               through benchd; the lock: refusal with the holder, waiting in the queue, expiry, a dead pid, "lock run",
               423 for another owner's command and "force"; bench_busy follows the lock; an update as a job (fw.py
               flash --image); the admin page (no external URL), POST needs X-Bench; a lost S3 and its return
  Lan          --lan: the page and GET from the LAN address, POST from it refused without --lan-control
  Without      the tools without benchd as before (udplog.py direct, via "direct")
  Ports        two simulators on different BENCH_PORT_OFFSET at once, each tool reaches its own
Windows of 2 to 10 s (R26). BENCH_NO_ADB=1: the phone is never looked at.
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "bench"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BENCH))
os.environ["BENCH_NO_ADB"] = "1"
os.environ.pop("BENCH_USB", None)
# Away from a benchd that may run for the real bench on this PC, and from parallel test runs (their own offset).
BASE_OFFSET = int(os.environ.get("BENCH_PORT_OFFSET", "0") or 0)
OFFSET = BASE_OFFSET + 600
os.environ["BENCH_PORT_OFFSET"] = str(OFFSET)
import simproc  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t103_"))
TOKEN = re.search(r'"(.+)"', simproc.TOKEN.read_text(encoding="utf-8")).group(1)


def tool_env(**extra):
    """Environment of a bench tool beside the test's benchd: no BENCH_S3_ADDR, benchd found by its run file."""
    e = {k: v for k, v in os.environ.items() if k not in ("BENCH_S3_ADDR", "BENCH_NO_BENCHD", "BENCHD_URL")}
    e.update(PYTHONIOENCODING="utf-8", BENCH_NO_ADB="1", BENCH_PORT_OFFSET=str(OFFSET), BENCHD_HOME=str(TMP / "home"))
    e.update({k: str(v) for k, v in extra.items()})
    return e


def run(script, *args, env=None, timeout=60):
    r = subprocess.run([sys.executable, str(BENCH / script), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env or tool_env())
    return r.returncode, r.stdout, r.stderr


class Benchd:
    """benchd.py as a process for the tests."""

    def __init__(self, home, *extra, env=None):
        e = dict(env or tool_env())
        e.update(BENCH_S3_ADDR="127.0.0.1", BENCHD_HOME=str(home), BENCH_OTA_TOKEN=TOKEN)
        self.p = subprocess.Popen([sys.executable, str(BENCH / "benchd.py"), "--home", str(home), "--poll-s", "1",
                                   "--lost-s", "3", *extra], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env=e)
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("benchd did not start: %s" % self.p.stderr.read()[-2000:])
        self.run = json.loads(line)
        self.url = self.run["url"]

    def api(self, method, path, body=None, headers=None, url=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request((url or self.url) + path, data=data, method=method)
        h = {"X-Bench": "1", "Content-Type": "application/json"} if headers is None else headers
        for k, v in h.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait(self, cond, timeout=10.0, step=0.2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = cond()
            if got:
                return got
            time.sleep(step)
        return None

    def events(self, kind=None, since=None):
        q = "/events?limit=5000" + ("&type=" + kind if kind else "")
        evs = self.api("GET", q)[1]["events"]
        return [e for e in evs if not since or e["t"] >= since]

    def close(self):
        try:
            self.api("POST", "/shutdown", {}, timeout=3)
            self.p.wait(timeout=8)
        except Exception:
            self.p.kill()
            self.p.wait()
        self.p.stdout.close()
        self.p.stderr.close()


def now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


class Events(unittest.TestCase):
    def feed(self, idx, board, text, boot=1, seq=5):
        return idx.feed({"board": {"s3": 0, "lyrat": 1}[board], "boot": boot, "seq": seq, "text": text})

    def test_lines_of_the_firmware(self):
        from bench_events import EventIndex
        idx = EventIndex()
        cases = [
            ("lyrat", "I (1) LyraT: BOOT reset_reason=BROWNOUT code=9", 0, {"type": "boot", "reason": "BROWNOUT", "code": 9}),
            ("lyrat", "I (9) BT: *** SLC CONNECTED ***", 1, {"type": "slc_up"}),
            ("lyrat", "I (9) BT: SCO audio state=3 codec=mSBC", 2, {"type": "sco_open", "codec": "mSBC"}),
            ("lyrat", "I (9) BT: SCO audio state=0 codec=mSBC", 3, {"type": "sco_close"}),
            ("lyrat", "W (9) BT: SLC DOWN after 125 s, last_acl_reason=0x8", 4, {"type": "slc_down", "after_s": 125,
                                                                              "acl_reason": "0x8"}),
            ("lyrat", "I (9) BT: HFP conn state=0", 5, {"type": "hfp_down"}),
            ("lyrat", "W (9) BT: ACL DOWN reason=0x13 handle=0x80", 6, {"type": "acl_down", "reason": "0x13"}),
            ("lyrat", "I (9) lota: erasing 1274736 bytes of ota_0", 7, {"type": "ota_start", "bytes": 1274736}),
            ("lyrat", "W (9) lota: new image in ota_0, restart now", 8, {"type": "ota_done", "slot": "ota_0"}),
            ("s3", "I (9) s3ota: receiving 912000 bytes into ota_1", 9, {"type": "ota_start", "slot": "ota_1"}),
            ("s3", "W (9) s3ota: new image in ota_1, restarting", 10, {"type": "ota_done", "slot": "ota_1"}),
            ("s3", "E (9) s3ota: lyrat update failed: no answer", 11, {"type": "ota_fail", "why": "no answer"}),
            ("s3", "W (9) s3ota: lyrat rolled back to 1a2b3c", 12, {"type": "ota_rollback", "elf": "1a2b3c"}),
            ("s3", "E (9) s3ota: self-test failed: eth=0 ip=0", 13, {"type": "ota_fail"}),
            ("s3", "W (9) s3eth: wifi lost (reason 201), reconnecting", 14, {"type": "net_down", "reason": 201}),
            ("s3", "I (9) s3eth: wifi ip 192.168.1.50", 15, {"type": "net_up"}),
            ("lyrat", "Guru Meditation Error: Core 0 panic'ed", 16, {"type": "panic"}),
        ]
        for board, text, seq, want in cases:
            with self.subTest(text):
                got = self.feed(idx, board, text, seq=seq)
                self.assertTrue(got, text)
                self.assertTrue(any(all(e.get(k) == v for k, v in want.items()) for e in got), got)
        self.assertEqual(self.feed(idx, "lyrat", "I (9) LyraT: line 5 of nothing", seq=20), [])

    def test_start_dips_and_sco_closed_by_a_restart(self):
        from bench_events import EventIndex
        idx = EventIndex()
        self.assertEqual(self.feed(idx, "lyrat", "I (5) x: nothing", boot=7, seq=3), [])     # not a start line: no event
        self.feed(idx, "lyrat", "I (9) BT: SCO audio state=2 codec=CVSD", boot=7, seq=4)
        self.assertEqual(self.feed(idx, "lyrat", "CMD:   bod on=1 level=7 trips=0 last_ms=-1", boot=7, seq=5), [])
        got = self.feed(idx, "lyrat", "CMD:   bod on=1 level=7 trips=3 last_ms=900", boot=7, seq=6)
        self.assertEqual((got[0]["type"], got[0]["new"]), ("bod", 3))
        got = self.feed(idx, "lyrat", "I (1) LyraT: BOOT reset_reason=SW code=3", boot=8, seq=0)
        self.assertEqual([e["type"] for e in got], ["boot", "sco_close"])
        self.assertEqual(got[0]["reason"], "SW")
        got = self.feed(idx, "s3", "I (1) s3eth: something", boot=100, seq=40)
        self.assertEqual(got, [])                                  # the first record of a board is not a start
        got = self.feed(idx, "s3", "I (1) s3eth: start", boot=101, seq=0)
        self.assertEqual((got[0]["type"], got[0]["reason"]), ("boot", "UNKNOWN"))
        # A reply line without its record (a command through benchd) never makes a start; its dips still count.
        got = idx.feed({"board": 1, "text": "CMD:   bod on=1 level=7 trips=3 last_ms=900"})
        self.assertEqual([e["type"] for e in got], ["bod"])

    def test_events_of_a_file(self):
        from bench_events import events_of_file
        p = TMP / "ev.txt"
        p.write_text("2026-11-18T10:00:00.000 lyrat     1.000 I (1) BT: *** SLC CONNECTED ***\n"
                     "2026-11-18T10:00:01.000 lyrat     2.000 W (2) BT: ACL DOWN reason=0x8 handle=0x1\n", encoding="utf-8")
        self.assertEqual([(e["type"], e["t"][11:19]) for e in events_of_file(p)], [("slc_up", "10:00:00"),
                                                                                 ("acl_down", "10:00:01")])

    def test_time_words(self):
        import benchd
        now = datetime(2026, 11, 18, 12, 0, 0)
        self.assertEqual(benchd.parse_time("-10m", now), "2026-11-18T11:50:00.000")
        self.assertEqual(benchd.parse_time("-2h", now), "2026-11-18T10:00:00.000")
        self.assertEqual(benchd.parse_time("09:30", now), "2026-11-18T09:30:00.000")
        self.assertEqual(benchd.parse_time("2026-11-17T08:00:01", now), "2026-11-17T08:00:01.000")
        self.assertIsNone(benchd.parse_time("", now))
        with self.assertRaises(ValueError):
            benchd.parse_time("yesterday", now)


class StoreUnit(unittest.TestCase):
    def test_rotation_by_size_and_disk_limit(self):
        import benchd
        home = TMP / "store_size"
        st = benchd.Store(home, rotate_mb=0.01, rotate_min=60, disk_mb=0.035)
        t0 = datetime(2026, 11, 18, 10, 0, 0)
        for i in range(800):
            st.write("lyrat" if i % 2 else "s3", i * 10, "W (%d) LyraT: line %d peer 11:22:33:44:55:66" % (i, i),
                     t0 + timedelta(milliseconds=i * 50))
        st.flush()
        files = sorted(home.glob("*/log-*.txt"))
        self.assertGreater(st.counts["files"], 4)                   # rotated by size
        self.assertGreater(st.counts["deleted"], 0)                 # the oldest went
        self.assertIn(st.path, files)                               # the open one stays
        self.assertLessEqual(sum(p.stat().st_size for p in files), 0.035 * 1024 * 1024 + 11000)
        text = "".join(p.read_text(encoding="utf-8") for p in files)
        self.assertNotIn("11:22:33:44", text)
        self.assertIn("xx:xx:xx:xx:55:66", text)
        got = st.logs(None, None, "lyrat", r"line 7\d\d", limit=1000)
        self.assertTrue(got["lines"] and all(" lyrat " in ln for ln in got["lines"]))
        self.assertEqual(st.tail(after=795)["lines"][-1].split("line ")[1][:3], "799")
        st.close()

    def test_rotation_by_time_and_day_and_query_window(self):
        import benchd
        home = TMP / "store_time"
        st = benchd.Store(home, rotate_mb=20, rotate_min=60, disk_mb=100)
        day1 = datetime(2026, 11, 17, 23, 59, 58)
        st.write("s3", 1, "I (1) a", day1)
        st.opened -= 3601                                            # an hour went by
        st.write("s3", 2, "I (2) b", day1 + timedelta(seconds=1))
        st.write("s3", 3, "I (3) c", day1 + timedelta(seconds=3))   # the next day
        st.close()
        self.assertEqual(st.counts["files"], 3)
        self.assertEqual(sorted(p.name for p in home.iterdir() if p.is_dir()), ["2026-11-17", "2026-11-18"])
        got = st.logs("2026-11-17T23:59:59.000", "2026-11-18T23:00:00.000")
        self.assertEqual([ln.split()[-1] for ln in got["lines"]], ["b", "c"])
        ev = st.event({"type": "slc_up", "t": "2026-11-18T00:00:02.000"})
        self.assertEqual(st.query_events("2026-11-18T00:00:00.000", None, "slc_up")["events"], [ev])

    def test_lock_survives_a_restart_and_expires(self):
        import benchd
        home = TMP / "lockstate"
        st = benchd.Store(home)
        lk = benchd.BenchLock(st, home / "state.json")
        code, ans = lk.acquire("g7", ttl_s=60)
        self.assertEqual(code, 200)
        lk2 = benchd.BenchLock(st, home / "state.json")             # benchd started again
        self.assertEqual(lk2.view()["holder"]["owner"], "g7")
        self.assertIsNone(lk2.allows("g7"))
        self.assertEqual(lk2.allows("g8"), "g7")
        lk2.holder["until_epoch"] = time.time() - 1                 # its time is up
        self.assertIsNone(lk2.view()["holder"])
        self.assertEqual(st.query_events(kind="lock")["events"][-1]["action"], "expired")
        st.close()


class Service(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = simproc.Sim()
        cls.home = TMP / "home"
        cls.d = Benchd(cls.home)
        ok = cls.d.wait(lambda: cls.d.api("GET", "/status")[1]["benchd"]["counts"]["lines"] > 20, 10)
        if not ok:
            raise RuntimeError("benchd wrote no lines")

    @classmethod
    def tearDownClass(cls):
        cls.d.close()
        cls.sim.close()

    def setUp(self):
        self.d.api("POST", "/lock/release", {"owner": "test", "force": True})

    def test_a_log_files_status_and_mac_hidden(self):
        st = self.d.wait(lambda: (lambda s: s if s["s3"]["net"] else None)(self.d.api("GET", "/status")[1]), 12)
        self.assertTrue(st, "no stat line of the S3")
        self.assertEqual((st["s3"]["addr"], st["s3"]["found"], st["s3"]["lost"], st["lyrat"]["lost"]),
                         ("127.0.0.1", True, False, False))
        self.assertEqual(st["s3"]["net"], "wifi")
        self.assertIsNotNone(st["s3"]["boot"])
        self.assertEqual(st["relay_port"], 5081 + OFFSET)
        files = sorted(self.home.glob("*/log-*.txt"))
        text = "".join(p.read_text(encoding="utf-8") for p in files)
        self.assertRegex(text, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} lyrat +\d+\.\d{3} ")
        self.assertRegex(text, r" s3 +\d+\.\d{3} stat up=")
        self.assertNotIn("11:22:33:44", text)
        self.assertIn("LyraT: boot line 0", text)           # the start lines waited in the ring for the first listener
        got = self.d.api("GET", "/logs?from=-5m&board=lyrat&grep=line%20%5Cd%2B&limit=10")[1]
        self.assertEqual(len(got["lines"]), 10)
        self.assertTrue(got["truncated"])
        self.assertTrue(all(" lyrat " in ln for ln in got["lines"]))
        self.assertEqual(self.d.api("GET", "/logs?from=nonsense")[0], 400)

    def test_b_commands_sco_and_a_start(self):
        t = now_iso()
        code, r = self.d.api("POST", "/cmd", {"target": "lyrat", "cmd": "simsco 1", "owner": "t103"})
        self.assertEqual((code, r["status"], r["text"]), (200, 0, "simsco = 1"))
        self.assertTrue(self.d.wait(lambda: self.d.events("sco_open", t)))
        self.d.api("POST", "/cmd", {"target": "lyrat", "cmd": "simsco 0", "owner": "t103"})
        self.assertTrue(self.d.wait(lambda: self.d.events("sco_close", t)))
        cmds = self.d.events("cmd", t)
        self.assertEqual([(c["cmd"], c["owner"], c["status"]) for c in cmds], [("simsco 1", "t103", 0),
                                                                               ("simsco 0", "t103", 0)])
        self.d.api("POST", "/action", {"name": "reboot_lyrat", "owner": "t103"})
        boot = self.d.wait(lambda: self.d.events("boot", t), 10)
        self.assertTrue(boot)
        self.assertEqual((boot[0]["board"], boot[0]["reason"]), ("lyrat", "SW"))
        self.assertTrue(self.d.wait(lambda: [e for e in self.d.events("slc_up", t) if e["t"] > boot[0]["t"]], 10))
        code, r = self.d.api("POST", "/cmd", {"target": "s3", "cmd": "query"})
        self.assertEqual((code, r["reply"]["fw"]), (200, "s3tap"))
        self.assertEqual(self.d.api("POST", "/cmd", {"target": "s3", "cmd": "format flash"})[0], 400)
        self.assertEqual(self.d.api("POST", "/action", {"name": "bt_mode", "mode": "pc"})[0], 400)
        self.assertEqual(self.d.api("POST", "/action", {"name": "nonsense"})[0], 400)
        st = self.d.api("GET", "/status")[1]
        self.assertGreaterEqual(st["events_last_hour"].get("boot", 0), 1)

    def test_c_relay_two_udplogs_at_once_and_benchd_keeps_its_log(self):
        before = self.d.api("GET", "/status")[1]
        outs = [TMP / ("relay%d" % i) for i in range(2)]
        procs = [subprocess.Popen([sys.executable, str(BENCH / "udplog.py"), "--seconds", "4", "--out", str(o)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=tool_env())
                 for o in outs]
        results = []
        for p in procs:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, out + err)
            results.append(json.loads(out.strip().splitlines()[-1]))
        for res, o in zip(results, outs):
            self.assertEqual(res["via"], "benchd")
            self.assertGreater(res["boards"]["lyrat"]["lines"], 20)
            self.assertEqual(res["boards"]["lyrat"]["lost_way"], 0)
            self.assertIn(" lyrat ", (o / "udplog.log").read_text(encoding="utf-8"))
        after = self.d.api("GET", "/status")[1]
        self.assertGreater(after["benchd"]["counts"]["lines"], before["benchd"]["counts"]["lines"] + 40)
        self.assertEqual(after["log_counts"]["lyrat"]["lost_way"], before["log_counts"]["lyrat"]["lost_way"])

    def test_d_netwatch_and_s3_uart_through_benchd(self):
        saved = dict(os.environ)
        try:
            e = tool_env()
            os.environ.clear()
            os.environ.update(e)
            import benchd_client
            import console
            benchd_client._cache["at"] = None
            self.assertEqual(benchd_client.s3_addr(), "127.0.0.1")
            self.assertEqual(benchd_client.log_endpoint(), ("127.0.0.1", 5081 + OFFSET))
            w = console.NetWatch(TMP / "nw", board="lyrat")
            try:
                w.pump(time.monotonic() + 5, until=lambda: w.sp is not None and w.lines > 5)
                self.assertGreater(w.lines, 5)
                self.assertEqual((w.hub.host, w.hub.port), ("127.0.0.1", 5081 + OFFSET))
                since = w.now()
                w.send("status")
                w.pump(time.monotonic() + 3, until=lambda: any(t >= since and "slc=" in ln for t, ln in w.recent))
                self.assertTrue(any(t >= since and "slc=" in ln for t, ln in w.recent))
            finally:
                w.disconnect()
            # A test on another simulator beside this benchd stays on its simulator.
            os.environ["BENCH_S3_ADDR"] = "127.0.0.2"
            self.assertIsNone(benchd_client.log_endpoint())
            os.environ.pop("BENCH_S3_ADDR")
            os.environ["BENCH_NO_BENCHD"] = "1"
            self.assertIsNone(benchd_client.base_url())
        finally:
            os.environ.clear()
            os.environ.update(saved)
        rc, out, err = run("s3_uart.py", "query")            # no BENCH_S3_ADDR: the address comes from benchd
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["fw"], "s3tap")

    def test_e_lock_refusal_queue_expiry_and_dead_pid(self):
        code, a = self.d.api("POST", "/lock", {"owner": "g1", "ttl_s": 60, "note": "window"})
        self.assertEqual(code, 200)
        code, b = self.d.api("POST", "/lock", {"owner": "g2"})
        self.assertEqual((code, b["holder"]), (409, "g1"))
        self.assertEqual(self.d.api("POST", "/lock", {"owner": "g1"})[1]["token"], a["token"])     # same owner: renewed
        got = {}

        def waiter():
            got["r"] = self.d.api("POST", "/lock", {"owner": "g2", "wait_s": 8})
        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(1.0)
        self.assertEqual([q["owner"] for q in self.d.api("GET", "/lock")[1]["queue"]], ["g2"])
        self.assertEqual(self.d.api("POST", "/lock/release", {"owner": "g3"})[0], 409)          # not the holder
        self.assertEqual(self.d.api("POST", "/lock/release", {"owner": "g1", "token": a["token"]})[0], 200)
        th.join(12)
        self.assertEqual(got["r"][0], 200)
        self.assertEqual(self.d.api("GET", "/lock")[1]["holder"]["owner"], "g2")
        # Expiry: a lock not renewed goes after its time.
        self.d.api("POST", "/lock/release", {"owner": "g2"})
        self.assertEqual(self.d.api("POST", "/lock", {"owner": "g4", "ttl_s": 5})[0], 200)
        self.assertTrue(self.d.wait(lambda: not self.d.api("GET", "/lock")[1]["holder"], 9, 0.5))
        # A dead holder: its pid goes, the lock goes with it.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertEqual(self.d.api("POST", "/lock", {"owner": "g5", "ttl_s": 600, "pid": child.pid})[0], 200)
            time.sleep(1.5)
            self.assertEqual(self.d.api("GET", "/lock")[1]["holder"]["owner"], "g5")
        finally:
            child.kill()
            child.wait()
        self.assertTrue(self.d.wait(lambda: not self.d.api("GET", "/lock")[1]["holder"], 5))
        whys = [e.get("why") for e in self.d.events("lock") if e["action"] == "expired"]
        self.assertIn("ttl", whys)
        self.assertTrue(any(w and w.startswith("pid") for w in whys))

    def test_f_commands_under_another_owners_lock(self):
        _, a = self.d.api("POST", "/lock", {"owner": "g1", "ttl_s": 60})
        code, r = self.d.api("POST", "/cmd", {"target": "lyrat", "cmd": "tone 0", "owner": "g2"})
        self.assertEqual((code, r["holder"]), (423, "g1"))
        self.assertEqual(self.d.api("POST", "/cmd", {"target": "lyrat", "cmd": "tone 0", "owner": "g1"})[0], 200)
        self.assertEqual(self.d.api("POST", "/cmd", {"target": "lyrat", "cmd": "tone 0", "token": a["token"]})[0], 200)
        t = now_iso()
        self.assertEqual(self.d.api("POST", "/cmd", {"target": "lyrat", "cmd": "tone 0", "owner": "owner",
                                                     "force": True})[0], 200)
        self.assertEqual(self.d.events("lock", t)[0]["action"], "overridden")
        rc, out, err = run("bench.py", "cmd", "tone 0", env=tool_env(BENCH_LOCK_OWNER="g2"))
        self.assertEqual(rc, 1)
        self.assertIn("g1", out)
        self.d.api("POST", "/lock/release", {"owner": "g1"})

    def test_g_lock_run_and_bench_busy(self):
        script = ("import os, sys, json; sys.path.insert(0, %r); import owner_common as oc, benchd_client as bc; "
                  "print(json.dumps({'owner': os.environ.get('BENCH_LOCK_OWNER'), 'busy': oc.bench_busy(None, "
                  "need_lyrat=False, need_udp=False), 'holder': bc.lock_state()['holder']['owner']}))") % str(BENCH)
        rc, out, err = run("bench.py", "lock", "run", "--owner", "g9", "--ttl", "60", "--", sys.executable, "-c", script)
        self.assertEqual(rc, 0, out + err)
        got = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(got, {"owner": "g9", "busy": None, "holder": "g9"})
        self.assertIsNone(self.d.api("GET", "/lock")[1]["holder"])                 # given back at the end
        rc, out, err = run("bench.py", "lock", "run", "--owner", "g9", "--", sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(rc, 7)
        self.d.api("POST", "/lock", {"owner": "g1", "ttl_s": 60, "note": "окно 15 с"})
        try:
            saved = dict(os.environ)
            e = tool_env()
            os.environ.clear()
            os.environ.update(e)
            import owner_common as oc
            msg = oc.bench_busy(None, need_lyrat=False, need_udp=False)
            self.assertIn("g1", msg or "")
            os.environ["BENCH_LOCK_OWNER"] = "g1"
            self.assertIsNone(oc.bench_busy(None, need_lyrat=False, need_udp=False))
            rc, out, err = run("bench.py", "lock", "run", "--owner", "g2", "--wait", "1", "--", sys.executable, "-c", "1")
            self.assertEqual(rc, 1)
            self.assertIn("g1", out)
        finally:
            os.environ.clear()
            os.environ.update(saved)
            self.d.api("POST", "/lock/release", {"owner": "g1"})
        rc, out, err = run("bench.py", "status")
        self.assertEqual(rc, 0, err)
        self.assertIn("Замок  свободен", out)
        rc, out, err = run("bench.py", "events", "--type", "lock")
        self.assertEqual(rc, 0, err)
        self.assertTrue(all(json.loads(ln)["type"] == "lock" for ln in out.strip().splitlines()))
        rc, out, err = run("bench.py", "logs", "--from", "-1m", "--board", "s3", "--grep", "stat up=", "--limit", "3")
        self.assertEqual((rc, len(out.strip().splitlines())), (0, 3), err)

    def test_h_update_as_a_job(self):
        import images
        syn = TMP / "synthetic"
        images.prepare(syn)
        code, job = self.d.api("POST", "/ota", {"board": "s3", "image": str(syn / "s3-ota" / "s3_eth_bridge.bin"),
                                                "owner": "t103"})
        self.assertEqual(code, 202, job)
        done = self.d.wait(lambda: (lambda j: j if j.get("ended") else None)(self.d.api("GET", "/jobs/" + job["id"])[1]),
                           240, 1.0)
        self.assertTrue(done)
        self.assertEqual(done["exit"], 0, done.get("tail"))
        self.assertIn('"result": "done"', done["tail"])
        kinds = [(e["action"], e.get("exit")) for e in self.d.events("job") if e["id"] == job["id"]]
        self.assertEqual(kinds, [("start", None), ("end", 0)])
        self.assertEqual(self.d.api("POST", "/ota", {"board": "both", "image": "x.bin"})[0], 400)
        self.assertEqual(self.d.api("POST", "/ota", {"board": "s3", "image": str(TMP / "none.bin")})[0], 400)

    def test_i_admin_page_and_post_rules(self):
        code, page = self.d.api("GET", "/", headers={})
        self.assertEqual(code, 200)
        html = page.decode("utf-8")
        for word in ("Перезапуск LyraT", "Перезапуск S3", "Прошить обе платы", "call_check --probe 1", "confirm(",
                     "X-Bench", "События за час", "Лог"):
            self.assertIn(word, html)
        self.assertIsNone(re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", html))   # nothing from outside
        self.assertNotIn("@import", html)
        code, r = self.d.api("POST", "/cmd", {"target": "lyrat", "cmd": "tone 0"}, headers={"Content-Type": "application/json"})
        self.assertEqual(code, 403)
        code, r = self.d.api("POST", "/lock", None, headers={"X-Bench": "1", "Content-Type": "text/plain"})
        self.assertEqual(code, 400)                  # no owner
        self.assertEqual(self.d.api("GET", "/nope")[0], 404)

    def test_j_jobs_listed_and_tail(self):
        code, r = self.d.api("GET", "/tail?after=0&board=s3&grep=stat")
        self.assertEqual(code, 200)
        self.assertTrue(r["lines"] and all(" s3 " in ln for ln in r["lines"]))
        last = r["last_id"]
        time.sleep(1.5)
        r2 = self.d.api("GET", "/tail?after=%d" % last)[1]
        self.assertTrue(r2["lines"])
        self.assertEqual(self.d.api("GET", "/tail?grep=(")[0], 400)

    def test_z_lost_s3_and_its_return(self):
        t = now_iso()
        ports = self.sim.ports
        self.sim.close()
        lost = self.d.wait(lambda: [e for e in self.d.events("board_lost", t) if e["board"] == "s3"], 10, 0.5)
        self.assertTrue(lost)
        self.assertTrue(self.d.api("GET", "/status")[1]["s3"]["lost"])
        type(self).sim = simproc.Sim()                         # the same ports: the S3 is back with a new boot id
        self.assertEqual(self.sim.ports["log_port"], ports["log_port"])
        back = self.d.wait(lambda: [e for e in self.d.events("board_back", t) if e["board"] == "s3"], 10, 0.5)
        self.assertTrue(back)
        self.assertTrue(self.d.wait(lambda: [e for e in self.d.events("boot", t) if e["board"] == "s3"], 8))


def lan_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))          # no packet is sent: only the route is looked up
        ip = s.getsockname()[0]
    except OSError:
        ip = None
    finally:
        s.close()
    return ip if ip and not ip.startswith("127.") else None


class Lan(unittest.TestCase):
    def test_lan_reads_but_does_not_control(self):
        ip = lan_address()
        self.assertIsNotNone(ip, "this PC has no LAN address to try --lan from")
        home = TMP / "home_lan"
        d = Benchd(home, "--lan", "--http-port", str(5090 + OFFSET), "--relay-port", str(5091 + OFFSET))
        try:
            url = "http://%s:%d" % (ip, 5090 + OFFSET)
            code, st = d.api("GET", "/status", url=url)
            self.assertEqual(code, 200)
            self.assertEqual(d.api("GET", "/", headers={}, url=url)[0], 200)
            code, r = d.api("POST", "/lock", {"owner": "lan"}, url=url)
            self.assertEqual(code, 403)
            self.assertEqual(d.api("POST", "/lock", {"owner": "local", "ttl_s": 5})[0], 200)   # from this PC it works
        finally:
            d.close()
        # Without --lan only 127.0.0.1 answers.
        d = Benchd(TMP / "home_local", "--http-port", str(5092 + OFFSET), "--relay-port", str(5093 + OFFSET))
        try:
            with self.assertRaises(OSError):
                urllib.request.urlopen("http://%s:%d/status" % (ip, 5092 + OFFSET), timeout=3)
        finally:
            d.close()


class Without(unittest.TestCase):
    def test_tools_without_benchd_as_before(self):
        sim = simproc.Sim()
        try:
            env = tool_env(BENCH_S3_ADDR="127.0.0.1", BENCHD_HOME=str(TMP / "no_benchd_here"))
            rc, out, err = run("udplog.py", "--seconds", "3", "--out", str(TMP / "direct"), env=env)
            self.assertEqual(rc, 0, out + err)
            res = json.loads(out.strip().splitlines()[-1])
            self.assertEqual(res["via"], "direct")
            self.assertGreater(res["boards"]["lyrat"]["lines"], 10)
            rc, out, err = run("bench.py", "status", env=env)
            self.assertEqual(rc, 2)
            sys.path.insert(0, str(BENCH))
            saved = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update(env)
                import benchd_client
                t0 = time.monotonic()
                self.assertIsNone(benchd_client.lock_state())
                self.assertIsNone(benchd_client.s3_addr())
                self.assertLess(time.monotonic() - t0, 0.5)             # no network call without the run file
            finally:
                os.environ.clear()
                os.environ.update(saved)
        finally:
            sim.close()


class Ports(unittest.TestCase):
    def test_two_simulators_on_two_offsets(self):
        sims, saved = [], os.environ.get("BENCH_PORT_OFFSET")
        try:
            for off in (OFFSET + 20, OFFSET + 40):
                os.environ["BENCH_PORT_OFFSET"] = str(off)
                sims.append((off, simproc.Sim()))
        finally:
            os.environ["BENCH_PORT_OFFSET"] = saved
        try:
            boots = set()
            for off, sim in sims:
                self.assertEqual((sim.ports["tap_port"], sim.ports["log_port"], sim.ports["call_port"]),
                                 (5006 + off, 5008 + off, 5004 + off))
                env = tool_env(BENCH_S3_ADDR="127.0.0.1", BENCH_PORT_OFFSET=off, BENCH_NO_BENCHD=1)
                rc, out, err = run("s3_uart.py", "query", env=env)
                self.assertEqual(rc, 0, out + err)
                boots.add(json.loads(out.strip().splitlines()[-1])["boot"])
                rc, out, err = run("udplog.py", "--seconds", "2", "--out", str(TMP / ("off%d" % off)), env=env)
                self.assertEqual(rc, 0, out + err)
            self.assertEqual(len(boots), 2)                 # each tool reached its own simulator
        finally:
            for _, sim in sims:
                sim.close()


if __name__ == "__main__":
    unittest.main()
