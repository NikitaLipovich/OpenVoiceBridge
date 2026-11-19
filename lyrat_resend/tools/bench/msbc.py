"""mSBC frames of the LyraT on the PC: the firmware's own codec (main/msbc_codec.c with the Bluedroid SBC of ESP-IDF).

Built as tools/ota_test/build/msbchost.dll by tools/ota_test/build_msbc.cmd (MSVC); built on first use when missing.
Frames are MSBC_FRAME_BYTES (57) without the H2 header and padding, as msbc_encode writes them. Goal 112.
"""
import ctypes as C
import subprocess
import threading
from pathlib import Path

import numpy as np

FRAME_BYTES = 57
PCM_SAMPLES = 120          # 7.5 ms at 16 kHz
RATE = 16000

OTA_TEST = Path(__file__).resolve().parent.parent / "ota_test"
DLL = OTA_TEST / "build" / "msbchost.dll"
BUILD = OTA_TEST / "build_msbc.cmd"

_lib = None
_lock = threading.Lock()     # the codec keeps its state in statics: one user at a time


def build(timeout=300):
    """Builds the DLL; returns None or the tail of the compiler output on failure."""
    r = subprocess.run(["cmd", "/c", str(BUILD)], capture_output=True, text=True, timeout=timeout)
    if DLL.exists():
        return None
    return (r.stdout + r.stderr)[-2000:]


def lib():
    global _lib
    if _lib is None:
        if not DLL.exists():
            err = build()
            if err:
                raise RuntimeError(f"msbchost.dll build failed: {err}")
        _lib = C.CDLL(str(DLL))
        _lib.host_msbc_decode.argtypes = [C.c_char_p, C.c_int, C.c_void_p]
        _lib.host_msbc_decode.restype = C.c_int
        _lib.host_msbc_encode.argtypes = [C.c_void_p, C.c_int, C.c_void_p]
    return _lib


def reset():
    with _lock:
        lib().host_msbc_reset()


def encode(pcm, reset_first=True):
    """int16 PCM (padded with zeros to whole frames) -> bytes of n * FRAME_BYTES."""
    x = np.asarray(pcm, dtype=np.int16)
    n = -(-len(x) // PCM_SAMPLES)
    buf = np.zeros(n * PCM_SAMPLES, np.int16)
    buf[:len(x)] = x
    out = C.create_string_buffer(n * FRAME_BYTES)
    with _lock:
        if reset_first:
            lib().host_msbc_reset()
        lib().host_msbc_encode(buf.ctypes.data, n, out)
    return out.raw


def decode(frames: bytes, reset_first=False):
    """bytes of n * FRAME_BYTES -> (int16 PCM of n * PCM_SAMPLES, good frames). A bad frame decodes as silence."""
    n = len(frames) // FRAME_BYTES
    pcm = np.zeros(n * PCM_SAMPLES, np.int16)
    if n == 0:
        return pcm, 0
    with _lock:
        if reset_first:
            lib().host_msbc_reset()
        good = lib().host_msbc_decode(frames[:n * FRAME_BYTES], n, pcm.ctypes.data)
    return pcm, good
