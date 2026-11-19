#!/usr/bin/env python3
"""Every check of this bench that runs on the PC alone, with one command. No board, no phone, no COM port, no adb, no sound.

  python lyrat_resend/tools/run_tests.py --fast     during work: the behaviour only, no builds, about a minute
  python lyrat_resend/tools/run_tests.py            full: builds of the firmwares (cached) and of the Android probe, the
                                                    real images through OTA, every variant; once before a push/release
  python lyrat_resend/tools/run_tests.py --only s3_sim,bench_tools [--fast]   some checks (names: --list)
  python lyrat_resend/tools/run_tests.py --fast --port-offset 100      every port moved by 100 more (BENCH_PORT_OFFSET)
  --quick is the old name of --fast.  --jobs N: how many test processes at once (default 16; 1 = one after another)

Prints PASS / FAIL / SKIP per check and writes goals_harness/bench_runs/tests/<stamp>[-fast]/summary.json with each
check's status, time, log file, shards and notes, and the log of each check beside it. Exit code 0 only when every check
that ran passed and none was skipped in a full run; --fast skips the builds and the cases marked "full run only"
(tests/tiers.py), and says how many.

Two levels (goal 110, docs/TESTS.md): --fast has every check of behaviour (protocol, mode rules, log parsing, commands,
service, panel, updater and runner rules) with small synthetic images for OTA (BENCH_SYNTH_IMAGE_BYTES) and the firmware
rules read from the sources (fw_rules) instead of the builds; four long variants of a function whose short case is in it
too are left to the full run. The full run builds both firmwares (tests/fw_build_check.py: skipped for a board whose
inputs did not change) and the Android probe, and the OTA tests take images made from those builds (tests/images.py).

Parallel (goal 110): independent checks, and shards of the long ones (parts of a test module, tests/shard.py), run at the
same time, longest first. Each process gets ports of its own: BENCH_PORT_OFFSET = --port-offset + 1700 * k, so two of
them never meet, nor the bench service on 5080 nor the bench; a window holding a port another program uses is skipped. host_dll and platform_dll build the DLLs the tests
load before the rest; the image checks of the full run wait for fw_build; child_cleanup's last case runs after all.
Each process runs in a job object of its own (tests/procjob.py, goal 107): its whole tree ends with it - after PASS or
FAIL (leftovers are killed and named), at its timeout, on Ctrl+C (all running ones, exit 130, nothing more starts) and
when this runner dies. Made by goal 092.
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
SLOT_SPAN = 1700            # ports of one test process: 5004 + offset .. 5110 + offset + 1560 (bench_ui's own check)
FAST_IMAGE_BYTES = 48_000   # synthetic OTA images of --fast: the modelled UART takes seconds, not minutes
HOST = ["host_dll"]           # ota_test/build/otahost.dll: the firmware's C code most tests load
PLAT = ["platform_dll"]       # platform/build: the platform tests

sys.path.insert(0, str(TESTS))
import images  # noqa: E402
import procjob  # noqa: E402
from tiers import FULL_ONLY  # noqa: E402


def unittest_cmd(module):
    return [PY, "-m", "unittest", "-v", module]


def check(name, what, cmd, cwd, timeout, kind="base", images=False, est=10, deps=None, module=None, shards=None,
          timing=False):
    """kind: "base" in both levels, "build" only in the full run, "fast" only in --fast. deps: checks that must end
    first (default the build of otahost.dll). module + shards: run as parts of a unittest module, see shards_of().
    timing: it asserts latencies measured in real time (the simulator must keep up with the clock); in a parallel run
    its processes run at ABOVE_NORMAL priority, so they get the CPU first as on a quiet PC (goal 110 #002)."""
    return {"name": name, "what": what, "cmd": cmd, "cwd": cwd, "timeout": timeout, "kind": kind, "images": images,
            "est": est, "deps": HOST if deps is None else deps, "module": module, "shards": shards, "timing": timing}


def ut(name, what, module, cwd, timeout, est, images=False, shards=None, deps=None, timing=False):
    return check(name, what, unittest_cmd(module), cwd, timeout, images=images, est=est, module=module, shards=shards,
                 deps=deps, timing=timing)


# A shard: (tests, estimated seconds[, "final" | "full"]) runs those tests of the module ("Class" or "Class.test"); REST
# runs the others. "final": after every other process; "full": only full-only tests, not started by --fast at all.
REST = "rest"

CHECKS = [
    check("host_dll", "the firmware's protocol C code (ota_link, ota_sha256, dbg_log, ctl_frame) builds with MSVC /W4 /WX",
          ["cmd", "/c", str(OTA_TEST / "build_host.cmd")], OTA_TEST, 300, est=5, deps=[]),
    check("platform_dll", "the platform's ROM loader C code builds with MSVC /W4 /WX",
          ["cmd", "/c", str(PLATFORM / "build_host.cmd")], PLATFORM, 300, est=5, deps=HOST),   # one MSVC at a time
    check("static", "no secret in git, .gitignore covers the secret files, the S3 builds for Wi-Fi by default",
          [PY, str(TESTS / "static_check.py")], TESTS, 120, est=1, deps=[]),
    check("fw_rules", "without a build: the partition tables of partitions_ota.csv = docs/OTA.md, rollback and the table "
                      "file in sdkconfig.defaults, the pc build's define (the rules fw_build checks in the images)",
          [PY, str(TESTS / "fw_build_check.py"), "--sources", "--out", "{run}/fw_rules"], TESTS, 120, kind="fast", est=2,
          deps=[]),
    check("fw_build", "S3, LyraT (phone and pc builds) and its updater built in their own folders (a board whose inputs "
                      "did not change is not built again): default mode, table, rollback, size in the slot, whole image, "
                      "secrets, Wi-Fi only in the updater; working build untouched",
          [PY, str(TESTS / "fw_build_check.py"), "--out", str(BUILDS)], TESTS, 7200, kind="build", est=360, deps=[]),
    check("sco_probe", "the Android probe app builds with the plain SDK tools (never installed here)",
          [PY, str(TOOLS / "android" / "build_apk.py"), "--out", "{run}/apk"], TOOLS / "android", 600, kind="build",
          est=14, deps=[]),
    ut("c_ctl_frame", "UART framing in C: limits of the length, CRC and header damage, resync, pauses, repeats",
       "test_ctl_frame", TESTS, 120, 1),
    ut("c_dbg_log", "log ring in C: long lines in parts and their join, ring full mid-line, repeats; old and new stat "
                    "line; a failed UDP send keeps its lines", "test_dbg_log", TESTS, 120, 1),
    ut("fw_review", "findings of the review of goal 094: soft limiter in C, Wi-Fi retry pause, self-test only once, "
                    "UX delay in any build, noise mode at start", "test_fw_review", TESTS, 120, 1),
    ut("upd_rules", "the LyraT's updater in C (upd_mode.c): firmware state from the NVS marks, the 10/120 s window, a new "
                    "image at once, a bad one kept out and retried, the command upd; no Wi-Fi call in the headset "
                    "firmware", "test_upd_mode", TESTS, 120, 1),
    ut("ota_protocol", "OTA protocol C against Python: loss, reorder, bit errors, token, rollback rules, log ring",
       "test_protocol", OTA_TEST, 900, 7, images=True),
    ut("ota_tools", "ota.py of both boards (dry run, update, rollback, wrong token and board) and udplog.py vs the "
                    "emulator", "test_tools", OTA_TEST, 1800, 20, images=True,
       shards=[(["OtaTool.test_6_lyrat_dry_run"], 31), (["OtaTool.test_7_lyrat_update_then_rollback"], 68),
               (["OtaCallRule"], 45), (REST, 30)]),
    ut("s3_sim", "the S3 simulator against the firmware source: ports, JSON keys, stream header, stat line, relay rules",
       "test_s3_sim", TESTS, 300, 10, images=True),
    ut("bench_tools", "s3_uart.py, ota.py --dry-run, udplog.py, udp_record.py --tap end to end against the simulator",
       "test_bench_tools", TESTS, 1200, 28, images=True,
       shards=[(["Log", "Tap"], 10), (["TapLossy"], 5), (["Ota"], 6), (REST, 7)]),
    ut("bench_unit", "fw.py variants, sound analysis on synthetic WAV, phone_mic.level, every bench module loads",
       "test_bench_unit", TESTS, 300, 8),
    ut("usb_free", "the bench without USB vs the simulator: first_flash --dry-run, NetWatch, link trials, round power, "
                   "fw.py/s3.py/s3_uart.py over the network, Wi-Fi power in stat, WAV silence by frame, call_record",
       "test_usb_free", TESTS, 1500, 120, images=True,
       shards=[(["Link"], 23), (["Stages.test_fw_erase_bonds_and_flash_over_the_network"], 16),
               (["Stages.test_round_power_sees_a_reboot"], 12), (["Stages.test_round_power_over_the_network"], 9),
               (["Stages"], 21), (["FirstFlash"], 17), (["NetConsole"], 14), (["LossyCall"], 12), (REST, 2)]),
    ut("call_check", "the owner's test call vs the simulator: checks, channel seen, both lines aligned, report; quality "
                     "numbers on synthetic lines (tone, silence, cut pieces, clipping, noise, shifted lines); live "
                     "pipeline, PCM output and its latency (goal 097)", "test_call_check", TESTS, 900, 32, images=True,
       shards=[(["EndToEndTest.test_call_recorded_aligned_and_reported"], 14),
               (["EndToEndTest.test_losses_are_counted_and_judged"], 12, "full"), (REST, 7)], timing=True),
    ut("call_listen", "the live stream cut into utterances (goal 101): borders against synthetic speech in quiet and "
                      "noise, silence, clicks, a background step, overlap, the 30 s cut, missing sound and lost blocks, "
                      "files bit for bit, the recogniser hook, CPU; a dropped stream and its reconnect; a scenario "
                      "played by the simulator through call_check to call_listen", "test_call_listen", TESTS, 600, 84,
       images=True, shards=[(["EndToEndTest.test_scenario_through_the_simulator"], 34),
                            (["EndToEndTest.test_scenario_with_losses_on_the_way"], 34, "full"),
                            (["SocketTest"], 11), (REST, 9)], timing=True),
    ut("phone_compare", "the LyraT mic before Bluetooth against the phone's recording: alignment (offset, clock drift, a "
                        "jump of the time line, no convergence), lost frames knocked out at known times found within "
                        "7.5 ms with their type, files and report, the check against tone_blocks, call_check --compare "
                        "plumbing (goal 099)", "test_phone_compare", TESTS, 300, 18),
    ut("dual_mode", "the LyraT's two modes, phone and PC (goal 102): the C rules of hf_mode.c, the scenarios of the bench "
                    "on a model of the stack, a phone and Windows (switch, refusal during SCO, frequent app audio, link "
                    "loss, pushy phone, first pairing, unknown AG, reboot, build pc), the phone mode line for line as "
                    "phone-bringup, the audio path byte for byte, hfp_states.py on those logs",
       "test_dual_mode", TESTS, 300, 1),
    ut("mic_mute", "the LyraT's mic mute (goal 111): the C rules of mic_mute.c (key Rec debounce vs the GPIO36 erratum, toggle on "
                  "release, the command, the 10 ms ramp), the glue in main.c, events, benchd, panel, call_check mute "
                  "spans, and s3_uart/call_check against the simulator",
       "test_mic_mute", TESTS, 300, 17, images=True),
    ut("sent_tap", "what the LyraT sends to the phone (goal 112): the firmware's mSBC codec on the PC (msbchost.dll, built on "
                  "first use) on a tone and voice, against ffmpeg; frames placed on the up line's clock, lost frames, muted "
                  "spans; the payload layout of both firmwares read from the source",
       "test_sent_tap", TESTS, 300, 2),
    ut("call_agc", "the call AGC toward the headphones (goal 113): the firmware's call_agc.c on the PC (agchost.dll, built on first "
                  "use): no sample over the guard on hostile input, level to the target, gain held in pauses and steady "
                  "noise, no steps; the glue in main.c",
       "test_call_agc", TESTS, 300, 2),
    ut("updater", "the LyraT through its updater vs the simulator: image over Wi-Fi, a bad image kept out, no network "
                  "then the UART way, fw.py --via auto, every start through the updater (goal 096)",
       "test_updater", TESTS, 1500, 74, images=True,
       shards=[(["Updater.test_1_wifi_update", "Updater.test_2_bad_image_kept_out_then_good_one"], 25),
               (["Updater.test_3_no_wifi_then_uart_in_the_same_window"], 22, "full"),
               (["Updater.test_4_fw_auto_falls_back_to_uart"], 22), (REST, 8)]),
    ut("benchd", "the bench service vs the simulator (goal 103): log of both boards in rotated files, event index (start "
                 "and reason, SLC, SCO, commands, a lost S3), API and CLI, the log relay to two udplog.py and NetWatch "
                 "at once, the bench lock (refusal, queue, expiry, dead pid, lock run, bench_busy), an update as a job, "
                 "the admin page and its POST rules, --lan read only; tools without the service; two simulators on two "
                 "port offsets", "test_benchd", TESTS, 900, 55, images=True,
       shards=[(["Service.test_e_lock_refusal_queue_expiry_and_dead_pid", "Service.test_b_commands_sco_and_a_start",
                 "Service.test_a_log_files_status_and_mac_hidden"], 16),
               (["Service.test_z_lost_s3_and_its_return", "Service.test_c_relay_two_udplogs_at_once_and_benchd_keeps_its_log"], 15),
               (["Service"], 16), (["Lan"], 7), (["Ports"], 7), (REST, 7)]),
    check("bench_ui_check", "the bench UI spec, its generated web page and phone screen, and benchd's routes agree "
                            "(gen_bench_ui.py --check --strict: (a) generated files, (b) routes and parameters, (c) "
                            "status paths against benchd + the simulator, (d) every control route has a button; goal 104)",
          [PY, str(TOOLS / "bench" / "gen_bench_ui.py"), "--check", "--strict"], TOOLS / "bench", 300, images=True,
          est=9),
    ut("bench_ui", "the bench UI generator (goal 104): deterministic, --check catches (a)-(d) on broken copies, the "
                   "template language the same in Python, the page's JS (node) and the phone's Java (JVM), the page "
                   "renders against benchd + simulator, the LAN token, UDP discovery", "test_bench_ui", TESTS, 900, 47,
       images=True, shards=[(["Drift"], 25), (REST, 30)]),     # each shard pays setUpModule (sim, benchd, a job)
    ut("bench_find", "the phone finds benchd and the S3 itself (goal 105): the phone's Java (JVM) against real benchd "
                     "processes - signed answer, foreign and old services not taken, cache only a hint, another port "
                     "found anew - and direct mode against the simulator (UQ, log by LOG1/LOG0, UC, UX), its status = "
                     "s3_direct.py, log parsing = udplog, every write to the S3 only in direct mode",
       "test_bench_find", TESTS, 600, 52,
       shards=[(["Direct"], 16), (["Restart"], 17), (["Find"], 11), (REST, 6)]),
    check("platform_rom", "platform: ROM loader in C against the esptool protocol model",
          [PY, "test_rom.py"], PLATFORM, 900, est=20, deps=PLAT),
    check("platform_tools", "platform: platform_ota.py and udplog.py against the U3 emulator",
          [PY, "test_platform_tools.py", "-v"], PLATFORM, 1800, est=92, module="test_platform_tools", deps=HOST + PLAT,
          shards=[(["Tools.test_03_nodes_app"], 14), (["Tools.test_03b_u2_app"], 22), (["Tools.test_02_u3"], 15), (["Tools.test_04_rom"], 12),
                  (["LinkFaults"], 15, "full"), (["RomFaults"], 11), (REST, 10)]),
    check("platform_pins", "platform: wiring table against the pin rules, with mutations that must be caught",
          [PY, "plat_pins_check.py", "--mutate"], PLATFORM, 120, est=1, deps=[]),
    # The last case runs after every other process of the run: then no one holds ota_test/build/otahost.dll.
    ut("child_cleanup", "what a run starts ends with it (goal 107): fake checks that pass, fail, crash or time out, "
                        "Ctrl+C and a killed runner leave no process and no busy DLL; processes outside the run and the "
                        "bench service untouched; a test process run alone takes its children with it; otahost.dll free",
       "test_child_cleanup", TESTS, 300, 15, timing=True,     # "gone within 1 s" of a whole process tree
       shards=[(REST, 15), (["ChildCleanup.test_e_otahost_dll_free"], 1, "final")]),
]
NAMES = [c["name"] for c in CHECKS]
T_ALL = [0.0]


def learned(fast):
    """Seconds of each shard in the last parallel run of the same level: {(check, tests): s}, to order the next."""
    for f in sorted(RUNS.glob("*/summary.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:10]:
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if s.get("mode") != ("fast" if fast else "full") or s.get("only") or s.get("jobs", 1) < 2:
            continue
        out = {}
        for c in s["checks"]:
            if c.get("status") != "PASS":       # a failed or skipping check's times say nothing about the next run
                continue
            for sh in c.get("shards") or [{"tests": None, "seconds": c.get("seconds")}]:
                if sh.get("seconds") is not None:
                    out[(c["name"], json.dumps(sh["tests"]))] = sh["seconds"]
        return out
    return {}


def shards_of(c):
    """The processes of a check: [{"cmd", "ids", "est", "final"}]."""
    if not c.get("shards"):
        return [{"cmd": c["cmd"], "ids": None, "est": c["est"], "final": False, "full": False}]
    named = [n for s in c["shards"] if s[0] != REST for n in s[0]]
    out = []
    for s in c["shards"]:
        ids, est, final, full = s[0], s[1], s[2:] == ("final",), s[2:] == ("full",)
        # "Stages" as a shard of its own after "Stages.test_x" shards: the class without those
        if ids == REST:
            sel = ["--exclude", ",".join(named)]
        else:
            taken = [n for n in named if "." in n and n.split(".")[0] in ids and n not in ids]
            sel = ["--only", ",".join(ids)] if not taken else ["--only", ",".join(ids), "--minus", ",".join(taken)]
        out.append({"cmd": [PY, str(TESTS / "shard.py"), c["module"], *sel], "ids": ids, "est": est, "final": final,
                    "full": full})
    return out


def ports_in_use():
    """Local TCP ports listening and UDP ports bound now, by any program (netstat -an)."""
    try:
        out = subprocess.run(["netstat", "-an"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    used = set()
    for line in out.splitlines():
        cells = line.split()
        if len(cells) >= 2 and (cells[0] == "UDP" or (cells[0] == "TCP" and "LISTENING" in cells)):
            port = cells[1].rsplit(":", 1)[-1]
            if port.isdigit():
                used.add(int(port))
    return used


def slot_offsets(base, jobs):
    """BENCH_PORT_OFFSET of each slot: base + SLOT_SPAN * k, skipping a window that holds a port another program uses
    (Windows' Delivery Optimization listens on 7680, for one) - a test there fails with WinError 10013 (goal 110)."""
    used, out, k = ports_in_use(), [], 1
    while len(out) < jobs:
        off = base + SLOT_SPAN * k
        lo, hi = off + 5004, off + 5004 + SLOT_SPAN - 1
        if hi > 49151:
            raise SystemExit("run_tests: no free ports for %d slots above --port-offset %d" % (jobs, base))
        if not any(lo <= u <= hi for u in used):
            out.append(off)
        k += 1
    return out


def prepare_images(run_dir: Path, fast: bool):
    """OTA_TEST_BUILDS for the tests: --fast small synthetic ones; full from the test builds when they exist, else
    synthetic. Returns (dir, kinds)."""
    d = run_dir / "images"
    if fast:
        os.environ["BENCH_SYNTH_IMAGE_BYTES"] = str(FAST_IMAGE_BYTES)     # images.synthetic() of this process too
        return d, images.prepare(d)
    s3 = BUILDS / "s3" / "s3_eth_bridge.bin"
    ly = BUILDS / "lyrat" / "lyrat_resend.bin"
    return d, images.prepare(d, s3 if s3.is_file() else None, ly if ly.is_file() else None)


def unittest_counts(text):
    ran = re.search(r"^Ran (\d+) tests?", text, re.M)
    skipped = re.search(r"skipped=(\d+)", text)
    full_only = len(re.findall(r"skipped '%s" % FULL_ONLY, text))
    return (int(ran.group(1)) if ran else None), (int(skipped.group(1)) if skipped else 0), full_only


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


class Proc:
    """One process of a check (the check itself or one shard) in a job object of its own (tests/procjob.py, goal
    107): whatever it starts dies with it - after PASS, FAIL, its timeout, Ctrl+C, or this runner's own death - and
    processes left behind are named in the result."""

    def __init__(self, c, index, n, shard, run_dir: Path, env, slot, offset, high=False):
        self.c, self.index, self.n, self.shard, self.slot, self.offset = c, index, n, shard, slot, offset
        tag = c["name"] + ("" if n == 1 else ".%d" % (index + 1))
        self.out_f, self.err_f = run_dir / (tag + ".stdout.tmp"), run_dir / (tag + ".stderr.tmp")
        self.t0 = time.monotonic()
        self.deadline = self.t0 + c["timeout"]
        self.code, self.head, self.leftover, self.interrupted = None, "", [], False
        cmd = [x.replace("{run}", str(run_dir)) for x in shard["cmd"]]
        self.job = procjob.Job(procjob.ABOVE_NORMAL if high else None)
        self.p = None
        # Output into files, not pipes: a grandchild still holding a pipe would keep a reader waiting for ever.
        self.fo, self.fe = open(self.out_f, "wb"), open(self.err_f, "wb")
        try:
            self.p = procjob.popen(self.job, cmd, cwd=str(c["cwd"]), stdin=subprocess.DEVNULL, stdout=self.fo,
                                   stderr=self.fe, env=env)
        except OSError as e:
            self.head = "could not start: %s" % e

    def poll(self):
        """True when it ended (by itself or at its timeout)."""
        if self.p is None:
            return True
        code = self.p.poll()
        if code is None and time.monotonic() < self.deadline:
            return False
        if code is None:
            self.head = "TIMEOUT after %d s\n" % self.c["timeout"]
        self.code = code
        return True

    def stop(self, why):
        self.code, self.head, self.interrupted = None, why, True

    def finish(self):
        try:
            if self.p is not None and (self.code is None or self.job.pids()):
                self.leftover = [x for x in self.job.terminate() if x != self.p.pid]   # the check is not a leftover
                self.p.wait()
        finally:
            self.cpu = self.job.cpu_seconds()
            self.job.close()
            self.fo.close()
            self.fe.close()
        self.seconds = round(time.monotonic() - self.t0, 1)
        names = proc_names(set(self.leftover)) if self.leftover else {}
        read = lambda f: f.read_bytes().decode("utf-8", "replace") if f.exists() else ""  # noqa: E731
        stdout, stderr = read(self.out_f), read(self.err_f)
        for f in (self.out_f, self.err_f):
            try:
                f.unlink()
            except OSError:
                pass
        if self.head.startswith(("TIMEOUT", "INTERRUPTED")):
            stdout = stdout[-5000:]
        text = self.head + stdout + ("\n--- stderr ---\n" + stderr if stderr else "")
        if self.leftover:
            text += "\n--- killed with the check's job (goal 107) ---\n" + "".join(
                "%d %s\n" % (x, names.get(x, "?")) for x in self.leftover)
        self.text = text
        self.leftover_named = ["%s:%d" % (names.get(x, "?"), x) for x in self.leftover]
        return self


def result_of(c, procs, run_dir: Path, fast: bool):
    """One entry of summary.json for a check from its processes, and its log file."""
    name = c["name"]
    if len(procs) == 1:
        log_text = procs[0].text
    else:
        log_text = "".join("=== shard %d/%d: %s (port offset %d, %.1f s, exit %s) ===\n%s\n" % (
            p.index + 1, p.n, ",".join(p.shard["ids"]) if isinstance(p.shard["ids"], list) else "the rest",
            p.offset, p.seconds, p.code, p.text) for p in sorted(procs, key=lambda p: p.index))
    log = run_dir / (name + ".log")
    log.write_text(log_text, encoding="utf-8")
    t0 = min(p.t0 for p in procs)
    seconds = round(max(p.t0 + p.seconds for p in procs) - t0, 1)
    codes = [p.code for p in procs]
    res = {"name": name, "what": c["what"], "status": "PASS" if all(x == 0 for x in codes) else "FAIL",
           "exit": next((x for x in codes if x != 0), 0), "seconds": seconds, "log": log.name}
    leftover = [x for p in procs for x in p.leftover_named]
    if leftover:
        res["leftover"] = leftover
    if any(p.interrupted for p in procs):
        res["interrupted"] = True
    counts = [unittest_counts(p.text) for p in procs]
    if any(r is not None for r, _, _ in counts):
        res["tests"] = sum(r or 0 for r, _, _ in counts)
        res["skipped"] = sum(s for _, s, _ in counts)
        full_only = sum(f for _, _, f in counts)
        if fast and full_only:
            res["full_only"] = full_only
        unexpected = res["skipped"] - (full_only if fast else 0)
        if unexpected:
            res["status"] = "FAIL"           # a skipped case proves nothing: the runner gives what it needs
            res["note"] = "%d test(s) skipped, see the log" % unexpected
    res["cpu_seconds"] = round(sum(p.cpu or 0 for p in procs), 1)
    if len(procs) > 1:
        res["shards"] = [{"tests": p.shard["ids"] if isinstance(p.shard["ids"], list) else "rest",
                          "start_s": round(p.t0 - T_ALL[0], 1), "seconds": p.seconds, "cpu_seconds": p.cpu, "exit": p.code, "port_offset": p.offset}
                         for p in sorted(procs, key=lambda p: p.index)]
        res["busy_seconds"] = round(sum(p.seconds for p in procs), 1)
    if name == "fw_build":
        report = BUILDS / "fw_build.json"
        if report.exists():
            rep = json.loads(report.read_text(encoding="utf-8"))
            res["detail"] = {b: {k: v.get("pass") for k, v in e.get("checks", {}).items()} |
                             {"build_s": e.get("build", {}).get("seconds"),
                              "cached": bool(e.get("build", {}).get("cached")),
                              "slot_use_pct": e.get("checks", {}).get("size", {}).get("use_pct"),
                              "ccache_trap": bool(e.get("rebuild"))}
                             for b, e in rep["boards"].items()}
            res["detail"]["untouched"] = rep["untouched"]
    return res


def print_line(res):
    extra = res.get("note") or ("%s tests" % res["tests"] if "tests" in res else "")
    if res.get("full_only"):
        extra += ", %d left to the full run" % res["full_only"]
    if res.get("leftover"):
        extra += "  killed leftovers: %s" % ", ".join(res["leftover"])
    print("  %-15s %-4s %6s  %s" % (res["name"], res["status"], ("%.0f s" % res["seconds"]) if "seconds" in res else "",
                                     extra), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fast", action="store_true", help="the behaviour only: no builds, small images, ~1 min")
    ap.add_argument("--quick", action="store_true", help="old name of --fast")
    ap.add_argument("--only", help="comma separated names of checks, see --list")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--jobs", type=int, default=None, help="test processes at once (default 16; --check-file: 1)")
    ap.add_argument("--out", help="folder instead of goals_harness/bench_runs/tests/<stamp>")
    ap.add_argument("--port-offset", type=int, default=None,
                    help="added to BENCH_PORT_OFFSET of every process: runs (worktrees) on different offsets do not meet")
    ap.add_argument("--check-file", help=argparse.SUPPRESS)   # JSON list of checks instead of CHECKS: the cleanup test's fakes
    args = ap.parse_args()
    fast = args.fast or args.quick
    checks = list(CHECKS)
    if args.check_file:
        checks = [check(c["name"], c.get("what", "test of the runner"), c["cmd"], Path(c.get("cwd", ".")), c["timeout"],
                        deps=[]) for c in json.loads(Path(args.check_file).read_text(encoding="utf-8"))]
    jobs = max(1, args.jobs or (1 if args.check_file else 16))
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
            print("%-15s %-6s %s" % (c["name"], c["kind"], c["what"]))
        return 0
    only = [n.strip() for n in args.only.split(",")] if args.only else None
    if only:
        unknown = [n for n in only if n not in [c["name"] for c in checks]]
        if unknown:
            ap.error("unknown check(s) %s; known: %s" % (", ".join(unknown), ", ".join(NAMES)))
    base = args.port_offset if args.port_offset is not None else int(os.environ.get("BENCH_PORT_OFFSET", "0") or 0)
    offsets = [base] if args.check_file else slot_offsets(base, jobs)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out).resolve() if args.out else RUNS / (stamp + ("-fast" if fast else ""))
    run_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("MSYSTEM", "BENCH_S3_ADDR", "BENCH_U3_ADDR")}
    env["PYTHONIOENCODING"] = "utf-8"
    env["BENCH_NO_ADB"] = "1"       # the phone is never touched by a test (goal 093)
    env["BENCH_NO_BENCHD"] = "1"    # a bench service running on this PC is not used by the tests (test_benchd starts its own)
    env.pop("BENCHD_URL", None)
    env.pop("BENCH_USB", None)
    env["BENCH_TEST_TIER"] = "fast" if fast else "full"
    if fast:
        env["BENCH_SYNTH_IMAGE_BYTES"] = str(FAST_IMAGE_BYTES)
    else:
        env.pop("BENCH_SYNTH_IMAGE_BYTES", None)
    summary = {"stamp": stamp, "mode": "fast" if fast else "full", "only": only, "out": str(run_dir), "port_offset": base,
               "jobs": jobs, "python": sys.version.split()[0], "checks": []}
    print("tests (%s, %d at once) -> %s" % (summary["mode"], jobs, run_dir), flush=True)

    selected = [c for c in checks if not only or c["name"] in only]
    results = {}
    for c in selected:
        if c["kind"] == "build" and fast:
            results[c["name"]] = {"name": c["name"], "what": c["what"], "status": "SKIP",
                                  "note": "--fast leaves out the builds"}
            print_line(results[c["name"]])
    run_names = {c["name"] for c in selected if c["name"] not in results and not (c["kind"] == "fast" and not fast)}
    pending, skipped_full = [], {}
    for c in selected:
        if c["name"] in run_names:
            sh = shards_of(c)
            if fast:
                skipped_full[c["name"]] = sum(len(s["ids"]) for s in sh if s.get("full"))
                sh = [s for s in sh if not s.get("full")]
            for i, s in enumerate(sh):
                pending.append((c, i, len(sh), s))
    seen = learned(fast)
    for c, _, _, sh in pending:
        sh["est"] = seen.get((c["name"], json.dumps(sh["ids"])), sh["est"])
    # Longest chain first: a shard's own estimate plus the longest shard of what waits for its check (the DLLs first).
    by_name = {c["name"]: c for c in selected if c["name"] in run_names}
    tail = {}

    def tail_of(name):
        if name not in tail:
            waiting = [c for c in by_name.values() if name in c["deps"] or (name == "fw_build" and c["images"])]
            tail[name] = max([max(s["est"] for s in shards_of(c)) + tail_of(c["name"]) for c in waiting] or [0])
        return tail[name]
    pending.sort(key=lambda x: -(x[3]["est"] + tail_of(x[0]["name"])))
    left = {n: sum(1 for x in pending if x[0]["name"] == n) for n in run_names}
    done_procs = {n: [] for n in run_names}
    running, free = [], list(range(jobs))
    image_ready = False
    t_all = T_ALL[0] = time.monotonic()
    interrupted = False

    def deps_of(c):
        d = [x for x in c["deps"] if x in run_names]
        if c["images"] and not fast and "fw_build" in run_names:
            d.append("fw_build")
        return d

    def ready(item):
        c, _, _, s = item
        if any(left[d] for d in deps_of(c)):
            return False
        if s["final"]:
            return all(x[3]["final"] for x in pending) and all(r.shard["final"] for r in running)
        return True

    try:
        while pending or running:
            for item in list(pending):
                if not free:
                    break
                if not ready(item):
                    continue
                c, i, n, s = item
                if c["images"] and not image_ready:
                    d, kinds = prepare_images(run_dir, fast)
                    env["OTA_TEST_BUILDS"] = str(d)
                    summary["images"] = kinds
                    image_ready = True
                slot = free.pop(0)
                penv = dict(env)
                offset = offsets[0] if args.check_file else offsets[slot]
                penv["BENCH_PORT_OFFSET"] = str(offset)
                penv["OTA_TEST_OUT"] = str(run_dir / "ota_test_runs" / ("%s-%d" % (c["name"], i + 1)))
                pending.remove(item)
                running.append(Proc(c, i, n, s, run_dir, penv, slot, offset, high=jobs > 1 and c["timing"]))
            for r in list(running):
                if r.poll():
                    running.remove(r)
                    free.append(r.slot)
                    free.sort()
                    name = r.c["name"]
                    done_procs[name].append(r.finish())
                    left[name] -= 1
                    if not left[name]:
                        results[name] = result_of(r.c, done_procs[name], run_dir, fast)
                        if skipped_full.get(name):
                            results[name]["full_only"] = results[name].get("full_only", 0) + skipped_full[name]
                        print_line(results[name])
            time.sleep(0.1)
    except KeyboardInterrupt:
        interrupted = summary["interrupted"] = True
        for r in running:
            r.stop("INTERRUPTED (Ctrl+C) after %.0f s\n" % (time.monotonic() - r.t0))
            done_procs[r.c["name"]].append(r.finish())
        for r in running:
            name = r.c["name"]
            if name not in results:
                results[name] = result_of(r.c, done_procs[name], run_dir, fast)
                print_line(results[name])
        print("interrupted: the checks not started are not run", flush=True)
    summary["checks"] = [results[c["name"]] for c in selected if c["name"] in results]
    summary["seconds"] = round(time.monotonic() - t_all, 1)
    summary["busy_seconds"] = round(sum(p.seconds for ps in done_procs.values() for p in ps), 1)
    summary["cpu_seconds"] = round(sum(p.cpu or 0 for ps in done_procs.values() for p in ps), 1)
    statuses = [c["status"] for c in summary["checks"]]
    skipped_on_purpose = fast or only
    summary["pass"] = not interrupted and bool(statuses) and all(s == "PASS" or (s == "SKIP" and skipped_on_purpose) for s in statuses)
    summary["counts"] = {s: statuses.count(s) for s in ("PASS", "FAIL", "SKIP")}
    summary["full_only"] = sum(c.get("full_only", 0) for c in summary["checks"])
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("%s  %d pass, %d fail, %d skip, %.0f s%s  %s" % (
        "PASS" if summary["pass"] else "FAIL", summary["counts"]["PASS"], summary["counts"]["FAIL"],
        summary["counts"]["SKIP"], summary["seconds"],
        (", %d cases left to the full run" % summary["full_only"]) if summary["full_only"] else "",
        run_dir / "summary.json"))
    return 130 if interrupted else 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
