#!/usr/bin/env python3
"""The recording of the owner's own call: both sides through the S3-ETH board, separate files, a summary. Dials nothing.

  python lyrat_resend/tools/bench/call_record.py                 start, then make the call yourself, stops by itself
  python lyrat_resend/tools/bench/call_record.py --recover DIR   make the files from the journal of a recording that died

It stops on Enter, on Ctrl+C, or GRACE seconds after the channel closed. --only-enter turns the last one off. It never
touches the phone: no adb here, so nothing of the call can reach a file from the phone's side, and it plays nothing on the
PC (R15). The only place a caller number exists on this bench is the CLIP line of the LyraT console, which serial_watch
reduces before it is written or shown.

Files in bench_runs/call/<stamp>/ or --out, outside git:
  call.wav             DSDIN, what the phone sends to the headphones: the other party. 16 kHz, mono
  mic.wav              ASDOUT, the codec's line, before any processing of the board: the owner. 16 kHz, two channels
  call_summary.json    the numbers below; output.txt what was printed
  serial.log           the LyraT console, reduced; tap.json the numbers of the streams
  packets.journal      every packet as it arrived, flushed each second; removed once the files are written

What survives what. The packets go to the journal as they arrive, so a dead process loses at most the last second, and
--recover makes the files. A LyraT reset stops the I2S bus: that time is written as silence by the arrival clock, so the time
axis stays real, and it is counted. An S3 restart the same, and the recorder asks for the stream again. A lost console port
reopens by itself and the channel is then followed through the S3's heartbeat of the LyraT. With no source of the channel's
state left the recording goes on until Enter: it never stops on not knowing.

Result: PASS only if a channel was seen, the codec is mSBC, both files hold sound, durations agree within 1 s, losses stay
within the limit of round.py record, and nothing was reset or dropped. A recording with a fault is kept and says so.
As a module: CallRecording(out, ...).start(), .tick(), .stop(), judge(summary). Exit code: 0 PASS, 1 FAIL, 2 НЕ ПРОВЕРЕНО.
"""
import argparse
import json
import re
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import RUNS_ROOT, keep_awake, redact, stamp, write_json  # noqa: E402
import owner_common as oc  # noqa: E402
import s3_uart  # noqa: E402
from serial_watch import SerialWatch  # noqa: E402
from udp_record import JOURNAL_NAME, MIC_FLOOR_WARN, RATE, TapRecorder, floor_too_high  # noqa: E402

SLOTS = ("L", "LR")
GRACE_S = 10.0              # a short re-route of the audio during a call must not end the recording
GRACE_AFTER_FAULT_S = 40.0  # a LyraT back after a reset gets the channel of a running call back by itself, boot and SLC take about 15 s
BUS_GAP_S = 1.0
FIRST_PACKET_S = 8.0
MAX_LOSS_PCT = 2.0          # what round.py record allows
MAX_DURATION_ERROR_S = 1.0
PROGRESS_S = 30.0
STREAM_SILENT_S = 3.0
QUERY_PERIOD_S = 1.0
FRESH_HB_MS = 3000
SCO_OPEN = "*** SCO OPEN"
SCO_CLOSED = "*** SCO CLOSED"
SCO_CODEC = re.compile(r"SCO audio state=[23] codec=([A-Za-z]+)")
SCO_PKT = re.compile(r"SCO pkt: rx_total=(\d+) rx_correct=(\d+) rx_err=(\d+) rx_none=(\d+) rx_lost=(\d+) tx_total=(\d+) tx_discarded=(\d+)")
SCO_PKT_KEYS = ("rx_total", "rx_correct", "rx_err", "rx_none", "rx_lost", "tx_total", "tx_discarded")
BT_RATE = re.compile(r"SCO\s+= (\w+) bt_rate=(\d+)")
RATE_CODEC = {16000: "mSBC", 8000: "CVSD"}


