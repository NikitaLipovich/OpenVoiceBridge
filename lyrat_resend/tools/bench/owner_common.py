"""What the owner's commands share: the form of a result, and the checks that the bench is free before a step touches it.

A step ends in exactly one of three results, printed in Russian with its numbers and what to do:
  PASS            measured and within the limits; exit code 0
  FAIL            measured and wrong, or a recording that lost something; exit code 1
  НЕ ПРОВЕРЕНО    not measured: port busy, nothing to measure with, a check that only a person can do; exit code 2
A step never turns into PASS from an exit code alone. Everything printed also goes to output.txt in the step's folder under
bench_runs/owner/, outside git, with addresses and any caller number reduced by benchlib.redact.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import REPO_DIR, RUNS_ROOT, redact, stamp, write_json  # noqa: E402

PASS, FAIL, NOT_CHECKED = "PASS", "FAIL", "НЕ ПРОВЕРЕНО"
EXIT = {PASS: 0, FAIL: 1, NOT_CHECKED: 2}
LYRAT_VID = 0x10C4
S3_VID = 0x303A
WANT_CODEC = "mSBC"
WANT_NS = "web2"
TOOLS = Path(__file__).resolve().parent
TOOLS_CMD = "python lyrat_resend/tools/bench/"
FLASH_BACK = TOOLS_CMD + "fw.py flash --variant mac_b1"
SDK_TOOLS = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools"
# The tools that hold the bench while they run. A line of a process matches on its script name.
BENCH_TOOLS = re.compile(r"(?:^|[\\/\s\"])((?:round|voice_check|udp_record|call_record|call_check|call_stream|owner_check|fw|s3|s3_uart|serial_watch|btlog"
                         r"|phone|idf|esptool|idf_monitor)\.py)\b|\besptool\b")
OPEN_ISSUES = [
    ("#004 гоала 057", "обрывы в сторону телефона по 14,6 мс (одна потеря кадра mSBC и её маскировка), около 5-7 в минуту против 0,37 в гоале 045.",
                       "Гоал 062: это нехватка запаса приёма на телефоне: на -12 дБм 150 в минуту, на обычной мощности 5-7, на +9 дБм 1,5. Почему запас упал, не установлено, просьба к владельцу: NEEDS-OWNER пункт 51. В записи через S3 их не видно: mic.wav снят до Bluetooth."),
    ("#005 гоала 057", "периоды шума на линии микрофона по минутам и часам, причина не найдена. Виден шагом mic."),
    ("#003 гоала 057", "цикл сбросов LyraT на старте радио, после перестановки в другой порт USB-блока не возвращался. "
                       "Причина не разделена. Если плата перезапускается по кругу (шаг stable): проверить питание и порт блока."),
    ("#001 гоала 057", "зависшая запись bt_sco на телефоне: при ней канал не откроется и настоящий звонок может не перейти в "
                       "гарнитуру. Виден шагом bluetooth. Перезагрузка телефона не решение (R17), решение ищет гоал 058."),
]


def console_utf8():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def adb_on_path():
    """A fresh terminal may not have the SDK on its PATH; the tools below call plain adb."""
    if not any((Path(d) / "adb.exe").exists() or (Path(d) / "adb").exists() for d in os.environ.get("PATH", "").split(os.pathsep)):
        if (SDK_TOOLS / "adb.exe").exists():
            os.environ["PATH"] = str(SDK_TOOLS) + os.pathsep + os.environ.get("PATH", "")
    # The Git Bash path conversion spoils /sdcard/... when a tool is started from there.
    os.environ["MSYS_NO_PATHCONV"] = "1"


class Step:
    """One step's folder, its output and its result."""

    def __init__(self, name, title, out=None):
        self.name, self.title = name, title
        self.out = Path(out) if out else RUNS_ROOT / "owner" / ("%s-%s" % (stamp(), name))
        self.out.mkdir(parents=True, exist_ok=True)
        self.numbers = []
        self.t0 = time.monotonic()
        self.log = (self.out / "output.txt").open("a", encoding="utf-8", errors="replace")
        self.say("=== %s: %s ===" % (name, title))

    def say(self, text=""):
        text = redact(str(text))
        print(text, flush=True)
        self.log.write(text + "\n")
        self.log.flush()

    def num(self, label, value):
        self.numbers.append((label, value))

    def finish(self, result, why, todo=None, extra=None):
        """Prints the result and writes result.json. todo is mandatory unless the step passed."""
        assert result in EXIT
        self.say("")
        self.say("ИТОГ %s: %s" % (self.name, result))
        self.say("  Почему: %s" % why)
        for label, value in self.numbers:
            self.say("  %s: %s" % (label, value))
        if todo:
            self.say("  Что делать: %s" % todo)
        elif result != PASS:
            self.say("  Что делать: см. lyrat_resend/docs/OWNER_CHECK.md")
        self.say("  Папка: %s" % self.out)
        data = {"step": self.name, "title": self.title, "result": result, "why": why, "todo": todo,
                "numbers": [[k, v] for k, v in self.numbers], "seconds": round(time.monotonic() - self.t0, 1),
                "dir": str(self.out), "at": stamp(), "extra": extra}
        write_json(self.out / "result.json", json.loads(redact(json.dumps(data, ensure_ascii=False))))
        self.log.close()
        return data


