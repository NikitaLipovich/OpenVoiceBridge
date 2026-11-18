"""The phone finds benchd and the S3 itself, and works with the S3 directly when the PC is off (goal 105).

The phone's code (BenchFind.java, S3Direct.java: plain Java) runs on the PC's JDK through BenchNetMain.java:
  Protocol  signature and verdicts the same as benchd_client (ok, unsigned, old, bad_sig, wrong_nonce, not_benchd)
  Logic     pauses, the search after 2 failed polls, the subnet's broadcast, typed addresses, the choice among services
  Find      against real benchd processes: found by broadcast with no cache; a cached address that is no benchd, then a
            foreign benchd (other token) -> both refused, its own found; a benchd without the token's signature; an old
            benchd (goal 104) that only answers "BENCHD?" -> not taken with a token but seen (no direct mode); the same
            service on another HTTP port -> found anew; nothing there -> the screen line says why
  Direct    against s3_sim.py: UQ by broadcast, the log by LOG1 (lines of both boards), LOG0 ends it, UC with its reply,
            an unknown command, UX restarts the S3; the status of S3Direct.State = s3_direct.DirectState on the same
            input; log datagrams and long lines = udplog; redact = benchlib.redact; the S3 commands of direct mode
  Spec      the direct ops of the generator, s3_direct.py, the generated BenchSpec and BenchActivity agree; in
            BenchActivity every write to the S3 is inside the direct mode (DirectRun, sendDirect)
Ports: BENCH_PORT_OFFSET + 1000 (+1020, +1040). No phone, no adb, BENCH_NO_ADB=1.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
BENCH = TOOLS / "bench"
SRC = TOOLS / "android" / "sco_probe" / "src" / "com" / "openvoicebridge" / "scoprobe"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(TOOLS / "android"))
BASE_OFFSET = int(os.environ.get("BENCH_PORT_OFFSET", "0") or 0)
OFFSET = BASE_OFFSET + 1000
os.environ["BENCH_PORT_OFFSET"] = str(OFFSET)
os.environ["BENCH_NO_ADB"] = "1"
os.environ["BENCH_NO_BENCHD"] = "1"
import benchd_client as bc  # noqa: E402
import benchlib  # noqa: E402
import gen_bench_ui as G  # noqa: E402
import s3_direct  # noqa: E402
import udplog  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t105_"))
JAVA = None
BCAST = "255.255.255.255"


def setUpModule():
    global JAVA
    import build_apk
    jdk = build_apk.find_jdk(None)
    classes = TMP / "classes"
    classes.mkdir()
    srcs = [SRC / n for n in ("BenchFind.java", "S3Direct.java", "MiniJson.java", "BenchTemplate.java")]
    r = subprocess.run([str(jdk / "bin" / "javac"), "-encoding", "UTF-8", "-d", str(classes), *map(str, srcs),
                        str(HERE / "BenchNetMain.java")], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError("javac: %s" % r.stderr)
    JAVA = [str(jdk / "bin" / "java"), "-Dfile.encoding=UTF-8", "-cp", str(classes),
            "com.openvoicebridge.scoprobe.BenchNetMain"]


def java(*args, timeout=120):
    r = subprocess.run(JAVA + [str(a) for a in args], capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise AssertionError("java %s: %s" % (args[0], r.stderr.decode("utf-8", "replace")[-2000:]))
    out = r.stdout.decode("utf-8").strip()
    return json.loads(out) if out[:1] in "[{\"" or out in ("null", "true", "false") else out


class Benchd:
    """benchd.py on its own ports and home. lan: --lan [--lan-control]; token: a token file made beforehand."""

    def __init__(self, http, disc, home, control=True, lan=True, token=None, name="pc-test"):
        home.mkdir(parents=True, exist_ok=True)
        if token:
            (home / "lan_token.txt").write_text(token + "\n", encoding="utf-8")
        env = dict(os.environ, BENCHD_HOME=str(home), BENCH_S3_ADDR="127.0.0.99", PYTHONIOENCODING="utf-8")
        argv = [sys.executable, str(BENCH / "benchd.py"), "--home", str(home), "--http-port", str(http),
                "--relay-port", str(http + 1), "--discovery-port", str(disc), "--name", name, "--poll-s", "30"]
        argv += (["--lan"] + (["--lan-control"] if control else [])) if lan else []
        self.p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("benchd did not start: %s" % self.p.stderr.read()[-2000:])
        self.http = http
        self.url = json.loads(line)["url"]
        try:
            self.token = (home / "lan_token.txt").read_text(encoding="utf-8").strip()
        except OSError:
            self.token = None

    def close(self):
        try:
            req = urllib.request.Request(self.url + "/shutdown", data=b"{}", method="POST",
                                         headers={"X-Bench": "1", "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).read()
            self.p.wait(timeout=8)
        except Exception:
            self.p.kill()
            self.p.wait()
        self.p.stdout.close()
        self.p.stderr.close()


class Old104:
    """A benchd of goal 104 as far as discovery goes: answers only the plain "BENCHD?", 404 on /api/whoami."""

    def __init__(self, disc):
        import socket
        import threading
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("0.0.0.0", disc))
        self.s.settimeout(0.2)
        self.stop = False

        def run():
            while not self.stop:
                try:
                    data, peer = self.s.recvfrom(256)
                except (OSError, socket.timeout):
                    continue
                if data.strip() == b"BENCHD?":
                    self.s.sendto(b'{"benchd": 1, "version": "104.1", "http_port": 5999, "control": true}', peer)
        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()

    def close(self):
        self.stop = True
        self.t.join(2)
        self.s.close()


# --- Protocol --------------------------------------------------------------------------------------------------------

class Protocol(unittest.TestCase):
    def test_signature_and_id_as_benchd(self):
        tok = "x" * 32
        self.assertEqual(java("id", tok), bc.disc_id(tok))
        self.assertEqual(java("sign", tok, "ab" * 16, "abc123", 5980, 1, "my-pc"),
                         bc.disc_sig(tok, "ab" * 16, "abc123", 5980, True, "my-pc"))

    def test_verdicts_as_benchd_client(self):
        tok, other, n = "t" * 32, "u" * 32, "cd" * 16
        good = bc.disc_answer(tok, n, 5080, True, "pc", "105.1")
        cases = [(good, tok, n), (good, other, n), (good, tok, "ef" * 16), (good, "", n),
                 (bc.disc_answer(None, n, 5080, False, "pc", "105.1"), tok, n),
                 ({"benchd": 1, "version": "104.1", "http_port": 5080, "control": True}, tok, n),
                 (dict(good, http_port=5081), tok, n), (dict(good, name="evil"), tok, n), (dict(good, control=False), tok, n),
                 ({"benchd": 2, "http_port": 1}, tok, n), ({"benchd": 1, "http_port": "5080"}, tok, n),
                 (bc.disc_answer(tok, n, 5080, True, "мой-пк", "105.1"), tok, n)]
        f = TMP / "verify.json"
        f.write_text(json.dumps([{"answer": json.dumps(a, ensure_ascii=False), "token": t, "nonce": nn}
                                 for a, t, nn in cases], ensure_ascii=False),
                     encoding="utf-8")
        want = [bc.verify_answer(a, t, nn) for a, t, nn in cases]
        self.assertEqual(want, ["ok", "bad_sig", "wrong_nonce", "unsigned", "unsigned", "old", "bad_sig", "bad_sig",
                                "bad_sig", "not_benchd", "not_benchd", "ok"])
        self.assertEqual(java("verify", f), want)

    def test_request_parse(self):
        self.assertEqual(bc.parse_request(b"BENCHD?"), "")
        self.assertEqual(bc.parse_request(b"BENCHD? 2 " + b"ab" * 16), "ab" * 16)
        for bad in (b"BENCHD? 3 " + b"ab" * 16, b"BENCHD? 2 xyz", b"BENCHD? 2 " + b"ab" * 4, b"hello"):
            self.assertIsNone(bc.parse_request(bad))


class Logic(unittest.TestCase):
    def test_logic(self):
        got = java("logic")
        self.assertEqual(got["backoff"], [0, 3000, 6000, 12000, 24000, 30000, 30000, 30000])
        self.assertEqual(got["errors"], [False, False, True, True])
        self.assertEqual(got["subnet"], ["192.168.0.255", "10.1.255.255", "172.16.15.255", None, None])
        self.assertEqual(got["host"], ["192.168.0.5:5080", "192.168.0.5:5980", None, None, None])
        self.assertEqual(got["choose"], ["ccc", "aaa", "bbb"])     # cached id, else cached address, else the fastest
        self.assertEqual(got["acceptable"], [True, False, True, False, False])


# --- Find ------------------------------------------------------------------------------------------------------------

class Find(unittest.TestCase):
    """Two services of their own, A (the phone's token) and B (another token), on their own ports."""

    @classmethod
    def setUpClass(cls):
        cls.ha, cls.da = 5080 + OFFSET, 5082 + OFFSET
        cls.hb, cls.db = 5080 + OFFSET + 20, 5082 + OFFSET + 20
        cls.a = Benchd(cls.ha, cls.da, TMP / "home_a", name="pc-a")
        cls.b = Benchd(cls.hb, cls.db, TMP / "home_b", name="pc-b")

    @classmethod
    def tearDownClass(cls):
        cls.a.close()
        cls.b.close()

    def find(self, token, ports, hints="-", cache_id="-", cache_host="-", skip=False):
        args = ["find", token or "-", ",".join(map(str, ports)), BCAST, hints, cache_id, cache_host]
        return java(*args + (["skip"] if skip else []))

    def test_found_with_no_cache(self):
        r = self.find(self.a.token, [self.da, self.db])
        self.assertIsNotNone(r["chosen"], r)
        self.assertEqual((r["chosen"]["http_port"], r["chosen"]["verdict"], r["chosen"]["via"]), (self.ha, "ok", "broadcast"))
        verdicts = sorted(x["verdict"] for x in r["all"])
        self.assertEqual(verdicts, ["bad_sig", "ok"], r)              # B answered and was not taken
        self.assertIn("чужих не взято: 1", r["line"])
        self.assertLess(r["ms"], 4000)

    def test_wrong_cache_then_foreign_cache(self):
        # A cached address where nothing listens, then one where a foreign benchd answers: both refused, A found.
        r = self.find(self.a.token, [self.da], hints="127.0.0.1:%d,127.0.0.1:%d" % (self.ha + 5, self.hb),
                      cache_host="127.0.0.1:%d" % (self.ha + 5))
        self.assertEqual([x["verdict"] for x in r["all"][:2]], ["no_answer", "bad_sig"], r)
        self.assertEqual((r["chosen"]["http_port"], r["chosen"]["via"]), (self.ha, "broadcast"))

    def test_cache_confirmed_without_broadcast(self):
        r = self.find(self.a.token, [], hints="127.0.0.1:%d" % self.ha)
        self.assertEqual((r["chosen"]["via"], r["chosen"]["verdict"], len(r["all"])), ("cache", "ok", 1), r)
        self.assertIn("адрес из кэша подтверждён", r["line"])

    def test_without_token_read_only(self):
        r = self.find(None, [self.da])
        self.assertEqual(r["chosen"]["verdict"], "unsigned")
        self.assertIn("только просмотр", r["line"])

    def test_nothing_there(self):
        r = self.find(self.a.token, [5082 + OFFSET + 60])
        self.assertIsNone(r["chosen"])
        self.assertEqual(r["all"], [])
        self.assertIn("ПК не найден", r["line"])
        self.assertIn("изоляции", r["line"])

    def test_old_benchd_is_seen_not_taken(self):
        old = Old104(5082 + OFFSET + 40)
        try:
            r = self.find(self.a.token, [5082 + OFFSET + 40])
            self.assertIsNone(r["chosen"])
            self.assertEqual([x["verdict"] for x in r["all"]], ["old"])       # seen: the phone stays out of direct mode
            self.assertIn("старая версия", r["line"])
            r = self.find(None, [5082 + OFFSET + 40])
            self.assertEqual(r["chosen"]["verdict"], "old")                    # read only without a token, as in 104
        finally:
            old.close()


class Restart(unittest.TestCase):
    def test_same_service_on_another_port_found_anew(self):
        home = TMP / "home_r"
        disc = 5082 + OFFSET + 30
        s = Benchd(5080 + OFFSET + 30, disc, home, name="pc-r")
        tok = s.token
        first = java("find", tok, disc, BCAST, "-", "-", "-")
        s.close()
        s = Benchd(5080 + OFFSET + 35, disc, home, name="pc-r")       # the same home: the same token and id
        try:
            r = java("find", tok, disc, BCAST, "127.0.0.1:%d" % (5080 + OFFSET + 30), first["chosen"]["id"],
                     "127.0.0.1:%d" % (5080 + OFFSET + 30))
            self.assertEqual(r["all"][0]["verdict"], "no_answer")             # the old port is gone
            self.assertEqual((r["chosen"]["http_port"], r["chosen"]["id"]), (5080 + OFFSET + 35, first["chosen"]["id"]))
        finally:
            s.close()

    def test_lan_without_control_signs_with_an_existing_token(self):
        home = TMP / "home_n"
        s = Benchd(5080 + OFFSET + 50, 5082 + OFFSET + 50, home, control=False, token="n" * 32)
        try:
            r = java("find", "n" * 32, 5082 + OFFSET + 50, BCAST, "-", "-", "-")
            self.assertEqual(r["chosen"]["verdict"], "ok")
            self.assertFalse((home / "lan_token.txt").read_text().strip() != "n" * 32)
        finally:
            s.close()
        home2 = TMP / "home_n2"
        s = Benchd(5080 + OFFSET + 50, 5082 + OFFSET + 50, home2, control=False)
        try:
            self.assertFalse((home2 / "lan_token.txt").exists())         # --lan alone makes no token
            r = java("find", "n" * 32, 5082 + OFFSET + 50, BCAST, "-", "-", "-")
            self.assertIsNone(r["chosen"])
            self.assertEqual([x["verdict"] for x in r["all"]], ["unsigned"])
        finally:
            s.close()


# --- Direct ----------------------------------------------------------------------------------------------------------

class Direct(unittest.TestCase):
    def test_against_the_simulator(self):
        import simproc
        sim = simproc.Sim("--host", "0.0.0.0")
        try:
            tap, log = sim.ports["tap_port"], sim.ports["log_port"]
            r = java("s3", "127.0.0.1", tap, log, BCAST + ",127.0.0.1", timeout=180)
        finally:
            sim.close()
        self.assertIn("127.0.0.1", r["found"])
        self.assertGreater(r["lines"], 20, r)
        self.assertTrue(r["boards"][0] > 0 and r["boards"][1] > 0, r)        # the S3's lines and the LyraT's
        self.assertTrue(r["log_reply"].startswith("LOG 1 "), r)
        st = r["status"]
        self.assertEqual((st["mode"], st["s3"]["found"], st["s3"]["lost"], st["lyrat"]["lost"]), ("s3-direct", True, False, False))
        self.assertEqual((st["s3"]["net"], st["s3"]["rssi"] is not None), ("wifi", True))  # from the stat line
        self.assertEqual(r["uc_s"][0], 0)
        self.assertTrue(r["uc_s"][2])
        self.assertEqual(r["uc_unknown"], 1)
        self.assertEqual(r["lines_after_log0"], 0)                          # LOG0 gives the log back at once
        self.assertEqual(r["ux"][0], 0)
        self.assertTrue(r["boot_changed"])

    def test_state_as_the_reference(self):
        uq = {"fw": "s3tap", "boot": 77, "up_ms": 12350, "pins_ok": 1, "tx_mode": 2, "tx_on": 1, "last_frame_ms": 400,
              "lyrat_up_s": 11, "lyrat_flags": 3}
        lines = [("s3", "stat up=12 eth=down link=0 ip=10.0.0.9 rssi=-61 net=wifi txp=60 selftest=off"),
                 ("lyrat", "I (1) main: start ver=abc123"), ("s3", "I (2) s3eth: ver=s3v9"), ("u3", "ignored ver=no")]
        for now, uq_t, flags, lf in ((13000, 12000, 3, 400), (30000, 12000, 1, 400), (13000, 12000, -1, -1),
                                      (13000, 12000, 0, 5000)):
            q = dict(uq, lyrat_flags=flags, last_frame_ms=lf)
            py = s3_direct.DirectState("10.0.0.9", lost_s=10.0)
            py.feed_uq(q, uq_t / 1000.0, 3, "T1")
            ev = [{"uq": q, "t": uq_t, "ms": 3, "iso": "T1"}]
            for b, t in lines:
                py.feed_line(b, t, "T2")
                ev.append({"board": b, "line": t, "iso": "T2"})
            py.datagrams = 5
            f = TMP / "feed.json"
            f.write_text(json.dumps({"addr": "10.0.0.9", "lost_ms": 10000, "events": ev, "now": now, "datagrams": 5}),
                         encoding="utf-8")
            self.assertEqual(java("state", f), json.loads(json.dumps(py.status(now / 1000.0))), (now, flags, lf))
        empty = s3_direct.DirectState("10.0.0.9").status(1.0)
        self.assertTrue(empty["s3"]["lost"] and empty["lyrat"]["lost"])

    def test_log_datagrams_as_udplog(self):
        def rec(board, seq, text, boot=5, lost=0):
            b = text.encode("utf-8")
            return struct.pack("<BBHIIIH", board, 3, boot, seq, seq * 10, lost, len(b)) + b

        def dg(*recs, count=None):
            return b"UL" + bytes([1, len(recs) if count is None else count]) + b"".join(recs)

        grams = [dg(rec(0, 1, "stat up=1 eth=x"), rec(0, 2, "+ rssi=-50 net=wifi"), rec(1, 7, "привет ver=1")),
                 dg(rec(1, 8, "line two"), rec(1, 9, "+ not joined: other board?"), rec(0, 3, "+ late part")),
                 dg(rec(0, 4, "x"), count=3), b"UL\x02\x01" + rec(0, 5, "v2"), b"XX", dg(rec(0, 6, "cut")[:-1])]
        got = java("ul", *[g.hex() for g in grams])
        want_recs, want_lines, j = [], [], udplog.Joiner()
        for g in grams:
            recs = udplog.parse_datagram(g)
            want_recs.append(recs and [{k: r[k] for k in ("board", "level", "boot", "seq", "t_ms", "lost", "text")} for r in recs])
            for r in recs or []:
                want_lines += ["%s %s" % (udplog.BOARD_NAMES[w["board"]], w["text"]) for w in j.feed(r)]
        want_lines += ["%s %s" % (udplog.BOARD_NAMES[w["board"]], w["text"]) for w in j.flush()]
        self.assertEqual(got["records"], want_recs)
        self.assertEqual(got["lines"], want_lines)
        self.assertIn("s3 stat up=1 eth=x rssi=-50 net=wifi", got["lines"])

    def test_redact_as_benchlib(self):
        for line in ("peer AA:BB:CC:DD:EE:FF up", "CLIP: \"+70000000000\",145", "two 01:02:03:04:05:06 and a1:b2:c3:d4:e5:f6",
                     "nothing here"):
            self.assertEqual(java("redact", line), benchlib.redact(line))

    def test_direct_commands(self):
        for target, text in (("lyrat", "s"), ("lyrat", "reboot 1"), ("s3", "query"), ("s3", "restart"), ("s3", "power 40"),
                             ("s3", "tx 0"), ("lyrat", ""), ("lyrat", "тон")):
            got = java("cmd", target, (text or " ").encode("utf-8").hex())
            try:
                kind, payload = s3_direct.direct_command(target, text)
                self.assertEqual(got[:2], [kind, payload], (target, text))
            except ValueError:
                self.assertTrue(isinstance(got, str) and got.startswith("refused"), (target, text, got))


# --- Spec ------------------------------------------------------------------------------------------------------------

class Spec(unittest.TestCase):
    def test_direct_ops_agree(self):
        self.assertEqual(G.DIRECT_OPS, s3_direct.DIRECT_OPS)
        java_spec = (SRC / "BenchSpec.java").read_text(encoding="utf-8")
        m = re.search(r"DIRECT_OPS = new String\[\]\{(.*?)\};", java_spec)
        self.assertEqual(tuple(re.findall(r'"([a-z]+)"', m.group(1))), G.DIRECT_OPS)
        act = (SRC / "BenchActivity.java").read_text(encoding="utf-8")
        for op in G.DIRECT_OPS:
            self.assertIn('"%s".equals(a.directOp)' % op, act)
        spec = G.load_spec(G.SPEC)
        for a in spec["actions"]:
            if "s3-direct" in G.via(a):
                self.assertIn(a["direct"]["op"], G.DIRECT_OPS)
        flash = [a for a in spec["actions"] if a["id"] in ("ota", "flash_both")]
        self.assertTrue(flash and all("s3-direct" not in G.via(a) and a.get("direct_unavailable") for a in flash))

    def test_writes_to_the_s3_only_in_direct_mode(self):
        """Every UC/UX/LOG1 of the screen is in DirectRun or in the job sendDirect queues after its fresh search."""
        src = (SRC / "BenchActivity.java").read_text(encoding="utf-8")
        start = src.index("private final class DirectRun")
        end = src.index("private Map<String, Object> directInfo")
        sd = src.index("private void sendDirect(")
        sd_end = src.index("private void directReply(")
        for call in (".uc(", ".ux(", ".logLease("):
            for m in re.finditer(re.escape(call), src):
                self.assertTrue(start < m.start() < end or sd < m.start() < sd_end,
                                "%s outside direct mode at %d" % (call, src[:m.start()].count("\n") + 1))
        body = src[sd:sd_end]
        self.assertLess(body.index("BenchFind.find("), body.index("run.jobs.add("))   # the search before the job
        self.assertIn("r.pcPresent()", body)

    def test_check_catches_direct_errors(self):
        spec = G.load_spec(G.SPEC)
        bad = json.loads(json.dumps(spec))
        a = [x for x in bad["actions"] if x["id"] == "reboot_lyrat"][0]
        a["direct"] = {"op": "flash"}
        b = [x for x in bad["actions"] if x["id"] == "cmd"][0]
        b["params"] = [p for p in b["params"] if p["name"] != "target"]
        c = [x for x in bad["actions"] if x["id"] == "ota"][0]
        c["direct"] = {"op": "uc", "text": "x"}
        bad["feeds"][0]["via"] = ["benchd", "s3-direct"]
        bad["panels"][0]["via"] = ["usb"]
        errs = "\n".join(G.validate(bad))
        for frag in ("(e) action reboot_lyrat: via s3-direct needs direct.op", "(e) action cmd: op cmd needs a parameter target",
                     "(e) action ota: a direct block without s3-direct", "(e) feed events: only the log",
                     "panel boards: via ['usb']"):
            self.assertIn(frag, errs)
        d = json.loads(json.dumps(spec))
        d["panels"][0]["fields"].append({"id": "x_direct", "label": "x", "cases": [{"text": "{lock.holder.owner}"}]})
        sample = s3_direct.DirectState("10.0.0.9").status(0.0)
        err, _ = G.check_direct(d, sample)
        self.assertTrue(any("lock.holder.owner" in e for e in err), err)
        self.assertEqual(G.check_direct(spec, sample)[0], [])

    def test_web_page_has_no_direct_only_parts(self):
        spec = G.load_spec(G.SPEC)
        d = json.loads(json.dumps(spec))
        d["actions"].append(dict(d["actions"][4], id="only_direct", via=["s3-direct"]))
        w = G.web_spec(d)
        self.assertNotIn("only_direct", [a["id"] for a in w["actions"]])
        self.assertIn("phone", [f["id"] for f in w["panels"][0]["fields"]])


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
