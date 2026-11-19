"""Both firmwares built as a check, in their own build folders, and what the images must hold. Goal 092.

  python fw_build_check.py [--out DIR] [--only s3|lyrat] [--no-build] [--no-cache]
  python fw_build_check.py --sources [--out DIR]     no build: the rules readable from the sources (goal 110, --fast)

For each board: idf.py -B DIR/<board> -D SDKCONFIG=DIR/<board>.sdkconfig build (the working build/ and sdkconfig stay as
they are, and the check proves it), then
  table      the partition table in the built partition-table.bin = partitions_ota.csv = the table of docs/OTA.md
  rollback   CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y in the sdkconfig of this build
  default_mode  LyraT (goal 102): "lyrat" starts in the phone mode, "lyrat_pc" (-D LYRAT_DEFAULT_MODE=pc) in the PC mode
  size       the app image within 85 % of its slot (docs/OTA.md): ota_0 for the LyraT's headset firmware, factory for
             its updater (goal 096), ota_0 for the S3
  wifi       LyraT (goal 096): the headset firmware links no Wi-Fi (R13), the updater does; read from the link map
  image      the app image is whole: segments, checksum, appended SHA-256 (tests/images.py)
  secrets    S3 and the LyraT's updater: the Wi-Fi name, password and OTA token (main/ota_secrets.h of the S3; the updater
             takes the network from lyrat_resend/main/wifi_secrets.h) are in the image, compared as bytes, never printed. A hit of the ccache trap of docs/OTA.md (old object without the header) is rebuilt once with
             CCACHE_RECACHE=1 and said so
  untouched  the working sdkconfig, sdkconfig.old and build/ of both projects and git status are the same as before
Cache (goal 110): a board whose inputs are those of its last good build is not built again; its image is checked as it
is. The inputs: every tracked or unignored file of both firmware trees (s3_eth_bridge, and lyrat_resend with updater/
but without tools/, docs/, harness/: the S3 compiles C files of lyrat_resend/main), the SHA-256 of the secret files (never their values), the -D defines of the variant,
IDF_PATH, ADF_PATH and the ESP-IDF version; kept in DIR/<board>.fingerprint. --no-cache builds anyway.
--sources: the partition table of partitions_ota.csv against docs/OTA.md, rollback and the table file in
sdkconfig.defaults, the pc define in main/CMakeLists.txt; no build, nothing written but DIR/fw_sources.json.
Output: DIR/fw_build.json and the build logs; DIR defaults to goals_harness/bench_runs/tests/builds (outside git, kept
between runs so that the next build is incremental). Exit code 0 when every check passed.
ESP-IDF: IDF_PATH (default C:\\esp\\v5.5.2\\esp-idf) and IDF_TOOLS_PATH (default C:\\Espressif), as fw.py.
"""
import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
LYRAT = TOOLS.parent
REPO = LYRAT.parent
sys.path.insert(0, str(HERE))
import images  # noqa: E402

RUNS = REPO / "goals_harness" / "bench_runs"
DEFAULT_OUT = RUNS / "tests" / "builds"
IDF_PATH = os.environ.get("IDF_PATH", r"C:\esp\v5.5.2\esp-idf")
IDF_TOOLS = os.environ.get("IDF_TOOLS_PATH", r"C:\Espressif")
SLOT_LIMIT = 0.85
S3_SECRETS = REPO / "s3_eth_bridge" / "main" / "ota_secrets.h"
WIFI_SECRETS = LYRAT / "main" / "wifi_secrets.h"
# csv: the table file; slot: the partition the image goes to; wifi: True links Wi-Fi, False must not, None not checked.
BOARDS = {
    "s3": {"dir": REPO / "s3_eth_bridge", "bin": "s3_eth_bridge.bin", "doc": "S3-ETH", "slot": "ota_0", "wifi": None,
           "csv": REPO / "s3_eth_bridge" / "partitions_ota.csv", "csv_conf": "partitions_ota.csv",
           "secrets": [(S3_SECRETS, ("OTA_TOKEN", "WIFI_SSID", "WIFI_PASS"))]},
    "lyrat": {"dir": LYRAT, "bin": "lyrat_resend.bin", "doc": "LyraT", "slot": "ota_0", "wifi": False,
              "csv": LYRAT / "partitions_ota.csv", "csv_conf": "partitions_ota.csv", "secrets": None,
              "default_mode": "phone"},
    # Goal 102: the same headset firmware built to start in the PC mode while NVS holds no mode (main/CMakeLists.txt)
    "lyrat_pc": {"dir": LYRAT, "bin": "lyrat_resend.bin", "doc": "LyraT", "slot": "ota_0", "wifi": False,
                 "csv": LYRAT / "partitions_ota.csv", "csv_conf": "partitions_ota.csv", "secrets": None,
                 "defs": ["LYRAT_DEFAULT_MODE=pc"], "default_mode": "pc"},
    "lyrat_updater": {"dir": LYRAT / "updater", "bin": "lyrat_updater.bin", "doc": "LyraT", "slot": "factory", "wifi": True,
                      "csv": LYRAT / "partitions_ota.csv", "csv_conf": "../partitions_ota.csv",
                      "secrets": [(S3_SECRETS, ("OTA_TOKEN",)), (WIFI_SECRETS, ("WIFI_SSID", "WIFI_PASS"))]},
}
OTA_DOC = LYRAT / "docs" / "OTA.md"
TYPES = {0: "app", 1: "data"}
DATA_SUB = {0x00: "ota", 0x01: "phy", 0x02: "nvs"}


