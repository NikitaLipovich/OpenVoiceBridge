"""The LyraT's call AGC on the PC: the firmware's own main/call_agc.c (goal 113), built as tools/ota_test/build/agchost.dll
by tools/ota_test/build_agc.cmd (MSVC), built on first use when missing.

run(x) passes a 16 kHz int16 signal through it in frames of 240 (the firmware's frame) and returns the output aligned with
the input (the module sends each frame one frame late), the gain per frame, the speech and guard flags and the stats.
fixed(x, gain) is the old path: constant gain and a hard clip.
"""
import ctypes as C
import subprocess
from pathlib import Path

import numpy as np

FRAME = 240
RATE = 16000
LIMIT = 29491

OTA_TEST = Path(__file__).resolve().parent.parent / "ota_test"
DLL = OTA_TEST / "build" / "agchost.dll"
BUILD = OTA_TEST / "build_agc.cmd"

_lib = None


def build(timeout=300):
    """Builds the DLL; returns None or the tail of the compiler output on failure."""
    r = subprocess.run(["cmd", "/c", str(BUILD)], capture_output=True, text=True, timeout=timeout)
    if r.returncode == 0 and DLL.exists():
        return None
    return (r.stdout + r.stderr)[-2000:]


def lib():
    global _lib
    if _lib is None:
        if not DLL.exists():
            err = build()
            if err:
                raise RuntimeError(f"agchost.dll build failed: {err}")
        _lib = C.CDLL(str(DLL))
        _lib.host_agc_size.restype = C.c_int
        _lib.host_agc_init.argtypes = [C.c_void_p, C.c_float, C.c_float, C.c_float, C.c_float]
        _lib.host_agc_run.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.c_int, C.c_int, C.c_void_p, C.c_void_p,
                                      C.c_void_p]
        _lib.host_agc_stats.argtypes = [C.c_void_p] + [C.c_void_p] * 4
    return _lib


def run(x, init_gain=2.0, target_db=-18.0, gain_min=0.25, gain_max=8.0, frame=FRAME):
    """-> dict y (int16, len(x), aligned), gain_db / speech / lim per frame of x, level_db, floor_db, clips, lim_frames."""
    L = lib()
    x = np.asarray(x, dtype=np.int16)
    nf = -(-len(x) // frame) + 1             # one more frame of zeros pushes the last one out
    buf = np.zeros(nf * frame, np.int16)
    buf[:len(x)] = x
    out = np.zeros_like(buf)
    g = np.zeros(nf, np.float32)
    sp = np.zeros(nf, np.int32)
    lm = np.zeros(nf, np.int32)
    st = C.create_string_buffer(L.host_agc_size())
    L.host_agc_init(st, init_gain, target_db, gain_min, gain_max)
    L.host_agc_run(st, buf.ctypes.data, out.ctypes.data, len(buf), frame, g.ctypes.data, sp.ctypes.data, lm.ctypes.data)
    lev, flo = C.c_float(), C.c_float()
    clips, limf = C.c_uint(), C.c_uint()
    L.host_agc_stats(st, C.byref(lev), C.byref(flo), C.byref(clips), C.byref(limf))
    m = len(x) // frame
    # frame f of the output is input frame f-1: its gain is g[f], decided while frame f was analysed
    return {"y": out[frame:frame + len(x)].copy(), "gain_db": g[1:m + 1], "speech": sp[:m].astype(bool),
            "lim": lm[1:m + 1].astype(bool), "level_db": lev.value, "floor_db": flo.value, "clips": clips.value,
            "lim_frames": limf.value}


def fixed(x, gain=2):
    """The old path: x * gain clipped to int16. -> (y, clipped sample count)."""
    s = np.asarray(x, dtype=np.int32) * int(gain)
    n = int(np.sum((s > 32767) | (s < -32768)))
    return np.clip(s, -32768, 32767).astype(np.int16), n