# --- whether the bench is free ---------------------------------------------------------------------------------------

def process_table():
    """pid -> (parent, command line) of the python and tool processes. Read only."""
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|esptool|idf' } | "
              "ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.ParentProcessId, $_.CommandLine }")
    try:
        res = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                              "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    table = {}
    for line in res.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            table[int(parts[0])] = (int(parts[1]) if parts[1].isdigit() else 0, parts[2])
    return table


def other_bench_processes():
    """Bench tools running now, apart from this process and those that started it. None if that cannot be read."""
    table = process_table()
    if table is None:
        return None
    mine = set()
    pid = os.getpid()
    while pid and pid not in mine:
        mine.add(pid)
        pid = table.get(pid, (0, ""))[0]
    found = []
    for pid, (_, line) in table.items():
        hit = BENCH_TOOLS.search(line)
        if pid not in mine and hit:
            found.append((pid, hit.group(1) or "esptool", tool_args(line, hit)))
    return found


def tool_args(line, hit):
    """The words after the script's name, enough to tell which series it is. No paths."""
    words = line[hit.end():].replace('"', " ").split()
    return " ".join(w for w in words[:3] if "\\" not in w and "/" not in w)


def goals_running():
    """Goals of the chain marked running; only for the message, a goal between two series holds no port."""
    found = []
    root = REPO_DIR / "goals_harness" / "phone-then-pc"
    try:
        for path in sorted(root.glob("*/STATE.md")):
            with path.open(encoding="utf-8", errors="replace") as fh:
                if fh.readline().strip() == "state: running":
                    found.append(path.parent.name)
    except OSError:
        pass
    return found


def lyrat_port():
    """BENCH_PORT wins; otherwise the CP210x bridge, because the number may change between USB ports."""
    env = os.environ.get("BENCH_PORT")
    if env:
        return env
    found = [p.device for p in list_ports.comports() if p.vid == LYRAT_VID]
    return found[0] if found else None


def s3_port():
    env = os.environ.get("BENCH_S3_PORT")
    if env:
        return env
    found = [p.device for p in list_ports.comports() if p.vid == S3_VID]
    return found[0] if found else None


def port_state(port):
    """free, busy or absent. Lines stay released: DTR and RTS of this bench are wired to reset."""
    if port is None or not any(p.device.upper() == port.upper() for p in list_ports.comports()):
        return "absent"
    sp = serial.Serial()
    sp.port, sp.baudrate, sp.timeout, sp.dtr, sp.rts = port, 115200, 0.1, False, False
    try:
        sp.open()
    except serial.SerialException as e:
        return "busy" if "PermissionError" in str(e) or "Access is denied" in str(e) or "Отказано" in str(e) else "absent"
    sp.close()
    return "free"


