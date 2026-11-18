#!/usr/bin/env python3
"""First flash of both bench boards over USB, once. After it the bench works over Wi-Fi through the S3. Goal 093.

  python lyrat_resend/tools/bench/first_flash.py [--variant NAME] [--dry-run] [--log-seconds 20] [--wifi-timeout 120]
                                                 [--label NAME]

With both boards on USB, in this order:
  1 secrets   s3_eth_bridge/main/ota_secrets.h: when missing it is made from lyrat_resend/main/wifi_secrets.h (network
              name and password) and a new random token; no value is ever printed or written elsewhere
  2 ports     the LyraT (CP210x) and the S3 (USB of the ESP32-S3) found by USB id, both free
  3 sdkconfig both generated sdkconfig files (an old one keeps the old partition table) move to a backup in the run
              folder, outside git; nothing is deleted
  4 build     both firmwares with a fresh sdkconfig and CCACHE_RECACHE=1 (the ccache trap of docs/OTA.md); the LyraT keeps
              the variant it was built with (bench_variant.h; mac_b1 is the address the phone is paired with), --variant
              changes it; the images are checked: OTA table, rollback, whole image, the S3 holds the Wi-Fi secrets
  5 flash     over USB, S3 first: bootloader, partition table, otadata, app; four hashes confirmed by esptool. The
              LyraT (goal 096): bootloader, table, otadata, the updater (lyrat_resend/updater) into factory and the
              headset firmware into ota_0, five hashes (fw.lyrat_usb_command). NVS is never written, so the LyraT keeps
              its pairing with the phone (and the S3 its own NVS)
  6 network   the S3 answers over Wi-Fi, the LyraT is alive behind it (a fresh heartbeat on the UART)
  7 query     ota.py query: token present, both images valid, and the running images are the ones just built (ELF SHA);
              the LyraT first starts its updater for 10 s, the query waits for its headset firmware (mode=app)
  8 log       udplog.py for --log-seconds: lines of both boards over the network
and prints a summary in Russian that ends with "USB можно отключать" only when every step passed.

--dry-run touches no port, file or build: it checks the secrets read-only, prints what steps 3-5 would do, and runs
steps 6-8 for real (against the bench, or the simulator tests/s3_sim.py with BENCH_S3_ADDR=127.0.0.1).
Output: goals_harness/bench_runs/first_flash/<stamp>[-label]/first_flash.json, the build and flash logs, the backup of the
sdkconfig files, udplog/. The S3 address is not written. Exit code: 0 all passed, 1 a step failed, 2 a precondition is
missing (secrets, ports): nothing was flashed. Docs for the owner: lyrat_resend/docs/FIRST_FLASH.md.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from benchlib import FW_DIR, REPO_DIR, run_dir, write_json  # noqa: E402

S3_DIR = REPO_DIR / "s3_eth_bridge"
OTA_SECRETS = S3_DIR / "main" / "ota_secrets.h"
WIFI_SECRETS = FW_DIR / "main" / "wifi_secrets.h"
PLACEHOLDERS = {"YOUR_SSID", "YOUR_PASSWORD", "your-2.4-GHz-network", "your-password", "CHANGE-ME-16-or-more-random-chars"}
TOKEN_MIN = 16
FLASH_IMAGES = 4            # bootloader, partition table, otadata, app
LYRAT_FRESH_MS = 3000       # a heartbeat younger than this: the LyraT is alive behind the S3
QUERY_POLL_S = 2.0
PROJECTS = {"s3": (S3_DIR, "s3_eth_bridge"), "lyrat": (FW_DIR, "lyrat_resend"),
            "lyrat_updater": (FW_DIR / "updater", "lyrat_updater")}
QUERY_WAIT_S = 45.0         # the S3 asks the LyraT for its status every 10 s (s3_ota.c STATUS_EVERY_MS), and since goal
                            # 096 the LyraT's updater waits 10 s before its headset firmware starts


# --- secrets ---------------------------------------------------------------------------------------------------------

def defines(path: Path):
    """String #defines of a header; values stay in memory only."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    return {m.group(1): m.group(2) for m in re.finditer(r'^\s*#define\s+(\w+)\s+"((?:[^"\\]|\\.)*)"', text, re.M)}


