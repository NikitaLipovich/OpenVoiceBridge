#!/usr/bin/env python3
"""Firmware over the network: the S3-ETH itself, and the LyraT through the S3. Protocol: docs/OTA.md.

  ota.py s3 IMAGE [options]       image to the S3 over UDP 5007
  ota.py lyrat IMAGE [options]    image to the LyraT: staged and checked on the S3, then sent over the control UART
  ota.py query                    state of both boards, JSON
  ota.py info IMAGE               what the file is: chip, project, version, ELF SHA-256, size

Options: --host ADDR (else BENCH_S3_ADDR, else found by broadcast like s3_uart.py), --port 5007, --token-file PATH
(else BENCH_OTA_TOKEN, else s3_eth_bridge/main/ota_secrets.h), --dry-run (everything is received and checked, nothing is
activated), --test-rollback (the new image fails its self-test once on purpose: success is the old image running again),
--force (LyraT: also with SLC up or SCO on), --baud N (LyraT: UART baud of the transfer, both ends agree, 115200 after),
--window N, --timeout S, --label NAME, --quiet.

The image is checked before sending: chip, project and size. After the transfer the tool waits for the restart, compares
the ELF SHA-256 of the running image with the file and waits until the board confirmed it (self-test passed).
Exit code: 0 done, 1 refused or failed, 2 S3 not found, 3 rolled back (0 with --test-rollback), 4 timeout.

As a module: Image, Sender and Receiver (the protocol model the PC tests run against the C code), Link (UDP),
upload(), wait_s3(), wait_lyrat(). The token never goes over the network; it is not written into the run record.
"""
import argparse
import hashlib
import hmac
import json
import os
import random
import re
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import REPO_DIR, run_dir, write_json  # noqa: E402

OTA_PORT = 5007
UDP_VERSION = 1
CHUNK_MAX = 1008
REQ_HDR = 9
BEGIN_BYTES = 78
BEGIN_SIGNED = 46
REPLY_HDR = 10
HEAD_MIN = 208
NONCE_BYTES = 16

OP_BEGIN, OP_DATA, OP_END, OP_ABORT, OP_STATUS, OP_HELLO, OP_QUERY = 1, 2, 3, 4, 5, 0x10, 0x11
TARGET_SELF, TARGET_LYRAT = 0, 1
F_FORCE, F_FAILTEST, F_DRYRUN = 1, 2, 4

STATUS = ["ok", "busy", "refused", "no_session", "too_big", "bad_image", "flash", "sha", "incomplete", "auth", "bad_req",
          "verify", "no_slot", "timeout"]
ST = {name: i for i, name in enumerate(STATUS)}

CHIP_ESP32, CHIP_ESP32S3 = 0x0000, 0x0009
BOARDS = {
    "s3": {"target": TARGET_SELF, "chip": CHIP_ESP32S3, "project": "s3_eth_bridge", "slot": 0x300000},
    "lyrat": {"target": TARGET_LYRAT, "chip": CHIP_ESP32, "project": "lyrat_resend", "slot": 0x1F0000},
}
TOKEN_FILE = REPO_DIR / "s3_eth_bridge" / "main" / "ota_secrets.h"

EXIT_OK, EXIT_FAIL, EXIT_NOT_FOUND, EXIT_ROLLED_BACK, EXIT_TIMEOUT = 0, 1, 2, 3, 4


def status_name(code):
    return STATUS[code] if 0 <= code < len(STATUS) else "?%d" % code


# ---------------------------------------------------------------------------------------------------------------------
# Image file

