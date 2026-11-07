"""ctypes wrapper of otahost.dll: the firmware's own ota_link.c, ota_sha256.c, dbg_log.c and ctl_frame.h, built with MSVC.

Build first: build_host.cmd (MSVC). Used by the tests and by the S3 emulator.
"""
import ctypes as C
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DLL = HERE / "build" / "otahost.dll"
lib = C.CDLL(str(DLL))

u8p = C.POINTER(C.c_uint8)
lib.ota_status_name.restype = C.c_char_p
lib.ota_rx_state_name.restype = C.c_char_p
lib.ota_tx_state_name.restype = C.c_char_p
lib.ota_image_elf_sha.restype = C.c_void_p
for name in ("ota_rx_handle", "ota_tx_poll", "ota_build_begin", "ota_build_short", "dbg_ring_pack", "dbg_rec_encode",
             "host_ctl_build", "host_ctl_len", "host_sizeof_rx"):
    getattr(lib, name).restype = C.c_size_t
for name in ("host_ctl_parser_new", "host_tx_new", "host_rx_new", "host_ring_new", "dbg_ring_reserve", "host_ctl_payload"):
    getattr(lib, name).restype = C.c_void_p
lib.host_ctl_parser_free.argtypes = [C.c_void_p]
lib.host_free.argtypes = [C.c_void_p]
lib.host_ctl_feed.argtypes = [C.c_void_p, C.c_uint8, C.c_uint32]
lib.host_ctl_type.argtypes = [C.c_void_p]
lib.host_ctl_type.restype = C.c_uint8
lib.host_ctl_seq.argtypes = [C.c_void_p]
lib.host_ctl_seq.restype = C.c_uint16
lib.host_ctl_len.argtypes = [C.c_void_p]
lib.host_ctl_payload.argtypes = [C.c_void_p]
lib.host_ctl_counts.argtypes = [C.c_void_p, C.c_void_p]
lib.ota_rx_handle.argtypes = [C.c_void_p, C.c_char_p, C.c_size_t, C.c_uint32, C.c_void_p, C.c_size_t]
lib.ota_rx_tick.argtypes = [C.c_void_p, C.c_uint32]
lib.ota_rx_take_events.argtypes = [C.c_void_p]
lib.ota_rx_take_events.restype = C.c_uint8
lib.host_rx_info.argtypes = [C.c_void_p, C.c_void_p]
lib.host_tx_info.argtypes = [C.c_void_p, C.c_void_p]
lib.host_tx_config.argtypes = [C.c_void_p, C.c_void_p]
lib.host_tx_set_auth.argtypes = [C.c_void_p, C.c_char_p]
lib.ota_tx_start.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32, C.c_char_p, C.c_uint8, C.c_uint32, C.c_uint32]
lib.ota_tx_poll.argtypes = [C.c_void_p, C.c_uint32, C.c_void_p, C.c_size_t]
lib.ota_tx_on_reply.argtypes = [C.c_void_p, C.c_char_p, C.c_size_t, C.c_uint32]
lib.host_ring_new.argtypes = [C.c_uint32, C.c_uint8, C.c_uint16]
lib.host_ring_log.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p]
lib.host_ring_info.argtypes = [C.c_void_p, C.c_void_p]
lib.dbg_ring_pack.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t, C.c_void_p]
lib.dbg_ring_push_record.argtypes = [C.c_void_p, C.c_char_p, C.c_size_t, C.POINTER(C.c_size_t)]
lib.dbg_line_level.argtypes = [C.c_char_p, C.c_size_t]
lib.ota_image_check.argtypes = [C.c_char_p, C.c_size_t, C.c_uint16, C.c_char_p]
lib.ota_image_elf_sha.argtypes = [C.c_char_p, C.c_size_t]
lib.ota_auth_make.argtypes = [C.c_char_p, C.c_size_t, C.c_char_p, C.c_char_p, C.c_void_p]
lib.ota_auth_check.argtypes = [C.c_char_p, C.c_size_t, C.c_char_p, C.c_char_p]
lib.ota_hmac_sha256.argtypes = [C.c_char_p, C.c_size_t, C.c_char_p, C.c_size_t, C.c_void_p]
lib.ota_sha256_start.argtypes = [C.c_void_p]
lib.ota_sha256_update.argtypes = [C.c_void_p, C.c_char_p, C.c_size_t]
lib.ota_sha256_finish.argtypes = [C.c_void_p, C.c_void_p]

CTL_T_CMD, CTL_T_REPLY, CTL_T_HB, CTL_T_HB_ACK, CTL_T_LOG, CTL_T_OTA, CTL_T_OTA_ACK = 1, 2, 3, 4, 5, 6, 7
EV_BEGUN, EV_CLOSED, EV_DONE = 1, 2, 4


def sha256(data: bytes) -> bytes:
    ctx = C.create_string_buffer(256)
    out = C.create_string_buffer(32)
    lib.ota_sha256_start(ctx)
    # Uneven pieces: the block buffering is part of what is tested.
    i, step = 0, 1
    while i < len(data):
        lib.ota_sha256_update(ctx, data[i:i + step], len(data[i:i + step]))
        i += step
        step = step * 3 % 97 + 1
    lib.ota_sha256_finish(ctx, out)
    return out.raw


