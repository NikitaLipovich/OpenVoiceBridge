"""The bench UI spec and its generator (goal 104), against benchd and the S3 simulator on 127.0.0.1. No board, no phone.

What is proven here, and only on the PC:
  Generate   gen_bench_ui.py is deterministic and the files in the repo are what the spec gives; --check --strict on
             the repo is clean; the generated files carry the "do not edit" mark
  Drift      gen_bench_ui.py --check on broken copies catches each kind: (a) a generated file edited by hand (web,
             Java) or the hand-written template changed without a new generation; (b) an endpoint, a parameter, a
             choice, an action name that benchd has not, lan_control on a route benchd keeps local, a route taken out of
             benchd; (c) a status path that /status has not, also inside a list; (d) a control route or action of
             benchd with no button: a warning, an error with --strict; a spec error (unknown op) is refused
  Render     the template language gives the same text in Python (reference), JS (the generated page, node) and Java
             (the phone's code on a desktop JVM): the whole panel for live and edge status samples, and a table of
             formats and conditions (rounding ties, negative zero, numeric strings, null, empty lists, quotes)
  Live       benchd 104 against the simulator: /api/routes is the literal the generator reads; the page served is the
             generated one and renders in a headless Chrome (when there is one); a /status sample with a job and the
             lock verifies every status path of the spec; --check with its own benchd + simulator; LAN control: no
             token or a wrong one 401, the right one 200, reading without token, /shutdown never from the LAN; the
             UDP "BENCHD?" answer; bench.py phone-link without a token refuses before adb
Ports: BENCH_PORT_OFFSET + 800 (and + 1500 for the generator's own live check). BENCH_NO_ADB=1.
"""
import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
BENCH = TOOLS / "bench"
ANDROID = TOOLS / "android"
SRC = ANDROID / "sco_probe" / "src" / "com" / "openvoicebridge" / "scoprobe"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(ANDROID))
os.environ["BENCH_NO_ADB"] = "1"
BASE_OFFSET = int(os.environ.get("BENCH_PORT_OFFSET", "0") or 0)
OFFSET = BASE_OFFSET + 800
os.environ["BENCH_PORT_OFFSET"] = str(OFFSET)
import gen_bench_ui as G  # noqa: E402
import simproc  # noqa: E402
from tiers import full_only  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t104_"))
OTA_TOKEN = re.search(r'"(.+)"', simproc.TOKEN.read_text(encoding="utf-8")).group(1)
SAMPLE = TMP / "status_live.json"         # written by Live.setUpClass, used by Drift and Render


