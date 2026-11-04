#!/usr/bin/env python3
"""Follow the phone's Bluetooth log from the first moment of a series; addresses reduced, output outside git.

    python btlog.py --out FILE [--seconds N] [--no-clear]
Without --seconds it runs until killed. From Python: start_btlog(path) returns the process, stop_btlog(proc) ends it.
Only the Bluetooth stack's own tags are kept. Nothing from telephony is kept, so no caller number can land here.
"""
import argparse
import ctypes
import re
import subprocess
import sys
import time
from ctypes import wintypes

# Stack tags of the phone, see tools/catalogs/zui_bt_ui.md.
KEEP = re.compile(r"\s[VDIWEF] (bt_\w+|HeadsetStateMachine)\s*:")
MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){4}([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})")
# What a series is judged by. Keep in step with the phone's library, see goal 055 DECISIONS D2.
ENTRY_ADDED = "blacklisting device"
ENTRY_KNOWN = "is already blacklisted for codec negotiation"
ENTRY_USED = "disable codec negotiation for phone"
NEGOTIATION_TIMEOUT = "bta_ag_codec_negotiation_timer_cback"


def die_with_this_process(proc):
    """On Windows a killed parent leaves its child running: tie the child to a job closed by the parent's death."""
    if sys.platform != "win32":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # JOBOBJECT_EXTENDED_LIMIT_INFORMATION on 64-bit: LimitFlags is at offset 16, total size 144 bytes.
    info = (ctypes.c_byte * 144)()
    ctypes.cast(ctypes.byref(info, 16), ctypes.POINTER(wintypes.DWORD))[0] = 0x2000  # KILL_ON_JOB_CLOSE
    job = kernel.CreateJobObjectW(None, None)
    child = kernel.OpenProcess(0x0101, False, proc.pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
    if not job or not child or not kernel.SetInformationJobObject(job, 9, info, ctypes.sizeof(info)) \
            or not kernel.AssignProcessToJobObject(job, child):
        raise OSError(ctypes.get_last_error(), "job for adb logcat")
    kernel.CloseHandle(child)
    return job  # stays open for this process's whole life


def follow(out, seconds=None, clear=True):
    if clear:
        subprocess.run(["adb", "logcat", "-c"], check=False)
    # The server must already run, or it would land in the job below and die with this process.
    subprocess.run(["adb", "start-server"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # stdin and stderr are not inherited: a surviving child would hold the caller's pipe open and hang "| tail".
    proc = subprocess.Popen(["adb", "logcat", "-b", "all", "-v", "threadtime"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")
    die_with_this_process(proc)
    end = time.time() + seconds if seconds else None
    with open(out, "a", encoding="utf-8") as fh:
        while end is None or time.time() < end:
            line = proc.stdout.readline()
            if not line:
                break
            if KEEP.search(line) and "ScoProbe" not in line:
                fh.write(MAC.sub(r"<m>:\1", line))
                fh.flush()
    proc.kill()


def start_btlog(path):
    return subprocess.Popen([sys.executable, __file__, "--out", str(path)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_btlog(proc):
    proc.kill()
    proc.wait()
    # The child "adb logcat" goes with the follower: see die_with_this_process.


def scan(path):
    """Counts of the lines a series is judged by, and the first timestamp of an added entry."""
    res = {"entry_added": 0, "entry_known": 0, "entry_used": 0, "first_added": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if ENTRY_ADDED in line:
                    res["entry_added"] += 1
                    res["first_added"] = res["first_added"] or line[:18]
                elif ENTRY_KNOWN in line:
                    res["entry_known"] += 1
                elif ENTRY_USED in line:
                    res["entry_used"] += 1
    except FileNotFoundError:
        pass
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--no-clear", action="store_true")
    args = ap.parse_args()
    follow(args.out, args.seconds, not args.no_clear)


if __name__ == "__main__":
    main()