def hmac_sha256(key: bytes, msg: bytes) -> bytes:
    out = C.create_string_buffer(32)
    lib.ota_hmac_sha256(key, len(key), msg, len(msg), out)
    return out.raw


def auth_make(token, nonce, begin):
    out = C.create_string_buffer(32)
    lib.ota_auth_make(token, len(token), nonce, begin, out)
    return out.raw


def image_check(head: bytes, chip: int, project):
    return lib.ota_image_check(head, len(head), chip, project.encode() if project else None)


def image_elf(head: bytes):
    p = lib.ota_image_elf_sha(head, len(head))
    return C.string_at(p, 32).hex() if p else None


def status_name(code):
    return lib.ota_status_name(code).decode()


# --- UART frame -------------------------------------------------------------------------------------------------------

def ctl_frame(kind, seq, payload=b""):
    out = C.create_string_buffer(8 + len(payload) + 2)
    n = lib.host_ctl_build(out, kind, seq, payload, len(payload))
    return out.raw[:n]


class CtlParser:
    def __init__(self):
        self.p = lib.host_ctl_parser_new()

    def __del__(self):
        lib.host_ctl_parser_free(self.p)

    def feed(self, data: bytes, now_ms: int):
        """Whole valid frames in the bytes: list of (type, seq, payload)."""
        out = []
        for b in data:
            if lib.host_ctl_feed(self.p, b, now_ms & 0xFFFFFFFF):
                n = lib.host_ctl_len(self.p)
                out.append((lib.host_ctl_type(self.p), lib.host_ctl_seq(self.p),
                            C.string_at(lib.host_ctl_payload(self.p), n)))
        return out

    def counts(self):
        v = (C.c_uint32 * 5)()
        lib.host_ctl_counts(self.p, v)
        return dict(zip(("frames", "bad_header", "bad_crc", "truncated", "noise"), v))


# --- Receiver --------------------------------------------------------------------------------------------------------

CAP = C.CFUNCTYPE(C.c_uint32, C.c_void_p)
MAYB = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint8)
BUSY = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint8)
HEAD = C.CFUNCTYPE(C.c_int, C.c_void_p, u8p, C.c_size_t)
AUTH = C.CFUNCTYPE(C.c_int, C.c_void_p, u8p)
BEGIN = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint32, C.c_uint8)
WRITE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint32, u8p, C.c_size_t)
FINISH = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint32, u8p, C.c_uint8)
ABORT = C.CFUNCTYPE(None, C.c_void_p)
STEXT = C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.c_void_p, C.c_size_t)


class RxPort(C.Structure):
    _fields_ = [("ctx", C.c_void_p), ("capacity", CAP), ("may_begin", MAYB), ("busy", BUSY), ("check_head", HEAD),
                ("auth", AUTH), ("begin", BEGIN), ("write", WRITE), ("finish", FINISH), ("abort", ABORT),
                ("status_text", STEXT)]


RX_FIELDS = ("state", "session", "size", "next", "flags", "baud", "last_err", "begins", "chunks", "dups", "gaps", "busy",
             "aborts", "timeouts", "done")


class CReceiver:
    """The C receiver with a flash in a bytearray. Policy attributes: capacity, slc, sco, chip, project, token, nonce,
    fail_write_at (offset that fails), fail_finish (status). After a good END: active holds the image, unless DRYRUN."""

    def __init__(self, capacity, chip=None, project=None, token=None, idle_ms=30000):
        self.capacity, self.chip, self.project, self.token = capacity, chip, project, token
        self.slc = self.sco = False
        self.nonce = None
        self.flash = bytearray()
        self.active = None
        self.dryrun_ok = 0
        self.fail_write_at = None
        self.fail_finish = 0
        self.writes_seen = []
        self._cbs = [CAP(self._cap), MAYB(self._may), BUSY(self._busy), HEAD(self._head),
                     AUTH(self._auth) if token is not None else C.cast(None, AUTH), BEGIN(self._begin),
                     WRITE(self._write), FINISH(self._finish), ABORT(self._abort), STEXT(self._stext)]
        self.port = RxPort(None, *self._cbs)
        self.rx = lib.host_rx_new(C.byref(self.port), idle_ms)
        self.out = C.create_string_buffer(256)

    def _cap(self, ctx):
        return self.capacity

    def _may(self, ctx, flags):
        return 2 if (self.slc or self.sco) and not flags & 1 else 0

    def _busy(self, ctx, flags):
        return 1 if self.sco else 0

    def _head(self, ctx, p, n):
        head = C.string_at(p, n)
        if self.chip is None:
            return 0
        return lib.ota_image_check(head, n, self.chip, self.project.encode() if self.project else None)

    def _auth(self, ctx, p):
        begin = C.string_at(p, 78)
        nonce, self.nonce = self.nonce, None        # one BEGIN per nonce
        if nonce is None:
            return 0
        return 1 if lib.ota_auth_check(self.token, len(self.token), nonce, begin) else 0

    def _begin(self, ctx, size, flags):
        self.flash = bytearray(b"\xff" * size)      # erased
        self.writes_seen = []
        return 0

    def _write(self, ctx, offset, p, n):
        if self.fail_write_at is not None and offset >= self.fail_write_at:
            return 1
        self.flash[offset:offset + n] = C.string_at(p, n)
        self.writes_seen.append((offset, n))
        return 0

    def _finish(self, ctx, size, sha, flags):
        if self.fail_finish:
            return self.fail_finish
        if flags & 4:
            self.dryrun_ok += 1
        else:
            self.active = bytes(self.flash[:size])
        return 0

    def _abort(self, ctx):
        self.flash = bytearray()

    def _stext(self, ctx, out, cap):
        text = b"run=host"[:cap - 1]
        C.memmove(out, text, len(text))
        return len(text)

    def handle(self, req: bytes, now_ms: int) -> bytes:
        n = lib.ota_rx_handle(self.rx, req, len(req), now_ms & 0xFFFFFFFF, self.out, 256)
        return self.out.raw[:n]

    def tick(self, now_ms):
        lib.ota_rx_tick(self.rx, now_ms & 0xFFFFFFFF)

    def events(self):
        return lib.ota_rx_take_events(self.rx)

    def info(self):
        v = (C.c_uint32 * 15)()
        lib.host_rx_info(self.rx, v)
        d = dict(zip(RX_FIELDS, v))
        d["state_name"] = lib.ota_rx_state_name(d["state"]).decode()
        d["err_name"] = status_name(d["last_err"])
        return d

    def reset(self):
        """A restart of the board: the session is gone."""
        lib.host_free(self.rx)
        self.rx = lib.host_rx_new(C.byref(self.port), 30000)