# --- partition tables ------------------------------------------------------------------------------------------------

def _num(text):
    return int(text.strip().split()[0], 0)


def table_from_bin(data: bytes):
    """(name, type, subtype, offset, size) of the entries of a partition-table.bin."""
    out = []
    for at in range(0, len(data) - 31, 32):
        magic = data[at:at + 2]
        if magic != b"\xaa\x50":
            break
        ptype, sub, off, size = struct.unpack_from("<BBII", data, at + 2)
        label = data[at + 12:at + 28].split(b"\0", 1)[0].decode("ascii")
        out.append((label, ptype, sub, off, size))
    return out


def _subtype(ptype, sub):
    if ptype == 0:
        return {0x10: "ota_0", 0x11: "ota_1", 0x00: "factory"}.get(sub, hex(sub))
    return DATA_SUB.get(sub, hex(sub))


def table_from_csv(path: Path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        f = [x.strip() for x in line.split(",")]
        ptype = {"app": 0, "data": 1}[f[1]]
        if ptype == 0:
            sub = {"ota_0": 0x10, "ota_1": 0x11, "factory": 0}[f[2]]
        else:
            sub = {v: k for k, v in DATA_SUB.items()}.get(f[2]) if not f[2].startswith("0x") else int(f[2], 16)
        out.append((f[0], ptype, sub, _num(f[3]), _num(f[4])))
    return out


def table_from_doc(board_heading: str):
    """The table under '### <heading>' of docs/OTA.md as (name, type, subtype, offset, size)."""
    text = OTA_DOC.read_text(encoding="utf-8")
    m = re.search(r"^### %s\b(.*?)(?=^##|\Z)" % re.escape(board_heading), text, re.S | re.M)
    out = []
    for row in re.findall(r"^\| *(\w+) *\| *([\w/]+) *\| *(0x[0-9A-Fa-f]+) *\| *(0x[0-9A-Fa-f]+)", m.group(1), re.M):
        name, kind, off, size = row
        if kind == "app":
            ptype, sub = 0, {"ota_0": 0x10, "ota_1": 0x11}.get(name, 0)
        else:
            s = kind.split("/", 1)[1]
            ptype, sub = 1, int(s, 16) if s.startswith("0x") else {v: k for k, v in DATA_SUB.items()}[s]
        out.append((name, ptype, sub, int(off, 16), int(size, 16)))
    return out


# --- the working state that must not change ---------------------------------------------------------------------------

def working_state():
    """What a test build must leave as it was: the working configs and builds of both projects, and git status."""
    state = {}
    for name, b in BOARDS.items():
        d = b["dir"]
        for rel in ("sdkconfig", "sdkconfig.old", "build/" + b["bin"], "build/build.ninja", "build/CMakeCache.txt",
                    "build/partition_table/partition-table.bin"):
            p = d / rel
            state["%s:%s" % (name, rel)] = [p.stat().st_size, p.stat().st_mtime_ns] if p.exists() else None
    # git status: a build must add or change no tracked or unignored file (an edit by a person meanwhile shows up too)
    git = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True, text=True)
    for line in git.stdout.splitlines():
        p = REPO / line[3:].strip('"')
        state["git:" + line] = p.stat().st_mtime_ns if p.is_file() else None
    return state


# --- cache (goal 110) -------------------------------------------------------------------------------------------------

SKIP_DIRS = {"tools", "docs", "harness", "build", "managed_components"}


def tree_files(root: Path):
    """Tracked and unignored files under root as git lists them, without the tools, docs and build folders."""
    rel_root = root.relative_to(REPO).as_posix()
    r = subprocess.run(["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard", "-z", "--", rel_root],
                       capture_output=True, timeout=60)
    out = []
    for rel in r.stdout.decode("utf-8", "replace").split("\0"):
        if rel and not SKIP_DIRS & set(Path(rel).relative_to(rel_root).parts[:-1]):
            out.append(rel)
    return sorted(out)