class Image:
    """An app image as idf.py writes it: header, first segment, then the app description."""

    def __init__(self, data: bytes, name: str = ""):
        self.data = bytes(data)
        self.name = name
        self.size = len(self.data)
        self.sha256 = hashlib.sha256(self.data).digest()
        self.magic_ok = self.size >= HEAD_MIN and self.data[0] == 0xE9 and \
            struct.unpack_from("<I", self.data, 32)[0] == 0xABCD5432
        self.chip = struct.unpack_from("<H", self.data, 12)[0] if self.size >= 14 else None
        self.version = self._text(48, 32) if self.magic_ok else ""
        self.project = self._text(80, 32) if self.magic_ok else ""
        self.elf = self.data[176:208].hex() if self.magic_ok else ""

    def _text(self, at, n):
        return self.data[at:at + n].split(b"\0", 1)[0].decode("ascii", "replace")

    @classmethod
    def load(cls, path):
        p = Path(path)
        return cls(p.read_bytes(), p.name)

    def problem(self, board):
        """None when the file fits the board, else why not."""
        b = BOARDS[board]
        if not self.magic_ok:
            return "not an app image (magic)"
        if self.chip != b["chip"]:
            return "chip id %d, the %s needs %d" % (self.chip, board, b["chip"])
        if self.project != b["project"]:
            return "project %r, the %s runs %r" % (self.project, board, b["project"])
        if self.size > b["slot"]:
            return "%d bytes, the slot holds %d" % (self.size, b["slot"])
        return None

    def info(self):
        return {"name": self.name, "size": self.size, "chip": self.chip, "project": self.project, "version": self.version,
                "elf": self.elf[:16], "sha256": self.sha256.hex()}


# ---------------------------------------------------------------------------------------------------------------------
# Messages

def build_begin(session, size, sha, flags=0, baud=0, auth=None):
    msg = struct.pack("<BII", OP_BEGIN, session, size) + sha + struct.pack("<BI", flags, baud)
    return msg + (auth if auth else bytes(32))


def build_short(op, session):
    return struct.pack("<BI", op, session)


def build_data(session, offset, chunk):
    return struct.pack("<BII", OP_DATA, session, offset) + chunk


def auth_make(token: bytes, nonce: bytes, begin: bytes) -> bytes:
    return hmac.new(token, nonce + begin[:BEGIN_SIGNED], hashlib.sha256).digest()


def parse_reply(rep):
    if len(rep) < REPLY_HDR:
        return None
    op, st, session, nxt = struct.unpack_from("<BBII", rep, 0)
    return {"op": op, "status": st, "session": session, "next": nxt, "text": rep[REPLY_HDR:].decode("ascii", "replace")}


# ---------------------------------------------------------------------------------------------------------------------
# Sender, go-back-N. Times in seconds. Same rules as ota_tx in ota_link.c, written from the spec, not from the C.

