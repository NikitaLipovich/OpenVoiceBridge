#!/usr/bin/env python3
"""Firmware of the variant D platform (goal 095): U3 over Wi-Fi, U1/U2 through U3. Protocol: docs/PLATFORM_OTA.md.

  platform_ota.py u3 IMAGE [--dry-run] [--test-rollback]              U3's own image over UDP 5007
  platform_ota.py u1|u2 IMAGE [--force] [--baud N] [--dry-run] [--test-rollback]
                                                                       app of a node: staged on U3, sent over C1/C2
  platform_ota.py u1-rom|u2-rom BUNDLE|BUILD_DIR [--baud N] [--dry-run]
                                                                       emergency: everything through the node's ROM
                                                                       loader (EN, IO0, UART0); BUILD_DIR = idf.py build
  platform_ota.py bundle BUILD_DIR [-o FILE]                          make a ROM bundle from flasher_args.json
  platform_ota.py query [u1|u2]                                       state of U3, or of a node in detail
  platform_ota.py cmd u1|u2 TEXT                                      console command of a node (ota, ulog, ver, link)
  platform_ota.py mode u1|u2 N                                        R21, reserved: the nodes answer "unsupported"
  platform_ota.py con auto|u1|u2|off                                  which node's UART0 console U3 reads

Options: --host ADDR (else BENCH_U3_ADDR, else a broadcast QUERY on 5007), --port 5007, --log-port 5008,
--token-file PATH (else BENCH_OTA_TOKEN, else platform/u3_relay/main/plat_secrets.h), --timeout S, --label, --quiet.
Exit code as ota.py: 0 done, 1 refused or failed, 2 U3 not found, 3 rolled back (0 with --test-rollback), 4 timeout.

The protocol is ota.py's (imported, not copied): Image, upload(), Link, wait_s3(). The token never goes over the
network and is not written into the run record (goals_harness/bench_runs/platform_ota/<stamp>/).
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bench"))
sys.path.insert(0, str(HERE))
import ota  # noqa: E402
import rombundle  # noqa: E402
from benchlib import REPO_DIR, run_dir, write_json  # noqa: E402

TOKEN_FILE = Path(REPO_DIR) / "platform" / "u3_relay" / "main" / "plat_secrets.h"
LOG_PORT = 5008
TARGET_U3, TARGET_U1, TARGET_U2, TARGET_ROM = 0, 1, 2, 0x10
NODES = {
    "u3": {"target": TARGET_U3, "chip": ota.CHIP_ESP32S3, "project": "u3_relay", "slot": 0x300000},
    "u1": {"target": TARGET_U1, "chip": ota.CHIP_ESP32, "project": "u1_hf", "slot": 0x1F0000},
    "u2": {"target": TARGET_U2, "chip": ota.CHIP_ESP32, "project": "u2_ag", "slot": 0x1F0000},
}
STAGE = 0x200000
EXIT_OK, EXIT_FAIL, EXIT_NOT_FOUND, EXIT_ROLLED_BACK, EXIT_TIMEOUT = 0, 1, 2, 3, 4


class Blob:
    """What ota.upload() needs of an image: data, size, sha256."""

    def __init__(self, data):
        self.data = bytes(data)
        self.size = len(self.data)
        self.sha256 = hashlib.sha256(self.data).digest()


def log(quiet, *parts):
    if not quiet:
        print(*parts, file=sys.stderr, flush=True)


def image_problem(img, board):
    b = NODES[board]
    if not img.magic_ok:
        return "not an app image (magic)"
    if img.chip != b["chip"]:
        return "chip id %d, %s needs %d" % (img.chip, board, b["chip"])
    if img.project != b["project"]:
        return "project %r, %s runs %r" % (img.project, board, b["project"])
    if img.size > b["slot"]:
        return "%d bytes, the slot holds %d" % (img.size, b["slot"])
    return None


def load_bundle(path, board):
    """(bytes, info) from a bundle file or an idf.py build directory; raises ValueError."""
    p = Path(path)
    if p.is_dir():
        data, info = rombundle.from_build(p, NODES[board]["project"])
    else:
        data = p.read_bytes()
        info = rombundle.parse(data)
        info = {"file": str(p), "total": info["total"], "flash_size": info["flash_size"],
                "segments": [{"offset": hex(s["offset"]), "size": s["size"]} for s in info["segments"]]}
    rombundle.parse(data)
    if len(data) > STAGE:
        raise ValueError("%d bytes, the staging area holds %d" % (len(data), STAGE))
    return data, info


def find_u3(port=ota.OTA_PORT, timeout=1.5):
    """Address of the U3 that answers a broadcast QUERY, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    s.settimeout(0.2)
    try:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                s.sendto(b"OT" + bytes([ota.UDP_VERSION, TARGET_U3]) + ota.build_short(ota.OP_QUERY, 0),
                         ("255.255.255.255", port))
            except OSError:
                pass
            try:
                data, peer = s.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            if data[:2] == b"OT" and b'"fw":"u3relay"' in data:
                return peer[0]
    finally:
        s.close()
    return None