class CallWatch(SerialWatch):
    """The LyraT console read for what a recording needs to know. Time is time.monotonic, the clock of the packets."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.sco_open = None        # None: no open or close line seen, the console cannot tell
        self.codecs = []
        self.pkt = None
        self.notes = []             # (monotonic, kind)

    def add_reset(self, source, reason=None):
        super().add_reset(source, reason)
        self.notes.append((time.monotonic(), "console_reset"))
        self.sco_open = False

    def feed(self, raw):
        drops = self.slc_drops
        super().feed(raw)
        line = self.recent[-1][1]
        if SCO_OPEN in line:
            self.sco_open = True
        elif SCO_CLOSED in line:
            self.sco_open = False
        m = SCO_CODEC.search(line)
        if m:
            self.codecs.append(m.group(1))
        m = SCO_PKT.search(line)
        if m:
            self.pkt = dict(zip(SCO_PKT_KEYS, map(int, m.groups())))
        if self.slc_drops > drops:
            self.notes.append((time.monotonic(), "slc_drop"))


class S3View(threading.Thread):
    """The S3's heartbeat of the LyraT, once a second: channel, uptime, restarts of both. Asks nothing of the LyraT."""

    def __init__(self):
        super().__init__(daemon=True)
        self.channel = s3_uart.Channel()
        self.halt = threading.Event()
        self.lock = threading.Lock()
        self.flags = None
        self.fresh_at = None
        self.up_s = None
        self.s3_boot = None
        self.pins_ok = None
        self.lyrat_resets = 0
        self.s3_restarts = 0
        self.notes = []
        self.queries = self.answered = 0
        self.want_status = threading.Event()
        self.status_text = None
        self.want_pkt = threading.Event()
        self.pkt_text = None

    def run(self):
        while not self.halt.is_set():
            got = self.channel.query()
            now = time.monotonic()
            self.queries += 1
            if got is None:
                self.channel.board = None    # broadcast again: the board may come back with another address
            else:
                self.answered += 1
                with self.lock:
                    if self.s3_boot is not None and got.get("boot") != self.s3_boot:
                        self.s3_restarts += 1
                        self.notes.append((now, "s3_restart"))
                    self.s3_boot = got.get("boot")
                    self.pins_ok = got.get("pins_ok")
                    # Without a frame for a while the flags are old news about a LyraT that may be gone.
                    if got.get("last_frame_ms") is not None and got["last_frame_ms"] < FRESH_HB_MS:
                        up = got.get("lyrat_up_s")
                        if self.up_s is not None and up is not None and up < self.up_s:
                            self.lyrat_resets += 1
                            self.notes.append((now, "lyrat_reset"))
                        self.up_s = up
                        self.flags = got.get("lyrat_flags")
                        self.fresh_at = now
            for want, name in ((self.want_status, "status"), (self.want_pkt, "pkt")):
                if want.is_set():
                    want.clear()
                    reply = self.channel.command(name, timeout=0.5, retries=3)
                    if reply["status"] == 0:
                        setattr(self, name + "_text", reply["text"])
            self.halt.wait(QUERY_PERIOD_S)
        self.channel.close()

    def says_open(self):
        with self.lock:
            if self.fresh_at is None or time.monotonic() - self.fresh_at > FRESH_HB_MS / 1000.0 + QUERY_PERIOD_S:
                return None
            return bool(self.flags & 2)

    def says_slc(self):
        with self.lock:
            return None if self.flags is None else bool(self.flags & 1)