class Sender:
    def __init__(self, data, session, flags=0, baud=0, auth=None, chunk=CHUNK_MAX, window=4, rto=0.3, begin_rto=1.0,
                 begin_max=90.0, busy_wait=1.0, busy_max=1200.0, max_stalls=20, max_restarts=0):
        self.data, self.size = data, len(data)
        self.sha = hashlib.sha256(data).digest()
        self.session, self.flags, self.baud, self.auth = session, flags, baud, auth
        self.chunk, self.window, self.rto, self.begin_rto, self.begin_max = chunk, window, rto, begin_rto, begin_max
        self.busy_wait, self.busy_max, self.max_stalls, self.max_restarts = busy_wait, busy_max, max_stalls, max_restarts
        self.state = "begin"
        self.fail = None
        self.acked = self.sent_to = 0
        self.deadline = self.phase = None
        self.busy_since = None
        self.busy_now = False
        self.fast_at = None
        self.stalls = self.restarts = self.sent = self.resent = self.busy_replies = self.stale = 0

    def _failed(self, status):
        self.state, self.fail = "failed", status

    def poll(self, now):
        """The next request to send now, or None."""
        if self.phase is None:
            self.phase = self.deadline = now
        if self.state == "begin":
            if now < self.deadline:
                return None
            if now - self.phase > self.begin_max:
                self._failed(ST["timeout"])
                return None
            self.deadline = now + self.begin_rto
            self.sent += 1
            return build_begin(self.session, self.size, self.sha, self.flags, self.baud, self.auth)
        if self.state == "send":
            if self.busy_now:
                if now < self.deadline:
                    return None
                self.busy_now = False
                self.sent_to = self.acked
            limit = min(self.size, self.acked + self.window * self.chunk)
            if self.sent_to < limit:
                n = min(self.chunk, self.size - self.sent_to)
                if self.sent_to == self.acked:
                    self.deadline = now + self.rto
                req = build_data(self.session, self.sent_to, self.data[self.sent_to:self.sent_to + n])
                self.sent += 1
                self.sent_to += n
                return req
            if now >= self.deadline:
                self.stalls += 1
                if self.stalls > self.max_stalls:
                    self._failed(ST["timeout"])
                    return None
                self.resent += 1
                self.sent_to = self.acked
                return self.poll(now)
            return None
        if self.state == "end":
            if now < self.deadline:
                return None
            if now - self.phase > self.begin_max:
                self._failed(ST["timeout"])
                return None
            self.deadline = now + self.begin_rto
            self.sent += 1
            return build_short(OP_END, self.session)
        return None

    def _end(self, now):
        self.state, self.deadline, self.phase = "end", now, now

    def on_reply(self, rep, now):
        r = parse_reply(rep)
        if r is None or self.state in ("done", "failed"):
            return
        if r["session"] != self.session:
            self.stale += 1
            return
        st, nxt = r["status"], r["next"]
        if r["op"] == OP_BEGIN:
            if self.state != "begin":
                return
            if st != ST["ok"]:
                self._failed(st)
                return
            self.state = "send"
            self.acked = self.sent_to = nxt if nxt <= self.size else 0
            self.deadline = self.phase = now
            self.stalls = 0
            if self.acked == self.size:
                self._end(now)
        elif r["op"] == OP_DATA:
            if self.state != "send":
                return
            if st == ST["busy"]:
                self.busy_replies += 1
                if self.busy_since is None:
                    self.busy_since = now
                elif now - self.busy_since > self.busy_max:
                    self._failed(st)
                    return
                self.busy_now = True
                self.deadline = now + self.busy_wait
                self.sent_to = self.acked
                return
            self.busy_since = None
            if st == ST["no_session"]:
                self.restarts += 1
                if self.auth or self.restarts > self.max_restarts:
                    self._failed(st)
                    return
                self.session = (self.session + 1) & 0xFFFFFFFF
                self.state, self.acked, self.sent_to = "begin", 0, 0
                self.deadline = self.phase = now
                self.busy_now, self.fast_at = False, None
                return
            if st != ST["ok"]:
                self._failed(st)
                return
            if nxt > self.size:
                self._failed(ST["bad_req"])
                return
            if nxt > self.acked:
                self.acked, self.stalls, self.fast_at = nxt, 0, None
                self.deadline = now + self.rto
                self.sent_to = max(self.sent_to, self.acked)
                if self.acked == self.size:
                    self._end(now)
            elif nxt == self.acked and self.sent_to > self.acked:
                if self.fast_at != self.acked:
                    self.fast_at = self.acked
                    self.resent += 1
                    self.sent_to = self.acked
                    self.deadline = now + self.rto
            elif nxt < self.acked:
                self.acked = self.sent_to = nxt
                self.deadline = now + self.rto
        elif r["op"] == OP_END:
            if self.state != "end":
                return
            if st == ST["ok"]:
                self.state = "done"
            elif st == ST["incomplete"] and nxt < self.size:
                self.state, self.acked, self.sent_to, self.deadline = "send", nxt, nxt, now
            else:
                self._failed(st)
        else:
            self.stale += 1


# ---------------------------------------------------------------------------------------------------------------------
# Receiver model: what the boards do, in Python, for the tests of the C sender and of the S3 emulator