def host_of(args):
    return args.host or os.environ.get("BENCH_U3_ADDR") or find_u3(args.port)


def query(link, target=TARGET_U3, tries=3):
    rep = link.ask(target, ota.build_short(ota.OP_QUERY, 0), ota.OP_QUERY, tries=tries)
    r = ota.parse_reply(rep) if rep else None
    if not r:
        return None
    try:
        return json.loads(r["text"])
    except ValueError:
        return None


def wait_node(link, board, img_elf, rom=False, timeout=900.0, quiet=False):
    """Follows U3's relay (or ROM job) for a node until it ends. Returns (state, last node query)."""
    target = NODES[board]["target"]
    end = time.monotonic() + timeout
    q, shown = None, 0.0
    while time.monotonic() < end:
        time.sleep(0.5)
        got = query(link, target, tries=1)
        if not got:
            continue
        q = got
        if rom:
            r = q.get("rom") or {}
            if r.get("job") in ("done", "no_link", "failed"):
                return r["job"], q
            line = "  %s rom %s/%s %s/%s bytes, syncs %s, restarts %s/%s" % (
                board, r.get("job"), r.get("phase"), r.get("done"), r.get("total"), r.get("syncs"),
                r.get("session_restarts"), r.get("segment_restarts"))
        else:
            state = q.get("relay")
            if state in ("done", "rolled_back", "failed"):
                return state, q
            line = "  %s %s %s/%s bytes, resent %s, busy %s, err %s" % (
                board, state, q.get("acked"), q.get("size"), q.get("resent"), q.get("busy"), q.get("err"))
        if time.monotonic() - shown > 2.0:
            shown = time.monotonic()
            log(quiet, line)
    return "timeout", q


def run_update(args, what):
    rom = what.endswith("-rom")
    board = what[:2]
    record = {"what": what, "dry_run": args.dry_run, "test_rollback": args.test_rollback, "force": args.force,
              "baud": args.baud}
    try:
        if rom:
            data, info = load_bundle(args.image, board)
            blob = Blob(data)
            record["bundle"] = info
            elf = ""
        else:
            img = ota.Image.load(args.image)
            record["image"] = img.info()
            why = image_problem(img, board)
            if why:
                raise ValueError(why)
            blob, elf = img, img.elf
    except (ValueError, OSError, KeyError) as e:
        log(args.quiet, "refused here: %s" % e)
        record["result"] = "bad_image"
        return EXIT_FAIL, record
    token = ota.read_token(args.token_file or (None if os.environ.get("BENCH_OTA_TOKEN") else TOKEN_FILE))
    if not token:
        log(args.quiet, "no token: set BENCH_OTA_TOKEN or write platform/u3_relay/main/plat_secrets.h")
        record["result"] = "no_token"
        return EXIT_FAIL, record
    host = host_of(args)
    if not host:
        record["result"] = "not_found"
        return EXIT_NOT_FOUND, record
    link = ota.Link(host, args.port)
    try:
        before = query(link)
        if before is None:
            record["result"] = "not_found"
            return EXIT_NOT_FOUND, record
        record["before"] = {k: before.get(k) for k in ("ver", "elf", "img", "run", "u1", "u2")}
        flags = (ota.F_DRYRUN if args.dry_run else 0) | (ota.F_FAILTEST if args.test_rollback else 0) | \
            (ota.F_FORCE if args.force else 0)
        target = NODES[board]["target"] | (TARGET_ROM if rom else 0)
        log(args.quiet, "%s: %d bytes to target 0x%02x" % (what, blob.size, target))
        up = ota.upload(link, target, blob, token, flags, args.baud if board != "u3" else 0, args.window, args.timeout,
                        args.quiet)
        record["upload"] = up
        log(args.quiet, "upload: %s in %.1f s" % (up["status"], up["seconds"]))
        if up["status"] != "ok":
            record["result"] = up["status"]
            return (EXIT_TIMEOUT if up["status"] == "timeout" else EXIT_FAIL), record
        if args.dry_run and (board == "u3" or rom):
            record["result"] = "dry_run_ok"
            return EXIT_OK, record
        if board == "u3":
            state, q = ota.wait_s3(link, img, before, args.test_rollback, quiet=args.quiet)
        else:
            state, q = wait_node(link, board, elf, rom=rom, timeout=args.timeout, quiet=args.quiet)
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


