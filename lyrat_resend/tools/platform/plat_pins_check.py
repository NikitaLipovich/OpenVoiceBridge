#!/usr/bin/env python3
"""Pins of the firmware against the wire table of goal 068 (variant D: U1, U2, U3). Goal 095.

  plat_pins_check.py [--header platform/components/plat_core/include/plat_pins.h] [--wires .../asm_data.py] [--mutate]

Checks, all must pass:
  1. every signal wire end on U1/U2/U3 in the 068 table (list W of asm_data.py) is a row of PLAT_PIN_TABLE with the same
     board, wire name and pin, and every row is such a wire end (both ways, as multisets);
  2. IO0 of U1 is driven by U3 GPIO12 (068: not 47, which is 1.8 V on the N16R16VA);
  3. pin rules of the chips: ESP32 (U1, U2) no flash pins 6-11, no PSRAM 16/17 of a WROVER, GPIO34-39 inputs only;
     ESP32-S3 (U3) no strapping 0/3/45/46, no USB 19/20, no flash/PSRAM 26-37, not 38 (RGB LED), not 43/44 (UART0
     bridge), not 47/48 (1.8 V on N16R16VA); no pin used twice on a board;
  4. R13 in the code of U3: no gpio_set_level(..., 1) anywhere in platform/u3_relay, the EN/IO0 pins of the nodes used
     only by u3_port.c.
--mutate also runs the check on copies with known faults (IO0 back on GPIO47, a swapped UART pair, a dropped row, an
output on GPIO34) and fails unless every one is caught.
Prints one JSON line; exit 0 when nothing is wrong.
"""
import argparse
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
HEADER = REPO / "platform" / "components" / "plat_core" / "include" / "plat_pins.h"
WIRES = REPO / "goals_harness" / "phone-then-pc" / "068-node-placement" / "sandbox" / "scripts" / "asm_data.py"
U3_SRC = REPO / "platform" / "u3_relay" / "main"
BOARDS = ("U1", "U2", "U3")

ESP32_BAD = set(range(6, 12)) | {16, 17}
ESP32_INPUT_ONLY = set(range(34, 40))
S3_BAD = {0, 3, 45, 46, 19, 20, 38, 43, 44, 47, 48} | set(range(26, 38))
S3_VALID = set(range(0, 22)) | set(range(26, 49))


def parse_header(text):
    """(rows, defines): rows = [(board, wire, pin)], pin an int or 'EN'."""
    defs = {}
    for m in re.finditer(r"^#define\s+(\w+)\s+(\(-2\)|-?\d+|PLAT_PIN_EN)\b", text, re.M):
        v = m.group(2)
        defs[m.group(1)] = "EN" if v in ("PLAT_PIN_EN", "(-2)") else int(v)
    rows = []
    for m in re.finditer(r'X\((U\d),\s*"([^"]+)",\s*(\w+)\)', text):
        board, wire, name = m.groups()
        if name not in defs:
            raise ValueError("table row %s %s: %s is not defined" % (board, wire, name))
        rows.append((board, wire, defs[name]))
    return rows, defs


def board_pin(board, pin):
    """068 pin label -> GPIO number or 'EN'; None for power and ground."""
    if "GND" in pin or "3V3" in pin:
        return None
    if board == "U3":
        return int(pin) if pin.isdigit() else None
    if pin == "EN":
        return "EN"
    if pin == "TX":
        return 1
    if pin == "RX":
        return 3
    m = re.fullmatch(r"P(\d+)", pin)
    return int(m.group(1)) if m else None