def secrets_problems(d, keys):
    """Names of the keys that are absent, empty or still the template's value."""
    if d is None:
        return list(keys)
    return [k for k in keys if not d.get(k) or d[k] in PLACEHOLDERS or (k == "OTA_TOKEN" and len(d[k]) < TOKEN_MIN)]


def ensure_secrets(ota_path=OTA_SECRETS, wifi_path=WIFI_SECRETS, create=True):
    """Checks the S3's secrets and makes them from the LyraT's Wi-Fi file when they are missing. Returns a dict without
    any value: ok, state (present, created, would_create, missing), problems (names of keys), source."""
    ota = defines(ota_path)
    if ota is not None:
        bad = secrets_problems(ota, ("OTA_TOKEN", "WIFI_SSID", "WIFI_PASS"))
        if bad == ["WIFI_PASS"] and ota.get("WIFI_PASS") == "":
            bad = []            # an open network: an empty password is allowed by the firmware
        return {"ok": not bad, "state": "present", "problems": bad, "file": ota_path.name}
    wifi = defines(wifi_path)
    bad = secrets_problems(wifi, ("WIFI_SSID", "WIFI_PASS"))
    if wifi is not None and bad == ["WIFI_PASS"] and wifi.get("WIFI_PASS") == "":
        bad = []
    if bad:
        return {"ok": False, "state": "missing", "problems": bad, "file": ota_path.name,
                "source": wifi_path.name if wifi is not None else None}
    if not create:
        return {"ok": True, "state": "would_create", "problems": [], "file": ota_path.name, "source": wifi_path.name}
    token = secrets.token_urlsafe(24)      # 32 characters of [A-Za-z0-9_-]
    body = ("// Made by tools/bench/first_flash.py from %s and a new random token. git ignores this file.\n"
            "#pragma once\n\n#define OTA_TOKEN \"%s\"\n\n#define WIFI_SSID \"%s\"\n#define WIFI_PASS \"%s\"\n"
            % (wifi_path.name, token, wifi["WIFI_SSID"], wifi["WIFI_PASS"]))    # already C literals: copied as written
    ota_path.write_text(body, encoding="utf-8")
    return {"ok": True, "state": "created", "problems": [], "file": ota_path.name, "source": wifi_path.name}


# --- sdkconfig and builds ----------------------------------------------------------------------------------------------

def backup_sdkconfig(project: Path, dest: Path, dry):
    """Moves sdkconfig and sdkconfig.old of a project into dest. Returns the names moved (or that would be)."""
    moved = []
    for name in ("sdkconfig", "sdkconfig.old"):
        src = project / name
        if src.exists():
            moved.append(name)
            if not dry:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest / name))
    return moved


def image_checks(board):
    """What the built image of a board must be: the OTA table, rollback on, a whole image; the S3 and the LyraT's
    updater: the secrets inside; the LyraT's headset firmware: no Wi-Fi linked (goal 096). The rules of
    tests/fw_build_check.py BOARDS."""
    sys.path.insert(0, str(HERE.parent / "tests"))
    import fw_build_check as fbc
    project, name = PROJECTS[board]
    b = fbc.BOARDS[board]
    build = project / "build"
    out = {}
    built = fbc.table_from_bin((build / "partition_table" / "partition-table.bin").read_bytes())
    out["table"] = built == fbc.table_from_csv(b["csv"])
    sdk = (project / "sdkconfig").read_text(encoding="utf-8")
    out["rollback"] = ("CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in sdk
                       and 'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="%s"' % b["csv_conf"] in sdk)
    image = (build / (name + ".bin")).read_bytes()
    out["image"] = fbc.images.check(image) is None
    for path, keys in b["secrets"] or ():
        values = fbc.secret_values(path, keys)
        ok = bool(values) and all(fbc.secrets_check(image, {k: v for k, v in values.items() if v}).values())
        out["secrets"] = out.get("secrets", True) and ok
    if b["wifi"] is not None:
        out["wifi"] = fbc.wifi_linked(build / (name + ".map")) == b["wifi"]
    return out


def build_elf(board):
    import ota
    project, name = PROJECTS[board]
    path = project / "build" / (name + ".bin")
    return ota.Image.load(path).elf[:16] if path.exists() else None


# --- network -----------------------------------------------------------------------------------------------------------