class Receiver:
    """Flash and policy are plain attributes so a test can set them: capacity, slc, sco, token, nonce, chip, project."""

    def __init__(self, capacity, chip=None, project=None, token=None, idle=30.0):
        self.capacity, self.chip, self.project, self.token, self.idle = capacity, chip, project, token, idle
        self.slc = self.sco = False
        self.nonce = None
        self.flash = bytearray()
        self.active = None          # the image of the last END: bytes
        self.state = "idle"
        self.session = self.size = self.next = 0
        self.flags = self.baud = 0
        self.sha = b""
        self.last = 0.0
        self.last_err = 0
        self.memo = None
        self.events = set()
        self.count = {k: 0 for k in ("begins", "chunks", "dups", "gaps", "busy", "aborts", "timeouts", "done")}

    def _rep(self, op, st, session, nxt, text=b""):
        return struct.pack("<BBII", op, st, session, nxt) + text

    def _close(self, err=0):
        self.state = "idle"
        self.events.add("closed")
        if err:
            self.last_err = err

    def head_ok(self, head):
        img = Image(head + bytes(max(0, HEAD_MIN - len(head))))
        if not img.magic_ok or (self.chip is not None and img.chip != self.chip):
            return False
        return self.project is None or img.project == self.project

    def handle(self, req, now):
        if len(req) < 5:
            return self._rep(req[0] if req else 0, ST["bad_req"], 0, 0)
        op, session = struct.unpack_from("<BI", req, 0)
        if op == OP_BEGIN:
            if len(req) != BEGIN_BYTES:
                return self._rep(op, ST["bad_req"], session, 0)
            if self.state == "recv" and session == self.session:
                self.last = now
                return self._rep(op, ST["ok"], session, self.next)
            if self.state == "done":
                same = session == self.session
                return self._rep(op, ST["ok"] if same else ST["refused"], session, self.size if same else 0)
            if self.token is not None:
                ok = self.nonce is not None and hmac.compare_digest(
                    auth_make(self.token, self.nonce, req), req[BEGIN_SIGNED:])
                self.nonce = None
                if not ok:
                    self.last_err = ST["auth"]
                    return self._rep(op, ST["auth"], session, 0)
            size, = struct.unpack_from("<I", req, 5)
            flags, baud = struct.unpack_from("<BI", req, 41)
            st = ST["no_slot"] if not self.capacity else \
                ST["refused"] if (self.slc or self.sco) and not flags & F_FORCE else ST["ok"]
            if st == ST["ok"] and not 0 < size <= self.capacity:
                st = ST["too_big"]
            if st:
                self.last_err = st
                return self._rep(op, st, session, 0)
            if self.state == "recv":
                self._close()
                self.count["aborts"] += 1
            self.state, self.session, self.size, self.flags, self.baud = "recv", session, size, flags, baud
            self.sha, self.next, self.flash, self.last = req[9:41], 0, bytearray(), now
            self.count["begins"] += 1
            self.events.add("begun")
            return self._rep(op, ST["ok"], session, 0)
        if op == OP_DATA:
            if not REQ_HDR < len(req) <= REQ_HDR + CHUNK_MAX:
                return self._rep(op, ST["bad_req"], session, 0)
            if self.state != "recv" or session != self.session:
                return self._rep(op, ST["no_session"], session, 0)
            self.last = now
            if self.sco:
                self.count["busy"] += 1
                return self._rep(op, ST["busy"], session, self.next)
            offset, = struct.unpack_from("<I", req, 5)
            data = req[REQ_HDR:]
            if offset != self.next:
                self.count["dups" if offset < self.next else "gaps"] += 1
                return self._rep(op, ST["ok"], session, self.next)
            if len(data) > self.size - offset:
                return self._rep(op, ST["bad_req"], session, self.next)
            if offset == 0:
                if len(data) < min(self.size, HEAD_MIN):
                    return self._rep(op, ST["bad_req"], session, self.next)
                if not self.head_ok(data):
                    self._close(ST["bad_image"])
                    return self._rep(op, ST["bad_image"], session, 0)
            self.flash += data
            self.next += len(data)
            self.count["chunks"] += 1
            return self._rep(op, ST["ok"], session, self.next)
        if op == OP_END and len(req) == 5:
            is_open = self.state == "recv" and session == self.session
            if not is_open and self.memo and self.memo[0] == session:
                return self._rep(op, self.memo[1], session, self.memo[2])
            if not is_open:
                return self._rep(op, ST["no_session"], session, 0)
            self.last = now
            if self.next != self.size:
                return self._rep(op, ST["incomplete"], session, self.next)
            if hashlib.sha256(bytes(self.flash)).digest() != self.sha:
                self.memo = (session, ST["sha"], self.next)
                self._close(ST["sha"])
                return self._rep(op, ST["sha"], session, self.next)
            self.count["done"] += 1
            self.memo = (session, ST["ok"], self.next)
            self.last_err = 0
            if self.flags & F_DRYRUN:
                self._close()
            else:
                self.active = bytes(self.flash)
                self.state = "done"
                self.events.add("done")
            return self._rep(op, ST["ok"], session, self.next)
        if op == OP_ABORT and len(req) == 5:
            if self.state == "recv" and session == self.session:
                self._close()
                self.count["aborts"] += 1
                return self._rep(op, ST["ok"], session, self.next)
            return self._rep(op, ST["no_session"], session, 0)
        if op == OP_STATUS:
            text = "state=%s size=%d next=%d err=%s" % (self.state, self.size, self.next, status_name(self.last_err))
            return self._rep(op, ST["ok"], self.session, self.next, text.encode())
        return self._rep(op, ST["bad_req"], session, 0)

    def tick(self, now):
        if self.state == "recv" and now - self.last > self.idle:
            self.count["timeouts"] += 1
            self._close(ST["timeout"])