def idf_version():
    p = Path(IDF_PATH) / "tools" / "cmake" / "version.cmake"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else "?"


def fingerprint(board):
    """SHA-256 over what a build of this board is made from; any change of them means a new build."""
    b = BOARDS[board]
    h = hashlib.sha256()
    # Both trees for every board: the S3 compiles C files of lyrat_resend/main too, the updater those of its parent.
    for rel in tree_files(REPO / "s3_eth_bridge") + tree_files(LYRAT):
        p = REPO / rel
        if p.is_file():
            h.update(rel.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    for path in [S3_SECRETS, WIFI_SECRETS]:
        h.update(path.name.encode() + b"\0" + (hashlib.sha256(path.read_bytes()).digest() if path.exists() else b"-"))
    h.update(repr((board, b.get("defs"), IDF_PATH, os.environ.get("ADF_PATH"), idf_version())).encode())
    return h.hexdigest()


# --- build -----------------------------------------------------------------------------------------------------------

def idf_build(board, out: Path, recache=False, timeout=3600):
    b = BOARDS[board]
    build_dir, sdk = out / board, out / (board + ".sdkconfig")
    log = out / (board + "_build.log")
    script = ("$env:IDF_TOOLS_PATH='%s'; . '%s\\export.ps1' | Out-Null; %s"
              "idf.py -C '%s' -B '%s' -D 'SDKCONFIG=%s'%s build; exit $LASTEXITCODE"
              % (IDF_TOOLS, IDF_PATH, "$env:CCACHE_RECACHE='1'; " if recache else "", b["dir"], build_dir, sdk,
                 "".join(" -D '%s'" % d for d in b.get("defs", []))))
    t0 = time.monotonic()
    env = {k: v for k, v in os.environ.items() if k != "MSYSTEM"}    # ESP-IDF refuses to start under Git Bash's MSYSTEM
    with open(log, "w", encoding="utf-8", errors="replace") as f:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                            script], stdout=f, stderr=subprocess.STDOUT, timeout=timeout, env=env)
    return {"rc": r.returncode, "seconds": round(time.monotonic() - t0, 1), "log": str(log), "recache": recache}


def secret_values(path: Path, keys):
    """The string values of the #defines; nothing of them is ever printed or written."""
    if not path or not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    out = {}
    for k in keys:
        m = re.search(r'^\s*#define\s+%s\s+"((?:[^"\\]|\\.)*)"' % k, text, re.M)
        out[k] = m.group(1).encode("utf-8").decode("unicode_escape").encode("latin-1") if m else None
    return out


def wifi_linked(map_path: Path):
    """True when the memory map of the link defines esp_wifi_init: the driver is in the image. The part before
    "Linker script and memory map" lists the discarded sections, which name it in every ESP-IDF build."""
    text = map_path.read_text(encoding="utf-8", errors="replace")
    text = text[text.find("Linker script and memory map"):]
    return re.search(r"^\s+0x[0-9a-f]+\s+esp_wifi_init\s*$", text, re.M) is not None


def default_mode_of(build_dir: Path):
    """"pc" when main.c of this build was compiled with BENCH_DEFAULT_MODE_PC, else "phone"; None without the
    compile database."""
    db = build_dir / "compile_commands.json"
    if not db.exists():
        return None
    for e in json.loads(db.read_text(encoding="utf-8")):
        if e.get("file", "").replace("\\", "/").endswith("lyrat_resend/main/main.c"):
            return "pc" if "BENCH_DEFAULT_MODE_PC" in e.get("command", "") else "phone"
    return None


def secrets_check(image: bytes, values):
    """Per key: True when its value is in the image (an empty or absent value: False)."""
    return {k: bool(v) and v in image for k, v in values.items()}


