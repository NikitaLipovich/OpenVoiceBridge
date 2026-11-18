#!/usr/bin/env python3
"""The phone finds the PC and the S3 itself, and works with the S3 directly when the PC is off (goal 105). On the real
phone over the real Wi-Fi, against services of this PC on a port offset: no board, the S3 is the simulator in the LAN.
Not part of run_tests.py (that one never uses adb).

  bench_find_check.py [--serial S] [--offset 900] [--no-build] [--out DIR]

Scenarios, read from the screen with uiautomator dump:
  1 empty cache: phone-link --forget hands only the token; the app finds benchd A by broadcast; the S3 line says the
    phone's S3 address is the service's
  2 a wrong hint: phone-link --forget --host <a foreign benchd B (other token)> and then <an address with nothing> ->
    neither is taken, A is found again
  3 benchd off: A stops -> after the failed polls "ПК не найден", direct mode with the simulator: status from UQ, the log
    on the screen and in the app's file (bench.py phone-logs fetches it), the console command "s", restart of the LyraT
    and of the S3 (the simulator's), "Прошить плату" refused with its reason
  4 an old benchd answers (only "BENCHD?", as goal 104): the phone leaves direct mode (a PC is on: never two masters)
  5 benchd back on another HTTP port: found anew, direct mode ends (LOG0: the service gets the log)
The phone is part of the bench (goal 100 runs its windows with sco_probe): run this under the bench lock, which also
holds the install of the APK: bench.py lock run --owner goal-105 --note "phone UI check" -- python bench_find_check.py
(#001 of goal 105). It stops at once (exit 3) when the "Стенд" screen is no longer on top: it never taps on a screen
that is not its own.
Safety: everything on --offset ports (discovery, the S3's request and log ports): the live benchd and the real S3 are
never asked; the direct commands reach only the simulator. Only our app is installed (adb install -r); no phone setting,
no Bluetooth, no sound (R3, R4, R15). Writes <out>/summary.json and the screen dumps.
Exit code: 0 all checks passed, 1 a check failed, 2 the phone or a tool is missing.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
BENCH = TOOLS / "bench"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(HERE))
from benchlib import RUNS_ROOT  # noqa: E402
from bench_screen_check import Phone, by_desc, by_text  # noqa: E402


class Busy(Exception):
    pass


def top_activity(phone):
    r = phone.adb("shell", "dumpsys", "activity", "activities")
    m = re.search(r"topResumedActivity=\S+ \S+ (\S+)", r.stdout)
    return m.group(1) if m else ""


class Service:
    """benchd.py --lan --lan-control on its own ports and home; the S3 given (the simulator at the PC's LAN address)."""

    def __init__(self, home, http, disc, s3_addr, name):
        env = dict(os.environ, BENCHD_HOME=str(home), BENCH_S3_ADDR=s3_addr, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, str(BENCH / "benchd.py"), "--home", str(home), "--http-port", str(http),
                                   "--relay-port", str(http + 1), "--discovery-port", str(disc), "--poll-s", "1",
                                   "--lan", "--lan-control", "--name", name, "--host", s3_addr],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("benchd did not start: %s" % self.p.stderr.read()[-1500:])
        self.run = json.loads(line)
        self.url = self.run["url"]
        self.http = http
        self.token = (Path(home) / "lan_token.txt").read_text(encoding="utf-8").strip()

    def status(self):
        with urllib.request.urlopen(self.url + "/status", timeout=5) as r:
            return json.loads(r.read())

    def close(self):
        try:
            req = urllib.request.Request(self.url + "/shutdown", data=b"{}", method="POST", headers={"X-Bench": "1"})
            urllib.request.urlopen(req, timeout=5).read()
            self.p.wait(timeout=8)
        except Exception:
            self.p.kill()
            self.p.wait()
        self.p.stdout.close()
        self.p.stderr.close()


class Old104:
    """Answers only the plain "BENCHD?" as benchd of goal 104 did."""

    def __init__(self, disc):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("0.0.0.0", disc))
        self.s.settimeout(0.2)
        self.stop = False
        self.t = threading.Thread(target=self.serve, daemon=True)
        self.t.start()

    def serve(self):
        while not self.stop:
            try:
                data, peer = self.s.recvfrom(256)
            except (OSError, socket.timeout):
                continue
            if data.strip() == b"BENCHD?":
                self.s.sendto(b'{"benchd": 1, "version": "104.1", "http_port": 5999, "control": true}', peer)

    def close(self):
        self.stop = True
        self.t.join(2)
        self.s.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--serial")
    ap.add_argument("--offset", type=int, default=900, help="BENCH_PORT_OFFSET of the test services and the simulator")
    ap.add_argument("--no-build", action="store_true", help="the app is already installed")
    ap.add_argument("--out")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if not args.offset:
        print("--offset 0 would meet the live benchd and the real S3", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else RUNS_ROOT / "bench_find" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    phone = Phone(args.serial)
    checks = []
    n_dump = [0]

    def check(name, ok, note=""):
        checks.append({"name": name, "ok": bool(ok), "note": note})
        print("%s  %s  %s" % ("PASS" if ok else "FAIL", name, note), flush=True)
        return ok

    def dump():
        n_dump[0] += 1
        nodes = phone.dump(out / ("screen%02d.xml" % n_dump[0]))
        if not by_desc(nodes, "mode") and by_text(nodes, "Да") is None:
            top = top_activity(phone)
            if not top.endswith(".BenchActivity"):
                raise Busy("the screen on top is %s, not «Стенд»: somebody else uses the phone" % (top or "?"))
        return nodes

    def text(nodes, desc):
        n = by_desc(nodes, desc)
        return n["text"] if n else None

    def wait(pred, timeout, step=2.0):
        """Dumps the screen until pred(nodes) is true; (True|False, last nodes)."""
        end = time.monotonic() + timeout
        nodes = []
        while True:
            nodes = dump()
            if pred(nodes):
                return True, nodes
            if time.monotonic() > end:
                return False, nodes
            time.sleep(step)

    def top():
        for _ in range(6):
            phone.scroll(down=False)
        time.sleep(1)

    def reach(desc, tries=8):
        """Scroll down until the node is on the screen; the nodes, or None."""
        nodes = dump()
        for _ in range(tries):
            if by_desc(nodes, desc):
                return nodes
            phone.scroll()
            time.sleep(1)
            nodes = dump()
        return nodes if by_desc(nodes, desc) else None

    def press_confirmed(desc):
        """Tap the action, answer every confirmation "Да"; the questions asked."""
        nodes = reach(desc)
        if not nodes:
            return None
        phone.tap(by_desc(nodes, desc))
        asked = []
        for _ in range(3):
            time.sleep(1.5)
            dlg = dump()
            yes = by_text(dlg, "Да")
            if not yes:
                break
            asked += [n["text"] for n in dlg if n["text"] and n["text"] not in ("Да", "Нет")]
            phone.tap(yes)
        return asked

    def link(*extra, token):
        env = dict(os.environ, BENCHD_HOME=str(home_a if token == "a" else home_b))
        r = subprocess.run([sys.executable, str(BENCH / "bench.py"), "phone-link", *extra] +
                           (["--serial", args.serial] if args.serial else []), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        return r

    if "\tdevice" not in phone.adb("devices").stdout:
        print("no phone on adb", file=sys.stderr)
        return 2
    if not args.no_build:
        r = subprocess.run([sys.executable, str(HERE / "build_apk.py"), "--install"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        (out / "build.txt").write_text(r.stdout + r.stderr, encoding="utf-8")
        if not check("build and install sco_probe", r.returncode == 0, r.stderr.strip()[-200:]):
            return 1

    off = args.offset
    os.environ["BENCH_PORT_OFFSET"] = str(off)
    os.environ["BENCH_NO_ADB"] = "1"
    sys.path.insert(0, str(TOOLS / "tests"))
    import bench  # noqa: E402
    import simproc  # noqa: E402
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", phone.adb("shell", "ip -4 addr show wlan0").stdout)
    if not m:
        print("the phone has no Wi-Fi address", file=sys.stderr)
        return 2
    pc = bench.pc_address_for(m.group(1))
    http_a, disc = 5080 + off, 5082 + off
    home_a, home_b = Path(tempfile.mkdtemp(prefix="find_a_")), Path(tempfile.mkdtemp(prefix="find_b_"))
    sim = simproc.Sim("--host", "0.0.0.0")
    a = b = old = None
    busy = None
    try:
        a = Service(home_a, http_a, disc, pc, "pc-a")
        b = Service(home_b, 5080 + off + 20, 5082 + off + 20, pc, "pc-b")     # foreign: another token, another port
        time.sleep(2)
        phone.adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")

        # 1 empty cache, only the token
        r = link("--forget", token="a")
        check("1 phone-link hands only the token", r.returncode == 0 and "token is on the phone" in r.stdout
              and "hint" not in r.stdout, r.stdout.strip()[-160:])
        ok, nodes = wait(lambda n: (text(n, "mode") or "").startswith("через службу"), 25)
        check("1 empty cache: found by itself", ok and "%s:%d" % (pc, http_a) in text(nodes, "mode"), text(nodes, "mode"))
        check("1 found by the search in the network", "(поиск в сети)" in (text(nodes, "conn") or ""), text(nodes, "conn"))
        ok, nodes = wait(lambda n: "совпадает" in (text(n, "s3line") or ""), 40)
        check("1 the phone sees the S3 at the service's address", ok, text(nodes, "s3line"))

        # 2 wrong hints: a foreign benchd, then nothing
        for hint, label in (("%s:%d" % (pc, b.http), "a foreign benchd (other token)"), ("%s:%d" % (pc, http_a + 7), "nothing")):
            r = link("--forget", "--host", hint, token="a")
            ok, nodes = wait(lambda n: (text(n, "mode") or "").startswith("через службу") and
                             "(поиск в сети)" in (text(n, "conn") or ""), 30)
            check("2 hint at %s: not taken, A found" % label, ok and "%s:%d" % (pc, http_a) in text(nodes, "mode"),
                  "%s | %s" % (text(nodes, "mode"), text(nodes, "conn")))

        # 3 benchd off -> direct mode with the simulator
        a.close()
        a = None
        ok, nodes = wait(lambda n: (text(n, "mode") or "").startswith("напрямую"), 45)
        check("3 PC off: direct mode with the S3 (simulator)", ok, "%s | %s" % (text(nodes, "mode"), text(nodes, "conn")))
        check("3 the search said why", "ПК не найден" in (text(nodes, "conn") or ""), text(nodes, "conn"))
        ok, nodes = wait(lambda n: text(n, "f-s3") == "на связи" and text(n, "f-lyrat") == "на связи", 20)
        check("3 status of the S3 and the LyraT from UQ", ok, "%s / %s" % (text(nodes, "f-s3"), text(nodes, "f-lyrat")))
        check("3 the header says: no PC, no lock", (text(nodes, "svc") or "").startswith("напрямую с S3"), text(nodes, "svc"))
        check("3 the lock panel is hidden", by_desc(nodes, "f-lock") is None)
        log_nodes = reach("feed-log")
        log_text = text(log_nodes, "feed-log") if log_nodes else ""
        check("3 the log of both boards on the screen", bool(log_text) and " s3 " in log_text and " lyrat " in log_text,
              (log_text or "")[:120])
        top()
        # the console: "s" to the LyraT
        nodes = reach("a-cmd-cmd")
        if check("3 the console field is there", nodes is not None):
            phone.tap(by_desc(nodes, "a-cmd-cmd"))
            phone.adb("shell", "input", "text", "s")
            asked = press_confirmed("a-cmd")
            check("3 the console asks first", asked and any("Послать «s» на lyrat" in q for q in asked), asked)
            ok, nodes = wait(lambda n: (text(n, "msg-bar") or "").startswith(("готово (напрямую)", "S3 не ответила",
                                                                                 "ПК снова")), 25)
            check("3 console 's' answered directly", ok and text(nodes, "msg-bar").startswith("готово (напрямую)"),
                  (text(nodes, "msg-bar") or "")[:120])
        boot0 = None
        for desc, want in (("a-reboot_lyrat", "готово (напрямую)"), ("a-restart_s3", "готово (напрямую): S3 перезапускается")):
            top()
            asked = press_confirmed(desc)
            ok, nodes = wait(lambda n: (text(n, "msg-bar") or "").startswith(("готово", "S3 не ответила", "ПК снова")), 25)
            check("3 %s directly (confirmed: %s)" % (desc, bool(asked)), bool(asked) and ok and
                  text(nodes, "msg-bar").startswith(want), (text(nodes, "msg-bar") or "")[:120])
        top()
        ok, nodes = wait(lambda n: text(n, "f-s3") == "на связи" and (text(n, "mode") or "").startswith("напрямую"), 30)
        check("3 the S3 (simulator) back after its restart, still direct", ok, "%s | %s" % (text(nodes, "f-s3_addr"),
                                                                                           text(nodes, "mode")))
        top()
        nodes = reach("a-ota")
        if check("3 'Прошить плату' is visible in direct mode", nodes is not None):
            phone.tap(by_desc(nodes, "a-ota"))
            time.sleep(1.5)
            nodes = dump()
            check("3 flashing refused with its reason, no dialog", by_text(nodes, "Да") is None and
                  (text(nodes, "msg-bar") or "").startswith("прошивка — только через службу"), text(nodes, "msg-bar"))
        time.sleep(3)
        r = subprocess.run([sys.executable, str(BENCH / "bench.py"), "phone-logs", "--delete"] +
                           (["--serial", args.serial] if args.serial else []), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        got = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip().startswith("{") else {}
        lines = 0
        if got.get("out"):
            for f in Path(got["out"]).glob("*.log"):
                lines += sum(1 for _ in f.open(encoding="utf-8"))
        check("3 the log written to the app's file and fetched (bench.py phone-logs)", got.get("files", 0) >= 1 and lines > 20,
              "%s files, %d lines, %s" % (got.get("files"), lines, got.get("out")))

        # 4 an old benchd answers: a PC is on, so no direct mode
        old = Old104(disc)
        top()
        ok, nodes = wait(lambda n: not (text(n, "mode") or "").startswith("напрямую"), 30)
        check("4 an old benchd answers: direct mode left", ok, "%s | %s" % (text(nodes, "mode"), text(nodes, "conn")))
        check("4 the screen says why", "старая версия" in (text(nodes, "conn") or ""), text(nodes, "conn"))
        old.close()
        old = None

        # 5 benchd back on another HTTP port (same home: same token and id)
        ok, nodes = wait(lambda n: (text(n, "mode") or "").startswith("напрямую"), 60)
        check("5 without any PC: direct mode again", ok, text(nodes, "mode"))
        time.sleep(12)          # two searches of direct mode: the line must keep saying "напрямую"
        nodes = dump()
        check("5 the mode line stays during the searches of direct mode", (text(nodes, "mode") or "").startswith("напрямую"),
              text(nodes, "mode"))
        a = Service(home_a, http_a + 10, disc, pc, "pc-a")
        ok, nodes = wait(lambda n: (text(n, "mode") or "").startswith("через службу"), 30)
        check("5 benchd on another port found anew", ok and "%s:%d" % (pc, http_a + 10) in text(nodes, "mode"),
              text(nodes, "mode"))
        time.sleep(6)
        st = a.status()
        check("5 the service has the S3's log (the phone let it go)", st["s3"]["datagrams"] > 0, "datagrams %s" % st["s3"]["datagrams"])
    except Busy as e:
        busy = str(e)
        check("the phone stayed ours", False, busy)
    finally:
        for s in (a, b):
            if s:
                s.close()
        if old:
            old.close()
        sim.close()
    ok = all(c["ok"] for c in checks)
    (out / "summary.json").write_text(json.dumps({"ok": ok, "checks": checks, "busy": busy}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print("%s  %d/%d  %s%s" % ("PASS" if ok else "FAIL", sum(c["ok"] for c in checks), len(checks), out,
                               "  (stopped: %s)" % busy if busy else ""))
    return 3 if busy else 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
