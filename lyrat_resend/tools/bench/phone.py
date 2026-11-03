#!/usr/bin/env python3
"""Phone automation for the bench: Bluetooth state and the LyraT_Phone pairing, through adb only.

  phone.py status            state as JSON, changes nothing
  phone.py connect           tap the paired LyraT_Phone row, wait for the headset profile
  phone.py forget            unpair LyraT_Phone (device page is verified before Unpair is tapped)
  phone.py pair              find LyraT_Phone in the settings list and accept the pairing dialog

Only LyraT_Phone is ever touched. Coordinates come from a fresh `uiautomator dump` and only nodes
with the exact board name are tapped. Every write action compares the bond list of the other devices
before and after; a difference is exit code 3. Tested on ColorOS, English UI, see
goals_harness/tools/catalogs/oplus_bt_ui.md. No MAC prefix is printed or saved, the failed-screen
dumps go to <runs>/phone/ with MAC addresses reduced to their last two octets.

Exit code: 0 done, 1 failed, 2 adb or the screen is not usable, 3 other bonds changed.
Based on goals_harness/tools/seeds/repro_connect.py.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import RUNS_ROOT, redact, stamp  # noqa: E402

BOARD_NAME = "LyraT_Phone"
UI_FILE = "/data/local/tmp/lyrat_ui.xml"
Node = namedtuple("Node", "text rid desc clickable checked x1 y1 x2 y2")
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
BONDED = re.compile(r"^\s*(?:[0-9A-Fa-f]{2}:){4}([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})\s+\[\s*([^\]]+?)\s*\]\[\s*([^\]]+?)\s*\]\s+(.*\S)\s*$")
# dumpsys masks the first octets as XX in the profile sections.
HEADSET_DEVICE = re.compile(r"(?:[0-9A-Fa-fXx]{2}:){4}([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})")


class PhoneError(Exception):
    code = 1


class Unusable(PhoneError):
    code = 2


class OtherBondsChanged(PhoneError):
    code = 3


def adb(*args, timeout=30.0):
    try:
        res = subprocess.run(["adb", *args], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unusable("adb failed: %s" % type(e).__name__)
    if res.returncode != 0:
        raise Unusable("adb %s: %s" % (args[0], (res.stderr or res.stdout).strip()[:120]))
    return res.stdout


# --- read-only state ---------------------------------------------------------------------------

def bond_list(dump=None):
    """All bonded devices as (suffix, type, name), in the phone's own order."""
    dump = dump if dump is not None else adb("shell", "dumpsys", "bluetooth_manager", timeout=60)
    out, inside = [], False
    for line in dump.splitlines():
        if "Bonded devices:" in line:
            inside = True
            continue
        if inside:
            m = BONDED.match(line)
            if not m:
                break
            out.append((m.group(1).lower(), m.group(2), m.group(4)))
    return out


def others(bonds):
    return sorted(b for b in bonds if b[2] != BOARD_NAME)


def is_bonded(bonds=None):
    return any(b[2] == BOARD_NAME for b in (bond_list() if bonds is None else bonds))


def hfp_connected(dump=None):
    """True when the phone's HeadsetService holds the board as a connected device."""
    dump = dump if dump is not None else adb("shell", "dumpsys", "bluetooth_manager", timeout=60)
    suffix = next((b[0] for b in bond_list(dump) if b[2] == BOARD_NAME), None)
    if suffix is None:
        return False
    i = dump.find("Profile: HeadsetService")
    if i < 0:
        return False
    j = dump.find("Profile: ", i + 10)
    section = dump[i:j if j > 0 else None]
    m = re.search(r"mCurrentDevice: (\S+)\s*\n\s*mCurrentState: (\w+)", section)
    if m:
        hit = HEADSET_DEVICE.search(m.group(1))
        return bool(hit) and hit.group(1).lower() == suffix and m.group(2) in ("Connected", "AudioOn", "AudioConnecting")
    return False


def status():
    dump = adb("shell", "dumpsys", "bluetooth_manager", timeout=60)
    bonds = bond_list(dump)
    win = adb("shell", "dumpsys", "window")
    bat = adb("shell", "dumpsys", "battery")
    level = re.search(r"level: (\d+)", bat)
    return {
        "state": adb("get-state").strip(),
        "bt_enabled": bool(re.search(r"enabled: true", dump)),
        "bonded": is_bonded(bonds),
        "hfp_connected": hfp_connected(dump),
        "bonds_total": len(bonds),
        "other_bonds": len(others(bonds)),
        "keyguard": "isKeyguardShowing=true" in win,
        "battery_level": int(level.group(1)) if level else None,
    }


# --- screen ------------------------------------------------------------------------------------

