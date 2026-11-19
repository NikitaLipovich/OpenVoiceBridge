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
lib.dbg_ring_push_long.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p, C.c_size_t]
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

    def log_long(self, t_ms, line: str):
        """s3_log_line() of the S3: a line longer than a slot as several records; returns how many."""
        b = line.encode("utf-8")
        return lib.dbg_ring_push_long(self.r, t_ms, b, len(b))

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


SEND_FN = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_uint8), C.c_size_t)
lib.host_udp_new.restype = C.c_void_p
lib.host_udp_info.argtypes = [C.c_void_p, C.c_void_p]
lib.dbg_udp_pack.restype = C.c_size_t
lib.dbg_udp_pack.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p]
lib.dbg_udp_round.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.c_int, SEND_FN, C.c_void_p]


class UdpSender:
    """dbg_udp_round() of the S3's log task over two rings; send(bytes) -> True when the datagram went (goal 094 #002)."""

    def __init__(self, first: "Ring", second: "Ring" = None):
        self.u = lib.host_udp_new()
        self.first, self.second = first, second

    def round(self, send, burst=8):
        def cb(ctx, data, n):
            return 0 if send(C.string_at(data, n)) else 1
        fn = SEND_FN(cb)
        return lib.dbg_udp_round(self.u, self.first.r, self.second.r if self.second else None, burst, fn, None)

    def info(self):
        v = (C.c_uint32 * 3)()
        lib.host_udp_info(self.u, v)
        return dict(zip(("held", "datagrams", "send_fail"), v))


lib.host_soft_limit.argtypes = [C.c_int32]
lib.host_soft_limit.restype = C.c_int16


def soft_limit(s: int) -> int:
    """call_soft_limit() of the LyraT (call_limit.h): a sample times the call gain in, the limited sample out."""
    return lib.host_soft_limit(s)


def line_level(text: str):
    b = text.encode()
    return lib.dbg_line_level(b, len(b))


# --- the LyraT's updater: the rules of upd_mode.c (goal 096) -----------------------------------------------------------

lib.upd_app_state.argtypes = [C.c_int, C.c_int, C.c_uint32]
lib.upd_app_name.argtypes = [C.c_int]
lib.upd_app_name.restype = C.c_char_p
lib.host_watch_new.restype = C.c_void_p
lib.upd_watch_init.argtypes = [C.c_void_p, C.c_uint32, C.c_int, C.c_uint8]
lib.upd_watch_step.argtypes = [C.c_void_p, C.c_uint32, C.c_int, C.c_uint32]
lib.upd_watch_set_app.argtypes = [C.c_void_p, C.c_int]
lib.upd_step_name.argtypes = [C.c_int]
lib.upd_step_name.restype = C.c_char_p
lib.upd_watch_left_s.argtypes = [C.c_void_p, C.c_uint32]
lib.upd_watch_left_s.restype = C.c_int32
lib.upd_command.argtypes = [C.c_char_p, C.c_int, C.c_void_p, C.c_size_t]
lib.upd_command.restype = C.c_uint8

UPD_APPS = ("none", "ok", "fresh", "bad")
UPD_STEPS = ("stay", "boot_app", "boot_retry", "boot_new")


def upd_app_state(image_ok, fresh, tries):
    return lib.upd_app_name(lib.upd_app_state(int(image_ok), int(fresh), tries)).decode()


def upd_command(args, sco_on=False):
    """(stay value 0/1/2, reply text) of the firmware's "upd" rules."""
    out = C.create_string_buffer(120)
    stay = lib.upd_command(args.encode(), int(sco_on), out, 120)
    return stay, out.value.decode()


class CUpdWatch:
    """The updater's window (upd_watch_*) with the clock given by the caller, in ms."""

    def __init__(self, now_ms, app="ok", stay=0):
        self.w = lib.host_watch_new()
        lib.upd_watch_init(self.w, now_ms & 0xFFFFFFFF, UPD_APPS.index(app), stay)

    def __del__(self):
        if getattr(self, "w", None):
            lib.host_free(self.w)

    def step(self, now_ms, active=False, done=0):
        return UPD_STEPS[lib.upd_watch_step(self.w, now_ms & 0xFFFFFFFF, int(active), done)]

    def set_app(self, app):
        lib.upd_watch_set_app(self.w, UPD_APPS.index(app))

    def left_s(self, now_ms):
        return lib.upd_watch_left_s(self.w, now_ms & 0xFFFFFFFF)