class CallRecording:
    def __init__(self, out, port=None, grace=GRACE_S, only_enter=False, say=print, no_stream=False, listen_ports=None):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.grace, self.only_enter, self.say = grace, only_enter, say
        self.no_stream = no_stream      # a control: nothing asks the S3 for the stream, as when it is away
        self.listen_ports = listen_ports
        self.port = port
        self.rec = self.watch = self.s3 = None
        self.halt = threading.Event()
        self.console_thread = None
        self.t_start = None
        self.view = None                # True, False, None: what the sources say now
        self.intervals = []             # [open, close] in monotonic time
        self.closed_since = None
        self.fault_at = None
        self.stop_reason = None
        self.seen_notes = 0
        self.console_was = None
        self.silent_since = None
        self.silences = 0
        self.next_progress = 0.0
        self.noise_warned = 0
        self.saved = None

    # --- start ------------------------------------------------------------------------------------------------------

    def start(self):
        self.t_start = time.monotonic()
        self.rec = TapRecorder(SLOTS, journal_dir=self.out, bus_gap_s=BUS_GAP_S, listen_only=self.no_stream,
                               ports=self.listen_ports)
        self.rec.start()
        if self.port:
            self.watch = CallWatch(self.port, self.out)
            self.console_thread = threading.Thread(target=self._console, daemon=True)
            self.console_thread.start()
        self.s3 = S3View()
        self.s3.start()
        self.next_progress = self.t_start + PROGRESS_S
        return self

    def _console(self):
        while not self.halt.is_set():
            self.watch.pump(time.monotonic() + 0.2)
        self.watch.close_port()

    def wait_first_packets(self, seconds=FIRST_PACKET_S):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.rec.received("call") and self.rec.received("mic"):
                return True
            time.sleep(0.1)
        return False

    # --- the state of the channel -------------------------------------------------------------------------------------

    def console_says(self):
        return self.watch.sco_open if self.watch and self.watch.sp is not None else None

    def channel_now(self):
        votes = [v for v in (self.console_says(), self.s3.says_open()) if v is not None]
        return any(votes) if votes else None

    def _note(self, kind, now):
        texts = {"console_reset": "LyraT перезагрузилась (консоль)", "lyrat_reset": "LyraT перезагрузилась (сердцебиение через S3)",
                 "slc_drop": "связь с телефоном оборвалась", "s3_restart": "S3 перезагрузилась"}
        self.say("%s: %s. Запись продолжается, это будет в сводке." % (self.at(now), texts[kind]))
        if kind != "s3_restart":
            self.fault_at = now

    def at(self, now):
        return "%02d:%02d" % (int(now - self.t_start) // 60, int(now - self.t_start) % 60)

    def tick(self):
        """One look at the state; the stop reason when it is time to stop, otherwise None."""
        now = time.monotonic()
        notes = (self.watch.notes if self.watch else []) + self.s3.notes
        for t, kind in sorted(notes)[self.seen_notes:]:
            self._note(kind, t)
        self.seen_notes = len(notes)
        if self.watch:
            alive = self.watch.sp is not None
            if self.console_was is True and not alive:
                self.say("%s: порт LyraT %s пропал. Запись продолжается, канал вижу через S3." % (self.at(now), self.port))
            elif self.console_was is False and alive:
                self.say("%s: порт LyraT вернулся." % self.at(now))
            self.console_was = alive

        last = max((s.last_at for s in self.rec.streams.values() if s.last_at), default=None)
        if last and now - last > STREAM_SILENT_S:
            if self.silent_since is None:
                self.silent_since = now
                self.silences += 1
                self.say("%s: поток с S3 молчит. Запись ждёт его, время идёт." % self.at(now))
        elif last and self.silent_since is not None:
            self.say("%s: поток вернулся, пропуск %.1f с записан тишиной." % (self.at(now), now - self.silent_since))
            self.silent_since = None

        view = self.channel_now()
        if view != self.view:
            if view is None:
                self.say("%s: состояние канала неизвестно: нет ни консоли, ни сердцебиения. Запись идёт, остановка по Enter." % self.at(now))
            elif view:
                self.intervals.append([now, None])
                self.say("%s: канал открыт." % self.at(now))
            elif self.view:
                self.say("%s: канал закрылся." % self.at(now))
        if view and not (self.watch and self.watch.codecs) and self.s3.status_text is None:
            self.s3.want_status.set()       # no console: the codec comes from the board's status, once
        if view is not False:
            self.closed_since = None
        elif self.closed_since is None:
            self.closed_since = now
        if view is False and self.view and self.intervals and self.intervals[-1][1] is None:
            self.intervals[-1][1] = now
        self.view = view

        if now >= self.next_progress:
            self.next_progress = now + PROGRESS_S
            self._progress(now)

        if self.only_enter or view is not False or not self.intervals:
            return None
        grace = self.grace
        if self.fault_at is not None and now - self.fault_at < GRACE_AFTER_FAULT_S:
            grace = GRACE_AFTER_FAULT_S
        return "закрытие канала" if now - self.closed_since >= grace else None

    def _progress(self, now):
        got = {name: self.rec.received(name) for name in ("call", "mic")}
        state = {True: "открыт", False: "закрыт", None: "неизвестно"}[self.view]
        self.say("%s: идёт запись, пакетов call %d, mic %d, канал %s. Enter остановит." % (self.at(now), got["call"], got["mic"], state))
        if not self.intervals:
            self.say("       Канал ещё не открывался: звонок не начат или не переведён в гарнитуру LyraT_Phone.")
        floors = self.rec.recent_floor("mic")
        if floor_too_high(floors):
            self.noise_warned += 1
            self.say("       ВНИМАНИЕ: сильный шум на линии микрофона, уровень %s при пределе %.0f (#005). Запись идёт, но этот "
                     "участок mic.wav испорчен, и собеседник слышит то же." % (floors, MIC_FLOOR_WARN))

    # --- stop ---------------------------------------------------------------------------------------------------------

    def stop(self, reason):
        self.stop_reason = reason
        now = time.monotonic()
        if self.intervals and self.intervals[-1][1] is None:
            self.intervals[-1][1] = now
            self.s3.want_pkt.set()
            time.sleep(min(2.0, QUERY_PERIOD_S * 2))
        self.rec.stop()
        stopped = time.monotonic()
        self.halt.set()
        self.s3.halt.set()
        if self.console_thread:
            self.console_thread.join(timeout=3)
        self.s3.join(timeout=4)
        self.say("Сохраняю файлы, на длинной записи это займёт до минуты...")
        tap = self.rec.save(self.out)
        serial = self.watch.finish(stopped - self.t_start) if self.watch else None
        self.saved = self.summary(tap, serial, stopped)
        write_json(self.out / "call_summary.json", self.saved)
        if self.saved["files_ok"] and not self.rec.journal_error:
            (self.out / JOURNAL_NAME).unlink(missing_ok=True)
        return self.saved

    def summary(self, tap, serial, stopped):
        call, mic = tap["call"], tap["mic"]
        origin = call.get("first_at") or mic.get("first_at") or self.t_start
        s3 = self.s3
        codecs = list(self.watch.codecs) if self.watch else []
        if not codecs and s3.status_text:
            m = BT_RATE.search(s3.status_text)
            if m and m.group(1) == "active":
                codecs = [RATE_CODEC.get(int(m.group(2)), "?")]
        pkt = self.watch.pkt if self.watch and self.watch.pkt else None
        if pkt is None and s3.pkt_text:
            m = SCO_PKT.search(s3.pkt_text)
            pkt = dict(zip(SCO_PKT_KEYS, map(int, m.groups()))) if m else None
        result = {
            "stop_reason": self.stop_reason, "recorder_ran_s": round(stopped - self.t_start, 3),
            "channel": [{"from_s": round(a - origin, 2), "to_s": round(b - origin, 2), "seconds": round(b - a, 2)}
                        for a, b in self.intervals if b is not None],
            "codecs": codecs, "codec": codecs[-1] if codecs else None, "bluetooth_packets": pkt,
            "lyrat_resets_console": len(self.watch.resets) if self.watch else None,
            "lyrat_resets_s3": s3.lyrat_resets, "s3_restarts": s3.s3_restarts,
            "console_port": self.port, "console_port_opens": serial["port_opens"] if serial else 0,
            "console_port_drops": serial["port_drops"] if serial else None,
            "console_denied": serial["open_denied"] if serial else None,
            "panics": serial["panics"] if serial else None,
            "slc_drops": serial["slc_drops"] if serial else None, "stream_silences": self.silences,
            "heartbeat_answers": "%d of %d" % (s3.answered, s3.queries), "pins_ok": s3.pins_ok,
            "noise_warnings": self.noise_warned, "journal_error": self.rec.journal_error, "files": {},
        }
        ok = True
        for name, stats in (("call", call), ("mic", mic)):
            info = {k: stats.get(k) for k in ("seconds", "wall_s", "unique", "lost_packets", "loss_pct", "max_burst", "gap_events",
                                              "ovf", "send_fail", "boots", "channels", "floor_rms", "rate_hz")}
            info["bus_stops"] = len(stats.get("bus_stops") or [])
            info["gaps_s"] = round(sum(g[1] for g in stats.get("gaps") or []) / RATE, 2)
            info.update(file_levels(self.out / (name + ".wav")))
            info["path"] = str(self.out / (name + ".wav"))
            result["files"][name] = info
            ok = ok and bool(info["bytes"])
        result["files_ok"] = ok
        return result


def file_levels(path):
    """Size and level of a WAV. A file full of zeros has a length too, so emptiness is judged on the samples."""
    if not Path(path).exists():
        return {"bytes": 0, "peak": 0, "nonzero_pct": 0.0}
    size = Path(path).stat().st_size
    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if not data.size:
        return {"bytes": 0, "peak": 0, "nonzero_pct": 0.0}
    return {"bytes": size, "peak": int(np.abs(data.astype(np.int32)).max()), "nonzero_pct": round(100.0 * np.count_nonzero(data) / data.size, 2)}


def judge(s):
    """(result, faults, notes) from a summary. Faults are what stops it being PASS; notes are the rest worth saying."""
    faults, unknown, notes = [], [], []
    files = s["files"]
    for name, f in files.items():
        if not f.get("bytes") or not f.get("unique"):
            faults.append("%s.wav пуст: поток не шёл." % name)
            continue
        # With no channel the call line is rightly silent; a mic of zeros is a fault in any case.
        if not f.get("peak") and (name == "mic" or s.get("channel")):
            faults.append("%s.wav из одних нулей: линия молчит." % name)
        if f.get("wall_s") is not None and abs(f["seconds"] - f["wall_s"]) > MAX_DURATION_ERROR_S:
            faults.append("%s.wav: длительность %.1f с не сходится с часами ПК %.1f с." % (name, f["seconds"], f["wall_s"]))
        if f.get("loss_pct") is not None and f["loss_pct"] > MAX_LOSS_PCT:
            faults.append("%s: потери S3 -> ПК %.2f %% при пределе %.1f %%." % (name, f["loss_pct"], MAX_LOSS_PCT))
        if f.get("ovf"):
            faults.append("%s: S3 теряла данные шины, ovf %d." % (name, f["ovf"]))
    if files.get("call", {}).get("bytes") and files.get("mic", {}).get("bytes"):
        if abs(files["call"]["seconds"] - files["mic"]["seconds"]) > MAX_DURATION_ERROR_S:
            faults.append("call.wav %.1f с и mic.wav %.1f с не сходятся." % (files["call"]["seconds"], files["mic"]["seconds"]))
    if floor_too_high(files.get("mic", {}).get("floor_rms")):
        faults.append("шум на линии микрофона, уровень %s при пределе %.0f (#005). Запись сохранена, разборчивость проверьте на слух. "
                      "Громкий постоянный звук в комнате выглядит так же." % (files["mic"]["floor_rms"], MIC_FLOOR_WARN))
    resets = max(s.get("lyrat_resets_console") or 0, s.get("lyrat_resets_s3") or 0)
    if resets:
        faults.append("LyraT перезагружалась %d раз. Уже записанное сохранено, время сброса записано тишиной." % resets)
    if s.get("s3_restarts"):
        faults.append("S3 перезагружалась %d раз. Уже записанное сохранено, пропуск записан тишиной." % s["s3_restarts"])
    if s.get("slc_drops"):
        faults.append("связь с телефоном обрывалась %d раз." % s["slc_drops"])
    if s.get("console_port_drops"):
        faults.append("порт LyraT пропадал %d раз. Уже записанное сохранено." % s["console_port_drops"])
    if s.get("panics"):
        faults.append("паника прошивки LyraT: %d." % s["panics"])
    if s.get("stream_silences"):
        faults.append("поток с S3 замолкал %d раз." % s["stream_silences"])
    if s.get("journal_error"):
        notes.append("журнал записи дал ошибку: %s. Файлы сделаны из памяти." % s["journal_error"])
    if not s.get("channel"):
        unknown.append("канал за время записи не открывался: в файлах нет разговора.")
    elif not s.get("codec"):
        unknown.append("кодек не определён: консоль LyraT не читалась.")
    elif s["codec"] != oc.WANT_CODEC:
        faults.append("кодек %s вместо %s: качество хуже, запись сохранена." % (s["codec"], oc.WANT_CODEC))
    if not s.get("console_port_opens"):
        notes.append("консоль LyraT не читалась: сбросы и обрывы видны только через S3.")
    notes.append("Настоящим звонком эта команда стендом не проверялась, только каналом из приложения.")
    result = oc.FAIL if faults else oc.NOT_CHECKED if unknown else oc.PASS
    return result, faults + unknown, notes


def explain(s, say):
    """The summary for a person."""
    files = s["files"]
    say("Остановка: %s. Запись шла %.1f с." % (s["stop_reason"], s["recorder_ran_s"]))
    for name, what in (("call", "собеседник, то, что телефон шлёт в наушники"), ("mic", "ваш микрофон, линия кодека до обработки платы")):
        f = files[name]
        say("%s.wav (%s):" % (name, what))
        say("  %s" % f["path"])
        say("  длительность %s с, по часам ПК %s с; каналов %s; пик %s; ненулевых отсчётов %s %%" % (
            f["seconds"], f["wall_s"], f["channels"], f["peak"], f["nonzero_pct"]))
        say("  потери S3 -> ПК: %s пакетов, %s %%; самая длинная серия %s; пропуски тишиной %s с, из них остановок шины %s; ovf %s; перезапусков потока S3 %s" % (
            f["lost_packets"], f["loss_pct"], f["max_burst"], f["gaps_s"], f["bus_stops"], f["ovf"], max((f["boots"] or 1) - 1, 0)))
    if s["channel"]:
        say("Канал в файле: %s" % "; ".join("с %.1f по %.1f с (%.1f с)" % (c["from_s"], c["to_s"], c["seconds"]) for c in s["channel"]))
    say("Кодек: %s. Сбросы LyraT: консоль %s, через S3 %s. Перезапуски S3: %s. Обрывы связи с телефоном: %s. Порт LyraT пропадал: %s." % (
        s["codec"], s["lyrat_resets_console"], s["lyrat_resets_s3"], s["s3_restarts"], s["slc_drops"], s["console_port_drops"]))
    pkt = s.get("bluetooth_packets")
    if pkt and pkt.get("rx_total"):
        say("Bluetooth к плате: пакетов %d, без данных %.3f %%, с ошибками %.3f %%. Сторона к телефону здесь не измеряется." % (
            pkt["rx_total"], 100.0 * pkt["rx_none"] / pkt["rx_total"], 100.0 * pkt["rx_err"] / pkt["rx_total"]))
    say("Уровень микрофона в тихие моменты: %s, предел %.0f." % (files["mic"]["floor_rms"], MIC_FLOOR_WARN))
    say("Не видно в записи: обрывы в сторону телефона (#004), шумоподавление web2: mic.wav снят до них. То, что услышал "
        "собеседник, отличается от mic.wav. Слушайте файлы своим плеером, этот скрипт звук не включает.")


def finish(step, s, extra_faults=()):
    result, reasons, notes = judge(s)
    reasons = list(extra_faults) + reasons
    if extra_faults and result != oc.FAIL:
        result = oc.FAIL
    explain(s, step.say)
    if reasons:
        step.say("")
    for line in reasons:
        step.say("  - %s" % line)
    for line in notes:
        step.say("  * %s" % line)
    step.num("Длительность", "call %s с, mic %s с" % (s["files"]["call"]["seconds"], s["files"]["mic"]["seconds"]))
    step.num("Потери S3 -> ПК", "call %s %%, mic %s %%" % (s["files"]["call"]["loss_pct"], s["files"]["mic"]["loss_pct"]))
    step.num("Сбросы", "LyraT %s, S3 %s" % (max(s["lyrat_resets_console"] or 0, s["lyrat_resets_s3"] or 0), s["s3_restarts"]))
    step.num("Кодек", s["codec"])
    why = "запись цела, канал был, кодек нужный, сбоев нет" if result == oc.PASS else " ".join(reasons)
    todo = None
    if result == oc.FAIL:
        todo = "Файлы сохранены, послушайте их. Причины выше. Шаги проверки: lyrat_resend/docs/OWNER_CHECK.md."
    elif result == oc.NOT_CHECKED:
        todo = "Запустите запись снова и сделайте звонок, при звонке выберите гарнитуру LyraT_Phone на телефоне."
    return step.finish(result, why, todo, extra={"summary": s})


def recover(directory):
    """Files from the journal of a recording whose process died. What the console saw comes from serial.log."""
    directory = Path(directory)
    step = oc.Step("recover", "Восстановление записи из журнала", out=directory)
    if not (directory / JOURNAL_NAME).exists():
        return step.finish(oc.NOT_CHECKED, "в папке нет журнала %s" % JOURNAL_NAME, "Проверьте путь к папке записи.")
    rec = TapRecorder.from_journal(directory, bus_gap_s=BUS_GAP_S)
    tap = rec.save(directory)
    log = directory / "serial.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    codecs = SCO_CODEC.findall(text)
    step.say("Канал и кодек только по консоли, запись оборвалась без сводки.")
    for name in ("call", "mic"):
        stats, levels = tap[name], file_levels(directory / (name + ".wav"))
        step.num(name + ".wav", "%s с, пик %s, потери %s %%, %s" % (stats["seconds"], levels["peak"], stats["loss_pct"], directory / (name + ".wav")))
    step.num("Кодек по консоли", codecs[-1] if codecs else None)
    step.num("Сбросы LyraT по консоли", len(re.findall(r"rst:0x[0-9a-f]+ \(", text)))
    if not tap["call"]["unique"] or not tap["mic"]["unique"]:
        return step.finish(oc.FAIL, "в журнале нет обеих линий", "Запись пропала: поток не шёл. Журнал оставлен.")
    return step.finish(oc.NOT_CHECKED, "файлы восстановлены, но запись оборвалась, полной проверки нет. Журнал оставлен.",
                       "Слушайте восстановленные файлы. Журнал можно удалить, когда они не нужны.")


def run_stdin_watch(event):
    """Enter ends the recording. A closed stdin is not Enter: a recording started without a console keeps going."""
    def reader():
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            return
        if line:
            event.set()
    threading.Thread(target=reader, daemon=True).start()


def run(args):
    out = Path(args.out) if args.out else RUNS_ROOT / "call" / stamp()
    step = oc.Step("call", "Запись звонка через S3", out=out)
    busy = oc.bench_busy(step, need_lyrat=False, need_udp=not args.no_stream)
    if busy:
        return step.finish(oc.NOT_CHECKED, busy, "Запустить запись снова, когда стенд свободен.")
    port = args.port or oc.lyrat_port()
    state = oc.port_state(port)
    if state != "free":
        step.say("Порт LyraT %s: %s. Запись пойдёт и без него, но кодек и сбросы будут видны хуже." % (
            port, {"busy": "занят другой программой, я её не трогаю", "absent": "не найден"}.get(state, state)))
        port = None if state == "absent" else port
    keep_awake()
    rec = CallRecording(out, port=port, grace=args.grace, only_enter=args.only_enter, say=step.say, no_stream=args.no_stream)
    try:
        rec.start()
    except OSError as e:
        return step.finish(oc.NOT_CHECKED, "сокеты приёма не открылись: %s" % e, "Запущена другая запись? Закройте её и повторите.")
    step.say("Папка: %s" % out)
    if not rec.wait_first_packets():
        s = rec.stop("нет потока")
        return finish(step, s, ["нет потока с S3 за %.0f с. S3-ETH включена, кабель в сети, ПК в той же сети? Настоящий разговор сейчас записан не будет." % FIRST_PACKET_S])
    slc = rec.s3.says_slc()
    if slc is False:
        step.say("ВНИМАНИЕ: телефон сейчас не подключён к LyraT_Phone. Подключите в меню телефона, запись уже идёт.")
    floors = rec.rec.recent_floor("mic")
    if floor_too_high(floors):
        step.say("ВНИМАНИЕ: прямо сейчас шум на линии микрофона, уровень %s при пределе %.0f (#005). Лучше подождать: см. шаг mic." % (floors, MIC_FLOOR_WARN))
    step.say("")
    step.say("ЗАПИСЬ ИДЁТ. Можно звонить. Звоните сами: скрипт не набирает номер и не отвечает на звонок.")
    step.say("На телефоне выберите гарнитуру LyraT_Phone. Запись остановится сама через %.0f с после закрытия канала, или нажмите Enter." % args.grace)
    step.say("")
    enter = threading.Event()
    run_stdin_watch(enter)
    reason = None
    try:
        while reason is None:
            time.sleep(0.2)
            reason = rec.tick()
            if enter.is_set():
                reason = "Enter"
            elif args.seconds and time.monotonic() - rec.t_start >= args.seconds:
                reason = "время --seconds"
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    s = rec.stop(reason)
    return finish(step, s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="folder; the default is bench_runs/call/<stamp>")
    ap.add_argument("--seconds", type=float, help="stop after this long, for tests")
    ap.add_argument("--grace", type=float, default=GRACE_S, help="seconds after the channel closed before the recording stops")
    ap.add_argument("--only-enter", action="store_true", help="stop only on Enter: for a call that leaves the headset for a while")
    ap.add_argument("--port", help="console of the LyraT; the default is found by USB id")
    ap.add_argument("--recover", metavar="DIR", help="make the files from the journal of a recording that died")
    ap.add_argument("--no-stream", action="store_true", help="control: nothing asks the S3 for the stream")
    args = ap.parse_args()
    oc.console_utf8()
    oc.adb_on_path()
    data = recover(args.recover) if args.recover else run(args)
    return oc.EXIT[data["result"]]


if __name__ == "__main__":
    sys.exit(main())