# ---------------------------------------------------------------------------------------------------------------------
# UDP transport to the S3

class Link:
    def __init__(self, host, port=OTA_PORT):
        self.host, self.port = host, port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("", 0))
        self.sock.settimeout(0.05)

    def close(self):
        self.sock.close()

    def send(self, target, payload):
        try:
            self.sock.sendto(b"OT" + bytes([UDP_VERSION, target]) + payload, (self.host, self.port))
        except OSError:
            pass

    def recv(self, timeout):
        """(target, payload) of one reply from the S3, or None."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                data, peer = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                return None
            if peer[0] == self.host and len(data) >= 4 and data[:2] == b"OT" and data[2] == UDP_VERSION:
                return data[3], data[4:]

    def ask(self, target, payload, op, tries=5, timeout=0.5):
        for _ in range(tries):
            self.send(target, payload)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                got = self.recv(end - time.monotonic())
                if got and got[0] == target and got[1][:1] == bytes([op]):
                    return got[1]
        return None

    def hello(self, target):
        rep = self.ask(target, build_short(OP_HELLO, 0), OP_HELLO)
        r = parse_reply(rep) if rep else None
        if not r or r["status"] != ST["ok"] or len(r["text"]) != 2 * NONCE_BYTES:
            return None
        return bytes.fromhex(r["text"])

    def query(self, tries=3):
        rep = self.ask(TARGET_SELF, build_short(OP_QUERY, 0), OP_QUERY, tries=tries)
        r = parse_reply(rep) if rep else None
        if not r:
            return None
        try:
            return json.loads(r["text"])
        except ValueError:
            return None


def read_token(path=None):
    if os.environ.get("BENCH_OTA_TOKEN") and not path:
        return os.environ["BENCH_OTA_TOKEN"].encode()
    p = Path(path) if path else TOKEN_FILE
    try:
        m = re.search(r'#define\s+OTA_TOKEN\s+"([^"]*)"', p.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1).encode() if m and m.group(1) else None


def log(quiet, *parts):
    if not quiet:
        print(*parts, file=sys.stderr, flush=True)


def upload(link, target, img, token, flags=0, baud=0, window=4, timeout=600.0, quiet=False):
    """Sends the image; the result dict has 'status' (name), 'seconds', and the sender counters."""
    t0 = time.monotonic()
    for attempt in range(2):
        nonce = link.hello(target)
        if nonce is None:
            return {"status": "no_hello", "seconds": time.monotonic() - t0}
        session = random.randrange(1, 1 << 32)
        begin = build_begin(session, img.size, img.sha256, flags, baud)
        tx = Sender(img.data, session, flags, baud, auth=auth_make(token, nonce, begin), window=window)
        shown = 0.0
        while tx.state not in ("done", "failed"):
            now = time.monotonic()
            if now - t0 > timeout:
                tx._failed(ST["timeout"])
                break
            while True:
                req = tx.poll(now)
                if req is None:
                    break
                link.send(target, req)
            got = link.recv(0.01)
            while got:
                if got[0] == target:
                    tx.on_reply(got[1], time.monotonic())
                got = link.recv(0.0005)
            if now - shown > 1.0:
                shown = now
                log(quiet, "  %s %d/%d bytes, resent %d, busy %d" % (tx.state, tx.acked, img.size, tx.resent,
                                                                   tx.busy_replies))
        result = {"status": "ok" if tx.state == "done" else status_name(tx.fail), "seconds": round(time.monotonic() - t0, 2),
                  "sent": tx.sent, "resent": tx.resent, "stalls": tx.stalls, "busy": tx.busy_replies, "attempt": attempt}
        # A receiver that restarted midway lost the session: one more try with a fresh nonce.
        if tx.state == "failed" and tx.fail == ST["no_session"] and attempt == 0:
            log(quiet, "  session lost, starting again")
            continue
        return result
    return result


def wait_s3(link, img, before, want_rollback=False, timeout=180.0, quiet=False):
    """Waits for the S3 to restart into the image and confirm it. Returns (result, last query)."""
    end = time.monotonic() + timeout
    restarted = False
    q = None
    while time.monotonic() < end:
        time.sleep(1.0)
        got = link.query(tries=1)
        if not got:
            continue
        q = got
        if q.get("boot") != before.get("boot"):
            restarted = True
        if not restarted:
            continue
        elf = (q.get("elf") or "")[:16]
        if elf == img.elf[:16]:
            if q.get("img") == "valid" and not want_rollback:
                return "done", q
            if q.get("selftest") == "fail" and not want_rollback:
                log(quiet, "  self-test failed, waiting for the rollback")
        elif elf == (before.get("elf") or "")[:16]:
            inv = (q.get("last_invalid") or {}).get("elf", "")[:16]
            if inv == img.elf[:16] and q.get("img") in ("valid", "undefined"):
                return "rolled_back", q
        log(quiet, "  boot=%s elf=%s img=%s selftest=%s" % (q.get("boot"), elf, q.get("img"), q.get("selftest")))
    return "timeout", q


def wait_lyrat(link, img, timeout=900.0, quiet=False):
    """Follows the S3's relay to the LyraT until it ends. Returns (relay state, last query)."""
    end = time.monotonic() + timeout
    q = None
    shown = 0.0
    while time.monotonic() < end:
        time.sleep(0.5)
        got = link.query(tries=1)
        if not got:
            continue
        q = got
        ly = q.get("lyrat") or {}
        state = ly.get("relay")
        if state in ("done", "rolled_back", "failed"):
            return state, q
        if time.monotonic() - shown > 2.0:
            shown = time.monotonic()
            log(quiet, "  lyrat %s %s/%s bytes, resent %s, busy %s, err %s" % (
                state, ly.get("acked"), ly.get("size"), ly.get("resent"), ly.get("busy"), ly.get("err")))
    return "timeout", q