def wait_network(timeout):
    """(seconds until the S3 answered over the network or None, the UQ reply of the moment the LyraT was seen alive or
    the last one, seconds until then or None)."""
    import s3_uart
    t0 = time.monotonic()
    s3_at = lyrat_at = None
    last = None
    ch = None
    try:
        while time.monotonic() - t0 < timeout:
            if ch is None:
                try:
                    ch = s3_uart.Channel()
                except OSError:
                    time.sleep(1.0)
                    continue
            if s3_at is None:
                if not ch.find(timeout=3.0):
                    continue
                s3_at = round(time.monotonic() - t0, 1)
            last = ch.query()
            if last and 0 <= last.get("last_frame_ms", -1) < LYRAT_FRESH_MS and last.get("lyrat_up_s", -1) >= 0:
                lyrat_at = round(time.monotonic() - t0, 1)
                break
            time.sleep(1.0)
    finally:
        if ch is not None:
            ch.close()
    return s3_at, last, lyrat_at


def tool(args, timeout):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return r.returncode, r.stdout, r.stderr


def lyrat_fields(status_text):
    return dict(kv.split("=", 1) for kv in (status_text or "").split() if "=" in kv)


def query_with_lyrat_status(wait_s=QUERY_WAIT_S, run=None, sleep=time.sleep, clock=time.monotonic):
    """ota.py query, repeated until the S3 holds a status text of the LyraT. The S3 asks for it every 10 s and only
    once its TX is on, so right after the LyraT showed up the text is still empty (goal 095, incident 004).
    Returns (rc, the parsed reply or None) of the last try."""
    run = run or (lambda: tool([HERE / "ota.py", "query"], 60))
    end = clock() + wait_s
    while True:
        rc, text, _ = run()
        q = None
        try:
            q = json.loads(text) if rc == 0 else None
        except ValueError:
            pass
        ly = (q or {}).get("lyrat") or {}
        status = ly.get("status") or ""
        # Goal 096: the status of the updater is not the end; its headset firmware follows within its window.
        ready = ly.get("status_age_ms", -1) >= 0 and ("mode=" not in status or "mode=app" in status)
        if (q and ready) or clock() >= end:
            return rc, q
        sleep(QUERY_POLL_S)


# --- the run -----------------------------------------------------------------------------------------------------------