def text_request(host, port, text, want, timeout=3.0):
    """One request on the log port of U3; the first answer starting with want."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    s.settimeout(0.3)
    try:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s.sendto(text.encode(), (host, port))
            t = time.monotonic() + 1.0
            while time.monotonic() < t:
                try:
                    data, peer = s.recvfrom(4096)
                except (socket.timeout, ConnectionResetError, OSError):
                    continue
                if peer[0] == host and data.startswith(want.encode()):
                    return data.decode("utf-8", "replace")
    finally:
        s.close()
    return None


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=["u3", "u1", "u2", "u1-rom", "u2-rom", "bundle", "query", "cmd", "mode", "con"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("-o", "--out-file")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int, default=ota.OTA_PORT)
    ap.add_argument("--log-port", type=int, default=LOG_PORT)
    ap.add_argument("--token-file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-rollback", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--baud", type=int, default=0)
    ap.add_argument("--window", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.what == "bundle":
        if not args.args:
            ap.error("bundle needs BUILD_DIR")
        data, info = rombundle.from_build(args.args[0])
        out = Path(args.out_file or (Path(args.args[0]) / "rom_bundle.rbnd"))
        out.write_bytes(data)
        info["out"] = str(out)
        print(json.dumps(info))
        return EXIT_OK
    if args.what in ("query", "cmd", "mode", "con"):
        host = host_of(args)
        if not host:
            return EXIT_NOT_FOUND
        if args.what == "query":
            link = ota.Link(host, args.port)
            target = NODES[args.args[0]]["target"] if args.args else TARGET_U3
            q = query(link, target)
            link.close()
            if q is None:
                return EXIT_NOT_FOUND
            print(json.dumps(q, indent=1))
            return EXIT_OK
        if args.what == "con":
            got = text_request(host, args.log_port, "CON " + (args.args[0] if args.args else ""), "CON ")
        elif len(args.args) < 2 or args.args[0] not in ("u1", "u2"):
            ap.error("%s needs u1|u2 and a value" % args.what)
        elif args.what == "cmd":
            got = text_request(host, args.log_port, "CMD %s %s" % (args.args[0], " ".join(args.args[1:])), "UR ")
        else:
            got = text_request(host, args.log_port, "MODE %s %d" % (args.args[0], int(args.args[1])), "MA ")
        print(got if got is not None else "no answer")
        return EXIT_OK if got else EXIT_FAIL
    if not args.args:
        ap.error("%s needs IMAGE" % args.what)
    args.image = args.args[0]
    code, record = run_update(args, args.what)
    record["exit"] = code
    if not args.no_record:
        out = run_dir("platform_ota", "-".join(x for x in (args.what, args.label) if x))
        write_json(out / "platform_ota.json", record)
        log(args.quiet, "record: %s" % out)
    print(json.dumps({k: record.get(k) for k in ("what", "result", "exit")}))
    return code


if __name__ == "__main__":
    sys.exit(main())
