#!/usr/bin/env python3
"""
knobs.py — лестница ручек для lyrat_harness.

Каждая ручка умеет apply(dry_run). При dry_run=True ничего не меняется,
возвращается описание того, что было бы сделано.

Типы:
  auto      — правит только Windows-состояние, обратимо
  gated     — трогает реестр
  gated_fw  — правит исходники прошивки, билдит и шьёт (требует --apply-gated)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# консоль Windows по умолчанию cp1252 — форсим UTF-8, иначе кириллица падает
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
MAIN_C = PROJ / "main" / "main.c"
SDK_DEFAULTS = PROJ / "sdkconfig.defaults"
WIN = HERE / "win"

IDF_PATH = r"C:\esp\v5.5.2\esp-idf"
IDF_TOOLS = r"C:\Espressif"
FLASH_PORT = "COM15"


# ---------------------------------------------------------------------------
# помощники
# ---------------------------------------------------------------------------
def local_ipv4() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def pwsh(cmd: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                          capture_output=True, text=True, timeout=timeout)


def backup(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + ".harness-bak")
    if not bak.exists():
        shutil.copy2(path, bak)
    return bak


def patch_file(path: Path, old: str, new: str, dry_run: bool) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if new in text and old not in text:
        return "уже применено (%s)" % path.name
    if old not in text:
        return "ЯКОРЬ НЕ НАЙДЕН в %s: %r" % (path.name, old[:60])
    if dry_run:
        return "dry-run: %s  %r -> %r" % (path.name, old[:60], new[:60])
    backup(path)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return "патч применён: %s" % path.name


def build_and_flash(dry_run: bool) -> str:
    if dry_run:
        return "dry-run: idf.py -p %s build flash" % FLASH_PORT
    cmd = (r"$env:IDF_TOOLS_PATH='%s'; . '%s\export.ps1' | Out-Null; "
           r"idf.py -C '%s' -p %s build flash" % (IDF_TOOLS, IDF_PATH, PROJ, FLASH_PORT))
    r = pwsh(cmd, timeout=900)
    tail = (r.stdout or "")[-600:] + (r.stderr or "")[-600:]
    return "build+flash rc=%d\n%s" % (r.returncode, tail)


# ---------------------------------------------------------------------------
# ручки
# ---------------------------------------------------------------------------
@dataclass
class Knob:
    id: str
    title: str
    kind: str
    fn: Callable[[bool], str]
    fixes: str = ""

    def apply(self, dry_run: bool) -> str:
        return self.fn(dry_run)


def k_firewall(dry_run: bool) -> str:
    ports = [5004, 5005, 5006]
    if dry_run:
        return "dry-run: inbound allow UDP %s" % ports
    out = []
    for p in ports:
        name = "LyraT UDP %d" % p
        chk = pwsh("Get-NetFirewallRule -DisplayName '%s' -ErrorAction SilentlyContinue" % name, 60)
        if chk.stdout.strip():
            out.append("%d: уже есть" % p)
            continue
        r = pwsh("New-NetFirewallRule -DisplayName '%s' -Direction Inbound -Protocol UDP "
                 "-LocalPort %d -Action Allow -Profile Any" % (name, p), 60)
        out.append("%d: rc=%d" % (p, r.returncode))
    return "; ".join(out)


def k_btconnect(dry_run: bool) -> str:
    if dry_run:
        return "dry-run: BluetoothSetServiceState(Handsfree, ENABLE)"
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(WIN / "bt_connect.ps1"), "-Match", "LyraT", "-Action", "enable"],
                       capture_output=True, text=True, timeout=120)
    return (r.stdout or r.stderr).strip()[-300:]


def k_stale_endpoints(dry_run: bool) -> str:
    """Удалить endpoint'ы с DeviceState=4 (NOTPRESENT) — протухшие от прошлой пары."""
    script = (
        "$out=@(); foreach ($flow in 'Capture','Render') { "
        "$root=\"HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\Audio\\$flow\"; "
        "Get-ChildItem $root -EA SilentlyContinue | ForEach-Object { "
        "$p=Get-ItemProperty \"$($_.PSPath)\\Properties\" -EA SilentlyContinue; "
        "$d=$p.'{b3f8fa53-0004-438e-9003-51a46e139bfc},6'; "
        "$st=[int](Get-ItemProperty $_.PSPath -EA SilentlyContinue).DeviceState; "
        "if ($d -like '*LyraT*' -and $st -eq 4) { %s; $out+=\"$flow/$($_.PSChildName)\" } } } "
        "$out -join ', '"
    )
    if dry_run:
        return "dry-run: удалить MMDevices-ключи LyraT с DeviceState=4 (нужен админ)\n" \
               + (pwsh(script % "$null", 60).stdout or "").strip()
    r = pwsh(script % "Remove-Item $_.PSPath -Recurse -Force -EA SilentlyContinue", 120)
    return "удалено: %s (rc=%d)" % ((r.stdout or "").strip() or "ничего", r.returncode)