class Run:
    def __init__(self, out, dry):
        self.out, self.dry = out, dry
        self.steps = []

    def step(self, name, ok, text, **detail):
        """ok: True, False, or None for a step that was only planned (dry run)."""
        self.steps.append({"name": name, "ok": ok, "text": text, **detail})
        mark = {True: "OK  ", False: "СБОЙ", None: "план"}[ok]
        print("  [%s] %s: %s" % (mark, name, text), flush=True)
        return ok


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", help="LyraT bench variant; default: the one it is built with now (bench_variant.h)")
    ap.add_argument("--dry-run", action="store_true", help="no port, file or build is touched; the network part runs")
    ap.add_argument("--log-seconds", type=float, default=20.0)
    ap.add_argument("--wifi-timeout", type=float, default=120.0, help="seconds for the S3 to join Wi-Fi and the LyraT to show")
    ap.add_argument("--retries", type=int, default=12, help="flash attempts per board when the port drops")
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)
    import fw
    out = run_dir("first_flash", "-".join(x for x in ("dry" if args.dry_run else "", args.label) if x))
    run = Run(out, args.dry_run)
    print("Первая прошивка стенда%s -> %s" % (" (сухой прогон)" if args.dry_run else "", out), flush=True)
    record = {"dry_run": args.dry_run, "steps": run.steps, "out": str(out)}

    def finish(code):
        record["exit"] = code
        record["usb_can_go"] = code == 0
        write_json(out / "first_flash.json", record)
        print()
        if code == 0:
            if args.dry_run:
                print("Итог сухого прогона: прошивка не менялась; S3 в сети Wi-Fi, LyraT отвечает через неё, журнал идёт по сети.")
            else:
                print("Итог: обе платы прошиты, S3 в сети Wi-Fi, LyraT отвечает через неё, журнал идёт по сети.")
            print("USB можно отключать." if not args.dry_run else "Сетевая часть в порядке; USB-шаги только показаны.")
        else:
            failed = [s for s in run.steps if s["ok"] is False]
            print("Итог: НЕ готово (%s). USB пока не отключать." % "; ".join("%s: %s" % (s["name"], s["text"]) for s in failed))
        print("Запись прогона: %s" % out)
        return code

    # 1 secrets
    sec = ensure_secrets(create=not args.dry_run)
    record["secrets"] = sec
    if not sec["ok"]:
        hint = ("заполните lyrat_resend/main/wifi_secrets.h по шаблону .example (сеть 2,4 ГГц, та же, что у ПК)"
                if sec["state"] == "missing" else "исправьте %s по шаблону .example" % sec["file"])
        run.step("секреты", False, "нет или шаблонные значения: %s; %s" % (", ".join(sec["problems"]), hint))
        return finish(2)
    run.step("секреты", True, {"present": "ota_secrets.h на месте", "created": "ota_secrets.h создан из wifi_secrets.h "
                               "и нового токена", "would_create": "ota_secrets.h будет создан из wifi_secrets.h"}[sec["state"]])

    # 2 ports
    if args.dry_run:
        run.step("порты", None, "не трогаю (сухой прогон)")
        ports = {"lyrat": None, "s3": None}
    else:
        import owner_common as oc
        ports = {"lyrat": oc.lyrat_port(), "s3": oc.s3_port()}
        states = {b: oc.port_state(p) for b, p in ports.items()}
        record["ports"] = states
        bad = {b: s for b, s in states.items() if s != "free"}
        if bad:
            names = {"lyrat": "LyraT (CP210x)", "s3": "S3 (USB ESP32-S3)"}
            why = {"absent": "не найден, подключите кабель", "busy": "занят другой программой (монитор, терминал), закройте её"}
            run.step("порты", False, "; ".join("%s: %s" % (names[b], why.get(s, s)) for b, s in bad.items()))
            return finish(2)
        run.step("порты", True, "LyraT и S3 на USB, порты свободны")

    # 3 sdkconfig, 4 build
    variant = args.variant or fw.current_variant()
    record["variant"] = variant
    backup = out / "sdkconfig_backup"
    moved = {b: backup_sdkconfig(p, backup / b, args.dry_run) for b, (p, _) in PROJECTS.items()}
    record["sdkconfig_moved"] = moved
    run.step("sdkconfig", None if args.dry_run else True, "%s %s" % (
        "перенесу в резервную копию:" if args.dry_run else "перенесены в %s:" % backup.relative_to(out.parent.parent),
        ", ".join("%s %s" % (b, "+".join(m) or "нет") for b, m in moved.items())))
    if args.dry_run:
        run.step("сборка", None, "S3 и LyraT (вариант %s) со свежим sdkconfig и CCACHE_RECACHE=1" % variant)
        run.step("прошивка USB", None, "S3, затем LyraT: загрузчик, таблица, otadata, обновлятор в factory, прошивка в ota_0; NVS не пишется")
        expect = {"s3": None, "lyrat": None}
    else:
        os.environ["CCACHE_RECACHE"] = "1"
        rc_s3 = fw.idf("idf.py -C '%s' build" % S3_DIR, "first-s3-build").returncode
        rc_ly = fw.build(variant) if rc_s3 == 0 else None
        rc_up = fw.build_updater() if rc_ly == 0 else None
        checks = {b: image_checks(b) for b, rc in (("s3", rc_s3), ("lyrat", rc_ly), ("lyrat_updater", rc_up)) if rc == 0}
        record["build"] = {"s3_rc": rc_s3, "lyrat_rc": rc_ly, "updater_rc": rc_up, "checks": checks}
        bad = ["%s.%s" % (b, k) for b, c in checks.items() for k, v in c.items() if not v]
        if rc_s3 != 0 or rc_ly != 0 or rc_up != 0 or bad:
            run.step("сборка", False, "код сборки S3 %s, LyraT %s, обновлятор %s; не прошли: %s (логи: bench_runs/fw/)" % (
                rc_s3, rc_ly, rc_up, ", ".join(bad) or "-"))
            return finish(1)
        run.step("сборка", True, "S3, LyraT (вариант %s) и её обновлятор: таблица, откат, образ целый, секреты в S3 и "
                 "обновляторе, в прошивке LyraT нет Wi-Fi" % variant)
        expect = {"s3": build_elf("s3"), "lyrat": build_elf("lyrat")}
        # 5 flash: the S3 first, so it joins Wi-Fi while the LyraT is written
        for board, command, images in (
                ("s3", "idf.py -C '%s' -p %s flash" % (S3_DIR, ports["s3"]), FLASH_IMAGES),
                ("lyrat", fw.lyrat_usb_command(ports["lyrat"]), fw.LYRAT_USB_IMAGES)):
            rc = fw.with_retries("first-flash-" + board, command, args.retries, verify_images=images)
            record.setdefault("flash", {})[board] = rc
            if rc != 0:
                run.step("прошивка USB", False, "%s: код %d (3: не все %d хэша подтверждены; логи bench_runs/fw/)" % (
                    board, rc, images))
                return finish(1)
        run.step("прошивка USB", True, "S3: %d образа, LyraT: %d (обновлятор в factory, прошивка в ota_0) подтверждены, "
                 "NVS не тронут" % (FLASH_IMAGES, fw.LYRAT_USB_IMAGES))
    record["expect_elf"] = expect

    # 6 network
    s3_at, uq, lyrat_at = wait_network(args.wifi_timeout)
    record["network"] = {"s3_s": s3_at, "lyrat_s": lyrat_at,
                         "uq": {k: (uq or {}).get(k) for k in ("pins_ok", "tx_on", "hb", "last_frame_ms", "lyrat_up_s")}}
    if s3_at is None:
        run.step("сеть", False, "S3 не ответила по Wi-Fi за %d с: имя сети и пароль в ota_secrets.h, сеть 2,4 ГГц, ПК в той "
                 "же сети?" % args.wifi_timeout)
        return finish(1)
    if lyrat_at is None:
        run.step("сеть", False, "S3 в сети за %.0f с, но LyraT за ней не отвечает (провода UART S3-LyraT, питание LyraT)" % s3_at)
        return finish(1)
    run.step("сеть", True, "S3 ответила по Wi-Fi через %.0f с, LyraT жива за ней через %.0f с" % (s3_at, lyrat_at))

    # 7 ota.py query
    rc, q = query_with_lyrat_status()
    ly = lyrat_fields(((q or {}).get("lyrat") or {}).get("status"))
    got = {"s3": (q or {}).get("elf"), "lyrat": ly.get("elf")}
    record["query"] = {"rc": rc, "token": (q or {}).get("token"), "s3_img": (q or {}).get("img"), "lyrat_img": ly.get("img"),
                       "s3_run": (q or {}).get("run"), "lyrat_run": ly.get("run"), "elf": got}
    problems = []
    if q is None:
        problems.append("ota.py query без ответа (код %s)" % rc)
    else:
        if q.get("token") != 1:
            problems.append("в S3 нет токена OTA: обновления по сети будут отклонены")
        if q.get("img") != "valid":
            problems.append("образ S3 %s, не valid" % q.get("img"))
        if ly.get("img") != "valid":
            problems.append("образ LyraT %s, не valid" % (ly.get("img") or "не прочитан"))
        for board in ("s3", "lyrat"):
            if expect[board] and got[board] != expect[board]:
                problems.append("на %s работает не только что собранный образ" % board)
    if problems:
        run.step("ota.py query", False, "; ".join(problems))
        return finish(1)
    run.step("ota.py query", True, "токен есть, оба образа valid%s" % (
        ", на платах только что собранные образы" if expect["s3"] else " (сверка ELF только при настоящей прошивке)"))

    # 8 udplog
    rc, text, err = tool([HERE / "udplog.py", "--seconds", args.log_seconds, "--out", out / "udplog"], args.log_seconds + 60)
    boards = {}
    try:
        boards = json.loads(text.strip().splitlines()[-1]).get("boards", {})
    except (ValueError, IndexError):
        pass
    lines = {b: (boards.get(b) or {}).get("lines", 0) for b in ("s3", "lyrat")}
    record["udplog"] = {"rc": rc, "lines": lines, "boards": boards}
    if not all(lines.values()):
        run.step("журнал по сети", False, "за %.0f с строк: S3 %d, LyraT %d" % (args.log_seconds, lines["s3"], lines["lyrat"]))
        return finish(1)
    run.step("журнал по сети", True, "за %.0f с строк: S3 %d, LyraT %d" % (args.log_seconds, lines["s3"], lines["lyrat"]))
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