def load_wires(path):
    spec = importlib.util.spec_from_file_location("asm_data_068", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ends = []      # (board, wire, pin, direction)
    for w in mod.wires():
        for side, (m, p) in (("out", w["a"]), ("in", w["b"])):
            if m not in BOARDS:
                continue
            gpio = board_pin(m, p)
            if gpio is None:
                continue
            ends.append((m, w["sig"], gpio, side, w["n"]))
    return ends


def check(header_text, wires_path, u3_src=U3_SRC):
    problems = []
    rows, defs = parse_header(header_text)
    ends = load_wires(wires_path)
    want = sorted((b, s, g) for b, s, g, _, _ in ends)
    have = sorted(rows)
    rest = list(have)
    for e in want:
        if e in rest:
            rest.remove(e)
        else:
            problems.append({"kind": "missing_in_code", "board": e[0], "wire": e[1], "pin": e[2]})
    for e in rest:
        problems.append({"kind": "not_in_068", "board": e[0], "wire": e[1], "pin": e[2]})

    b01 = [g for b, s, g in rows if b == "U3" and s == "B01_D"]
    if b01 != [12]:
        problems.append({"kind": "io0_u1", "want": 12, "got": b01})

    direction = {(b, g): d for b, _, g, d, _ in ends}
    seen = {}
    for b, s, g in rows:
        if g == "EN":
            continue
        if (b, g) in seen and seen[(b, g)] != s:
            problems.append({"kind": "pin_twice", "board": b, "pin": g, "wires": [seen[(b, g)], s]})
        seen.setdefault((b, g), s)
        if b in ("U1", "U2"):
            if g in ESP32_BAD or not 0 <= g <= 39:
                problems.append({"kind": "esp32_pin", "board": b, "wire": s, "pin": g})
            if g in ESP32_INPUT_ONLY and direction.get((b, g)) == "out":
                problems.append({"kind": "input_only_driven", "board": b, "wire": s, "pin": g})
        else:
            if g in S3_BAD or g not in S3_VALID:
                problems.append({"kind": "s3_pin", "board": b, "wire": s, "pin": g})

    r13 = {"files": 0, "set_high": [], "od_pins_outside_port": []}
    od_names = ("U3_PIN_EN1", "U3_PIN_EN2", "U3_PIN_BOOT1", "U3_PIN_BOOT2")
    for f in sorted(Path(u3_src).glob("*.c")):
        text = f.read_text(encoding="utf-8")
        r13["files"] += 1
        for m in re.finditer(r"gpio_set_level\s*\([^,]+,\s*1\s*\)", text):
            r13["set_high"].append("%s:%d" % (f.name, text[:m.start()].count("\n") + 1))
        if f.name != "u3_port.c":
            for name in od_names:
                if name in text:
                    r13["od_pins_outside_port"].append("%s:%s" % (f.name, name))
    if r13["set_high"] or r13["od_pins_outside_port"] or not r13["files"]:
        problems.append({"kind": "r13", **r13})
    return {"rows": len(rows), "wire_ends": len(want), "problems": problems, "r13_files": r13["files"]}


MUTATIONS = [
    ("io0 back on 47", r"#define U3_PIN_BOOT1        12", "#define U3_PIN_BOOT1        47"),
    ("C1 pair swapped", r"#define U3_PIN_C1_TX        1 ", "#define U3_PIN_C1_TX        2 "),
    ("row dropped", '    X(U2, "MEMS2", U2_PIN_I2S1_DIN) \\\n', ""),
    ("output on 34", "#define U1_PIN_I2S0_DOUT    26", "#define U1_PIN_I2S0_DOUT    34"),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--header", default=str(HEADER))
    ap.add_argument("--wires", default=str(WIRES))
    ap.add_argument("--mutate", action="store_true")
    args = ap.parse_args(argv)
    text = Path(args.header).read_text(encoding="utf-8")
    out = check(text, args.wires)
    ok = not out["problems"]
    if args.mutate:
        caught = {}
        for name, old, new in MUTATIONS:
            if old not in text:
                caught[name] = "pattern_missing"
                ok = False
                continue
            res = check(text.replace(old, new, 1), args.wires)
            caught[name] = len(res["problems"])
            ok &= res["problems"] != []
        out["mutations_caught"] = caught
    out["ok"] = ok
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