# Goal 106: the AFH map of the LyraT from the S3's view of Wi-Fi (afh_map.c).
class AfhNet(C.Structure):
    _fields_ = [("primary", C.c_uint8), ("second", C.c_uint8), ("rssi", C.c_int8)]


AFH_NETS = 8


class AfhReport(C.Structure):
    _fields_ = [("gen", C.c_uint8), ("ap_primary", C.c_uint8), ("ap_second", C.c_uint8), ("ap_rssi", C.c_int8),
                ("flags", C.c_uint8), ("scan_age_s", C.c_uint16), ("n", C.c_uint8), ("nets", AfhNet * AFH_NETS)]


lib.afh_report_parse.argtypes = [C.c_char_p, C.c_size_t, C.POINTER(AfhReport)]
lib.afh_report_encode.argtypes = [C.POINTER(AfhReport), C.c_void_p, C.c_size_t]
lib.afh_report_encode.restype = C.c_size_t
lib.afh_scan_reduce.argtypes = [C.POINTER(AfhNet), C.c_size_t, C.POINTER(AfhNet), C.c_size_t]
lib.afh_scan_reduce.restype = C.c_size_t
lib.afh_span.argtypes = [C.c_uint8, C.c_uint8, C.c_bool, u8p, u8p]
lib.afh_span.restype = C.c_bool
lib.afh_map_count.argtypes = [C.c_char_p]
lib.afh_map_hex.argtypes = [C.c_char_p, C.c_char_p]
lib.afh_map_from_hex.argtypes = [C.c_char_p, C.c_void_p]
lib.afh_map_from_hex.restype = C.c_bool
lib.afh_build.argtypes = [C.POINTER(AfhReport), C.c_bool, C.c_char_p, C.c_void_p, C.c_char_p, C.c_size_t]
lib.host_afh_ctl_new.argtypes = [C.c_int]
lib.host_afh_ctl_new.restype = C.c_void_p
lib.host_afh_ctl_info.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.c_char_p, C.c_size_t]
lib.afh_ctl_on_ack.argtypes = [C.c_void_p, C.c_char_p, C.c_size_t, C.c_uint32]
lib.afh_ctl_target.argtypes = [C.c_void_p, C.c_uint32, C.c_void_p]
lib.afh_ctl_on_slc.argtypes = [C.c_void_p]
lib.afh_ctl_set_mode.argtypes = [C.c_void_p, C.c_int, C.c_char_p]
lib.afh_ctl_poll.argtypes = [C.c_void_p, C.c_uint32, C.c_bool, C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)]
lib.afh_ctl_poll.restype = C.c_bool
lib.afh_ctl_applied.argtypes = [C.c_void_p, C.c_char_p, C.c_int]

AFH_MODES = ("all", "auto", "fixed")
AFH_SRCS = ("none", "s3", "cmd")
AFH_WHYS = ("s3", "slc", "sco_end", "stale", "cmd")
AFH_ALL = bytes([0xFF] * 9 + [0x7F])


def afh_report(ap=0, second=0, rssi=0, flags=1, age=0xFFFF, nets=(), gen=0):
    """An AfhReport; nets: (primary, second, rssi) tuples."""
    r = AfhReport(gen=gen, ap_primary=ap, ap_second=second, ap_rssi=rssi, flags=flags, scan_age_s=age, n=len(nets))
    for i, (p, s, q) in enumerate(nets):
        r.nets[i] = AfhNet(p, s, q)
    return r


def afh_ack(report, log=(1, 3)):
    """The bytes of a heartbeat ack of a new S3: two log bytes, then the report (afh_report_encode)."""
    buf = (C.c_uint8 * 64)()
    n = lib.afh_report_encode(C.byref(report), buf, 64)
    return bytes(log) + bytes(buf[:n])


