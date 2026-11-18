#!/usr/bin/env python3
"""Every check of this bench that runs on the PC alone, with one command. No board, no phone, no COM port, no adb, no sound.

  python lyrat_resend/tools/run_tests.py            all checks, firmware builds included
  python lyrat_resend/tools/run_tests.py --quick    without the builds of the firmwares and of the Android probe
  python lyrat_resend/tools/run_tests.py --only s3_sim,bench_tools    some checks (names: --list)

Prints PASS / FAIL / SKIP per check and writes goals_harness/bench_runs/tests/<stamp>/summary.json with each check's
status, time, log file and notes, and the log of each check beside it. Exit code 0 only when every check that ran passed
and none was skipped in a full run; --quick may skip the checks it leaves out on purpose.

The OTA tests need two images of each board: the full run makes them from its own firmware builds (the "ota" one a variant
with another version and ELF SHA, tests/images.py); --quick takes the builds of the last full run, else synthetic images,
and says which in summary.json. Docs: lyrat_resend/docs/TESTS.md. Made by goal 092.
"""
import argparse
import json
import os
import re
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
    ("platform_rom", "platform: ROM loader in C against the esptool protocol model",
     [PY, "test_rom.py"], PLATFORM, 900, "base", False),
    ("platform_tools", "platform: platform_ota.py and udplog.py against the U3 emulator",
     [PY, "test_platform_tools.py"], PLATFORM, 1800, "base", False),
    ("platform_pins", "platform: wiring table against the pin rules, with mutations that must be caught",
     [PY, "plat_pins_check.py", "--mutate"], PLATFORM, 120, "base", False),
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


def run_check(check, run_dir: Path, env):
    name, what, cmd, cwd, timeout, kind, _ = check
    cmd = [c.replace("{run}", str(run_dir)) for c in cmd]
    log = run_dir / (name + ".log")
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
        code, out = r.returncode, r.stdout + ("\n--- stderr ---\n" + r.stderr if r.stderr else "")
    except subprocess.TimeoutExpired as e:
        code, out = None, "TIMEOUT after %d s\n%s" % (timeout, (e.stdout or b"")[-5000:] if isinstance(e.stdout, bytes)
                                                     else (e.stdout or "")[-5000:])
    except OSError as e:
        code, out = None, "could not start: %s" % e
    seconds = round(time.monotonic() - t0, 1)
    log.write_text(out, encoding="utf-8")
    ran, skipped = unittest_counts(out)
    result = {"name": name, "what": what, "status": "PASS" if code == 0 else "FAIL", "exit": code, "seconds": seconds,
              "log": log.name}
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
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if args.list:
        for c in CHECKS:
            print("%-15s %-6s %s" % (c[0], c[5], c[1]))
        return 0
    only = [n.strip() for n in args.only.split(",")] if args.only else None
    if only:
        unknown = [n for n in only if n not in NAMES]
        if unknown:
            ap.error("unknown check(s) %s; known: %s" % (", ".join(unknown), ", ".join(NAMES)))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out) if args.out else RUNS / (stamp + ("-quick" if args.quick else ""))
    run_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("MSYSTEM", "BENCH_S3_ADDR", "BENCH_U3_ADDR")}
    env["PYTHONIOENCODING"] = "utf-8"
    env["BENCH_NO_ADB"] = "1"       # the phone is never touched by a test (goal 093)
    env.pop("BENCH_USB", None)
    env["OTA_TEST_OUT"] = str(run_dir / "ota_test_runs")
    summary = {"stamp": stamp, "mode": "quick" if args.quick else "full", "only": only, "out": str(run_dir),
               "python": sys.version.split()[0], "checks": []}
    t_all = time.monotonic()
    image_kinds = None
    print("tests -> %s" % run_dir, flush=True)
    for check in CHECKS:
        name, what, _, _, _, kind, needs_images = check
        if only and name not in only:
            continue
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
        summary["checks"].append(res)
        extra = res.get("note") or ("%s tests" % res["tests"] if "tests" in res else "")
        print("%s  %-15s %-4s %6s  %s" % ("\r" if sys.stdout.isatty() else "", name, res["status"], ("%.0f s" % res["seconds"]) if "seconds" in res else "",
                                           extra), flush=True)
    summary["seconds"] = round(time.monotonic() - t_all, 1)
    statuses = [c["status"] for c in summary["checks"]]
    skipped_on_purpose = args.quick or only
    summary["pass"] = bool(statuses) and all(s == "PASS" or (s == "SKIP" and skipped_on_purpose) for s in statuses)
    summary["counts"] = {s: statuses.count(s) for s in ("PASS", "FAIL", "SKIP")}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("%s  %d pass, %d fail, %d skip, %.0f s  %s" % ("PASS" if summary["pass"] else "FAIL", summary["counts"]["PASS"],
                                                        summary["counts"]["FAIL"], summary["counts"]["SKIP"],
                                                        summary["seconds"], run_dir / "summary.json"))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