def k_destip(dry_run: bool) -> str:
    ip = local_ipv4()
    if not ip:
        return "не удалось определить IP этого ПК"
    a, b, c, d = ip.split(".")
    text = MAIN_C.read_text(encoding="utf-8", errors="replace")
    cur = re.search(r"#define UDP_REMOTE_IP_A\s+(\d+)\s*\n#define UDP_REMOTE_IP_B\s+(\d+)\s*\n"
                    r"#define UDP_REMOTE_IP_C\s+(\d+)\s*\n#define UDP_REMOTE_IP_D\s+(\d+)", text)
    if not cur:
        return "не нашёл UDP_REMOTE_IP_* в main.c"
    old = cur.group(0)
    if (cur.group(1), cur.group(2), cur.group(3), cur.group(4)) == (a, b, c, d):
        return "уже %s" % ip
    new = ("#define UDP_REMOTE_IP_A  %s\n#define UDP_REMOTE_IP_B  %s\n"
           "#define UDP_REMOTE_IP_C  %s\n#define UDP_REMOTE_IP_D  %s" % (a, b, c, d))
    msg = patch_file(MAIN_C, old, new, dry_run)
    return "%s (%s -> %s); %s" % (
        msg, ".".join(cur.groups()), ip, build_and_flash(dry_run))


def k_coex_bt(dry_run: bool) -> str:
    msg = patch_file(MAIN_C,
                     "esp_coex_preference_set(ESP_COEX_PREFER_WIFI);",
                     "esp_coex_preference_set(ESP_COEX_PREFER_BT);",
                     dry_run)
    return "%s; %s" % (msg, build_and_flash(dry_run))


def k_txpower(dry_run: bool) -> str:
    msg = patch_file(MAIN_C,
                     "esp_wifi_set_max_tx_power(52);",
                     "esp_wifi_set_max_tx_power(32);",
                     dry_run)
    return "%s; %s" % (msg, build_and_flash(dry_run))


def k_nowbs(dry_run: bool) -> str:
    text = SDK_DEFAULTS.read_text(encoding="utf-8", errors="replace")
    if "CONFIG_BT_HFP_WBS_ENABLE=n" in text:
        return "уже выключено"
    if "CONFIG_BT_HFP_WBS_ENABLE=y" in text:
        msg = patch_file(SDK_DEFAULTS, "CONFIG_BT_HFP_WBS_ENABLE=y",
                         "CONFIG_BT_HFP_WBS_ENABLE=n", dry_run)
    else:
        if dry_run:
            return "dry-run: добавить CONFIG_BT_HFP_WBS_ENABLE=n в sdkconfig.defaults"
        backup(SDK_DEFAULTS)
        SDK_DEFAULTS.write_text(text.rstrip() + "\nCONFIG_BT_HFP_WBS_ENABLE=n\n", encoding="utf-8")
        msg = "добавлено в sdkconfig.defaults"
    # sdkconfig перекрывает defaults — синхронизируем и его
    sdk = PROJ / "sdkconfig"
    if sdk.exists() and not dry_run:
        t = sdk.read_text(encoding="utf-8", errors="replace")
        if "CONFIG_BT_HFP_WBS_ENABLE=y" in t:
            backup(sdk)
            sdk.write_text(t.replace("CONFIG_BT_HFP_WBS_ENABLE=y",
                                     "CONFIG_BT_HFP_WBS_ENABLE=n"), encoding="utf-8")
    return "%s; %s" % (msg, build_and_flash(dry_run))