def afh_parse(ack: bytes):
    r = AfhReport()
    return lib.afh_report_parse(ack, len(ack), C.byref(r)), r


def afh_span(primary, second=0, wide=False):
    lo, hi = C.c_uint8(), C.c_uint8()
    ok = lib.afh_span(primary, second, wide, C.cast(C.byref(lo), u8p), C.cast(C.byref(hi), u8p))
    return (lo.value, hi.value) if ok else None


def afh_hex(m: bytes) -> str:
    out = C.create_string_buffer(21)
    lib.afh_map_hex(m, out)
    return out.value.decode()


def afh_from_hex(text: str):
    m = (C.c_uint8 * 10)()
    return bytes(m) if lib.afh_map_from_hex(text.encode(), m) else None


def afh_count(m: bytes) -> int:
    return lib.afh_map_count(m)


def afh_build(report, scan_ok=True, prev=None):
    """(map bytes, usable channels, exclusions text)."""
    out = (C.c_uint8 * 10)()
    excl = C.create_string_buffer(96)
    n = lib.afh_build(C.byref(report), scan_ok, prev, out, excl, 96)
    return bytes(out), n, excl.value.decode()


def afh_reduce(nets, maximum=8):
    arr = (AfhNet * max(1, len(nets)))(*[AfhNet(*t) for t in nets])
    out = (AfhNet * 42)()
    n = lib.afh_scan_reduce(arr, len(nets), out, maximum)
    return [(out[i].primary, out[i].second, out[i].rssi) for i in range(n)]


class CAfhCtl:
    """The LyraT's AFH policy (afh_ctl_*) with the clock given by the caller, in ms."""

    def __init__(self, mode="all"):
        self.c = lib.host_afh_ctl_new(AFH_MODES.index(mode))

    def __del__(self):
        if getattr(self, "c", None):
            lib.host_free(self.c)

    def ack(self, data: bytes, now_ms):
        return lib.afh_ctl_on_ack(self.c, data, len(data), now_ms & 0xFFFFFFFF)

    def target(self, now_ms):
        out = (C.c_uint8 * 10)()
        src = lib.afh_ctl_target(self.c, now_ms & 0xFFFFFFFF, out)
        return bytes(out), AFH_SRCS[src]

    def slc(self):
        lib.afh_ctl_on_slc(self.c)

    def set_mode(self, mode, m=None):
        lib.afh_ctl_set_mode(self.c, AFH_MODES.index(mode), m)

    def poll(self, now_ms, sco=False, apply=True):
        """None, or (map, src, why) given to the controller (and marked applied unless apply=False)."""
        out = (C.c_uint8 * 10)()
        src, why = C.c_int(), C.c_int()
        if not lib.afh_ctl_poll(self.c, now_ms & 0xFFFFFFFF, sco, out, C.byref(src), C.byref(why)):
            return None
        if apply:
            lib.afh_ctl_applied(self.c, bytes(out), src.value)
        return bytes(out), AFH_SRCS[src.value], AFH_WHYS[why.value]

    def info(self):
        v = (C.c_uint32 * 10)()
        m = (C.c_uint8 * 10)()
        excl = C.create_string_buffer(96)
        lib.host_afh_ctl_info(self.c, v, m, excl, 96)
        keys = ("mode", "have", "applied_src", "force", "deferred", "reports", "bad", "applies", "deferrals", "rep_ms")
        d = dict(zip(keys, list(v)))
        d["mode"], d["applied_src"] = AFH_MODES[d["mode"]], AFH_SRCS[d["applied_src"]]
        d["applied"], d["excl"] = bytes(m), excl.value.decode()
        return d
# --- the LyraT's two modes, phone and PC: the rules of hf_mode.c (goal 102) ---------------------------------------------

