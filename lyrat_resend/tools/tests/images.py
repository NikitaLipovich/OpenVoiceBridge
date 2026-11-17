"""App images for the PC tests: parse the ESP image format, make a variant of a real build, or a synthetic image.

The OTA tests need two images of each board, an old and a newer one. A second firmware build costs minutes, so the newer
one is a variant of the real build: other version text and other ELF SHA in the app description, checksum and appended
SHA-256 made again, so the file stays a valid image as esptool and the bootloader see it. With no build at hand at all
(--quick before any full run) a synthetic image stands in: one segment holding the app description and filler.

  python images.py check FILE            the image is whole: segments, checksum, appended hash (JSON)
  python images.py prepare DIR [--s3 BIN] [--lyrat BIN]
                                          DIR/s3-base, s3-ota, lyrat-base, lyrat-ota as ota_test wants (OTA_TEST_BUILDS)
Made by goal 092.
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

IMAGE_MAGIC = 0xE9
DESC_MAGIC = 0xABCD5432
HEADER = 24
SEG_HEADER = 8
CHECKSUM_SEED = 0xEF
DESC_AT = HEADER + SEG_HEADER          # the app description opens the first segment
VERSION_AT, PROJECT_AT, ELF_AT = DESC_AT + 16, DESC_AT + 48, DESC_AT + 144
CHIP_ESP32, CHIP_ESP32S3 = 0x0000, 0x0009
BOARDS = {"s3": (CHIP_ESP32S3, "s3_eth_bridge", "s3_eth_bridge.bin"),
          "lyrat": (CHIP_ESP32, "lyrat_resend", "lyrat_resend.bin")}


def parse(data: bytes):
    """Layout of an image: segments, where the checksum byte is, whether a hash is appended. Raises ValueError."""
    if len(data) < HEADER + SEG_HEADER or data[0] != IMAGE_MAGIC:
        raise ValueError("not an image (magic)")
    count = data[1]
    hash_appended = data[23] == 1
    pos = HEADER
    checksum = CHECKSUM_SEED
    segments = []
    for _ in range(count):
        if pos + SEG_HEADER > len(data):
            raise ValueError("segment header past the end")
        addr, size = struct.unpack_from("<II", data, pos)
        start = pos + SEG_HEADER
        if start + size > len(data):
            raise ValueError("segment past the end")
        for b in data[start:start + size]:
            checksum ^= b
        segments.append((addr, start, size))
        pos = start + size
    checksum_at = pos + 15 - (pos % 16)
    end = checksum_at + 1 + (32 if hash_appended else 0)
    if end != len(data):
        raise ValueError("size %d, the layout ends at %d" % (len(data), end))
    return {"segments": segments, "checksum_at": checksum_at, "checksum": checksum, "hash_appended": hash_appended,
            "chip": struct.unpack_from("<H", data, 12)[0]}


def check(data: bytes):
    """None when the image is whole, else what is wrong."""
    try:
        lay = parse(data)
    except ValueError as e:
        return str(e)
    if data[lay["checksum_at"]] != lay["checksum"]:
        return "checksum"
    if lay["hash_appended"] and hashlib.sha256(data[:-32]).digest() != data[-32:]:
        return "appended sha256"
    if struct.unpack_from("<I", data, DESC_AT)[0] != DESC_MAGIC:
        return "app description magic"
    return None


def _seal(buf: bytearray):
    lay = parse(bytes(buf))
    buf[lay["checksum_at"]] = lay["checksum"]
    if lay["hash_appended"]:
        buf[-32:] = hashlib.sha256(bytes(buf[:-32])).digest()
    return bytes(buf)


def _text(value: str, n=32):
    raw = value.encode("ascii")[:n - 1]
    return raw + bytes(n - len(raw))


def variant(data: bytes, version: str, elf_seed: str = None):
    """The same image with another version and ELF SHA: the board sees another build."""
    buf = bytearray(data)
    buf[VERSION_AT:VERSION_AT + 32] = _text(version)
    buf[ELF_AT:ELF_AT + 32] = hashlib.sha256((elf_seed or version).encode() + bytes(data[ELF_AT:ELF_AT + 32])).digest()
    return _seal(buf)


def synthetic(board: str, version: str, size: int = 200_000):
    """A whole image of about size bytes for the board, with no code in it: for the protocol, never for a board."""
    chip, project, _ = BOARDS[board]
    desc = bytearray(256)
    struct.pack_into("<I", desc, 0, DESC_MAGIC)
    desc[16:48] = _text(version)
    desc[48:80] = _text(project)
    desc[80:96] = _text("00:00:00", 16)
    desc[96:112] = _text("Nov 17 2026", 16)
    desc[112:144] = _text("v5.5.2")
    desc[144:176] = hashlib.sha256(("synthetic " + project + version).encode()).digest()
    body = bytes(desc) + hashlib.shake_256(project.encode()).digest(max(size - 400, 0))
    head = bytearray(HEADER)
    head[0], head[1], head[2], head[3] = IMAGE_MAGIC, 1, 2, 0x4F
    struct.pack_into("<I", head, 4, 0x40080000)
    struct.pack_into("<H", head, 12, chip)
    head[23] = 1
    buf = bytearray(head) + struct.pack("<II", 0x3F400020, len(body)) + body
    pos = len(buf)
    buf += bytes(15 - (pos % 16)) + b"\0" + bytes(32)
    return _seal(buf)


def prepare(out: Path, s3_bin=None, lyrat_bin=None):
    """OTA_TEST_BUILDS layout; returns {board: "build" | "synthetic"}."""
    kinds = {}
    for board, src in (("s3", s3_bin), ("lyrat", lyrat_bin)):
        name = BOARDS[board][2]
        if src and Path(src).is_file():
            base = Path(src).read_bytes()
            kinds[board] = "build"
        else:
            base = synthetic(board, "base-092")
            kinds[board] = "synthetic"
        new = variant(base, "ota-092")
        for kind, data in (("base", base), ("ota", new)):
            d = out / ("%s-%s" % (board, kind))
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(data)
    return kinds


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("file")
    p = sub.add_parser("prepare")
    p.add_argument("dir")
    p.add_argument("--s3")
    p.add_argument("--lyrat")
    args = ap.parse_args()
    if args.cmd == "check":
        why = check(Path(args.file).read_bytes())
        print(json.dumps({"file": args.file, "ok": why is None, "problem": why}))
        return 0 if why is None else 1
    print(json.dumps(prepare(Path(args.dir), args.s3, args.lyrat)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
