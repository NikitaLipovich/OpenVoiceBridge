"""ROM bundle of the platform (goal 095): what U3 writes into U1/U2 through their ROM loader.

Format (rom_bundle.h): header of 256 bytes "RBND", version 1, chip, count, flags, u32 flash size, u32 total, then per
segment u32 offset, u32 size, md5[16]; then the data of the segments one after the other.

  make(segments, flash_size, chip=0) -> bytes        segments: [(offset, bytes)], sorted by the function
  parse(data) -> dict                                the same checks as rom_bundle.c (for the tools; the C is the truth)
  from_build(build_dir, project=None) -> (bytes, info)   from flasher_args.json of an idf.py build
"""
import hashlib
import json
import struct
from pathlib import Path

MAGIC = b"RBND"
VERSION = 1
HEAD = 256
MAX_SEG = 8
SECTOR = 4096
CHIP_ESP32 = 0
NVS = (0x9000, 0xF000)       # never written: pairings and calibration of the node


def make(segments, flash_size, chip=CHIP_ESP32):
    segs = sorted(segments)
    if not 1 <= len(segs) <= MAX_SEG:
        raise ValueError("1 to %d segments" % MAX_SEG)
    body = b"".join(data for _, data in segs)
    head = bytearray(HEAD)
    head[0:4] = MAGIC
    head[4], head[5], head[6], head[7] = VERSION, chip, len(segs), 0
    struct.pack_into("<II", head, 8, flash_size, HEAD + len(body))
    for i, (off, data) in enumerate(segs):
        struct.pack_into("<II", head, 16 + 24 * i, off, len(data))
        head[24 + 24 * i:40 + 24 * i] = hashlib.md5(data).digest()
    return bytes(head) + body


def parse(data, chip=CHIP_ESP32, protect=NVS):
    """dict with the segments, or raises ValueError with the name rom_bundle.c gives."""
    if len(data) < HEAD:
        raise ValueError("short")
    if data[:4] != MAGIC:
        raise ValueError("magic")
    if data[4] != VERSION:
        raise ValueError("version")
    if data[5] != chip:
        raise ValueError("chip")
    count = data[6]
    if not 1 <= count <= MAX_SEG:
        raise ValueError("count")
    flash, total = struct.unpack_from("<II", data, 8)
    if flash < 0x100000 or flash > 0x1000000 or flash & (flash - 1):
        raise ValueError("flash_size")
    at, end_prev, segs = HEAD, 0, []
    for i in range(count):
        off, size = struct.unpack_from("<II", data, 16 + 24 * i)
        md5 = data[24 + 24 * i:40 + 24 * i]
        if size == 0 or off % SECTOR:
            raise ValueError("align")
        if off < 0x1000 or off + size > flash:
            raise ValueError("range")
        if off < end_prev:
            raise ValueError("overlap")
        end_prev = (off + size + SECTOR - 1) // SECTOR * SECTOR
        if protect and off < protect[1] and end_prev > protect[0]:
            raise ValueError("protected")
        segs.append({"offset": off, "size": size, "md5": md5.hex(), "data_at": at})
        at += size
    if at != total or len(data) != total:
        raise ValueError("size")
    return {"chip": chip, "flash_size": flash, "total": total, "segments": segs}


def flash_size_value(text):
    t = str(text).upper().rstrip("B")
    if t.endswith("M"):
        return int(t[:-1]) * 0x100000
    if t.endswith("K"):
        return int(t[:-1]) * 1024
    return int(t, 0)


def from_build(build_dir, project=None):
    """Bundle of everything idf.py flash would write (bootloader, table, otadata, app), from flasher_args.json."""
    b = Path(build_dir)
    args = json.loads((b / "flasher_args.json").read_text(encoding="utf-8"))
    items = sorted((int(k, 0), v) for k, v in args["flash_files"].items())
    segs = [(off, (b / rel).read_bytes()) for off, rel in items]
    flash = flash_size_value(args.get("flash_settings", {}).get("flash_size", "4MB"))
    chip = args.get("extra_esptool_args", {}).get("chip", "esp32")
    if chip != "esp32":
        raise ValueError("ROM bundles are for the ESP32 nodes, this build is %s" % chip)
    data = make(segs, flash)
    info = {"build": str(b), "flash_size": flash, "total": len(data),
            "segments": [{"offset": hex(o), "size": len(d), "file": rel} for (o, d), (_, rel) in zip(segs, items)]}
    if project:
        app = args.get("app", {}).get("file", "")
        if Path(app).stem != project:
            raise ValueError("build is %s, not %s" % (Path(app).stem, project))
    return data, info