ADDR = C.c_uint8 * 6
lib.host_sizeof_hfm.restype = C.c_size_t
lib.hfm_init.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_void_p, C.c_void_p, C.c_void_p]
lib.hfm_on_paired.argtypes = [C.c_void_p, C.c_void_p]
lib.hfm_on_slc.argtypes = [C.c_void_p, C.c_void_p]
lib.hfm_next_target.argtypes = [C.c_void_p, C.c_void_p, C.c_int, C.c_void_p]
lib.hfm_command.argtypes = [C.c_void_p, C.c_char_p, C.c_int, C.c_void_p, C.c_char_p, C.c_size_t]
lib.hfm_forget_all.argtypes = [C.c_void_p]
lib.hfm_role.argtypes = [C.c_void_p, C.c_void_p]
lib.hfm_role.restype = C.c_char_p
lib.hfm_mode_name.argtypes = [C.c_int]
lib.hfm_mode_name.restype = C.c_char_p
lib.hfm_slc_name.argtypes = [C.c_int]
lib.hfm_slc_name.restype = C.c_char_p
lib.hfm_addr_short.argtypes = [C.c_void_p, C.c_char_p, C.c_size_t]
lib.hfm_addr_short.restype = C.c_char_p

HFM_MODES = ("phone", "pc")
HFM_DO_SAVE, HFM_DO_DROP, HFM_DO_KICK = 1, 2, 4


def _addr(a):
    return None if a is None else ADDR(*a)


class CHfm:
    """hfm_t of hf_mode.c. Addresses are 6-byte bytes; the NVS view (mode, slots) is read back from the struct."""

    # layout of hfm_t (hf_mode.h): mode, src, have[2], peer[2][6], have_paired, paired[6], dirty, migrated, (pad), turn
    class _S(C.Structure):
        _fields_ = [("mode", C.c_uint8), ("src", C.c_uint8), ("have", C.c_uint8 * 2), ("peer", (C.c_uint8 * 6) * 2),
                    ("have_paired", C.c_uint8), ("paired", C.c_uint8 * 6), ("dirty", C.c_uint8),
                    ("migrated", C.c_uint8), ("turn", C.c_uint32)]

    def __init__(self, nvs_mode=None, default_mode="phone", phone=None, pc=None, mru0=None):
        assert C.sizeof(self._S) == lib.host_sizeof_hfm(), "hfm_t layout changed: update CHfm._S"
        self.s = self._S()
        lib.hfm_init(C.byref(self.s), -1 if nvs_mode is None else HFM_MODES.index(nvs_mode),
                     HFM_MODES.index(default_mode), _addr(phone), _addr(pc), _addr(mru0))

    @property
    def mode(self):
        return HFM_MODES[self.s.mode]

    @property
    def src(self):
        return ("build", "nvs")[self.s.src]

    def slot(self, mode):
        i = HFM_MODES.index(mode)
        return bytes(self.s.peer[i]) if self.s.have[i] else None

    @property
    def dirty(self):
        return bool(self.s.dirty)

    def saved(self):
        """What a save writes to NVS, and clears dirty as the glue does after a good commit."""
        self.s.dirty = 0
        return {"mode": self.mode, "phone": self.slot("phone"), "pc": self.slot("pc")}

    def on_paired(self, bda):
        lib.hfm_on_paired(C.byref(self.s), _addr(bda))

    def on_slc(self, bda):
        return lib.hfm_slc_name(lib.hfm_on_slc(C.byref(self.s), _addr(bda))).decode()

    def next_target(self, bonds):
        arr = (ADDR * max(1, len(bonds)))(*[ADDR(*b) for b in bonds])
        out = ADDR()
        return bytes(out) if lib.hfm_next_target(C.byref(self.s), arr, len(bonds), out) else None

    def command(self, args, sco_on=False, slc_bda=None):
        """(action bits, reply text) of "mode <args>"."""
        out = C.create_string_buffer(160)
        act = lib.hfm_command(C.byref(self.s), args.encode(), int(sco_on), _addr(slc_bda), out, 160)
        return act, out.value.decode()

    def forget_all(self):
        lib.hfm_forget_all(C.byref(self.s))

    def role(self, bda):
        return lib.hfm_role(C.byref(self.s), _addr(bda)).decode()


def hfm_short(bda):
    out = C.create_string_buffer(16)
    return lib.hfm_addr_short(_addr(bda), out, 16).decode()