# --- Sender ----------------------------------------------------------------------------------------------------------

READ = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint32, u8p, C.c_size_t)


class TxPort(C.Structure):
    _fields_ = [("ctx", C.c_void_p), ("read", READ)]


TX_FIELDS = ("state", "fail_status", "acked", "sent_to", "session", "stalls", "restarts", "sent", "resent",
             "busy_replies", "stale")
TX_STATES = ("begin", "send", "end", "done", "failed")


class CSender:
    """The C sender reading from a bytes object. Times in ms."""

    def __init__(self, data: bytes, chunk=1008, window=2, rto=600, begin_rto=2000, begin_max=90000, busy_wait=1000,
                 busy_max=1200000, max_stalls=20, max_restarts=3):
        self.data = data
        self._read_cb = READ(self._read)
        self.port = TxPort(None, self._read_cb)
        self.tx = lib.host_tx_new(C.byref(self.port))
        cfg = (C.c_uint32 * 9)(chunk, window, rto, begin_rto, begin_max, busy_wait, busy_max, max_stalls, max_restarts)
        lib.host_tx_config(self.tx, cfg)
        self.out = C.create_string_buffer(1100)

    def _read(self, ctx, offset, p, n):
        C.memmove(p, self.data[offset:offset + n], n)
        return 0

    def start(self, session, sha, flags=0, baud=0, now_ms=0, auth=None):
        if auth:
            lib.host_tx_set_auth(self.tx, auth)
        lib.ota_tx_start(self.tx, session, len(self.data), sha, flags, baud, now_ms & 0xFFFFFFFF)

    def poll(self, now_ms):
        n = lib.ota_tx_poll(self.tx, now_ms & 0xFFFFFFFF, self.out, 1100)
        return self.out.raw[:n] if n else None

    def on_reply(self, rep, now_ms):
        lib.ota_tx_on_reply(self.tx, rep, len(rep), now_ms & 0xFFFFFFFF)

    def info(self):
        v = (C.c_uint32 * 11)()
        lib.host_tx_info(self.tx, v)
        d = dict(zip(TX_FIELDS, v))
        d["state_name"] = TX_STATES[d["state"]]
        d["fail_name"] = status_name(d["fail_status"])
        return d

    @property
    def state(self):
        return self.info()["state_name"]


# --- Log ring --------------------------------------------------------------------------------------------------------

class Ring:
    def __init__(self, n, board, boot):
        self.r = lib.host_ring_new(n, board, boot)

    def log(self, t_ms, line: str):
        return lib.host_ring_log(self.r, t_ms, line.encode("utf-8"))

    def push_record(self, rec: bytes):
        used = C.c_size_t(0)
        rc = lib.dbg_ring_push_record(self.r, rec, len(rec), C.byref(used))
        return rc, used.value

    def pack(self, cap=1396):
        out = C.create_string_buffer(cap)
        cnt = C.c_uint32(0)
        n = lib.dbg_ring_pack(self.r, out, cap, C.byref(cnt))
        return out.raw[:n], cnt.value

    def info(self):
        v = (C.c_uint32 * 4)()
        lib.host_ring_info(self.r, v)
        return dict(zip(("count", "lost", "seq", "taken"), v))


def line_level(text: str):
    b = text.encode()
    return lib.dbg_line_level(b, len(b))
