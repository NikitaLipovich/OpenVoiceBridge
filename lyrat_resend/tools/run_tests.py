#!/usr/bin/env python3
"""Every check of this bench that runs on the PC alone, with one command. No board, no phone, no COM port, no adb, no sound.

  python lyrat_resend/tools/run_tests.py            all checks, firmware builds included
  python lyrat_resend/tools/run_tests.py --quick    without the builds of the firmwares and of the Android probe
  python lyrat_resend/tools/run_tests.py --only s3_sim,bench_tools    some checks (names: --list)
  python lyrat_resend/tools/run_tests.py --quick --port-offset 100     on ports 5104..5110 (BENCH_PORT_OFFSET): another
                                                                      worktree's run on the default ports is not met

Prints PASS / FAIL / SKIP per check and writes goals_harness/bench_runs/tests/<stamp>/summary.json with each check's
status, time, log file and notes, and the log of each check beside it. Exit code 0 only when every check that ran passed
and none was skipped in a full run; --quick may skip the checks it leaves out on purpose.

The OTA tests need two images of each board: the full run makes them from its own firmware builds (the "ota" one a variant
with another version and ELF SHA, tests/images.py); --quick takes the builds of the last full run, else synthetic images,
and says which in summary.json. Each check runs in a job object of its own (tests/procjob.py, goal 107): its whole process
tree ends with it - after PASS or FAIL (leftovers are killed and named), at its timeout, on Ctrl+C (exit 130) and when this
runner dies. Docs: lyrat_resend/docs/TESTS.md. Made by goal 092.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
LYRAT = TOOLS.parent
REPO = LYRAT.parent
RUNS = REPO / "goals_harness" / "bench_runs" / "tests"
BUILDS = RUNS / "builds"
TESTS = TOOLS / "tests"
OTA_TEST = TOOLS / "ota_test"
PLATFORM = TOOLS / "platform"
PY = sys.executable

sys.path.insert(0, str(TESTS))
import images  # noqa: E402
import procjob  # noqa: E402


def unittest_cmd(module):
    return [PY, "-m", "unittest", "-v", module]


# name, what it proves, command, cwd, timeout s, kind ("build" checks are left out by --quick), needs images
CHECKS = [
    ("host_dll", "the firmware's protocol C code (ota_link, ota_sha256, dbg_log, ctl_frame) builds with MSVC /W4 /WX",
     ["cmd", "/c", str(OTA_TEST / "build_host.cmd")], OTA_TEST, 300, "base", False),
    ("platform_dll", "the platform's ROM loader C code builds with MSVC /W4 /WX",
     ["cmd", "/c", str(PLATFORM / "build_host.cmd")], PLATFORM, 300, "base", False),
    ("static", "no secret in git, .gitignore covers the secret files, the S3 builds for Wi-Fi by default",
     [PY, str(TESTS / "static_check.py")], TESTS, 120, "base", False),
    ("fw_build", "S3, LyraT and its updater built in their own folders: table, rollback, size in the slot, whole image, "
                 "secrets, Wi-Fi only in the updater; working build untouched",
     [PY, str(TESTS / "fw_build_check.py"), "--out", str(BUILDS)], TESTS, 7200, "build", False),
    ("sco_probe", "the Android probe app builds with the plain SDK tools (never installed here)",
     [PY, str(TOOLS / "android" / "build_apk.py"), "--out", "{run}/apk"], TOOLS / "android", 600, "build", False),
    ("c_ctl_frame", "UART framing in C: limits of the length, CRC and header damage, resync, pauses, repeats",
     unittest_cmd("test_ctl_frame"), TESTS, 120, "base", False),
    ("c_dbg_log", "log ring in C: long lines in parts and their join, ring full mid-line, repeats; old and new stat line; "
                  "a failed UDP send keeps its lines",
     unittest_cmd("test_dbg_log"), TESTS, 120, "base", False),
    ("c_afh_map", "the AFH map of the LyraT from the S3's Wi-Fi: bands, floor, hysteresis, the ack report, the policy",
     unittest_cmd("test_afh_map"), TESTS, 120, "base", False),
    ("fw_review", "findings of the review of goal 094: soft limiter in C, Wi-Fi retry pause, self-test only once, "
                  "UX delay in any build, noise mode at start",
     unittest_cmd("test_fw_review"), TESTS, 120, "base", False),
    ("upd_rules", "the LyraT's updater in C (upd_mode.c): firmware state from the NVS marks, the 10/120 s window, a new "
                  "image at once, a bad one kept out and retried, the command upd; no Wi-Fi call in the headset firmware",
     unittest_cmd("test_upd_mode"), TESTS, 120, "base", False),
    ("ota_protocol", "OTA protocol C against Python: loss, reorder, bit errors, token, rollback rules, log ring",
     unittest_cmd("test_protocol"), OTA_TEST, 900, "base", True),
    ("ota_tools", "ota.py of both boards (dry run, update, rollback, wrong token and board) and udplog.py vs the emulator",
     unittest_cmd("test_tools"), OTA_TEST, 1800, "base", True),
    ("s3_sim", "the S3 simulator against the firmware source: ports, JSON keys, stream header, stat line, relay rules",
     unittest_cmd("test_s3_sim"), TESTS, 300, "base", True),
    ("bench_tools", "s3_uart.py, ota.py --dry-run, udplog.py, udp_record.py --tap end to end against the simulator",
     unittest_cmd("test_bench_tools"), TESTS, 1200, "base", True),
    ("bench_unit", "fw.py variants, sound analysis on synthetic WAV, phone_mic.level, every bench module loads",
     unittest_cmd("test_bench_unit"), TESTS, 300, "base", False),
    ("usb_free", "the bench without USB vs the simulator: first_flash --dry-run, NetWatch, link trials, round power, "
                 "fw.py/s3.py/s3_uart.py over the network, Wi-Fi power in stat, WAV silence by frame, call_record",
     unittest_cmd("test_usb_free"), TESTS, 1500, "base", True),
    ("call_check", "the owner's test call vs the simulator: checks, channel seen, both lines aligned, report; quality numbers "
                   "on synthetic lines (tone, silence, cut pieces, clipping, noise, shifted lines); live pipeline, PCM output "
                   "and its latency (goal 097)",
     unittest_cmd("test_call_check"), TESTS, 900, "base", True),
    ("call_listen", "the live stream cut into utterances (goal 101): borders against synthetic speech in quiet and noise, "
                    "silence, clicks, a background step, overlap, the 30 s cut, missing sound and lost blocks, files bit for "
                    "bit, the recogniser hook, CPU; a dropped stream and its reconnect; a scenario played by the simulator "
                    "through call_check to call_listen",
     unittest_cmd("test_call_listen"), TESTS, 600, "base", True),
    ("phone_compare", "the LyraT mic before Bluetooth against the phone's recording: alignment (offset, clock drift, a jump of "
                      "the time line, no convergence), lost frames knocked out at known times found within 7.5 ms with "
                      "their type, files and report, the check against tone_blocks, call_check --compare plumbing (goal 099)",
     unittest_cmd("test_phone_compare"), TESTS, 300, "base", False),
    ("updater", "the LyraT through its updater vs the simulator: image over Wi-Fi, a bad image kept out, no network "
                "then the UART way, fw.py --via auto, every start through the updater (goal 096)",
     unittest_cmd("test_updater"), TESTS, 1500, "base", True),
    ("benchd", "the bench service vs the simulator (goal 103): log of both boards in rotated files, event index (start "
               "and reason, SLC, SCO, commands, a lost S3), API and CLI, the log relay to two udplog.py and NetWatch at "
               "once, the bench lock (refusal, queue, expiry, dead pid, lock run, bench_busy), an update as a job, the "
               "admin page and its POST rules, --lan read only; tools without the service; two simulators on two port "
               "offsets",
     unittest_cmd("test_benchd"), TESTS, 900, "base", True),
    ("bench_ui_check", "the bench UI spec, its generated web page and phone screen, and benchd's routes agree "
                       "(gen_bench_ui.py --check --strict: (a) generated files, (b) routes and parameters, (c) status paths "
                       "against benchd + the simulator, (d) every control route has a button; goal 104)",
     [PY, str(TOOLS / "bench" / "gen_bench_ui.py"), "--check", "--strict"], TOOLS / "bench", 300, "base", True),
    ("bench_ui", "the bench UI generator (goal 104): deterministic, --check catches (a)-(d) on broken copies, the template "
                 "language the same in Python, the page's JS (node) and the phone's Java (JVM), the page renders against "
                 "benchd + simulator, the LAN token, UDP discovery",
     unittest_cmd("test_bench_ui"), TESTS, 900, "base", True),
    ("bench_find", "the phone finds benchd and the S3 itself (goal 105): the phone's Java (JVM) against real benchd "
                   "processes - signed answer, foreign and old services not taken, cache only a hint, another port found "
                   "anew - and direct mode against the simulator (UQ, log by LOG1/LOG0, UC, UX), its status = s3_direct.py, "
                   "log parsing = udplog, every write to the S3 only in direct mode",
     unittest_cmd("test_bench_find"), TESTS, 600, "base", False),
    ("platform_rom", "platform: ROM loader in C against the esptool protocol model",
     [PY, "test_rom.py"], PLATFORM, 900, "base", False),
    ("platform_tools", "platform: platform_ota.py and udplog.py against the U3 emulator",
     [PY, "test_platform_tools.py"], PLATFORM, 1800, "base", False),
    ("platform_pins", "platform: wiring table against the pin rules, with mutations that must be caught",
     [PY, "plat_pins_check.py", "--mutate"], PLATFORM, 120, "base", False),
    # Keep last: it also proves no process of the checks above still holds ota_test/build/otahost.dll.
    ("child_cleanup", "what a run starts ends with it (goal 107): fake checks that pass, fail, crash or time out, Ctrl+C "
                      "and a killed runner leave no process and no busy DLL; processes outside the run and the bench "
                      "service untouched; a test process run alone takes its children with it; otahost.dll free",
     unittest_cmd("test_child_cleanup"), TESTS, 300, "base", False),
]
NAMES = [c[0] for c in CHECKS]


def prepare_images(run_dir: Path):
    """OTA_TEST_BUILDS for the tests: from the test builds when they exist, else synthetic. Returns (dir, kinds)."""
    s3 = BUILDS / "s3" / "s3_eth_bridge.bin"
    ly = BUILDS / "lyrat" / "lyrat_resend.bin"
    d = run_dir / "images"
    kinds = images.prepare(d, s3 if s3.is_file() else None, ly if ly.is_file() else None)
    return d, kinds


def unittest_counts(text):
    ran = re.search(r"^Ran (\d+) tests?", text, re.M)
    skipped = re.search(r"skipped=(\d+)", text)
    return (int(ran.group(1)) if ran else None), (int(skipped.group(1)) if skipped else 0)


def proc_names(pids):
    """Image names of these pids, for the note about leftovers (best effort)."""
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    names = {}
    for line in out.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) > 1 and cells[1].isdigit() and int(cells[1]) in pids:
            names[int(cells[1])] = cells[0]
    return names


def wait_in_steps(p, deadline):
    """p.wait() till the deadline in short steps: one long WaitForSingleObject does not wake for Ctrl+C on Windows,
    the KeyboardInterrupt would only come when the check ends by itself."""
    while True:
        try:
            return p.wait(timeout=max(0.0, min(0.25, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                raise


def run_check(check, run_dir: Path, env):
    """One check in a job of its own (tests/procjob.py, goal 107): whatever it starts dies with it - after PASS, FAIL,
    its timeout, Ctrl+C, or this runner's own death - and processes left behind are named in the result."""
    name, what, cmd, cwd, timeout, kind, _ = check
    cmd = [c.replace("{run}", str(run_dir)) for c in cmd]
    log = run_dir / (name + ".log")
    out_f, err_f = run_dir / (name + ".stdout.tmp"), run_dir / (name + ".stderr.tmp")
    t0 = time.monotonic()
    leftover, interrupted = [], False
    job = procjob.Job()
    try:
        # Output into files, not pipes: a grandchild still holding a pipe would keep a reader waiting for ever.
        with open(out_f, "wb") as fo, open(err_f, "wb") as fe:
            try:
                p = procjob.popen(job, cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, env=env)
            except OSError as e:
                p = None
                code, head = None, "could not start: %s" % e
            if p is not None:
                try:
                    code, head = wait_in_steps(p, t0 + timeout), ""
                except subprocess.TimeoutExpired:
                    code, head = None, "TIMEOUT after %d s\n" % timeout
                except KeyboardInterrupt:
                    code, head, interrupted = None, "INTERRUPTED (Ctrl+C) after %.0f s\n" % (time.monotonic() - t0), True
                if code is None or job.pids():
                    leftover = [x for x in job.terminate() if x != p.pid]     # the check itself is not a leftover
                    p.wait()
    finally:
        job.close()
    names = proc_names(set(leftover)) if leftover else {}
    read = lambda f: f.read_bytes().decode("utf-8", "replace") if f.exists() else ""
    stdout, stderr = read(out_f), read(err_f)
    for f in (out_f, err_f):
        try:
            f.unlink()
        except OSError:
            pass
    if head.startswith(("TIMEOUT", "INTERRUPTED")):
        stdout = stdout[-5000:]
    out = head + stdout + ("\n--- stderr ---\n" + stderr if stderr else "")
    if leftover:
        out += "\n--- killed with the check's job (goal 107) ---\n" + "".join(
            "%d %s\n" % (x, names.get(x, "?")) for x in leftover)
    seconds = round(time.monotonic() - t0, 1)
    log.write_text(out, encoding="utf-8")
    ran, skipped = unittest_counts(out)
    result = {"name": name, "what": what, "status": "PASS" if code == 0 else "FAIL", "exit": code, "seconds": seconds,
              "log": log.name}
    if leftover:
        result["leftover"] = ["%s:%d" % (names.get(x, "?"), x) for x in leftover]
    if interrupted:
        result["interrupted"] = True
    if ran is not None:
        result["tests"] = ran
        result["skipped"] = skipped
        if skipped:
            result["status"] = "FAIL"           # a skipped case proves nothing: the runner gives what it needs
            result["note"] = "%d test(s) skipped, see the log" % skipped
    if name == "fw_build":
        report = BUILDS / "fw_build.json"
        if report.exists():
            rep = json.loads(report.read_text(encoding="utf-8"))
            result["detail"] = {b: {k: v.get("pass") for k, v in e.get("checks", {}).items()} |
                                {"build_s": e.get("build", {}).get("seconds"),
                                 "slot_use_pct": e.get("checks", {}).get("size", {}).get("use_pct"),
                                 "ccache_trap": bool(e.get("rebuild"))}
                                for b, e in rep["boards"].items()}
            result["detail"]["untouched"] = rep["untouched"]
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="leave out the firmware and APK builds")
    ap.add_argument("--only", help="comma separated names of checks, see --list")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", help="folder instead of goals_harness/bench_runs/tests/<stamp>")
    ap.add_argument("--port-offset", type=int, default=None,
                    help="BENCH_PORT_OFFSET for the whole run: parallel runs (worktrees) on different offsets do not meet")
    ap.add_argument("--check-file", help=argparse.SUPPRESS)   # JSON list of checks instead of CHECKS: the cleanup test's fakes
    args = ap.parse_args()
    checks = list(CHECKS)
    if args.check_file:
        checks = [(c["name"], c.get("what", "test of the runner"), c["cmd"], Path(c.get("cwd", ".")), c["timeout"],
                   "base", False) for c in json.loads(Path(args.check_file).read_text(encoding="utf-8"))]
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)   # Ctrl+Break stops the run like Ctrl+C
    if sys.platform == "win32":
        # A process started with Ctrl+C ignored (CREATE_NEW_PROCESS_GROUP somewhere above: background shells, IDE
        # tasks) passes that on: Ctrl+C would not stop the run. Take it back (goal 107).
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if args.list:
        for c in checks:
            print("%-15s %-6s %s" % (c[0], c[5], c[1]))
        return 0
    only = [n.strip() for n in args.only.split(",")] if args.only else None
    if only:
        unknown = [n for n in only if n not in [c[0] for c in checks]]
        if unknown:
            ap.error("unknown check(s) %s; known: %s" % (", ".join(unknown), ", ".join(NAMES)))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out) if args.out else RUNS / (stamp + ("-quick" if args.quick else ""))
    run_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("MSYSTEM", "BENCH_S3_ADDR", "BENCH_U3_ADDR")}
    env["PYTHONIOENCODING"] = "utf-8"
    env["BENCH_NO_ADB"] = "1"       # the phone is never touched by a test (goal 093)
    env["BENCH_NO_BENCHD"] = "1"    # a bench service running on this PC is not used by the tests (test_benchd starts its own)
    env.pop("BENCHD_URL", None)
    if args.port_offset is not None:
        env["BENCH_PORT_OFFSET"] = str(args.port_offset)
    env.pop("BENCH_USB", None)
    env["OTA_TEST_OUT"] = str(run_dir / "ota_test_runs")
    summary = {"stamp": stamp, "mode": "quick" if args.quick else "full", "only": only, "out": str(run_dir),
               "port_offset": int(env.get("BENCH_PORT_OFFSET", "0") or 0),
               "python": sys.version.split()[0], "checks": []}
    t_all = time.monotonic()
    image_kinds = None
    print("tests -> %s" % run_dir, flush=True)
    interrupted = False
    for check in checks:
        name, what, _, _, _, kind, needs_images = check
        if only and name not in only:
            continue
        try:
            if args.quick and kind == "build":
                res = {"name": name, "what": what, "status": "SKIP", "note": "--quick leaves out the builds"}
            else:
                if needs_images and image_kinds is None:
                    d, image_kinds = prepare_images(run_dir)
                    env["OTA_TEST_BUILDS"] = str(d)
                    summary["images"] = image_kinds
                if sys.stdout.isatty():
                    print("  %-15s ..." % name, end="", flush=True)
                res = run_check(check, run_dir, env)
        except KeyboardInterrupt:          # outside a check's wait: its job is closed already, nothing of it runs
            res = {"name": name, "what": what, "status": "FAIL", "interrupted": True}
        summary["checks"].append(res)
        extra = res.get("note") or ("%s tests" % res["tests"] if "tests" in res else "")
        if res.get("leftover"):
            extra += "  killed leftovers: %s" % ", ".join(res["leftover"])
        print("%s  %-15s %-4s %6s  %s" % ("\r" if sys.stdout.isatty() else "", name, res["status"], ("%.0f s" % res["seconds"]) if "seconds" in res else "",
                                           extra), flush=True)
        if res.get("interrupted"):
            interrupted = summary["interrupted"] = True
            print("interrupted: the checks after %s are not run" % name, flush=True)
            break
    summary["seconds"] = round(time.monotonic() - t_all, 1)
    statuses = [c["status"] for c in summary["checks"]]
    skipped_on_purpose = args.quick or only
    summary["pass"] = not interrupted and bool(statuses) and all(s == "PASS" or (s == "SKIP" and skipped_on_purpose) for s in statuses)
    summary["counts"] = {s: statuses.count(s) for s in ("PASS", "FAIL", "SKIP")}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("%s  %d pass, %d fail, %d skip, %.0f s  %s" % ("PASS" if summary["pass"] else "FAIL", summary["counts"]["PASS"],
                                                        summary["counts"]["FAIL"], summary["counts"]["SKIP"],
                                                        summary["seconds"], run_dir / "summary.json"))
    return 130 if interrupted else 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