def check_board(board, out: Path):
    b = BOARDS[board]
    build_dir = out / board
    res = {}
    csv = table_from_csv(b["csv"])
    built = table_from_bin((build_dir / "partition_table" / "partition-table.bin").read_bytes())
    doc = table_from_doc(b["doc"])
    res["table"] = {"pass": built == csv == doc, "built": built, "csv_same": built == csv, "doc_same": built == doc}
    sdk = (out / (board + ".sdkconfig")).read_text(encoding="utf-8")
    res["rollback"] = {"pass": "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in sdk and
                       'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="%s"' % b["csv_conf"] in sdk}
    image = (build_dir / b["bin"]).read_bytes()
    slot = next(e[4] for e in built if e[0] == b["slot"])
    res["size"] = {"pass": len(image) <= SLOT_LIMIT * slot, "bytes": len(image), "slot": slot, "slot_name": b["slot"],
                   "limit": int(SLOT_LIMIT * slot), "use_pct": round(100.0 * len(image) / slot, 1)}
    if b["wifi"] is not None:
        linked = wifi_linked(build_dir / (b["bin"][:-4] + ".map"))
        res["wifi"] = {"pass": linked == b["wifi"], "linked": linked, "want": b["wifi"]}
    if "default_mode" in b:
        # Goal 102: the mode of a start without a mode in NVS, from the flags main.c was compiled with
        got = default_mode_of(build_dir)
        res["default_mode"] = {"pass": got == b["default_mode"], "got": got, "want": b["default_mode"]}
    why = images.check(image)
    res["image"] = {"pass": why is None, "problem": why}
    if b["secrets"] is not None:
        found, missing = {}, []
        for path, keys in b["secrets"]:
            values = secret_values(path, keys)
            if values is None:
                missing.append(path.name)
            else:
                found.update(secrets_check(image, values))
        if missing:
            res["secrets"] = {"pass": False, "problem": "no " + ", ".join(missing)}
        else:
            res["secrets"] = {"pass": all(found.values()), "found": found}
    return res


def run(out: Path, boards=tuple(BOARDS), build=True, cache=True):
    out.mkdir(parents=True, exist_ok=True)
    before = working_state()
    report = {"boards": {}, "out": str(out)}
    for board in boards:
        entry = {}
        fp_file, fp = out / (board + ".fingerprint"), None
        if build:
            fp = fingerprint(board)
            image = out / board / BOARDS[board]["bin"]
            if cache and image.exists() and fp_file.exists() and fp_file.read_text(encoding="utf-8").strip() == fp:
                entry["build"] = {"rc": 0, "seconds": 0.0, "cached": True}
            else:
                if fp_file.exists():
                    fp_file.unlink()
                entry["build"] = idf_build(board, out)
        if not build or entry["build"]["rc"] == 0:
            entry["checks"] = check_board(board, out)
            sec = entry["checks"].get("secrets")
            if build and sec and not sec["pass"] and "found" in sec:
                # The ccache trap of docs/OTA.md: rebuild once without the cache's old objects.
                entry["rebuild"] = idf_build(board, out, recache=True)
                if entry["rebuild"]["rc"] == 0:
                    entry["checks"] = check_board(board, out)
                    entry["checks"]["secrets"]["ccache_trap"] = True
        if fp and entry["build"]["rc"] == 0 and "checks" in entry and all(c["pass"] for c in entry["checks"].values()):
            fp_file.write_text(fp + "\n", encoding="utf-8")      # only a good build is reused
        report["boards"][board] = entry
    after = working_state()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    report["untouched"] = {"pass": not changed, "changed": changed}
    ok = report["untouched"]["pass"]
    for entry in report["boards"].values():
        ok = ok and (not build or entry["build"]["rc"] == 0) and "checks" in entry and \
            all(c["pass"] for c in entry["checks"].values())
    report["pass"] = ok
    (out / "fw_build.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def sources(out: Path):
    """The rules of the firmwares that need no build (goal 110, the fast level)."""
    res = {}
    for board, b in BOARDS.items():
        if board == "lyrat_pc":
            continue
        defaults = (b["dir"] / "sdkconfig.defaults").read_text(encoding="utf-8")
        csv, doc = table_from_csv(b["csv"]), table_from_doc(b["doc"])
        res[board] = {
            "table": {"pass": bool(csv) and csv == doc, "csv": csv, "doc_same": csv == doc},
            "rollback": {"pass": "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in defaults and
                         'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="%s"' % b["csv_conf"] in defaults},
        }
    cm = (LYRAT / "main" / "CMakeLists.txt").read_text(encoding="utf-8")
    pc = re.search(r'LYRAT_DEFAULT_MODE STREQUAL "pc"\)\s*\n\s*target_compile_definitions\([^)]*BENCH_DEFAULT_MODE_PC', cm)
    res["lyrat_pc"] = {"default_mode": {"pass": pc is not None}}
    report = {"sources": res, "pass": all(c["pass"] for e in res.values() for c in e.values())}
    out.mkdir(parents=True, exist_ok=True)
    (out / "fw_sources.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--only", choices=list(BOARDS))
    ap.add_argument("--no-build", action="store_true", help="check the images of an earlier build only")
    ap.add_argument("--no-cache", action="store_true", help="build even when the inputs are those of the last good build")
    ap.add_argument("--sources", action="store_true", help="no build: the rules readable from the sources (goal 110)")
    args = ap.parse_args()
    if args.sources:
        report = sources(Path(args.out))
        print(json.dumps(report, indent=1))
        return 0 if report["pass"] else 1
    report = run(Path(args.out), (args.only,) if args.only else tuple(BOARDS), not args.no_build, not args.no_cache)
    print(json.dumps(report, indent=1))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
