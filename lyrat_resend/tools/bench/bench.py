#!/usr/bin/env python3
"""bench.py: the command line of the bench service benchd (goal 103). Guide: lyrat_resend/docs/BENCHD.md.

  bench.py status [--json]                         boards, lock, jobs, events of the last hour
  bench.py logs [--from -10m] [--to T] [--board lyrat|s3] [--grep RE] [--limit N]    lines from benchd's files
  bench.py tail [--board B] [--grep RE] [--follow]  the newest lines (--follow: until Ctrl+C)
  bench.py events [--from -1h] [--to T] [--type slc_down,boot] [--limit N]           one JSON per line
  bench.py cmd "TEXT" [--s3]                        a LyraT console command through the S3 (--s3: query, restart,
                                                   power [N], tx 0|1|A), written to the event index
  bench.py ota --board lyrat|s3|both [--variant V | --image FILE] [--via auto|wifi|uart] [--wait]   fw.py flash as a job
  bench.py job ID [--wait]                         a job's state and the end of its output
  bench.py lock show | wait [--timeout S]
  bench.py lock acquire --owner G [--ttl S] [--wait S] [--note TEXT]    prints the token
  bench.py lock renew|release --owner G [--token T]
  bench.py lock run --owner G [--ttl S] [--wait S] [--note TEXT] -- COMMAND...   take the lock, run, give it back
Time: ISO, HH:MM[:SS] today, or -30s/-10m/-2h. --owner defaults to BENCH_LOCK_OWNER, --token to BENCH_LOCK_TOKEN.
"lock run" renews the lock while COMMAND runs and gives the child BENCH_LOCK_OWNER/BENCH_LOCK_TOKEN, so its bench
tools see the bench as theirs (owner_common.bench_busy); when bench.py dies the lock goes with its pid.
Exit code: 0 done, 1 refused or failed (the lock held by another: its holder is printed), 2 benchd not running;
lock run: the exit code of COMMAND, or 1 when the lock could not be had.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchd_client as bc  # noqa: E402


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def api(method, path, body=None, timeout=30.0):
    code, ans = bc.call(method, path, body, timeout=timeout)
    if code is None:
        print("benchd is not running (%s)" % ans.get("error"), file=sys.stderr)
        sys.exit(2)
    return code, ans


def query(path, **kw):
    kw = {k: v for k, v in kw.items() if v not in (None, "")}
    return path + ("?" + urlencode(kw) if kw else "")


def brief(st):
    s3, ly, lock = st["s3"], st["lyrat"], st["lock"]
    lines = ["S3     %s  %s  boot=%s up=%ss rssi=%s net=%s ver=%s" % (
        s3.get("addr"), "ПРОПАЛА" if s3.get("lost") else "на связи", s3.get("boot"), s3.get("up_s"), s3.get("rssi"),
        s3.get("net"), s3.get("ver")),
        "LyraT  %s  up=%ss last_frame=%sms sco=%s ver=%s" % (
        "ПРОПАЛА" if ly.get("lost") else "на связи", ly.get("up_s"), ly.get("last_frame_ms"),
        "открыт" if ly.get("sco_open") else "нет", ly.get("ver")),
        "Телефон %s" % ", ".join("%s=%s" % kv for kv in st.get("phone", {}).items())]
    h = lock.get("holder")
    lines.append("Замок  %s%s" % ("у %s до %s (%s)" % (h["owner"], h["until"][11:16], h.get("note") or "") if h else "свободен",
                                  "; очередь: " + ", ".join(q["owner"] for q in lock["queue"]) if lock["queue"] else ""))
    for j in st.get("jobs", []):
        lines.append("Задание %s %s (%s)" % (j["id"], j["kind"], j["owner"]))
    ev = st.get("events_last_hour") or {}
    lines.append("События за час: %s" % (", ".join("%s %d" % kv for kv in sorted(ev.items())) or "нет"))
    lines.append("Лог: %s строк, файл %s" % (st["benchd"]["counts"]["lines"], st["benchd"].get("file")))
    return "\n".join(lines)


def cmd_lock(args):
    owner = args.owner or os.environ.get("BENCH_LOCK_OWNER")
    token = getattr(args, "token", None) or os.environ.get("BENCH_LOCK_TOKEN")
    if args.what == "show":
        out(api("GET", "/lock")[1])
        return 0
    if args.what == "wait":
        end = time.monotonic() + args.timeout
        while True:
            st = api("GET", "/lock")[1]
            if not st.get("holder"):
                out(st)
                return 0
            if time.monotonic() >= end:
                out(st)
                return 1
            time.sleep(1.0)
    if not owner:
        print("--owner is needed (or BENCH_LOCK_OWNER)", file=sys.stderr)
        return 1
    if args.what == "acquire":
        code, ans = api("POST", "/lock", {"owner": owner, "ttl_s": args.ttl, "wait_s": args.wait, "note": args.note},
                        timeout=args.wait + 15)
        out(ans)
        return 0 if code == 200 else 1
    if args.what == "renew":
        code, ans = api("POST", "/lock/renew", {"owner": owner, "token": token, "ttl_s": args.ttl})
        out(ans)
        return 0 if code == 200 else 1
    if args.what == "release":
        code, ans = api("POST", "/lock/release", {"owner": owner, "token": token})
        out(ans)
        return 0 if code == 200 else 1
    # run
    command = args.command
    if not command:
        print("lock run: a command after --", file=sys.stderr)
        return 1
    code, ans = api("POST", "/lock", {"owner": owner, "ttl_s": args.ttl, "wait_s": args.wait, "pid": os.getpid(),
                                      "note": args.note or " ".join(Path(c).name for c in command[:3])},
                    timeout=args.wait + 15)
    if code != 200:
        out(ans)
        return 1
    token = ans["token"]
    stop = threading.Event()

    def keep():
        while not stop.wait(max(5.0, args.ttl / 3.0)):
            bc.renew(owner, token, args.ttl)
    threading.Thread(target=keep, daemon=True).start()
    env = dict(os.environ, BENCH_LOCK_OWNER=owner, BENCH_LOCK_TOKEN=token)
    try:
        rc = subprocess.call(command, env=env)
    except OSError as e:
        print("lock run: %s" % e, file=sys.stderr)
        rc = 1
    except KeyboardInterrupt:
        rc = 130
    finally:
        stop.set()
        bc.release(owner, token)
    return rc


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("logs")
    p.add_argument("--from", dest="t_from", default="-10m")
    p.add_argument("--to", dest="t_to")
    p.add_argument("--board")
    p.add_argument("--grep")
    p.add_argument("--limit", type=int, default=2000)
    p = sub.add_parser("tail")
    p.add_argument("--board")
    p.add_argument("--grep")
    p.add_argument("--follow", action="store_true")
    p = sub.add_parser("events")
    p.add_argument("--from", dest="t_from", default="-1h")
    p.add_argument("--to", dest="t_to")
    p.add_argument("--type")
    p.add_argument("--limit", type=int, default=1000)
    p = sub.add_parser("cmd")
    p.add_argument("text")
    p.add_argument("--s3", action="store_true")
    p.add_argument("--owner")
    p.add_argument("--force", action="store_true", help="even while another owner holds the lock (written as such)")
    p = sub.add_parser("ota")
    p.add_argument("--board", choices=["lyrat", "s3", "both"], required=True)
    p.add_argument("--variant")
    p.add_argument("--image")
    p.add_argument("--via", choices=["auto", "wifi", "uart"])
    p.add_argument("--owner")
    p.add_argument("--wait", action="store_true")
    p = sub.add_parser("job")
    p.add_argument("id")
    p.add_argument("--wait", action="store_true")
    p = sub.add_parser("lock")
    p.add_argument("what", choices=["show", "wait", "acquire", "renew", "release", "run"])
    p.add_argument("--owner")
    p.add_argument("--token")
    p.add_argument("--ttl", type=float, default=900.0)
    p.add_argument("--wait", type=float, default=0.0)
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--note", default="")
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in argv:                 # lock run ... -- COMMAND: everything after "--" is the child's
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    for i, a in enumerate(argv[:-1]):
        if a in ("--from", "--to") and argv[i + 1].startswith("-"):
            argv[i:i + 2] = [a + "=" + argv[i + 1], ""]     # "-10m" is a value, not an option
    args = ap.parse_args([a for a in argv if a != ""])
    args.command = command

    if args.cmd == "status":
        st = api("GET", "/status")[1]
        print(json.dumps(st, ensure_ascii=False, indent=1) if args.json else brief(st))
        return 0
    if args.cmd == "logs":
        ans = api("GET", query("/logs", **{"from": args.t_from, "to": args.t_to, "board": args.board,
                                          "grep": args.grep, "limit": args.limit}))[1]
        if "lines" not in ans:
            out(ans)
            return 1
        print("\n".join(ans["lines"]))
        if ans.get("truncated"):
            print("(only the last %d lines)" % args.limit, file=sys.stderr)
        return 0
    if args.cmd == "tail":
        after = 0
        first = True
        while True:
            ans = api("GET", query("/tail", after=after, board=args.board, grep=args.grep,
                                   limit=50 if first else 500))[1]
            if ans.get("lines"):
                print("\n".join(ans["lines"]), flush=True)
            after = ans.get("last_id", after)
            first = False
            if not args.follow:
                return 0
            try:
                time.sleep(1.0)
            except KeyboardInterrupt:
                return 0
    if args.cmd == "events":
        ans = api("GET", query("/events", **{"from": args.t_from, "to": args.t_to, "type": args.type,
                                            "limit": args.limit}))[1]
        for ev in ans.get("events", []):
            print(json.dumps(ev, ensure_ascii=False))
        return 0 if "events" in ans else 1
    owner = getattr(args, "owner", None) or os.environ.get("BENCH_LOCK_OWNER")
    token = os.environ.get("BENCH_LOCK_TOKEN")
    if args.cmd == "cmd":
        code, ans = api("POST", "/cmd", {"target": "s3" if args.s3 else "lyrat", "cmd": args.text, "owner": owner,
                                         "token": token, "force": args.force})
        out(ans)
        return 0 if code == 200 and ans.get("status") == 0 else 1
    if args.cmd == "ota":
        code, ans = api("POST", "/ota", {"board": args.board, "variant": args.variant, "image": args.image,
                                         "via": args.via, "owner": owner, "token": token})
        if code != 202 or not args.wait:
            out(ans)
            return 0 if code == 202 else 1
        args.id = ans["id"]
        args.cmd = "job"
    if args.cmd == "job":
        while True:
            code, ans = api("GET", "/jobs/%s" % args.id)
            if code != 200 or not args.wait or ans.get("ended"):
                out(ans)
                return 0 if code == 200 and ans.get("exit") in (0, None) else 1
            time.sleep(2.0)
    if args.cmd == "lock":
        return cmd_lock(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