def k_hf_connect(dry_run: bool) -> str:
    """F4 — плата сама инициирует SLC. Логика патча вынесена в patch_f4.py."""
    import patch_f4
    msg = patch_f4.apply(MAIN_C, dry_run, backup)
    if msg.startswith(("уже применено", "ЯКОРЯ")):
        return msg
    return "%s; %s" % (msg, build_and_flash(dry_run))



def k_unpair(dry_run: bool) -> str:
    """M1 - snyat odnostoronnyuyu paru."""
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(WIN / "bt_unpair.ps1"), "-Match", "LyraT"]
    if not dry_run:
        args.append("-Confirm")
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return (r.stdout or r.stderr).strip()[-400:]


LADDER = [
    Knob("N1", "firewall: inbound UDP 5004/5005/5006", "auto", k_firewall, "G2"),
    Knob("W1", "BluetoothSetServiceState(Handsfree, ENABLE)", "auto", k_btconnect, "G1"),
    Knob("W3", "удалить протухшие endpoint'ы DeviceState=4", "gated", k_stale_endpoints, "G1"),
    Knob("N2", "dest IP прошивки -> фактический IP ПК", "gated_fw", k_destip, "G2"),
    Knob("F4", "esp_hf_client_connect() — плата инициирует SLC", "gated_fw", k_hf_connect, "G1"),
    Knob("F3", "CONFIG_BT_HFP_WBS_ENABLE=n", "gated_fw", k_nowbs, "G1"),
    Knob("F1", "coex при SCO: PREFER_WIFI -> PREFER_BT", "gated_fw", k_coex_bt, "G3"),
    Knob("F2", "WiFi TX power 52 -> 32", "gated_fw", k_txpower, "G3"),
    Knob("M1", "снять одностороннюю пару (дальше спарить руками)", "gated", k_unpair, "G1"),
]


def ladder(include_gated: bool = False):
    if include_gated:
        return LADDER
    return [k for k in LADDER if k.kind == "auto"]


if __name__ == "__main__":
    import sys
    gated = "--gated" in sys.argv
    for k in ladder(include_gated=gated):
        print("%-3s %-10s %-55s fixes=%s" % (k.id, k.kind, k.title, k.fixes))
        print("     %s" % k.apply(dry_run=True).replace("\n", "\n     "))


# ---------------------------------------------------------------------------
# Применение набора ручек одним заходом: сперва все правки исходников,
# потом ОДИН build+flash (иначе каждая ручка пересобирала бы проект).
# ---------------------------------------------------------------------------
SOURCE_ONLY = {
    "N2": lambda dry: _src_destip(dry),
    "F1": lambda dry: patch_file(MAIN_C, "esp_coex_preference_set(ESP_COEX_PREFER_WIFI);",
                                 "esp_coex_preference_set(ESP_COEX_PREFER_BT);", dry),
    "F2": lambda dry: patch_file(MAIN_C, "esp_wifi_set_max_tx_power(52);",
                                 "esp_wifi_set_max_tx_power(32);", dry),
    "F4": lambda dry: __import__("patch_f4").apply(MAIN_C, dry, backup),
}


def _src_destip(dry: bool) -> str:
    """Правит четыре октета dest-IP по отдельности — без regex, построчно."""
    ip = local_ipv4()
    if not ip:
        return "не удалось определить IP этого ПК"
    octets = ip.split(".")
    text = MAIN_C.read_text(encoding="utf-8", errors="replace")
    msgs, cur = [], []
    for letter, want in zip("ABCD", octets):
        found = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#define UDP_REMOTE_IP_%s " % letter):
                found = (line, s.split()[-1])
                break
        if not found:
            return "не нашёл UDP_REMOTE_IP_%s в main.c" % letter
        old_line, have = found
        cur.append(have)
        if have == want:
            continue
        new_line = "#define UDP_REMOTE_IP_%s  %s" % (letter, want)
        msgs.append(patch_file(MAIN_C, old_line, new_line, dry))
        if not dry:
            text = MAIN_C.read_text(encoding="utf-8", errors="replace")
    if not msgs:
        return "уже %s" % ip
    return "%s -> %s (%s)" % (".".join(cur), ip, "; ".join(msgs))


def apply_set(ids, dry_run=True, build=False):
    out = []
    for kid in ids:
        fn = SOURCE_ONLY.get(kid)
        out.append("%s: %s" % (kid, fn(dry_run) if fn else "нет такой ручки"))
    if build:
        out.append(build_and_flash(dry_run))
    return chr(10).join(out)