def screen():
    """Current UI nodes from a fresh dump; the dump fails while the UI is animating, so retry."""
    last = ""
    for _ in range(6):
        adb("shell", "uiautomator", "dump", UI_FILE, timeout=30)
        xml = adb("exec-out", "cat", UI_FILE)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            last = xml[:80]
            time.sleep(0.7)
            continue
        nodes = []
        for e in root.iter("node"):
            m = BOUNDS.match(e.get("bounds", ""))
            if m:
                nodes.append(Node(e.get("text", ""), e.get("resource-id", "").split("/")[-1], e.get("content-desc", ""),
                                  e.get("clickable") == "true", e.get("checked") == "true", *map(int, m.groups())))
        return nodes
    raise Unusable("no UI dump: %s" % last)


def save_screen(tag):
    """Keep what was on the screen when an operation failed."""
    try:
        adb("shell", "uiautomator", "dump", UI_FILE, timeout=30)
        out = RUNS_ROOT / "phone"
        out.mkdir(parents=True, exist_ok=True)
        path = out / ("%s-%s.xml" % (stamp(), tag))
        path.write_text(redact(adb("exec-out", "cat", UI_FILE)), encoding="utf-8")
        return str(path)
    except (PhoneError, OSError):
        return None


def tap(node):
    adb("shell", "input", "tap", str((node.x1 + node.x2) // 2), str((node.y1 + node.y2) // 2))


def by_text(nodes, text, rid=None):
    return next((n for n in nodes if n.text == text and (rid is None or n.rid == rid)), None)


def row_of(nodes, title):
    """The smallest clickable node around a title: the list row itself."""
    cx, cy = (title.x1 + title.x2) // 2, (title.y1 + title.y2) // 2
    rows = [n for n in nodes if n.clickable and n.x1 <= cx <= n.x2 and n.y1 <= cy <= n.y2]
    return min(rows, key=lambda n: (n.x2 - n.x1) * (n.y2 - n.y1)) if rows else None


def gear_of(nodes, row):
    """A paired row carries a settings gear; a row found by scanning does not."""
    return next((n for n in nodes if n.rid == "deviceDetails" and row.y1 <= (n.y1 + n.y2) // 2 <= row.y2), None)


def prepare():
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "svc", "power", "stayon", "usb")
    if "isKeyguardShowing=true" in adb("shell", "dumpsys", "window"):
        raise Unusable("phone is locked, UI automation cannot run")


def open_settings():
    adb("shell", "am", "start", "--activity-clear-task", "-a", "android.settings.BLUETOOTH_SETTINGS")
    time.sleep(1.5)


def go_home():
    # An open settings page keeps scanning; leave it so a later measurement is not disturbed.
    adb("shell", "input", "keyevent", "HOME")


def scroll(nodes, down=True):
    top, bottom = min(n.y1 for n in nodes), max(n.y2 for n in nodes)
    x = str((min(n.x1 for n in nodes) + max(n.x2 for n in nodes)) // 2)
    a, b = int(top + (bottom - top) * 0.8), int(top + (bottom - top) * 0.3)
    adb("shell", "input", "swipe", x, str(a if down else b), x, str(b if down else a), "350")
    time.sleep(0.6)


def pairing_dialog(nodes):
    return by_text(nodes, "Bluetooth pairing request", "alertTitle")


def find_board_row(want_paired, timeout):
    """Scroll the settings list until the board's row of the wanted kind is visible."""
    end = time.monotonic() + timeout
    prev = None
    while time.monotonic() < end:
        nodes = screen()
        title = by_text(nodes, BOARD_NAME, "title")
        if title:
            row = row_of(nodes, title)
            gear = gear_of(nodes, row) if row else None
            if row and bool(gear) == want_paired:
                return nodes, row, gear
        texts = [n.text for n in nodes if n.rid == "title"]
        if texts == prev:      # the end of the list: back to the top, so the scan rows are seen again
            open_settings()
            prev = None
            continue
        prev = texts
        scroll(nodes)
    return None, None, None


# --- actions -----------------------------------------------------------------------------------

def guarded(action):
    """Run a write action and prove afterwards that nothing but LyraT_Phone changed."""
    def wrapper(*args, **kw):
        before = others(bond_list())
        prepare()
        try:
            return action(*args, **kw)
        finally:
            after = others(bond_list())
            go_home()
            if after != before:
                raise OtherBondsChanged("other bonds changed: %d -> %d" % (len(before), len(after)))
    return wrapper


@guarded
def forget(timeout=30.0):
    t0 = time.monotonic()
    if not is_bonded():
        return {"action": "forget", "ok": True, "already": True, "seconds": 0.0}
    suffix = next(b[0] for b in bond_list() if b[2] == BOARD_NAME)
    open_settings()
    nodes, row, gear = find_board_row(True, timeout)
    if not gear:
        raise PhoneError("paired row not found, screen %s" % save_screen("forget_no_row"))
    tap(gear)
    time.sleep(1.5)
    nodes = screen()
    name = next((n for n in nodes if n.rid == "assignment"), None)
    mac = next((n for n in nodes if n.rid == "summary" and HEADSET_DEVICE.search(n.text)), None)
    unpair = by_text(nodes, "Unpair", "title")
    # Both identities must agree: an Unpair tapped on another device's page would break R4.
    if not (name and name.text == BOARD_NAME and mac and unpair
            and HEADSET_DEVICE.search(mac.text).group(1).lower() == suffix):
        raise PhoneError("not the board's page, screen %s" % save_screen("forget_wrong_page"))
    tap(row_of(nodes, unpair) or unpair)
    end = time.monotonic() + 15
    while time.monotonic() < end:
        time.sleep(0.7)
        if not is_bonded():
            return {"action": "forget", "ok": True, "already": False, "seconds": round(time.monotonic() - t0, 2)}
    raise PhoneError("still bonded after Unpair, screen %s" % save_screen("forget_still_bonded"))


@guarded
def pair(timeout=90.0):
    t0 = time.monotonic()
    if is_bonded():
        return {"action": "pair", "ok": True, "already": True, "seconds": 0.0, "passkey": None}
    open_settings()
    nodes = screen()
    if pairing_dialog(nodes):
        # Left by an earlier attempt: not from the tap below, so its passkey is not ours to accept.
        cancel = by_text(nodes, "Cancel", "button2")
        if not cancel:
            raise PhoneError("stale dialog without Cancel, screen %s" % save_screen("pair_stale_dialog"))
        tap(cancel)
        time.sleep(1.0)
        open_settings()
    end = t0 + timeout
    tapped_at = None
    prev = None
    while time.monotonic() < end:
        nodes = screen()
        if pairing_dialog(nodes) and tapped_at is not None:
            key = next((n.text for n in nodes if n.rid == "passkey"), None)
            accept = by_text(nodes, "Pair", "button1")
            if not accept:
                raise PhoneError("dialog without Pair, screen %s" % save_screen("pair_no_button"))
            tap(accept)
            bond_end = time.monotonic() + 30
            while time.monotonic() < bond_end:
                time.sleep(0.7)
                if is_bonded():
                    return {"action": "pair", "ok": True, "already": False, "passkey": key,
                            "seconds": round(time.monotonic() - t0, 2)}
            raise PhoneError("not bonded after Pair, screen %s" % save_screen("pair_not_bonded"))
        if pairing_dialog(nodes):
            # Raised before our tap, so it is not ours to accept.
            cancel = by_text(nodes, "Cancel", "button2")
            if cancel:
                tap(cancel)
            time.sleep(1.0)
            continue
        if is_bonded():
            return {"action": "pair", "ok": True, "already": False, "passkey": None,
                    "seconds": round(time.monotonic() - t0, 2)}
        title = by_text(nodes, BOARD_NAME, "title")
        row = row_of(nodes, title) if title else None
        if row and not gear_of(nodes, row):
            if tapped_at is None or time.monotonic() - tapped_at > 12:
                tap(row)
                tapped_at = time.monotonic()
            time.sleep(1.0)
            continue
        if tapped_at is not None and time.monotonic() - tapped_at < 12:
            time.sleep(1.0)   # the row may have moved while connecting; the dialog is coming
            continue
        texts = [n.text for n in nodes if n.rid == "title"]
        if texts == prev:
            open_settings()   # bottom of the list: reopening also restarts the scan
            prev = None
            continue
        prev = texts
        scroll(nodes)
    raise PhoneError("no pairing within %.0f s, screen %s" % (timeout, save_screen("pair_timeout")))


@guarded
def connect(timeout=30.0):
    t0 = time.monotonic()
    if not is_bonded():
        raise PhoneError("not paired")
    if hfp_connected():
        return {"action": "connect", "ok": True, "already": True, "seconds": 0.0}
    open_settings()
    nodes, row, gear = find_board_row(True, timeout)
    if not row:
        raise PhoneError("paired row not found, screen %s" % save_screen("connect_no_row"))
    tap(row)
    end = t0 + timeout
    while time.monotonic() < end:
        time.sleep(1.0)
        if hfp_connected():
            return {"action": "connect", "ok": True, "already": False, "seconds": round(time.monotonic() - t0, 2)}
    raise PhoneError("no headset connection within %.0f s" % timeout)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["status", "connect", "forget", "pair"])
    ap.add_argument("--timeout", type=float, help="seconds for the operation")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    func = {"status": status, "connect": connect, "forget": forget, "pair": pair}[args.action]
    try:
        result = func(args.timeout) if args.timeout and args.action != "status" else func()
    except PhoneError as e:
        print(json.dumps({"action": args.action, "ok": False, "error": redact(str(e))}, ensure_ascii=False))
        return e.code
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