def gen(*args, env=None, timeout=120):
    r = subprocess.run([sys.executable, str(BENCH / "gen_bench_ui.py"), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=env)
    return r.returncode, r.stdout + r.stderr


def lan_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
    except OSError:
        ip = None
    finally:
        s.close()
    return ip if ip and not ip.startswith("127.") else None


def api(url, method, path, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
    for k, v in ({"X-Bench": "1", "Content-Type": "application/json"} if headers is None else headers).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Service:
    """s3_sim + benchd --lan --lan-control on OFFSET with a temporary home."""

    def __init__(self):
        self.sim = simproc.Sim()
        self.home = TMP / "home"
        env = dict(os.environ, BENCH_S3_ADDR="127.0.0.1", BENCHD_HOME=str(self.home), BENCH_OTA_TOKEN=OTA_TOKEN,
                   PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, str(BENCH / "benchd.py"), "--home", str(self.home), "--poll-s", "1",
                                   "--lan", "--lan-control", "--bt-mode-cmd", "btmode {mode}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("benchd did not start: %s" % self.p.stderr.read()[-2000:])
        self.run = json.loads(line)
        self.url = self.run["url"]
        self.port = self.run["http_port"]

    def close(self):
        try:
            api(self.url, "POST", "/shutdown", {}, timeout=3)
            self.p.wait(timeout=8)
        except Exception:
            self.p.kill()
            self.p.wait()
        self.p.stdout.close()
        self.p.stderr.close()
        self.sim.close()


SVC = None


def setUpModule():
    """One benchd for the module, and a /status sample with the lock held, a job running and a queue."""
    global SVC
    SVC = Service()
    end = time.monotonic() + 15
    while time.monotonic() < end and not api(SVC.url, "GET", "/status")[1]["s3"].get("boot"):
        time.sleep(0.3)
    assert api(SVC.url, "POST", "/lock", {"owner": "t104", "ttl_s": 120, "note": "test 104"})[0] == 200
    import images
    syn = TMP / "synthetic"
    images.prepare(syn)
    code, job = api(SVC.url, "POST", "/ota", {"board": "s3", "image": str(syn / "s3-ota" / "s3_eth_bridge.bin"),
                                              "owner": "t104"})
    assert code == 202, job
    st = api(SVC.url, "GET", "/status")[1]
    SAMPLE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    end = time.monotonic() + 240           # the job ends before the tests that change the lock
    while time.monotonic() < end and not api(SVC.url, "GET", "/jobs/" + job["id"])[1].get("ended"):
        time.sleep(1.0)
    api(SVC.url, "POST", "/lock/release", {"owner": "t104"})


def tearDownModule():
    if SVC:
        SVC.close()


def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


# --- Generate ---------------------------------------------------------------------------------------------------------

class Generate(unittest.TestCase):
    def test_deterministic_and_in_sync(self):
        outs = []
        for k in range(2):
            web, java = TMP / ("web%d.html" % k), TMP / ("Spec%d.java" % k)
            rc, out = gen("--web-out", str(web), "--java-out", str(java))
            self.assertEqual(rc, 0, out)
            outs.append((web.read_bytes(), java.read_bytes()))
        self.assertEqual(outs[0], outs[1])
        lf = lambda p: p.read_bytes().replace(b"\r\n", b"\n")      # noqa: E731  (a CRLF checkout is the same)
        self.assertEqual(outs[0][0], lf(G.WEB_OUT), "benchd_admin.html is not what the spec gives")
        self.assertEqual(outs[0][1], lf(G.JAVA_OUT), "BenchSpec.java is not what the spec gives")
        self.assertIn("do not edit", outs[0][0].decode("utf-8")[:300])
        self.assertIn("do not edit", outs[0][1].decode("utf-8")[:300])

    def test_check_of_the_repo_is_clean(self):
        rc, out = gen("--check", "--strict", "--status-file", str(SAMPLE))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("WARN", out)
        self.assertNotIn("not verified", out)       # the sample has a job and the lock: every path is seen

    def test_everything_of_the_page_of_103_is_in_the_spec(self):
        spec = json.loads(G.SPEC.read_text(encoding="utf-8"))
        labels = {a["label"] for a in spec["actions"]}
        for label in ("Перезапуск LyraT", "Перезапуск S3", "Режим BT", "Прошить обе платы", "call_check --probe 1",
                      "Взять стенд себе (1 ч)", "Отдать мой замок", "Снять чужой замок"):
            self.assertIn(label, labels)
        fields = [f["label"] for p in spec["panels"] for f in p["fields"]]
        for label in ("S3", "S3 адрес / boot / аптайм", "S3 Wi-Fi rssi / txp / версия", "LyraT",
                      "LyraT аптайм / последний кадр", "Канал гарнитуры (SCO)", "LyraT версия", "Телефон (только чтение)",
                      "Замок", "Очередь"):
            self.assertIn(label, fields)
        self.assertEqual({f["kind"] for f in spec["feeds"]}, {"events", "tail"})
        steal = [a for a in spec["actions"] if a["id"] == "steal"][0]
        self.assertTrue(steal.get("confirm") and steal.get("confirm2"))           # asked twice, as on the page of 103
        for a in spec["actions"]:
            if a["danger"]:
                self.assertTrue(a.get("confirm"), a["id"])


# --- Drift ------------------------------------------------------------------------------------------------------------

class Drift(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TMP))
        self.files = {}
        for key, src in (("spec", G.SPEC), ("template", G.TEMPLATE), ("web", G.WEB_OUT), ("java", G.JAVA_OUT),
                         ("benchd", G.BENCHD)):
            dst = self.dir / src.name
            shutil.copyfile(src, dst)
            self.files[key] = dst

    def edit(self, key, old, new, count=1):
        p = self.files[key]
        text = p.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), count, old)
        p.write_text(text.replace(old, new), encoding="utf-8")

    def spec_edit(self, fn):
        p = self.files["spec"]
        spec = json.loads(p.read_text(encoding="utf-8"))
        fn(spec)
        p.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    def regen(self):
        rc, out = gen("--spec", str(self.files["spec"]), "--template", str(self.files["template"]), "--web-out",
                      str(self.files["web"]), "--java-out", str(self.files["java"]), "--benchd", str(self.files["benchd"]))
        return rc, out

    def check(self, *extra):
        return gen("--check", "--status-file", str(SAMPLE), "--spec", str(self.files["spec"]), "--template",
                   str(self.files["template"]), "--web-out", str(self.files["web"]), "--java-out", str(self.files["java"]),
                   "--benchd", str(self.files["benchd"]), *extra)

    def assertCaught(self, kind, out_rc, text=None):
        rc, out = out_rc
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL (%s)" % kind, out)
        if text:
            self.assertIn(text, out)

    def test_clean_copy_passes(self):
        rc, out = self.check("--strict")
        self.assertEqual(rc, 0, out)

    def test_a_web_page_edited_by_hand(self):
        self.edit("web", "<h2>Платы</h2>", "<h2>Платы!</h2>")
        self.assertCaught("a", self.check(), "web page benchd_admin.html differs")

    def test_a_crlf_checkout_is_not_a_difference(self):
        for key in ("web", "java"):
            p = self.files[key]
            p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        rc, out = self.check("--strict")
        self.assertEqual(rc, 0, out)

    def test_a_java_edited_by_hand(self):
        self.edit("java", '"Перезапуск LyraT"', '"Перезапуск LyraT!"')
        self.assertCaught("a", self.check(), "BenchSpec.java differs")

    def test_a_template_changed_without_generation(self):
        self.edit("template", "<title>Стенд — benchd</title>", "<title>Стенд</title>")
        self.assertCaught("a", self.check(), "web page")

    def test_a_spec_changed_without_generation(self):
        self.spec_edit(lambda s: s["panels"][0].update(title="Платы стенда"))
        rc, out = self.check()
        self.assertCaught("a", (rc, out), "web page")
        self.assertIn("BenchSpec.java differs", out)
        self.assertEqual(self.regen()[0], 0)
        self.assertEqual(self.check("--strict")[0], 0)

    def act(self, spec, aid):
        return [a for a in spec["actions"] if a["id"] == aid][0]

    def test_b_unknown_endpoint(self):
        self.spec_edit(lambda s: self.act(s, "reboot_lyrat").update(endpoint="/actions"))
        self.regen()
        self.assertCaught("b", self.check(), "benchd has no POST /actions")

    def test_b_unknown_parameter(self):
        self.spec_edit(lambda s: self.act(s, "ota")["params"][0].update(name="boards"))
        self.regen()
        self.assertCaught("b", self.check(), "param boards is not a parameter of POST /ota")

    def test_b_choice_benchd_has_not(self):
        self.spec_edit(lambda s: self.act(s, "ota")["params"][1]["choices"].append(["ble", "BLE"]))
        self.regen()
        self.assertCaught("b", self.check(), "choices ['ble'] are not in benchd's")

    def test_b_unknown_action_name(self):
        self.spec_edit(lambda s: self.act(s, "restart_s3")["body"].update(name="restart_s4"))
        self.regen()
        self.assertCaught("b", self.check(), "name='restart_s4' is not an action of benchd")

    def test_b_action_parameter_and_feed_parameter(self):
        def broken(s):
            self.act(s, "bt_mode")["params"][0]["name"] = "modus"
            s["feeds"][1]["params"][0]["name"] = "plata"
        self.spec_edit(broken)
        self.regen()
        rc, out = self.check()
        self.assertCaught("b", (rc, out), "param modus is not a parameter of POST /action")
        self.assertIn("feed log: param plata is not a parameter of /tail", out)

    def test_b_lan_control_on_a_local_route(self):
        self.spec_edit(lambda s: self.act(s, "give").update(endpoint="/shutdown", body={}))
        self.regen()
        self.assertCaught("b", self.check(), "benchd refuses /shutdown from the LAN")

    def test_b_route_taken_out_of_benchd(self):
        self.edit("benchd", '{"method": "POST", "path": "/cmd", "control": True, "lan": True,\n', "{\"method\": \"POST\", "
                  "\"path\": \"/console\", \"control\": True, \"lan\": True,\n")
        self.assertCaught("b", self.check(), "action cmd: benchd has no POST /cmd")

    def test_b_choice_taken_out_of_benchd(self):
        self.edit("benchd", '"via": ["auto", "wifi", "uart"]', '"via": ["auto", "wifi"]')
        self.assertCaught("b", self.check(), "choices ['uart'] are not in benchd's")

    def test_c_status_path_that_is_not_there(self):
        self.spec_edit(lambda s: s["panels"][0]["fields"][1]["cases"][0].update(text="{s3.adress} / {s3.boot}"))
        self.regen()
        self.assertCaught("c", self.check(), "status path s3.adress")

    def test_c_condition_and_list_item(self):
        def broken(s):
            s["panels"][0]["fields"][0]["cases"][1]["if"][0]["path"] = "s3.lostt"
            [f for f in s["panels"][1]["fields"] if f["id"] == "jobs"][0]["cases"][0]["text"] = "{kindd} идёт"
        self.spec_edit(broken)
        self.regen()
        rc, out = self.check()
        self.assertCaught("c", (rc, out), "status path s3.lostt")
        self.assertIn("status path jobs[].kindd", out)

    def test_d_action_without_button_warns(self):
        self.spec_edit(lambda s: s.update(actions=[a for a in s["actions"] if a["id"] != "call_probe"]))
        self.regen()
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN (d) benchd action call_probe has no button", out)
        rc, out = self.check("--strict")
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL (d) benchd action call_probe", out)

    def test_d_new_control_route_of_benchd_warns(self):
        self.edit("benchd", '    {"method": "POST", "path": "/shutdown",',
                  '    {"method": "POST", "path": "/reset_bench", "control": True, "lan": True, "params": {"owner": "str"}},\n'
                  '    {"method": "POST", "path": "/shutdown",')
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN (d) benchd POST /reset_bench changes the bench and no action of the spec uses it", out)

    def test_spec_error_refused(self):
        self.spec_edit(lambda s: s["panels"][0]["fields"][0]["cases"][0]["if"][0].update(op="isnt"))
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("op isnt", out)
        rc, out = self.regen()
        self.assertEqual(rc, 1, out)