def find_host(args):
    if args.host:
        return args.host
    if os.environ.get("BENCH_S3_ADDR"):
        return os.environ["BENCH_S3_ADDR"]
    import s3_uart  # broadcast discovery on the bench network, the same as the control channel
    ch = s3_uart.Channel()
    try:
        return ch.board if ch.find() else None
    finally:
        ch.close()


def run_update(args, board):
    img = Image.load(args.image)
    why = img.problem(board)
    record = {"board": board, "image": img.info(), "dry_run": args.dry_run, "test_rollback": args.test_rollback,
              "force": args.force, "baud": args.baud}
    if why:
        log(args.quiet, "image refused here: " + why)
        record["result"] = "bad_image"
        return EXIT_FAIL, record
    token = read_token(args.token_file)
    if not token:
        log(args.quiet, "no token: set BENCH_OTA_TOKEN or write s3_eth_bridge/main/ota_secrets.h")
        record["result"] = "no_token"
        return EXIT_FAIL, record
    host = find_host(args)
    if not host:
        record["result"] = "not_found"
        return EXIT_NOT_FOUND, record
    link = Link(host, args.port)
    try:
        before = link.query()
        if before is None:
            record["result"] = "not_found"
            return EXIT_NOT_FOUND, record
        record["before"] = {k: before.get(k) for k in ("ver", "elf", "img", "run")}
        if board == "lyrat":
            record["before"]["lyrat"] = (before.get("lyrat") or {}).get("status", "")
        flags = (F_DRYRUN if args.dry_run else 0) | (F_FAILTEST if args.test_rollback else 0) | \
            (F_FORCE if args.force else 0)
        log(args.quiet, "%s: %s %s, %d bytes, elf %s" % (board, img.project, img.version, img.size, img.elf[:16]))
        up = upload(link, BOARDS[board]["target"], img, token, flags, args.baud if board == "lyrat" else 0,
                    args.window, args.timeout, args.quiet)
        record["upload"] = up
        log(args.quiet, "upload: %s in %.1f s" % (up["status"], up["seconds"]))
        if up["status"] != "ok":
            record["result"] = up["status"]
            return (EXIT_TIMEOUT if up["status"] == "timeout" else EXIT_FAIL), record
        if board == "s3":
            if args.dry_run:
                record["result"] = "dry_run_ok"
                return EXIT_OK, record
            state, q = wait_s3(link, img, before, args.test_rollback, quiet=args.quiet)
        else:
            state, q = wait_lyrat(link, img, quiet=args.quiet)
            if state == "done" and args.dry_run:
                state = "dry_run_ok"
        record["after"] = q
        record["result"] = state
        if state in ("done", "dry_run_ok"):
            return (EXIT_FAIL if args.test_rollback else EXIT_OK), record
        if state == "rolled_back":
            return (EXIT_OK if args.test_rollback else EXIT_ROLLED_BACK), record
        return (EXIT_TIMEOUT if state == "timeout" else EXIT_FAIL), record
    finally:
        link.close()


