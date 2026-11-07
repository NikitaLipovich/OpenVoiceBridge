"""ctypes wrapper of plathost.dll: the firmware's own rom_flasher.c and rom_bundle.c, built with MSVC (goal 095).

Build first: build_host.cmd. CRomFlasher runs the C flasher against rom_emu.Esp32Rom through a port of Python
callbacks with the emulator's virtual clock; bundle_parse() is the C check of a bundle header.
"""
import ctypes as C
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
lib = C.CDLL(os.environ.get("PLATHOST_DLL", str(HERE / "build" / "plathost.dll")))

WRITE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_uint8), C.c_size_t)
READ = C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.POINTER(C.c_uint8), C.c_size_t, C.c_uint32)
FLUSH = C.CFUNCTYPE(None, C.c_void_p)
BAUD = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint32)
PINS = C.CFUNCTYPE(None, C.c_void_p, C.c_int, C.c_int)
DELAY = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32)
NOW = C.CFUNCTYPE(C.c_uint32, C.c_void_p)
BUNDLE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint8), C.c_size_t)


class RomPort(C.Structure):
    _fields_ = [("ctx", C.c_void_p), ("write", WRITE), ("read", READ), ("flush_input", FLUSH), ("set_baud", BAUD),
                ("pins", PINS), ("delay", DELAY), ("now_ms", NOW), ("read_bundle", BUNDLE)]


for name in ("rom_err_name", "rom_phase_name", "rom_bundle_err_name"):
    getattr(lib, name).restype = C.c_char_p
    getattr(lib, name).argtypes = [C.c_int]
lib.host_rom_new.restype = C.c_void_p
lib.host_rom_new.argtypes = [C.c_void_p]
lib.host_rom_set.argtypes = [C.c_void_p, C.c_void_p]
lib.host_rom_info.argtypes = [C.c_void_p, C.c_void_p]
lib.host_rom_flash.argtypes = [C.c_void_p, C.c_void_p]
lib.host_rom_sync.argtypes = [C.c_void_p]
lib.host_rom_md5.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p]
lib.host_rom_enter.argtypes = [C.c_void_p]
lib.host_rom_reset.argtypes = [C.c_void_p]
lib.host_bundle_new.restype = C.c_void_p
lib.host_bundle_info.argtypes = [C.c_void_p, C.c_void_p]
lib.host_free.argtypes = [C.c_void_p]
lib.rom_bundle_parse.argtypes = [C.c_char_p, C.c_size_t, C.c_uint32, C.c_uint8, C.c_uint32, C.c_uint32, C.c_void_p]

INFO = ("phase", "seg", "done", "total", "err", "rom_status", "rom_error", "bad_seg", "magic", "baud", "syncs",
        "session_restarts", "segment_restarts", "blocks", "ms")


def err_name(code):
    return lib.rom_err_name(code).decode()


def bundle_parse(data, size=None, chip=0, protect=(0x9000, 0xF000)):
    """(error name, opaque bundle or None, info dict). The C check of rom_bundle.c."""
    b = lib.host_bundle_new()
    e = lib.rom_bundle_parse(data, len(data), len(data) if size is None else size, chip, protect[0], protect[1], b)
    v = (C.c_uint32 * 28)()
    lib.host_bundle_info(b, v)
    info = {"chip": v[0], "count": v[1], "flash_size": v[2], "total": v[3],
            "segments": [{"offset": v[4 + 3 * i], "size": v[5 + 3 * i], "data_at": v[6 + 3 * i]} for i in range(v[1])]}
    name = lib.rom_bundle_err_name(e).decode()
    if e:
        lib.host_free(b)
        return name, None, info
    return name, b, info


class CRomFlasher:
    """rom_flasher.c driving an Esp32Rom over the emulated wire."""

    def __init__(self, rom, bundle_bytes, baud_high=0, sync_tries=10, sessions=2, segment_tries=3):
        self.rom, self.data = rom, bundle_bytes
        self.host_baud = 115200
        self.pin_log = []
        self._cb = [WRITE(self._write), READ(self._read), FLUSH(self._flush), BAUD(self._baud), PINS(self._pins),
                    DELAY(self._delay), NOW(self._now), BUNDLE(self._bundle)]
        self.port = RomPort(None, *self._cb)
        self.f = lib.host_rom_new(C.byref(self.port))
        cfg = (C.c_uint32 * 4)(baud_high, sync_tries, sessions, segment_tries)
        lib.host_rom_set(self.f, cfg)

    def close(self):
        lib.host_free(self.f)

    # port
    def _write(self, ctx, data, n):
        self.rom.host_write(bytes(data[:n]), self.host_baud)
        return 0

    def _read(self, ctx, buf, cap, timeout):
        got = self.rom.host_read(cap, timeout, self.host_baud)
        C.memmove(buf, got, len(got))
        return len(got)

    def _flush(self, ctx):
        self.rom.host_flush()

    def _baud(self, ctx, baud):
        self.host_baud = baud
        return 0

    def _pins(self, ctx, en_low, boot_low):
        self.pin_log.append((round(self.rom.clock.ms, 1), en_low, boot_low))
        self.rom.pins(en_low, boot_low)

    def _delay(self, ctx, ms):
        self.rom.advance_to(self.rom.clock.ms + ms)

    def _now(self, ctx):
        return int(self.rom.clock.ms) & 0xFFFFFFFF

    def _bundle(self, ctx, at, buf, n):
        if at + n > len(self.data):
            return 1
        C.memmove(buf, self.data[at:at + n], n)
        return 0

    # calls
    def flash(self):
        name, b, info = bundle_parse(self.data)
        if not b:
            raise ValueError("bundle: " + name)
        try:
            return lib.host_rom_flash(self.f, b)
        finally:
            lib.host_free(b)

    def info(self):
        v = (C.c_uint32 * len(INFO))()
        lib.host_rom_info(self.f, v)
        out = dict(zip(INFO, v))
        out["err_name"] = err_name(out["err"])
        out["phase_name"] = lib.rom_phase_name(out["phase"]).decode()
        return out