def udp_state(ports=(5004, 5005)):
    """The stream ports this PC receives on; a second recorder cannot share them."""
    busy = []
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            busy.append(port)
        finally:
            s.close()
    return busy


def usb_mode():
    """USB consoles only when asked (owner_check.py --usb, BENCH_USB=1); the network through the S3 otherwise. Goal 093."""
    return os.environ.get("BENCH_USB") == "1"


def lyrat_over_network(timeout=5.0):
    """None when the S3 answers on the network and the LyraT is alive behind it, else what is missing, in Russian."""
    import s3_uart
    try:
        ch = s3_uart.Channel()
    except OSError as e:
        return "сеть ПК недоступна: %s" % e
    try:
        end = time.monotonic() + timeout
        if not ch.find(timeout=timeout):
            return "S3 не отвечает по сети (Wi-Fi). Она включена, ПК в той же сети?"
        while True:
            # A heartbeat comes once a second: right after a start of either board there may be none yet.
            q = ch.query() or {}
            last = q.get("last_frame_ms")
            if last is not None and 0 <= last <= 3000:
                return None
            if time.monotonic() >= end:
                return "LyraT не отвечает через S3 (нет кадров на UART %s мс). Плата включена, провода UART на месте?" % last
            time.sleep(0.5)
    finally:
        ch.close()


def bench_busy(step, need_lyrat=True, need_udp=True):
    """A message if something else holds the bench, otherwise None. Nothing here stops anything."""
    procs = other_bench_processes()
    if procs is None:
        return "список процессов не читается, занятость стенда неизвестна"
    if procs:
        names = "; ".join("%s %s (pid %d)" % (n, a, p) for p, n, a in procs[:5])
        running = goals_running()
        note = " Цепочка гоалов: %s." % ", ".join(running) if running else ""
        return ("стенд занят другой программой: %s.%s Я её не трогаю. Дождитесь конца или поставьте цепочку на паузу "
                "(строка в NEEDS-OWNER, пункт 45), потом запустите шаг снова." % (names, note))
    if need_lyrat and not usb_mode():
        missing = lyrat_over_network()
        if missing:
            return missing
    elif need_lyrat:
        port = lyrat_port()
        state = port_state(port)
        if state == "busy":
            return ("порт LyraT %s занят другой программой (монитор ESP-IDF, терминал). Закройте её, я её не трогаю." % port)
        if state == "absent":
            return "порта LyraT нет (%s). Плата включена, USB-кабель на месте?" % (port or "CP210x не найден")
    if need_udp:
        busy = udp_state()
        if busy:
            return "порты приёма потока заняты: %s. Запущена другая запись? Я её не трогаю." % busy
    return None


def refuse_if_busy(step, **kw):
    """Finishes the step with НЕ ПРОВЕРЕНО when the bench is taken; returns the result or None."""
    msg = bench_busy(step, **kw)
    if msg:
        return step.finish(NOT_CHECKED, msg, "Запустить шаг снова, когда стенд свободен.")
    return None


# --- the round.py stages ----------------------------------------------------------------------------------------------

