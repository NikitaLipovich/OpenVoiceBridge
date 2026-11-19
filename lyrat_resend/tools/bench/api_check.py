#!/usr/bin/env python3
"""Fast functional check of the live bench through its APIs, ~10 s (+ ~30 s with --sco).

    python lyrat_resend/tools/bench/api_check.py          # S3, LyraT console commands, mute, benchd, log
    python lyrat_resend/tools/bench/api_check.py --sco    # + the audio channel 10 s without a call (sco_probe)

Every check is a real request with a short timeout and a check of the answer's meaning; no screens, no app.
Takes the bench lock only while it runs (when benchd runs). Leaves the board as it found it (mute restored).
Exit code: 0 all OK, 1 something failed, 2 the bench is held by another owner.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import benchd_client  # noqa: E402
import s3_uart  # noqa: E402

rows = []


def check(name, fn):
    t0 = time.monotonic()
    try:
        ok, detail = fn()
    except Exception as e:  # a check that throws is a failed check, with the reason
        ok, detail = False, "%s: %s" % (type(e).__name__, e)
    ms = (time.monotonic() - t0) * 1000.0
    rows.append((name, ok, ms, detail))
    print("  %-4s %-22s %7.0f ms  %s" % ("OK" if ok else "FAIL", name, ms, detail), flush=True)
    return ok


def cmd(ch, text, want):
    r = ch.command(text)
    if r["status"] is None:
        return False, "no answer (%d attempts)" % r["attempts"]
    body = (r["text"] or "").strip()
    if r["status"] != 0:
        return False, "status %s: %s" % (r["status"], body[:80])
    m = re.search(want, body)
    return bool(m), (m.group(0) if m else "unexpected: " + body[:80])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sco", action="store_true", help="also open the audio channel 10 s without a call")
    args = ap.parse_args()
    t_all = time.monotonic()

    lock = benchd_client.Lock("api-check", ttl_s=120, wait_s=0, note="api_check")
    try:
        lock.__enter__()
    except RuntimeError as e:
        print("bench is busy: %s" % e)
        return 2
    try:
        ch = s3_uart.Channel()
        state = {}

        def s3():
            q = ch.query()
            if not q:
                return False, "no answer to UQ"
            ok = q.get("pins_ok") == 1 and q.get("tx_on") == 1
            return ok, "boot=%s up=%ds pins_ok=%s tx_on=%s frames=%s" % (
                q.get("boot"), q.get("up_ms", 0) // 1000, q.get("pins_ok"), q.get("tx_on"), q.get("frames"))
        check("S3 answers (UQ)", s3)
        check("LyraT status", lambda: cmd(ch, "status", r"mute=\w+"))
        check("LyraT mode", lambda: cmd(ch, "mode", r"mode=\w+ .*link=\S+"))
        check("LyraT esco", lambda: cmd(ch, "esco", r"esco mode=\w+"))
        check("LyraT bod (power)", lambda: cmd(ch, "bod", r"trips=\d+"))

        def mute_cycle():
            ok, was = cmd(ch, "mute ?", r"mute=(on|off)")
            if not ok:
                return False, was
            state["mute"] = was.split("=")[1]
            other = "off" if state["mute"] == "on" else "on"
            ok, got = cmd(ch, "mute " + other, r"mute=(on|off)")
            ok2, now = cmd(ch, "mute ?", r"mute=(on|off)")
            ok3, back = cmd(ch, "mute " + state["mute"], r"mute=(on|off)")
            good = ok and ok2 and ok3 and now == "mute=" + other and back == "mute=" + state["mute"]
            return good, "%s -> %s -> %s" % (was, now, back)
        check("LyraT mute toggle", mute_cycle)

        if benchd_client.base_url():
            def benchd():
                st = benchd_client.info(fresh=True)
                if not st:
                    return False, "no /status"
                s3s, ly = st.get("s3") or {}, st.get("lyrat") or {}
                ok = s3s.get("found") and not s3s.get("lost") and not ly.get("lost")
                if "mute" in state and ly.get("mic_muted") is not None:
                    ok = ok and bool(ly["mic_muted"]) == (state["mute"] == "on")
                return bool(ok), "s3 %s rssi=%s, lyrat up=%ss frame %sms ago, mic_muted=%s" % (
                    s3s.get("addr"), s3s.get("rssi"), ly.get("up_s"), ly.get("last_frame_ms"), ly.get("mic_muted"))
            check("benchd /status", benchd)

            def log():
                code, ans = benchd_client.call("GET", "/logs?from=-4s", timeout=3.0)
                lines = (ans or {}).get("lines") or []
                ly = sum(" lyrat " in l for l in lines)
                s3n = sum(" s3 " in l for l in lines)
                return code == 200 and ly > 0, "last 4 s: lyrat %d lines, s3 %d lines" % (ly, s3n)
            check("log stream", log)
        else:
            rows.append(("benchd", True, 0, "not running, skipped"))
            print("  --   benchd                not running, skipped")

        if args.sco:
            def sco():
                st = benchd_client.info(fresh=True) or {}
                if (st.get("lyrat") or {}).get("sco_open"):
                    return True, "skipped: the channel is already open (a call or another check)"
                p = subprocess.run([sys.executable, str(HERE / "call_check.py"), "--probe", "1", "--seconds", "10"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
                tail = [l for l in p.stdout.splitlines() if l.strip()][-6:]
                verdict = next((l for l in tail if "ИТОГ" in l or "PASS" in l or "FAIL" in l), tail[-1] if tail else "")
                return p.returncode == 0, verdict.strip()[:100]
            check("audio channel 10 s", sco)
        ch.close()
    finally:
        lock.__exit__(None, None, None)

    bad = [r for r in rows if not r[1]]
    print("%s: %d checks, %d failed, %.1f s" % ("PASS" if not bad else "FAIL", len(rows), len(bad),
                                                time.monotonic() - t_all))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
