#!/usr/bin/env python3
"""What fills a firmware image: bytes in the .bin per component, grouped, from `idf.py size-components --format json2`.

  python tools/size_table.py COMPONENTS.json [--base OLD.json] [--slot 0x1F0000] [--top 25] [--md]

Bytes in the image of one component = Flash Code + Flash Data + IRAM + DRAM .data (.bss takes no room in the .bin).
Groups put the many libraries under names a reader knows (Bluetooth, Wi-Fi, libc, ...). With --base the table gets a
column of the change, for "what did Wi-Fi add". Made by goal 096.
"""
import argparse
import json
import sys

# (group, prefixes of the archive name without "lib" and ".a")
GROUPS = [
    ("String constants of all components (log, assert texts)", ("(merged string constants)",)),
    ("Bluetooth (Bluedroid + controller)", ("bt", "btdm_app")),
    ("Wi-Fi (driver, supplicant, net80211, pp)", ("esp_wifi", "wpa_supplicant", "net80211", "pp", "core", "mesh",
                                                   "espnow", "smartconfig", "wapi", "esp_coex", "coexist")),
    ("TCP/IP (lwip, netif)", ("lwip", "esp_netif")),
    ("Crypto (mbedtls)", ("mbedtls", "mbedcrypto", "mbedx509", "p256m", "everest")),
    ("PHY / RF calibration", ("phy", "rtc", "esp_phy")),
    ("C library (newlib, libgcc, libstdc++)", ("c", "m", "gcc", "stdc++", "newlib", "c_speed", "gcov", "cxx")),
    ("Application (main: audio, NS, mSBC glue, OTA, log)", ("main",)),
    ("FreeRTOS", ("freertos",)),
    ("Drivers (I2S, I2C, GPIO, UART)", ("driver", "esp_driver_", "esp_hw_support", "hal", "soc", "esp_pm",
                                        "esp_ringbuf")),
    ("Flash, NVS, OTA, partitions", ("spi_flash", "nvs_flash", "app_update", "bootloader_support", "esp_partition",
                                     "esp_app_format", "esp_bootloader_format", "efuse")),
]


def image_bytes(entry):
    mt = entry.get("memory_types", {})
    total = 0
    for name, m in mt.items():
        if name in ("Flash Code", "Flash Data", "IRAM"):
            total += m.get("size", 0)
        elif name == "DRAM":
            total += m.get("sections", {}).get(".data", {}).get("size", 0)
    return total


def short(name):
    n = name.split(":")[0]
    if n.startswith("lib"):
        n = n[3:]
    if n.endswith(".a"):
        n = n[:-2]
    return n


def group_of(name):
    n = short(name)
    for g, prefixes in GROUPS:
        for p in prefixes:
            if n == p or (p.endswith("_") and n.startswith(p)):
                return g
    return "Other (ESP-IDF system, heap, log, timers, ...)"


MERGED = "(merged string constants)"


def per_component(path):
    """Bytes per component. The linker merges equal string literals of every object (log formats, assert texts,
    __func__) into one section and the map lists it under the first object that has one, esp_app_desc.c of
    esp_app_format: its .rodata is moved to a row of its own."""
    d = json.load(open(path, encoding="utf-8"))
    out = {k: image_bytes(v) for k, v in d.items()}
    for k, v in d.items():
        if short(k) == "esp_app_format":
            ro = v.get("memory_types", {}).get("Flash Data", {}).get("sections", {}).get(".rodata", {}).get("size", 0)
            if ro > 4096:
                out[k] -= ro
                out[MERGED] = ro
    return out


def table(cur, base=None, slot=None, top=25, md=False):
    groups = {}
    for k, v in cur.items():
        groups.setdefault(group_of(k), 0)
        groups[group_of(k)] += v
    bgroups = {}
    if base:
        for k, v in base.items():
            bgroups.setdefault(group_of(k), 0)
            bgroups[group_of(k)] += v
    total = sum(cur.values())
    out = []
    head = ["Group", "Bytes", "% of image"] + (["Change"] if base else [])
    if md:
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "---|" * len(head))
    for g, v in sorted(groups.items() | {(g, 0) for g in bgroups if g not in groups}, key=lambda x: -x[1]):
        row = [g, "%d" % v, "%.1f" % (100.0 * v / total)]
        if base:
            row.append("%+d" % (v - bgroups.get(g, 0)))
        out.append(("| " + " | ".join(row) + " |") if md else "%-52s %9s %6s %s" % tuple(row + [""] * (4 - len(row))))
    trow = ["**Total**" if md else "Total", "%d" % total, "100"]
    if base:
        trow.append("%+d" % (total - sum(base.values())))
    out.append(("| " + " | ".join(trow) + " |") if md else "%-52s %9s %6s %s" % tuple(trow + [""] * (4 - len(trow))))
    if slot:
        out.append("")
        out.append("Slot 0x%X = %d bytes: components fill %.1f %%" % (slot, slot, 100.0 * total / slot))
    out.append("")
    out.append("Largest components:" if not md else "**Largest components**")
    if md:
        out.append("")
        out.append("| Component | Group | Bytes |" + (" Change |" if base else ""))
        out.append("|---|---|---|" + ("---|" if base else ""))
    for k, v in sorted(cur.items(), key=lambda x: -x[1])[:top]:
        ch = ("%+d" % (v - base.get(k, 0))) if base else ""
        if md:
            out.append("| %s | %s | %d |%s" % (short(k), group_of(k), v, (" %s |" % ch) if base else ""))
        else:
            out.append("  %-28s %-45s %9d %s" % (short(k), group_of(k)[:45], v, ch))
    if base:
        new = [k for k in cur if k not in base and cur[k]]
        if new:
            out.append("")
            out.append("New components: " + ", ".join("%s %d" % (short(k), cur[k]) for k in sorted(new, key=lambda k: -cur[k])))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("components", help="output of idf.py size-components --format json2 --output-file F")
    ap.add_argument("--base", help="an older one, for the change column")
    ap.add_argument("--slot", type=lambda s: int(s, 0), help="slot size, e.g. 0x1F0000")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--md", action="store_true", help="Markdown tables")
    a = ap.parse_args()
    print(table(per_component(a.components), per_component(a.base) if a.base else None, a.slot, a.top, a.md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
