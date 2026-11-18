#!/usr/bin/env python3
"""The "Стенд" screen of sco_probe on the phone against benchd on this PC (goal 104). No board needed: the S3 is the
simulator. Not part of run_tests.py (that one never uses adb).

  bench_screen_check.py [--serial S] [--offset 900] [--no-build] [--out DIR]

Steps: build and install sco_probe (build_apk.py --install: adb install -r of our app only), start s3_sim.py and
benchd.py --lan --lan-control on BENCH_PORT_OFFSET=--offset with a temporary home (its own LAN token), hand the address
and the token to the app (bench.py phone-link), then read the screen with uiautomator dump: the boards' status, the
service line, the events. Presses only what is safe: "Обновить", and "Взять стенд себе" / "Отдать мой замок" of the
TEST benchd (its lock: no board, nothing real is held), which proves the token over the Wi-Fi. No dangerous button is
pressed, no phone setting is touched (R4), no sound. Writes <out>/summary.json and the screen dumps.
Exit code: 0 all checks passed, 1 a check failed, 2 the phone or a tool is missing.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
BENCH = TOOLS / "bench"
sys.path.insert(0, str(BENCH))
from benchlib import RUNS_ROOT  # noqa: E402

APP = "com.openvoicebridge.scoprobe"
DUMP = "/sdcard/window_dump_bench.xml"


class Phone:
    def __init__(self, serial):
        self.serial = serial

    def adb(self, *args, timeout=60, check=False):
        cmd = ["adb"] + (["-s", self.serial] if self.serial else []) + list(args)
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError("%s: %s" % (" ".join(args[:3]), r.stderr.strip() or r.stdout.strip()))
        return r

    def dump(self, path):
        """The nodes of our app on the screen: [{desc, text, bounds, cls}]."""
        # The screen polls benchd every second, so uiautomator may not see it idle: try again.
        for attempt in range(12):
            r = self.adb("shell", "uiautomator", "dump", DUMP, timeout=30)
            if r.returncode == 0 and "dumped to" in r.stdout:
                break
            time.sleep(1.0)
        else:
            raise RuntimeError("uiautomator dump failed: %s" % (r.stdout.strip() or r.stderr.strip()))
        self.adb("pull", DUMP, str(path), check=True)
        self.adb("shell", "rm", "-f", DUMP)
        nodes = []
        for n in ET.parse(path).iter("node"):
            if n.get("package") != APP:
                continue
            nodes.append({"desc": n.get("content-desc", ""), "text": n.get("text", ""), "bounds": n.get("bounds", ""),
                          "cls": n.get("class", "")})
        return nodes

    def tap(self, node):
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", node["bounds"]))
        self.adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2), check=True)

    def scroll(self, down=True):
        y1, y2 = (1500, 700) if down else (700, 1500)
        self.adb("shell", "input", "swipe", "500", str(y1), "500", str(y2), "300", check=True)


def by_desc(nodes, desc):
    return next((n for n in nodes if n["desc"] == desc), None)


def by_text(nodes, text):
    return next((n for n in nodes if n["text"].lower() == text.lower()), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--serial")
    ap.add_argument("--offset", type=int, default=900, help="BENCH_PORT_OFFSET of the test benchd")
    ap.add_argument("--no-build", action="store_true", help="the app is already installed")
    ap.add_argument("--out")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    out = Path(args.out) if args.out else RUNS_ROOT / "bench_screen" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    phone = Phone(args.serial)
    checks = []

    def check(name, ok, note=""):
        checks.append({"name": name, "ok": bool(ok), "note": note})
        print("%s  %s  %s" % ("PASS" if ok else "FAIL", name, note), flush=True)
        return ok

    devs = phone.adb("devices").stdout
    if "\tdevice" not in devs:
        print("no phone on adb", file=sys.stderr)
        return 2
    if not args.no_build:
        r = subprocess.run([sys.executable, str(HERE / "build_apk.py"), "--install"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        (out / "build.txt").write_text(r.stdout + r.stderr, encoding="utf-8")
        if not check("build and install sco_probe", r.returncode == 0, r.stderr.strip()[-200:]):
            return 1

    os.environ["BENCH_PORT_OFFSET"] = str(args.offset)
    os.environ["BENCH_NO_ADB"] = "1"
    sys.path.insert(0, str(TOOLS / "tests"))
    import simproc  # noqa: E402
    ota_token = re.search(r'"(.+)"', simproc.TOKEN.read_text(encoding="utf-8")).group(1)
    home = Path(tempfile.mkdtemp(prefix="benchscreen_"))
    env = dict(os.environ, BENCH_S3_ADDR="127.0.0.1", BENCHD_HOME=str(home), BENCH_OTA_TOKEN=ota_token,
               PYTHONIOENCODING="utf-8")
    sim = simproc.Sim()
    # Goal 105: the app finds benchd by itself; phone-link hands only the token (and this offset's ports).
    d = subprocess.Popen([sys.executable, str(BENCH / "benchd.py"), "--home", str(home), "--poll-s", "1", "--lan",
                          "--lan-control"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        run = json.loads(d.stdout.readline())
        url = run["url"]

        def status():
            with urllib.request.urlopen(url + "/status", timeout=5) as r:
                return json.loads(r.read())
        time.sleep(3)
        r = subprocess.run([sys.executable, str(BENCH / "bench.py"), "phone-link"] +
                           (["--serial", args.serial] if args.serial else []), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        if not check("bench.py phone-link", r.returncode == 0, (r.stdout + r.stderr).strip()[-200:]):
            return 1
        phone.adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        time.sleep(6)
        nodes = phone.dump(out / "screen1.xml")
        s3 = by_desc(nodes, "f-s3")
        check("the screen is open (S3 row)", s3 is not None)
        check("S3 'на связи' from benchd + simulator", s3 and s3["text"] == "на связи", s3 and s3["text"])
        svc = by_desc(nodes, "svc")
        check("service line", svc and svc["text"].startswith("служба: pid %s" % run["pid"]), svc and svc["text"])
        conn = by_desc(nodes, "conn")
        # Goal 105: the app finds the PC itself; the line says where.
        check("found the PC by itself", conn is not None and conn["text"].startswith("найден ПК")
              and ":%d (" % run["http_port"] in conn["text"], conn and conn["text"])
        lan = by_desc(nodes, "f-lan")
        check("LAN control shown as on", lan and lan["text"].startswith("включено"), lan and lan["text"])
        up_before = (by_desc(nodes, "f-s3_addr") or {}).get("text", "")
        refresh = by_desc(nodes, "refresh")
        if check("'Обновить' is there", refresh is not None):
            phone.tap(refresh)
            time.sleep(3)
            nodes = phone.dump(out / "screen2.xml")
            up_after = (by_desc(nodes, "f-s3_addr") or {}).get("text", "")
            check("refresh: S3 uptime moved", up_after and up_after != up_before, "%s -> %s" % (up_before, up_after))

        # The lock of the TEST benchd: a POST over the Wi-Fi with the token.
        take = None
        for _ in range(4):
            take = by_desc(nodes, "a-take")
            if take:
                break
            phone.scroll()
            time.sleep(1)
            nodes = phone.dump(out / "screen_scroll.xml")
        if check("'Взять стенд себе' is there", take is not None):
            phone.tap(take)
            time.sleep(2)
            dlg = phone.dump(out / "screen_confirm.xml")
            yes = by_text(dlg, "Да")
            check("the confirmation asks first", yes is not None and any("Гоалы будут ждать" in n["text"] for n in dlg))
            if yes:
                phone.tap(yes)
                time.sleep(3)
                holder = status()["lock"]["holder"] or {}
                check("lock taken from the phone (token over the LAN)",
                      holder.get("owner") == "owner" and holder.get("note") == "владелец (телефон)", json.dumps(holder,
                                                                                                         ensure_ascii=False))
                nodes = phone.dump(out / "screen3.xml")
                msg = by_desc(nodes, "msg-bar")
                check("the answer on the screen", msg and msg["text"] == "стенд ваш на 1 час", msg and msg["text"])
                give = by_desc(nodes, "a-give")
                for _ in range(4):           # goal 105: three more lines on top, the button may be below the screen
                    if give:
                        break
                    phone.scroll()
                    time.sleep(1)
                    nodes = phone.dump(out / "screen_give.xml")
                    give = by_desc(nodes, "a-give")
                if give:
                    phone.tap(give)
                    time.sleep(3)
                check("lock given back from the phone", not status()["lock"]["holder"])
        # "Найти ПК": the phone's UDP broadcast "BENCHD? 2 <nonce>", the test benchd answers signed (goal 105).
        for _ in range(8):
            find = by_text(nodes, "Найти ПК")
            if find:
                break
            phone.scroll(down=False)
            time.sleep(1)
            nodes = phone.dump(out / "screen_top.xml")
        if check("'Найти ПК' is there", find is not None):
            phone.tap(find)
            time.sleep(4)
            nodes = phone.dump(out / "screen_find.xml")
            conn = by_desc(nodes, "conn")
            check("the phone found the PC by UDP", conn and conn["text"].startswith("найден ПК")
                  and ":%d (поиск в сети)" % run["http_port"] in conn["text"], conn and conn["text"])
        for _ in range(6):
            ev = by_desc(nodes, "feed-events")
            if ev and ev["text"]:
                break
            phone.scroll()
            time.sleep(1)
            nodes = phone.dump(out / "screen_feed.xml")
        ev = by_desc(nodes, "feed-events")
        check("events feed shows the test benchd's events", ev and "lock" in ev["text"], ev and ev["text"][:120])
    finally:
        try:
            req = urllib.request.Request(url + "/shutdown", data=b"{}", method="POST", headers={"X-Bench": "1"})
            urllib.request.urlopen(req, timeout=5)
            d.wait(timeout=8)
        except Exception:
            d.kill()
        sim.close()
    ok = all(c["ok"] for c in checks)
    (out / "summary.json").write_text(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print("%s  %d/%d  %s" % ("PASS" if ok else "FAIL", sum(c["ok"] for c in checks), len(checks), out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