def main(argv=None):
    # Board lines carry non-ASCII ("—"): a cp1252 console must not stop the tool.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=["s3", "lyrat", "query", "info"])
    ap.add_argument("image", nargs="?")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int, default=OTA_PORT)
    ap.add_argument("--token-file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-rollback", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--baud", type=int, default=0)
    ap.add_argument("--window", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--no-record", action="store_true", help="do not write bench_runs/ota/")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.what == "info":
        if not args.image:
            ap.error("info needs IMAGE")
        img = Image.load(args.image)
        out = img.info()
        out["fits"] = {b: img.problem(b) or "ok" for b in BOARDS}
        out["slot_use"] = {b: round(img.size / BOARDS[b]["slot"], 3) for b in BOARDS}
        print(json.dumps(out, indent=1))
        return EXIT_OK
    if args.what == "query":
        host = find_host(args)
        if not host:
            return EXIT_NOT_FOUND
        link = Link(host, args.port)
        q = link.query()
        link.close()
        if q is None:
            return EXIT_NOT_FOUND
        print(json.dumps(q, indent=1))
        return EXIT_OK
    if not args.image:
        ap.error("%s needs IMAGE" % args.what)
    code, record = run_update(args, args.what)
    record["exit"] = code
    if not args.no_record:
        out = run_dir("ota", "-".join(x for x in (args.what, args.label) if x))
        write_json(out / "ota.json", record)
        log(args.quiet, "record: %s" % out)
    print(json.dumps({k: record.get(k) for k in ("board", "result", "exit")}))
    return code


if __name__ == "__main__":
    sys.exit(main())
