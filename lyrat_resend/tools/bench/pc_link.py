#!/usr/bin/env python3
"""What Windows sees of the LyraT (goal 102, docs/DUAL_MODE.md): read only, nothing in Windows is changed.

  python pc_link.py [--json]              ask Windows (PowerShell Get-PnpDevice), run by the owner on the PC paired with it
  python pc_link.py --from-json FILE      the same verdict from a saved answer (tests)

Lists the devices whose name has "LyraT_Phone": the Bluetooth device, its Hands-Free (HFP) parts and the audio
endpoints Zoom offers ("Headset Microphone (LyraT_Phone Hands-Free)" and the headphones/speaker one). Status OK means
present and connected; Unknown or Error means paired but not connected now (in the PC mode the LyraT calls the PC
within 8 s). Prints one line per device and a verdict:
  connected     the endpoints are there and OK: choose them in Zoom, audio opens when Zoom uses them
  paired        Windows knows LyraT_Phone, no live link: is the LyraT in the PC mode ("s3_uart.py mode pc")?
  not_paired    no LyraT_Phone at all: Settings > Bluetooth & devices > Add device > Bluetooth > LyraT_Phone
Exit code 0 connected, 1 otherwise. It does not record or play any sound (R15) and does not pair or connect (R10).
"""
import argparse
import json
import subprocess
import sys

NAME = "LyraT_Phone"
PS = ("Get-PnpDevice -FriendlyName '*%s*' -ErrorAction SilentlyContinue | "
      "Select-Object FriendlyName, Class, Status, Present | ConvertTo-Json -Compress" % NAME)


def ask_windows():
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    return r.stdout


def devices(text):
    text = (text or "").strip()
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def verdict(devs):
    """(verdict, rows): rows of name, kind, status."""
    rows = []
    for d in devs:
        name, cls = d.get("FriendlyName") or "", (d.get("Class") or "")
        if cls == "AudioEndpoint":
            kind = "mic" if "icrophone" in name or "Headset Microphone" in name else "speaker"
        elif "Hands-Free" in name:
            kind = "hfp"
        else:
            kind = "bt"
        rows.append({"name": name, "kind": kind, "status": d.get("Status") or "", "present": bool(d.get("Present"))})
    if not rows:
        return "not_paired", rows
    ends = [r for r in rows if r["kind"] in ("mic", "speaker")]
    ok = [r for r in ends if r["status"] == "OK" and r["present"]]
    if any(r["kind"] == "mic" for r in ok) and any(r["kind"] == "speaker" for r in ok):
        return "connected", rows
    return "paired", rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--from-json", help="a saved answer of Get-PnpDevice ... | ConvertTo-Json")
    args = ap.parse_args()
    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            text = f.read()
    else:
        text = ask_windows()
    v, rows = verdict(devices(text))
    if args.json:
        print(json.dumps({"verdict": v, "devices": rows}, indent=1))
    else:
        for r in rows:
            print("%-8s %-8s %s" % (r["kind"], r["status"] or "-", r["name"]))
        print("verdict: " + v)
    return 0 if v == "connected" else 1


if __name__ == "__main__":
    sys.exit(main())