# --- Render: Python = JS = Java ---------------------------------------------------------------------------------------

def edge_samples(base):
    a = copy.deepcopy(base)
    a["s3"].update(found=True, lost=False, rssi="-85", txp=None, up_s=0.125, boot=7, ver='v"1"\n2')
    a["lyrat"].update(lost=True, sco_open=True, up_s=-0.5, last_frame_ms=2.0005, ver=None)
    a["phone"].update(state="device", model="CPH2551")
    a["benchd"]["up_s"] = 2.5
    a["benchd"]["disk"].update(log_bytes=157286, limit_bytes=3 * 1048576 // 2)
    a["lock"] = {"holder": {"owner": "092", "since": "2026-11-18T15:00:00", "pid": 1, "note": "",
                            "until": "2026-11-18T15:40:00", "left_s": -0.5, "token_hint": "abc"},
                 "queue": [{"owner": "101", "since": "x"}, {"owner": "owner", "since": "y"}]}
    a["jobs"] = [{"id": "1-1", "kind": "flash-s3", "owner": None}, {"id": "1-2", "kind": "call_probe", "owner": "o"}]
    a["bt_mode"] = True
    a["lan"] = {"listen": True, "control": False}
    b = copy.deepcopy(a)
    b["s3"].update(found=False, rssi="-72")
    b["lock"] = {"holder": {"owner": "owner", "until": "bad", "left_s": "12.5", "note": "a, b"}, "queue": []}
    b["phone"] = {"state": None, "model": None, "devices": 0}
    b["lan"] = {"listen": False, "control": False}
    b["jobs"] = []
    b["bt_mode"] = False
    c = copy.deepcopy(b)
    c["s3"].update(found=True, lost=True, rssi=-60.25)
    c["lock"] = {"holder": None, "queue": None}
    return [a, b, c]


CASES = [
    {"tpl": "{x|int} {y|int} {z|int} {w|int} {s|int} {n|int}", "ctx": {"x": 2.5, "y": -2.5, "z": 0.49999, "w": "7.5", "s": "a",
                                                                     "n": None}},
    {"tpl": "{a|mb1} {b|mb0} {c|mb1} {d|mb1} {e|mb0}", "ctx": {"a": 157286, "b": 1572864, "c": -1000, "d": 0, "e": "x"}},
    {"tpl": "{a} {b} {c} {d} {e} {f} {g}", "ctx": {"a": 0.125, "b": 1e16, "c": -0.0004, "d": 3.0, "e": 1.0005,
                                                   "f": 2.675, "g": -7}},
    {"tpl": "{a} {b} {c} {d} {e}", "ctx": {"a": True, "b": False, "c": None, "d": [1, "x", None], "e": {"k": "v\"\n", "n": 1.5}}},
    {"tpl": "{a|json}|{b|json}|{c|json}", "ctx": {"a": "q\"\\\t\u0001ю", "b": [], "c": {"x": [True, None, 0.1]}}},
    {"tpl": "{t|hhmm} {t|hms} {u|hhmm} {n|hms}", "ctx": {"t": "2026-11-18T15:40:59.123", "u": "15:40", "n": 5}},
    {"tpl": "[{a|opt_comma}][{b|opt_comma}][{c|opt_comma}][{d|opt_comma}][{e|opt_comma}]",
     "ctx": {"a": "", "b": "note", "c": None, "d": 0, "e": [1]}},
    {"tpl": "[{q|owners}][{r|owners}][{s|owners}]", "ctx": {"q": [{"owner": "a"}, {"owner": None}, 3, {"x": 1}], "r": [],
                                                           "s": "no"}},
    {"tpl": "{a.b.0.c} {a.b.1} {a.b.5} {a.x.y} {$device} {$param.mode} {$none.x} {{}} plain", "ctx": {"a": {"b": [{"c": 1}, "z"]}},
     "vars": {"device": "телефон", "param": {"mode": "hfp"}}},
    {"conds": [{"path": "a", "op": "truthy"}], "ctx": {"a": "0"}},
    {"conds": [{"path": "a", "op": "truthy"}], "ctx": {"a": 0}},
    {"conds": [{"path": "a", "op": "falsy"}], "ctx": {"a": []}},
    {"conds": [{"path": "a", "op": "falsy"}], "ctx": {"a": {}}},
    {"conds": [{"path": "a", "op": "empty"}], "ctx": {"a": ""}},
    {"conds": [{"path": "a", "op": "nonempty"}], "ctx": {"a": [0]}},
    {"conds": [{"path": "a", "op": "eq", "value": -55}], "ctx": {"a": "-55"}},
    {"conds": [{"path": "a", "op": "eq", "value": True}], "ctx": {"a": 1}},
    {"conds": [{"path": "a", "op": "eq", "value": None}], "ctx": {}},
    {"conds": [{"path": "a", "op": "ne", "value": "owner"}], "ctx": {"a": "owner"}},
    {"conds": [{"path": "a", "op": "eq", "value": "1.0"}], "ctx": {"a": 1}},
    {"conds": [{"path": "a", "op": "le", "value": -80}], "ctx": {"a": "-80"}},
    {"conds": [{"path": "a", "op": "lt", "value": -80}], "ctx": {"a": "-80"}},
    {"conds": [{"path": "a", "op": "gt", "value": 1}], "ctx": {"a": "abc"}},
    {"conds": [{"path": "a", "op": "ge", "value": 2.5}], "ctx": {"a": 2.5}},
    {"conds": [{"path": "a", "op": "truthy"}, {"path": "b", "op": "lt", "value": 3}], "ctx": {"a": 1, "b": 2}},
    {"conds": [{"path": "a", "op": "truthy"}, {"path": "b", "op": "lt", "value": 3}], "ctx": {"a": 1, "b": 4}},
]


def py_cases(cases):
    out = []
    for c in cases:
        if "tpl" in c:
            out.append(G.render(c["tpl"], c["ctx"], c.get("vars") or {}))
        else:
            out.append(G.holds(c["conds"], c["ctx"], c.get("vars") or {}))
    return out


class Render(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.java = None
        try:
            import build_apk
            jdk = build_apk.find_jdk(None)
        except Exception:
            jdk = None
        if jdk:
            classes = TMP / "jclasses"
            classes.mkdir(exist_ok=True)
            srcs = [SRC / n for n in ("BenchTemplate.java", "MiniJson.java", "BenchUi.java", "BenchSpec.java")]
            r = subprocess.run([str(jdk / "bin" / "javac"), "-encoding", "UTF-8", "-d", str(classes),
                                *map(str, srcs), str(HERE / "BenchRenderMain.java")], capture_output=True, text=True,
                               errors="replace")
            if r.returncode != 0:
                raise RuntimeError("javac: %s" % r.stderr)
            cls.java = [str(jdk / "bin" / "java"), "-Dfile.encoding=UTF-8", "-cp", str(classes),
                        "com.openvoicebridge.scoprobe.BenchRenderMain"]

    def run_js(self, mode, path, *extra):
        r = subprocess.run([self.node, str(HERE / "bench_render.js"), str(G.WEB_OUT), mode, str(path), *extra],
                           capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return json.loads(r.stdout.decode("utf-8"))

    def run_java(self, mode, path, *extra):
        r = subprocess.run(self.java + [mode, str(path), *extra], capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return json.loads(r.stdout.decode("utf-8"))

    def engines(self):
        if not self.node:
            self.skipTest("node is not installed: the JS of the page is not compared")
        if not self.java:
            self.skipTest("no JDK: the Java of the phone is not compared")

    def test_reference_values(self):
        """The reference itself on the cases where languages differ by default."""
        got = py_cases(CASES[:3])
        self.assertEqual(got[0], "3 -2 0 8 — —")                 # floor(x + 0.5), numeric strings, no value
        self.assertEqual(got[1], "0.1 2 0.0 0.0 —")              # exact binary value, ties up, no negative zero
        self.assertEqual(got[2], "0.125 10000000000000000 0 3 1 2.675 -7")

    def test_cases_same_in_three(self):
        self.engines()
        path = TMP / "cases.json"
        path.write_text(json.dumps(CASES, ensure_ascii=False), encoding="utf-8")
        py = py_cases(CASES)
        js = self.run_js("cases", path)
        jv = self.run_java("cases", path)
        for i, (a, b, c) in enumerate(zip(py, js, jv)):
            self.assertEqual(a, b, "case %d: Python %r, JS %r" % (i, a, b))
            self.assertEqual(a, c, "case %d: Python %r, Java %r" % (i, a, c))
        self.assertEqual(len(py), len(js))
        self.assertEqual(len(py), len(jv))

    def test_panel_same_in_three(self):
        self.engines()
        spec = json.loads(G.SPEC.read_text(encoding="utf-8"))
        samples = [sample()] + edge_samples(sample())
        for k, st in enumerate(samples):
            path = TMP / ("panel%d.json" % k)
            path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            for key, device in (("page", "страница"), ("phone", "телефон")):
                py = json.loads(G.json_str(G.render_panel(spec, st, device)))
                self.assertEqual(py, self.run_js("panel", path, key), "sample %d %s: JS differs" % (k, key))
                self.assertEqual(py, self.run_java("panel", path, key), "sample %d %s: Java differs" % (k, key))
        # A few values a person would check by eye, on the first edge sample.
        py = G.render_panel(spec, samples[1])
        f = {x["id"]: x for x in py["fields"]}
        self.assertEqual((f["s3_wifi"]["text"], f["s3_wifi"]["color"]), ("-85 дБм / — / v\"1\"\n2", "bad"))
        self.assertEqual((f["lyrat"]["text"], f["lyrat"]["color"]), ("ПРОПАЛА", "bad"))
        self.assertEqual(f["lock"]["text"], "у «092» до 15:40 (0 с)")
        self.assertEqual(f["queue"]["text"], "101, owner")
        self.assertEqual(f["jobs.0"]["text"], "flash-s3 (—) идёт")
        self.assertEqual(f["phone"]["text"], "device, CPH2551")
        self.assertIn("диск 0.1 МБ из 2", py["header"])
        acts = {a["id"]: a for a in py["actions"]}
        self.assertTrue(acts["bt_mode"]["visible"])
        self.assertFalse(acts["renew"]["available"])            # the lock is 092's
        self.assertEqual(acts["take"]["body"], {"ttl_s": 3600, "note": "владелец (страница)"})
        py = G.render_panel(spec, samples[2], "телефон")
        f = {x["id"]: x for x in py["fields"]}
        self.assertEqual(f["s3"]["text"], "не найдена")
        self.assertEqual(f["lock"]["text"], "у «owner» до — (13 с), a, b")
        self.assertEqual(f["queue"]["text"], "пусто")
        acts = {a["id"]: a for a in py["actions"]}
        self.assertTrue(acts["renew"]["available"])
        self.assertFalse(acts["bt_mode"]["visible"])
        self.assertEqual(acts["take"]["body"]["note"], "владелец (телефон)")


# --- Live -------------------------------------------------------------------------------------------------------------

class Live(unittest.TestCase):
    def test_api_routes_is_the_literal(self):
        code, ans = api(SVC.url, "GET", "/api/routes")
        self.assertEqual(code, 200)
        routes, actions = G.benchd_tables(G.BENCHD)
        self.assertEqual(ans["routes"], routes)
        self.assertEqual(ans["actions"], actions)

    def test_page_served_is_the_generated_one(self):
        code, page = api(SVC.url, "GET", "/", headers={})
        self.assertEqual(code, 200)
        self.assertEqual(page, G.WEB_OUT.read_bytes())
        html = page.decode("utf-8")
        self.assertIsNone(re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", html))
        for word in ("generated by gen_bench_ui.py", "X-Bench-Token", "confirm(", "/api/routes"):
            self.assertIn(word, html)

    def test_page_renders_in_a_browser(self):
        chrome = next((c for c in (shutil.which("chrome"), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                                   r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                                   r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") if c and Path(c).is_file()),
                      None)
        if not chrome:
            self.skipTest("no Chrome or Edge for a headless look")
        r = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                            "--user-data-dir=%s" % (TMP / "chrome"), "--virtual-time-budget=6000", "--dump-dom", SVC.url + "/"],
                           capture_output=True, timeout=90)
        dom = r.stdout.decode("utf-8", "replace")
        self.assertIn('id="f-s3"', dom)
        row = re.search(r'id="f-s3".*?<span class="v ([a-z]*)">([^<]*)</span>', dom, re.S)
        self.assertIsNotNone(row, dom[:2000])
        self.assertEqual(row.group(2), "на связи")
        self.assertEqual(row.group(1), "ok")
        self.assertRegex(dom, r'id="svc">служба: pid %d, работает \d+ с' % SVC.run["pid"])
        self.assertRegex(dom, r'<td class="[a-z]*">benchd</td>')            # the events feed
        self.assertRegex(dom, r'<span class="act" id="w-bt_mode">')            # shown: benchd has --bt-mode-cmd
        self.assertNotRegex(dom, r'id="w-bt_mode" hidden')

    @full_only("the same command as the check bench_ui_check, which the fast level runs")
    def test_check_with_its_own_benchd_and_simulator(self):
        rc, out = gen("--check", timeout=180)
        self.assertEqual(rc, 0, out)
        self.assertIn("agree", out)

    def test_lan_token(self):
        ip = lan_address()
        self.assertIsNotNone(ip, "this PC has no LAN address")
        url = "http://%s:%d" % (ip, SVC.port)
        token = (SVC.home / "lan_token.txt").read_text(encoding="utf-8").strip()
        self.assertGreaterEqual(len(token), 24)
        self.assertEqual(api(url, "GET", "/status")[0], 200)                 # reading without the token
        code, ans = api(url, "POST", "/lock", {"owner": "lan", "ttl_s": 5})
        self.assertEqual(code, 401)
        self.assertIn("token", ans["error"])
        wrong = token[:-1] + ("y" if token.endswith("x") else "x")    # a token ending in "x" made it right (goal 110 #001)
        h = {"X-Bench": "1", "Content-Type": "application/json", "X-Bench-Token": wrong}
        self.assertEqual(api(url, "POST", "/lock", {"owner": "lan", "ttl_s": 5}, headers=h)[0], 401)
        h["X-Bench-Token"] = token
        self.assertEqual(api(url, "POST", "/lock", {"owner": "lan", "ttl_s": 5}, headers=h)[0], 200)
        self.assertEqual(api(url, "POST", "/lock/release", {"owner": "lan"}, headers=h)[0], 200)
        self.assertEqual(api(url, "POST", "/shutdown", {}, headers=h)[0], 403)
        self.assertEqual(api(SVC.url, "GET", "/status")[0], 200)              # still running
        self.assertTrue(api(SVC.url, "GET", "/status")[1]["lan"]["control"])
        h.pop("X-Bench")
        self.assertEqual(api(url, "POST", "/lock", {"owner": "lan"}, headers=h)[0], 403)   # X-Bench still needed

    def test_discovery(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        try:
            s.sendto(b"BENCHD?", ("127.0.0.1", 5082 + OFFSET))
            data, _ = s.recvfrom(512)
            s.sendto(b"OTHER", ("127.0.0.1", 5082 + OFFSET))
        finally:
            s.close()
        ans = json.loads(data)
        self.assertEqual((ans["benchd"], ans["http_port"], ans["control"]), (1, SVC.port, True))

    def test_phone_link_without_token_refuses_before_adb(self):
        env = dict(os.environ, BENCHD_HOME=str(TMP / "empty_home"), PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, str(BENCH / "bench.py"), "phone-link"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no LAN token yet", r.stderr)


if __name__ == "__main__":
    unittest.main()