def run_tool(step, args, tag, heartbeat_s=30.0, env=None):
    """A bench tool as a child, its whole output in a file of the step's folder; a line of life for the person.
    A killed or crashed child is an exit code and nothing more: the caller reads the measured file, not this."""
    path = step.out / (tag + ".txt")
    t0 = time.monotonic()
    with path.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen([sys.executable, *map(str, args)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
        next_beat = t0 + heartbeat_s
        for line in proc.stdout:
            fh.write(redact(line))
            fh.flush()
            if time.monotonic() >= next_beat:
                next_beat = time.monotonic() + heartbeat_s
                step.say("  ...идёт %d с" % (time.monotonic() - t0))
        rc = proc.wait()
    return rc, path


def run_round(step, stage, *extra, power="adapter", port=None, tag=None, env=None):
    """One stage of round.py. Returns (result dict or None, exit code). The result is the file this very run wrote,
    found by a label nobody else has, so an old round.json can never pass as this one."""
    label = "owner-%s-%s" % (stamp(), step.name)
    console = ["--port", port or lyrat_port()] if usb_mode() else []      # the network otherwise (goal 093)
    args = [TOOLS / "round.py", stage, "--power", power, "--label", label, *console, *extra]
    rc, _ = run_tool(step, args, tag or ("round_" + stage), env=env)
    found = sorted((RUNS_ROOT / stage).glob("*-%s/round.json" % label))
    if not found:
        return None, rc
    try:
        return json.loads(found[-1].read_text(encoding="utf-8")), rc
    except ValueError:
        return None, rc


def round_outcome(result, rc):
    """A stage's verdict, cross checked with its exit code: (PASS, FAIL or НЕ ПРОВЕРЕНО, reason)."""
    if result is None:
        return NOT_CHECKED, "инструмент не записал результат (код выхода %s)" % rc
    verdict = result.get("verdict")
    if verdict in ("NO_DATA", "INVALID_WINDOW"):
        return NOT_CHECKED, "%s: %s" % (verdict, result.get("error") or "окно замера недействительно")
    if verdict == "FAIL":
        failed = [k for k, v in (result.get("checks") or {}).items() if not v]
        return FAIL, "не прошли проверки: %s" % ", ".join(failed)
    if verdict == "PASS" and rc == 0 and result.get("checks") and all(result["checks"].values()):
        return PASS, "все проверки стадии прошли"
    return NOT_CHECKED, "вердикт %s и код выхода %s не сходятся" % (verdict, rc)


def worst_of(results):
    """Most severe of several results."""
    for wanted in (FAIL, NOT_CHECKED):
        if wanted in results:
            return wanted
    return PASS


# --- a real call during a run without a call (goal 097) ----------------------------------------------------------------

class RealCallGuard:
    """Watches the phone for a real call while a run without a call records (rehearsal, call_check --probe). R3/R6: such
    a run must not keep anything of a real conversation. Trips when a call is ringing or active (telecom calls, or a
    telephony state other than idle), or when the phone's call history grew since the start: a call that came and went
    between two looks. Reads counts only (round.telecom_state), never a number. Not for the owner's own call check, where
    the call is the point. state: a function returning telecom_state()'s dict or None, for tests."""

    def __init__(self, state=None, every=2.0):
        import threading
        if state is None:
            import round as rnd
            state = rnd.telecom_state
        self.state, self.every = state, every
        self.tripped = None             # the reason, in Russian
        self.unknown = 0
        self.looks = 0
        self.first = None
        self.halt = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def look(self):
        st = self.state()
        self.looks += 1
        if st is None:
            self.unknown += 1
            return None
        if self.first is None:
            self.first = st
        if st.get("calls_now") or st.get("telephony_busy"):
            self.tripped = self.tripped or "на телефоне настоящий звонок (звонит или идёт)"
        elif st.get("historical_lines", 0) > self.first.get("historical_lines", 0):
            self.tripped = self.tripped or "на телефоне был звонок во время прогона (история звонков выросла)"
        return st

    def _run(self):
        while not self.halt.is_set():
            try:
                self.look()
            except Exception:  # noqa: BLE001  adb gone mid-run: counted as unknown
                self.unknown += 1
            self.halt.wait(self.every)

    def start(self):
        self.look()
        self.thread.start()
        return self

    def stop(self):
        """A last look, so a call in the last seconds is seen too. Returns the reason or None."""
        self.halt.set()
        self.thread.join(timeout=self.every + 30)
        try:
            self.look()
        except Exception:  # noqa: BLE001
            self.unknown += 1
        return self.tripped


def discard_audio(directory):
    """Every sound file and packet journal of a run folder, gone: what is left are numbers. Returns the names removed."""
    gone = []
    for path in Path(directory).rglob("*"):
        if path.is_file() and path.suffix.lower() in (".wav", ".journal"):
            path.unlink(missing_ok=True)
            gone.append(path.name)
    return gone
