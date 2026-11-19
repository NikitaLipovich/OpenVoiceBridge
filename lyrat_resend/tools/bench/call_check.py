#!/usr/bin/env python3
"""The owner's test call: one command checks the bench, waits for the call, records both directions, says how it went.

  python lyrat_resend/tools/bench/call_check.py                    check, "Звоните", record the call, report
  python lyrat_resend/tools/bench/call_check.py --probe 3 [--seconds 20]  the same path without a call: sco_probe opens
                                                                   the channel, the phone plays tones (the LyraT at vol 0)
  python lyrat_resend/tools/bench/call_check.py --report DIR       remake the files and the report of a run folder
  python lyrat_resend/tools/bench/call_check.py --compare [--seconds 20]   no call: the owner speaks into the LyraT, the
                                                                   phone records what it gets; the two are aligned and
                                                                   each defect marked (phone_compare.py, goal 099)

Before the call: the S3 answers over Wi-Fi, versions of both boards, the LyraT alive behind it with the phone connected
(HFP), the S3 stream on (TAP1) with both lines coming, the clocks of the S3 and the PC brought together (round trip, the
alignment of the two lines). Each "no" is a line in Russian with what to do. The phone over adb is looked at when it is
there and is not needed. Then it prints "ЗВОНИТЕ" and waits for the headset channel (SCO) to open: it dials nothing (R3).
During the call one live line a second: time, level of both directions, packets lost, breaks of the channel, latency.
It stops 10 s after the channel closed, on Enter or on Ctrl+C.

Files in goals_harness/bench_runs/calls/<stamp>/, outside git (R6):
  <stamp>_down.wav   what the phone sends to the headphones: the other party. 16 kHz mono
  <stamp>_up.wav     the headset mic line before the board's processing (both ADC channels averaged, as the board does)
  <stamp>_both.wav   stereo, left down, right up, aligned in time
  <stamp>_sent.wav   what the LyraT sent to the phone: its mSBC frames after the board's processing, before the air, decoded
                     (goal 112, sent_tap.py: `senttap` on the LyraT, UDP 5009 from the S3); lost frames are silence
  <stamp>_sent_both.wav   stereo, left up (before processing), right sent, on one time line
  report.md, report.json   duration, codec, S3 stream losses and gaps, channel and link breaks, levels, floor, clipping,
                     drop-outs, latency, and one line "качество: норм" or "качество: есть проблемы: ..."
  call_summary.json, tap.json, serial.log    what call_record.py writes
The files cover the channel from its first opening to its last closing, 1 s more each side; with no channel, the whole.
Nothing is played on the PC (R15): the paths are printed, the owner listens with a player.

A run of --probe watches the phone for a real call (owner_common.RealCallGuard): one ringing, active, or come and gone
during the run stops it at once, its sound files are deleted unanalysed and the series ends (R3/R6). The owner's own
run has no such guard: there the call is the point.

Live output: while it runs, the aligned stream goes to 127.0.0.1:5010 (call_stream.py, format in its docstring) for a
future transcriber; --no-pcm turns it off. Exit code: 0 качество норм, 1 есть проблемы, 2 не проверено (no channel, bench
not ready). Made by goal 097 from call_record.py, owner_check.py (rehearsal) and call_stream.py.
"""
import argparse
import json
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import RUNS_ROOT, keep_awake, stamp, write_json  # noqa: E402
import call_quality as cq  # noqa: E402
import call_record  # noqa: E402
import call_stream as cs  # noqa: E402
import owner_common as oc  # noqa: E402
import sent_tap  # noqa: E402
from owner_common import FAIL, NOT_CHECKED, PASS  # noqa: E402
from udp_record import MIC_FLOOR_WARN, floor_too_high  # noqa: E402

RATE = 16000
TRIM_S = 1.0
CLOCK_WAIT_S = 6.0
LIVE_EVERY_S = 1.0
PROBE_TONES = [400, 1000, 1800]
SOCK_WAIT_S = 8.0
PROBE_S = 20                # owner rule 2026-11-18: a capture of 15-20 s is enough; the owner's call has no limit


def bar(dbfs, width=12):
    if dbfs is None:
        return " " * width
    n = int(round(max(0.0, min(1.0, (dbfs + 60.0) / 60.0)) * width))
    return "#" * n + "." * (width - n)


class Counter:
    """Lost packets per line as they come: unique first copies against the sequence numbers, per S3 boot."""

    def __init__(self):
        self.lock = threading.Lock()
        self.boots = {"call": {}, "mic": {}}

    def add(self, stream, data):
        seq = int.from_bytes(data[4:8], "little")
        boot = int.from_bytes(data[12:14], "little")
        with self.lock:
            b = self.boots[stream].setdefault(boot, [seq, seq, 0])
            b[0], b[1], b[2] = min(b[0], seq), max(b[1], seq), b[2] + 1

    def lost(self, stream):
        with self.lock:
            return sum(hi - lo + 1 - n for lo, hi, n in self.boots[stream].values())

    def got(self, stream):
        with self.lock:
            return sum(n for _, _, n in self.boots[stream].values())


class CallCheck:
    """One run: checks, waiting for the channel, the recording with the live pipeline, the files and the report."""

    def __init__(self, out, step, args, say=None):
        self.out = Path(out)
        self.step = step
        self.args = args
        self.say = say or step.say
        self.prefix = self.out.name.split("-probe")[0]
        self.counter = Counter()
        self.meter = cs.LiveMeter()
        self.latency = cs.LatencyStats()
        self.server = None
        self.pipe = None
        self.rec = None
        self.versions = {}
        self.checks = []
        self.live_last = 0.0
        self.sco_breaks = 0
        self.sent = None            # goal 112: the frames the LyraT sends to the phone

    # --- before the call ----------------------------------------------------------------------------------------------

    def check(self, ok, text, fix=None, hard=True):
        mark = "ок" if ok else ("НЕТ" if hard else "--")
        self.say("  [%s] %s" % (mark, text))
        if not ok and fix:
            self.say("        что сделать: %s" % fix)
        self.checks.append({"ok": bool(ok), "text": text, "fix": fix, "hard": hard})
        return ok or not hard

    def on_packet(self, stream, data, arrival):
        self.counter.add(stream, data)
        self.pipe.on_packet(stream, data, arrival)

    def prechecks(self):
        """True when the bench is ready for the call. The recorder runs from here on."""
        import ota
        import s3_uart
        self.say("Проверка стенда перед звонком:")
        busy = oc.bench_busy(self.step, need_lyrat=False, need_udp=True)
        if not self.check(busy is None, "стенд свободен" if busy is None else busy, "закройте другую запись или дождитесь её конца"):
            return False
        ch = s3_uart.Channel()
        try:
            found = ch.find(timeout=5.0)
            q = (ch.query() or {}) if found else {}
        finally:
            ch.close()
        if not self.check(found, "S3 отвечает по Wi-Fi" if found else "S3 не отвечает по Wi-Fi",
                          "включите коробку с платами (обе на аккумуляторах), подождите 30 с; ПК в той же сети Wi-Fi?"):
            return False
        self.check(bool(q.get("pins_ok")), "выводы S3 к LyraT только входы (pins_ok)", "прошивка S3 не та: lyrat_resend/docs/S3_TAP.md")
        try:
            link = ota.Link(ch.board, ota.OTA_PORT)
            info = link.query() or {}
            link.close()
        except OSError:
            info = {}
        ly = ota.status_fields((info.get("lyrat") or {}).get("status") or "") if info else {}
        self.versions = {"s3": info.get("ver"), "s3_img": info.get("img"), "lyrat": ly.get("ver"), "lyrat_img": ly.get("img"),
                         "lyrat_mode": ly.get("mode")}
        self.check(bool(info), "версии: S3 %s (%s), LyraT %s (%s, %s)" % (
            self.versions["s3"], self.versions["s3_img"], self.versions["lyrat"], self.versions["lyrat_img"],
            self.versions["lyrat_mode"]), "S3 не ответила на запрос версий: повторите через 10 с", hard=False)
        last = q.get("last_frame_ms")
        alive = last is not None and 0 <= last <= 3000
        if not self.check(alive, "LyraT жива (сердцебиение через S3 %s мс назад, работает %s с)" % (last, q.get("lyrat_up_s")),
                          "LyraT не отвечает через S3: она включена? Подождите 30 с после включения и повторите."):
            return False
        flags = q.get("lyrat_flags") or 0
        if not self.check(bool(flags & 1), "телефон подключён к LyraT_Phone (HFP)" if flags & 1 else "телефон НЕ подключён к LyraT_Phone",
                          "на телефоне: Bluetooth -> LyraT_Phone -> подключить; затем запустите команду снова"):
            return False
        if flags & 2:
            self.say("        канал гарнитуры уже открыт: идёт разговор? Запись начнётся сразу.")
        if not self.args.no_phone:
            self.phone_checks()
        try:
            self.server = None if self.args.no_pcm else cs.PcmServer(self.args.pcm_port)
        except OSError as e:
            self.check(False, "локальный выход для транскрибации (127.0.0.1:%d) не открылся: %s" % (self.args.pcm_port, e),
                       "порт занят другой программой; запись идёт и без него (--pcm-port другой)", hard=False)
        consumers = [c for c in (self.server, self.meter, self.latency) if c is not None]
        self.rec = call_record.CallRecording(self.out, grace=self.args.grace, only_enter=self.args.only_enter,
                                             say=self.say, on_packet=self.on_packet)
        self.pipe = cs.LivePipeline(consumers, hold_s=self.args.hold_ms / 1000.0)
        try:
            self.rec.start()
        except OSError as e:
            self.check(False, "сокеты приёма не открылись: %s" % e, "запущена другая запись? Закройте её.")
            return False
        self.pipe.clock = cs.ClockModel(self.rec.rec)
        self.pipe.start()
        got = self.rec.wait_first_packets()
        if not self.check(got, "поток S3 включён (TAP1): идут обе линии" if got else "нет потока с S3 за %.0f с" % call_record.FIRST_PACKET_S,
                          "S3 в сети, ПК в той же сети Wi-Fi? Брандмауэр Windows пропускает python на UDP 5004-5006?"):
            return False
        end = time.monotonic() + CLOCK_WAIT_S
        while time.monotonic() < end and (self.pipe.stats["align_frames"] is None or self.pipe.clock.rtt_min() is None):
            time.sleep(0.2)
        rtt = self.pipe.clock.rtt_min()
        align = self.pipe.stats["align_frames"]
        ok = rtt is not None and align is not None
        if not self.check(ok, "часы S3 и ПК сведены: кратчайший обмен %.1f мс, сдвиг счётчиков линий %s кадров (%.2f мс)" % (
                (rtt or 0) * 1000, align, (align or 0) / RATE * 1000) if ok else "часы S3 и ПК не сведены за %.0f с" % CLOCK_WAIT_S,
                "S3 не отвечает на запросы потока или одна линия не идёт; перезапустите команду"):
            return False
        if not getattr(self.args, "no_sent", False):
            self.start_sent()
        loss = {k: self.counter.lost(k) for k in ("call", "mic")}
        self.check(not any(loss.values()), "потерь в первые секунды нет" if not any(loss.values()) else "потери в первые секунды: %s" % loss,
                   "Wi-Fi слабый? Поставьте коробку ближе к роутеру", hard=False)
        floors = self.rec.rec.recent_floor("mic")
        self.check(not floor_too_high(floors), "фон микрофона в норме (%s)" % floors,
                   "шум на линии микрофона (#005 гоала 057): лучше подождать пару минут и запустить снова", hard=False)
        return True

    def start_sent(self):
        """Goal 112: the LyraT copies its frames to the phone up to the PC. Not needed for the call: a soft check."""
        try:
            self.sent = sent_tap.SentTap(self.out).start()
        except OSError as e:
            self.sent = None
            self.check(False, "приём отправленного телефону (UDP %d) не открылся: %s" % (sent_tap.PORT, e),
                       "порт занят другой записью; звонок пишется и без _sent.wav", hard=False)
            return
        try:
            ok, text = self.sent.enable()
        except OSError as e:
            ok, text = False, str(e)
        self.check(ok, "LyraT отдаёт копию отправленного телефону (senttap): %s" % text if ok else
                   "LyraT не включила копию отправленного (senttap): %s" % (text or "нет ответа"),
                   "прошивка LyraT до гоала 112? Запись идёт без _sent.wav", hard=False)

    def phone_checks(self):
        """The phone over adb is a help, not a need: the owner may call with the cable out."""
        try:
            import phone
            st = phone.status()
        except Exception as e:  # noqa: BLE001  adb absent, no device, any parse error
            self.check(False, "телефон по adb не виден (%s): проверка телефона пропущена, звонку это не мешает" % str(e)[:80],
                       hard=False)
            return
        self.check(bool(st.get("hfp_connected")), "телефон (adb): гарнитура подключена, батарея %s %%" % st.get("battery_level"),
                   "на телефоне подключите LyraT_Phone", hard=False)
        if st.get("keyguard"):
            self.check(False, "телефон заблокирован", "разблокируйте телефон", hard=False)

    # --- the call ------------------------------------------------------------------------------------------------------

    def live_line(self, now, force=False):
        if not force and now - self.live_last < LIVE_EVERY_S:
            return
        self.live_last = now
        down, up = self.meter.levels_db()
        opened = [a for a, _ in self.rec.intervals]
        t = now - opened[0] if opened else now - self.rec.t_start
        lat = self.latency.values[-50:]
        lat_ms = "%3.0f" % (1000 * float(np.median(lat))) if lat else "  -"
        breaks = max(len(self.rec.intervals) - 1, 0) + (self.rec.watch.slc_drops if self.rec.watch else 0) + self.rec.silences
        state = {True: "канал открыт", False: "канал закрыт", None: "канал ?"}[self.rec.view]
        show = lambda v: "-" if v is None else "тишина" if v < -90 else v  # noqa: E731
        text = "%02d:%02d %s | вниз %6s дБ %s | вверх %6s дБ %s | потеряно %d/%d пак. | обрывов %d | задержка %s мс" % (
            int(t) // 60, int(t) % 60, state, show(down), bar(down), show(up),
            bar(up), self.counter.lost("call"), self.counter.lost("mic"), breaks, lat_ms)
        # Goal 111: the LyraT's mic muted (key Rec, command, panel): the phone gets silence, the up line here is before it.
        text += mute_mark(self.rec.muted)
        if sys.stdout.isatty():
            sys.stdout.write("\r" + text + "   ")
            sys.stdout.flush()
        elif int(t) % 5 == 0 or force:
            print(text, flush=True)
        self.step.log.write(text + "\n")

    def run(self, driver=None, guard=None):
        """After prechecks: wait for the channel, record, stop. Returns the stop reason. guard: an
        owner_common.RealCallGuard of a run without a call; when it trips the run stops at once (R3/R6)."""
        self.say("")
        if driver is None:
            self.say("ЗВОНИТЕ. Позвоните с телефона (или примите звонок) и говорите как обычно; звук должен идти через "
                     "гарнитуру LyraT_Phone. Скрипт сам ничего не набирает. Остановка: сама через %.0f с после конца "
                     "разговора, или Enter, или Ctrl+C." % self.args.grace)
        else:
            self.say("Прогон без звонка: канал откроет приложение sco_probe.")
        self.say("")
        enter = threading.Event()
        if driver is None:
            call_record.run_stdin_watch(enter)
        reason = None
        waiting_said = time.monotonic()
        try:
            if driver:
                driver.begin()
            while reason is None:
                time.sleep(0.1)
                now = time.monotonic()
                reason = self.rec.tick()
                if getattr(self, "sent", None):
                    self.sent.tick(now)
                if driver:
                    driver.tick(now)
                if not self.rec.intervals:
                    if now - waiting_said >= 15.0:
                        waiting_said = now
                        self.say("жду начала разговора (канал гарнитуры ещё не открыт)...")
                else:
                    self.live_line(now)
                if guard is not None and guard.tripped:
                    reason = "настоящий звонок: " + guard.tripped
                elif enter.is_set():
                    reason = "Enter"
                elif self.args.seconds and now - self.rec.t_start >= self.args.seconds:
                    reason = "время --seconds"
                elif driver and driver.timed_out(now):
                    reason = "предел времени прогона"
        except KeyboardInterrupt:
            reason = "Ctrl+C"
        finally:
            if driver:
                driver.end()
        if self.rec.intervals:
            self.live_line(time.monotonic(), force=True)
            if sys.stdout.isatty():
                print()
        return reason

    def stop(self, reason):
        # The live stream ends with the recording: saving the files takes seconds, and blocks given out meanwhile would
        # only be the deadline's zeros.
        self.pipe.stop()
        if getattr(self, "sent", None):
            self.sent.stop()
        s = self.rec.stop(reason)
        tap = json.loads((self.out / "tap.json").read_text(encoding="utf-8"))
        self.pipe_summary = self.pipe.summary()
        return s, tap

    # --- after the call --------------------------------------------------------------------------------------------------

    def finish(self, s, tap, consumer=None):
        muted = self.rec.mute_intervals(self.rec.t_start + (s.get("recorder_ran_s") or 0))
        files = build_files(self.out, self.prefix, tap, self.rec.intervals, keep_raw=self.args.keep_raw, muted=muted)
        self.files = files      # start_at: the PC time of sample 0 of the files (call_check --compare needs it)
        closed = [iv for iv in self.rec.intervals if iv[1] is not None]
        latency = self.latency.summary(within=closed) if closed else self.latency.summary()
        latency["all_blocks"] = self.latency.summary()
        latency["all_blocks"].pop("over_100ms_at_s", None)
        latency["scope"] = "канал открыт" if closed else "вся запись"
        sent = build_sent(self.out, self.prefix, files, sent_tap.read_journal(self.out / "sent.bin")) if getattr(self, "sent", None) else None
        report = build_report(s, tap, files, self.pipe_summary, latency, self.versions, self.checks, consumer,
                              self.server.dropped if self.server else None, sent=sent)
        write_json(self.out / "report.json", report)
        (self.out / "report.md").write_text(render_md(report), encoding="utf-8")
        return report


# --- files -----------------------------------------------------------------------------------------------------------------

def _read(path):
    with wave.open(str(path), "rb") as w:
        ch = w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return x.reshape(-1, ch) if ch > 1 else x.reshape(-1, 1)


def _take(x, start, length):
    out = np.zeros(length, dtype=np.int16)
    a, b = max(start, 0), min(start + length, len(x))
    if b > a:
        out[a - start:b - start] = x[a:b]
    return out


def _write(path, data, channels):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.ascontiguousarray(data, dtype="<i2").tobytes())


def mono(x):
    return x[:, 0].copy() if x.shape[1] == 1 else ((x[:, 0].astype(np.int32) + x[:, 1].astype(np.int32)) >> 1).astype(np.int16)


def mute_mark(muted):
    """The live line's mark of the LyraT's mic (goal 111)."""
    return " | МИКРОФОН ВЫКЛ" if muted else ""


def zero_muted(x, muted_s, rate=RATE):
    """A copy of x with the muted stretches [[from_s, to_s]] (seconds of x) set to 0: the phone got silence there."""
    y = np.array(x, dtype=np.int16, copy=True)
    for a, b in muted_s:
        y[max(int(round(a * rate)), 0):max(int(round(b * rate)), 0)] = 0
    return y


def mute_gaps(muted_s, rate=RATE):
    """The muted stretches as gaps [start, length] in samples, the form call_quality.line_metrics takes."""
    return [[int(round(a * rate)), int(round((b - a) * rate))] for a, b in muted_s if b > a]


def cut_muted(x, gaps, muted_s, rate=RATE):
    """x without its muted stretches, and the gaps moved to the cut line (those inside a muted stretch go): the quality
    of the up line is judged only where the mic was on (goal 111)."""
    keep = np.ones(len(x), dtype=bool)
    for a, b in muted_s:
        keep[max(int(round(a * rate)), 0):max(int(round(b * rate)), 0)] = False
    if keep.all():
        return x, gaps
    pos = np.cumsum(keep) - 1          # index in the cut line of each kept sample
    moved = []
    for start, length in gaps:
        s0, s1 = max(start, 0), min(start + length, len(x))
        if s1 <= s0 or not keep[s0:s1].any():
            continue
        idx = np.nonzero(keep[s0:s1])[0]
        moved.append([int(pos[s0 + idx[0]]), int(idx.size)])
    return x[keep], moved


def build_files(out, prefix, tap, intervals, keep_raw=False, muted=None):
    """down, up and both on one time line. Each WAV of the recorder starts at its own first packet; t0_at (the lower
    envelope of the arrivals) is the PC time of its sample 0, so the shift between them is (t0 mic - t0 call)."""
    out = Path(out)
    call, mic = tap["call"], tap["mic"]
    down_all = mono(_read(out / "call.wav")) if (out / "call.wav").exists() else np.zeros(0, np.int16)
    up_all = mono(_read(out / "mic.wav")) if (out / "mic.wav").exists() else np.zeros(0, np.int16)
    t0c = call.get("t0_at")
    t0m = mic.get("t0_at")
    if t0c is None and t0m is None:
        t0c = t0m = 0.0
    t0c = t0m if t0c is None else t0c
    t0m = t0c if t0m is None else t0m
    ends = [t0c + len(down_all) / RATE, t0m + len(up_all) / RATE]
    closed = [iv for iv in intervals if iv[1] is not None]
    if closed:
        ta, tb = closed[0][0] - TRIM_S, closed[-1][1] + TRIM_S
        ta = max(ta, min(t0c, t0m))
        tb = min(tb, max(ends))
    else:
        ta, tb = min(t0c, t0m), max(ends)
    length = max(int(round((tb - ta) * RATE)), 0)
    sc, sm = int(round((ta - t0c) * RATE)), int(round((ta - t0m) * RATE))
    down, up = _take(down_all, sc, length), _take(up_all, sm, length)
    # Goal 111: the muted stretches in seconds of the files; up is zeroed there, as the phone got it.
    muted_s = [[round(max(a - ta, 0.0), 2), round(min(b - ta, length / RATE), 2)] for a, b in (muted or [])
               if b - ta > 0 and a - ta < length / RATE]
    up = zero_muted(up, muted_s)
    paths = {k: out / ("%s_%s.wav" % (prefix, k)) for k in ("down", "up", "both")}
    _write(paths["down"], down, 1)
    _write(paths["up"], up, 1)
    _write(paths["both"], np.stack([down, up], axis=1), 2)
    gaps = {"down": [[g[0] - sc, g[1]] for g in call.get("gaps") or []], "up": [[g[0] - sm, g[1]] for g in mic.get("gaps") or []]}
    sco = [[round(a - ta, 2), round(b - ta, 2)] for a, b in closed]
    if not keep_raw and length:
        for name in ("call.wav", "mic.wav"):
            (out / name).unlink(missing_ok=True)
    # The counters' offset seen by the files: what the live pipeline locked should agree with it.
    offset = None
    if call.get("first_frame") is not None and mic.get("first_frame") is not None and call.get("t0_at") and mic.get("t0_at"):
        offset = int(round((t0m - t0c) * RATE)) - (mic["first_frame"] - call["first_frame"])
    return {"paths": {k: str(v) for k, v in paths.items()}, "down": down, "up": up, "gaps": gaps, "sco": sco,
            "start_at": ta, "seconds": round(length / RATE, 2), "shift_ms": round((t0m - t0c) * 1000, 2), "counter_offset": offset,
            "muted": muted_s, "sm": sm, "mic_first_frame": mic.get("first_frame")}


def build_sent(out, prefix, files, packets):
    """Goal 112: _sent.wav and _sent_both.wav on the time line of the files; the summary for the report."""
    if not packets:
        return {"enabled": True, "packets": 0, "seconds": 0, "why": "кадров от LyraT не пришло"}
    r = sent_tap.build(out, prefix, packets, files["mic_first_frame"], files["sm"], len(files["up"]), files["up"])
    pcm = r.pop("pcm")
    files["sent"] = pcm
    files["paths"].update(r.get("paths") or {})
    r["dominant_hz"] = dominant_hz(pcm[pcm != 0])
    r["enabled"] = True
    r["packets"] = r["stats"]["packets"]
    r["file_start"] = {"sm": files["sm"], "mic_first_frame": files["mic_first_frame"]}
    return r


def dominant_hz(x):
    """The strongest frequency of a line, for the probe's tone (None under 0.5 s)."""
    if len(x) < RATE // 2:
        return None
    f = np.abs(np.fft.rfft(x.astype(np.float64) * np.hanning(len(x))))
    f[:int(len(f) * 50 / (RATE / 2))] = 0      # below 50 Hz
    return round(float(np.argmax(f)) * RATE / len(x), 1)


# --- report ------------------------------------------------------------------------------------------------------------------

def _speech_part(x, gaps, sco):
    """The part of a line from the first opening of the channel to the last closing, with its gaps."""
    if not sco:
        return x, gaps
    a, b = int(sco[0][0] * RATE), int(sco[-1][1] * RATE)
    return x[a:b], [[g[0] - a, g[1]] for g in gaps if g[0] + g[1] > a and g[0] < b]


def build_report(s, tap, files, pipe, latency, versions, checks, consumer=None, dropped=None, sent=None):
    lines = {}
    muted = files.get("muted") or []
    mute = {"intervals": muted, "seconds": round(sum(b - a for a, b in muted), 2), "count": len(muted), "whole": False}
    for key in ("down", "up"):
        part, gaps = _speech_part(files[key], files["gaps"][key], files["sco"])
        if key == "up" and muted:
            # Goal 111: silence while the owner muted the mic is no defect: only the rest of the line is judged.
            off = files["sco"][0][0] if files["sco"] else 0.0
            n = len(part)
            in_part = [[max(a - off, 0.0), min(b - off, n / RATE)] for a, b in muted if b - off > 0 and a - off < n / RATE]
            part, gaps = cut_muted(part, gaps, in_part)
            mute["in_channel_s"] = round(sum(b - a for a, b in in_part), 2)
            mute["whole"] = n > 0 and len(part) < RATE // 2      # under 0.5 s left with the mic on
        lines[key] = cq.line_metrics(part, gaps)
    stream = {}
    for key, name in (("down", "call"), ("up", "mic")):
        t = tap[name]
        stream[key] = {"packets": t.get("unique"), "lost": t.get("lost_packets"), "loss_pct": t.get("loss_pct"),
                       "max_burst": t.get("max_burst"), "gaps_s": round(sum(g[1] for g in t.get("gaps") or []) / RATE, 3),
                       "bus_stops": len(t.get("bus_stops") or []), "ovf": t.get("ovf"), "send_fail": t.get("send_fail"),
                       "rate_hz": t.get("rate_hz"), "floor_rms_channels": t.get("floor_rms")}
    sco = {"intervals": files["sco"], "seconds": round(sum(b - a for a, b in files["sco"]), 2), "opens": len(files["sco"]),
           "codec": s.get("codec"), "codecs": s.get("codecs"), "bluetooth_packets": s.get("bluetooth_packets")}
    events = {"sco_breaks": max(len(files["sco"]) - 1, 0), "slc_drops": s.get("slc_drops") or 0,
              "lyrat_resets": max(s.get("lyrat_resets_console") or 0, s.get("lyrat_resets_s3") or 0),
              "s3_restarts": s.get("s3_restarts") or 0, "stream_silences": s.get("stream_silences") or 0}
    live_off = pipe.get("align_frames")
    report = {
        "at": stamp(), "stop_reason": s.get("stop_reason"), "recorded_s": s.get("recorder_ran_s"),
        "files": files["paths"], "file_seconds": files["seconds"], "sco": sco, "stream": stream, "events": events,
        "lines": lines, "versions": versions, "checks": checks,
        "alignment": {"file_shift_ms": files["shift_ms"], "counter_offset_files": files["counter_offset"],
                      "counter_offset_live": live_off,
                      "agree": None if files["counter_offset"] is None or live_off is None else abs(files["counter_offset"] - live_off) <= 16},
        "latency": {"pipeline": latency, "rtt_min_ms": pipe.get("rtt_min_ms"), "s3_mark": pipe.get("s3_mark"),
                    "hold_ms": None, "consumer": consumer},
        "live": {k: pipe.get(k) for k in ("blocks", "blocks_no_down", "blocks_no_up", "late", "breaks", "relocks", "segments",
                                          "segment_log", "consumer_errors")},
        "pcm_dropped": dropped,
        "mute": mute,
        "sent": sent,
    }
    ok, problems = cq.verdict(report)
    report["verdict"] = {"ok": ok, "problems": problems, "line": cq.verdict_line(ok, problems)}
    return report


def _f(v, fmt="%.1f"):
    return "-" if v is None else fmt % v


def render_md(r):
    L = r["lines"]
    d, u = L["down"], L["up"]
    sd, su = r["stream"]["down"], r["stream"]["up"]
    lat = r["latency"]["pipeline"]
    out = ["# Проверка звонка %s" % r["at"], "", "**%s**" % r["verdict"]["line"], "",
           "## Файлы (слушайте своим плеером)", "",
           "- собеседник (вниз, телефон -> наушники): `%s`" % r["files"]["down"],
           "- ваш микрофон (вверх, линия кодека до обработки платы): `%s`" % r["files"]["up"],
           "- оба (стерео: слева собеседник, справа вы, выровнены по времени): `%s`" % r["files"]["both"], "",
           "Длительность файлов %.1f с. Канал гарнитуры открывался %d раз, всего %.1f с; кодек %s." % (
               r["file_seconds"], r["sco"]["opens"], r["sco"]["seconds"], r["sco"]["codec"] or "не определён"), "",
           "## Качество по направлениям", "",
           "| | вниз (собеседник) | вверх (микрофон) |", "|---|---|---|",
           "| уровень в речи, дБFS | %s | %s |" % (_f(d["rms_dbfs"]), _f(u["rms_dbfs"])),
           "| пик, дБFS | %s | %s |" % (_f(d["peak_dbfs"]), _f(u["peak_dbfs"])),
           "| фон в паузах, дБFS | %s | %s |" % (_f(d["floor_dbfs"]), _f(u["floor_dbfs"])),
           "| речь над фоном, дБ | %s | %s |" % (_f(d["snr_db"]), _f(u["snr_db"])),
           "| доля звука, %% | %s | %s |" % (_f(d["active_pct"]), _f(u["active_pct"])),
           "| выпадения звука (нули >= 7,5 мс) | %d (%.1f/мин) | %d (%.1f/мин) |" % (
               d["dropouts"], d["dropouts_per_min"], u["dropouts"], u["dropouts_per_min"]),
           "| клиппинг, %% отсчётов | %s | %s |" % (_f(d["clip_pct"], "%.3f"), _f(u["clip_pct"], "%.3f")),
           "| потери S3 -> ПК, пакетов (%%) | %s (%s) | %s (%s) |" % (sd["lost"], sd["loss_pct"], su["lost"], su["loss_pct"]),
           "| пропуски тишиной, с | %s | %s |" % (sd["gaps_s"], su["gaps_s"]), "",
           "## Сбои", ""]
    ev = r["events"]
    out.append("Обрывов канала: %d, обрывов связи с телефоном: %d, перезагрузок LyraT: %d, S3: %d, замолканий потока: %d." % (
        ev["sco_breaks"], ev["slc_drops"], ev["lyrat_resets"], ev["s3_restarts"], ev["stream_silences"]))
    pkt = r["sco"].get("bluetooth_packets")
    if pkt and pkt.get("rx_total"):
        out.append("Bluetooth телефон -> плата: пакетов %d, без данных %.3f %%, с ошибками %.3f %%." % (
            pkt["rx_total"], 100.0 * pkt["rx_none"] / pkt["rx_total"], 100.0 * pkt["rx_err"] / pkt["rx_total"]))
    out += ["", "## Живой поток (для будущей транскрибации)", "",
            "Задержка от звука на шине I2S до блока у потребителя, пока открыт канал: медиана %s мс, 95 %% %s мс, макс %s мс (цель <= 300). "
            "Кратчайший обмен с S3 %s мс. Блоков %s, без вниз %s, без вверх %s, поздних пакетов %s." % (
                lat.get("median_ms", "-"), lat.get("p95_ms", "-"), lat.get("max_ms", "-"), r["latency"]["rtt_min_ms"],
                r["live"]["blocks"], r["live"]["blocks_no_down"], r["live"]["blocks_no_up"], r["live"]["late"])]
    a = r["alignment"]
    out.append("Выравнивание линий: сдвиг файлов %s мс, сдвиг счётчиков по файлам %s, по живому потоку %s кадров." % (
        a["file_shift_ms"], a["counter_offset_files"], a["counter_offset_live"]))
    mu = r.get("mute") or {}
    if mu.get("count"):
        out += ["", "## Микрофон выключался (кнопка Rec, команда mute, пульт)", "",
                "%d раз, всего %.1f с: %s. В эти интервалы телефон получал тишину; линия `up` в файлах там обнулена, и "
                "качество микрофона считается только по остальному времени — тишина при выключенном микрофоне не дефект." % (
                    mu["count"], mu["seconds"], ", ".join("%.1f–%.1f с" % (a, b) for a, b in mu["intervals"]))]
    out += sent_md(r.get("sent"))
    if r["verdict"]["problems"]:
        out += ["", "## Проблемы", ""] + ["- %s" % p for p in r["verdict"]["problems"]]
    out += ["", "## Что не видно в записи", "",
            "- `up` снят с линии кодека до шумоподавления и до Bluetooth: собеседник слышит обработанный звук, обрывы в сторону "
            "телефона (#004 гоала 057) здесь не видны.",
            "- Версии: S3 %s, LyraT %s." % (r["versions"].get("s3"), r["versions"].get("lyrat")), ""]
    return "\n".join(out)


def sent_md(se):
    """The report's part on what the LyraT sent to the phone (goal 112)."""
    if not se:
        return []
    out = ["", "## Отправлено телефону (после обработки платы, до Bluetooth)", ""]
    if not se.get("seconds"):
        return out + ["Файла нет: %s." % (se.get("why") or "кадров нет")]
    st, lv, lu, al = se["stats"], se["levels"], se["levels_unmuted"], se["align"]
    how = ("по содержимому, обработка платы отстаёт на %.1f мс (совпадение %.2f)" % (al["lag_ms"], al["xcorr_peak"])
           if al["method"] == "xcorr" else "по отметкам S3 (содержимое не совпало: тон платы, тишина или выключен микрофон)")
    out += ["- файл: `%s`; стерео (слева микрофон до обработки, справа отправлено): `%s`" % (
                se["paths"]["sent"], se["paths"]["sent_both"]),
            "- %.1f с, кадров %d, потеряно %d (%s %%, %.0f мс тишиной в файле), из них отброшено на LyraT %d" % (
                se["seconds"], st["frames"], st["lost_frames"], se["loss_pct"], se["lost_ms"], st["lyrat_drops"]),
            "- уровень %s дБFS, пик %s дБFS; с включённым микрофоном %s дБFS" % (
                _f(lv["rms_dbfs"]), _f(lv["peak_dbfs"]), _f(lu["rms_dbfs"])),
            "- выравнивание с `up`: %s" % how,
            "- микрофон выключен по кадрам: %s" % (", ".join("%.1f–%.1f с" % (a, b) for a, b in se["muted"]) or "нет")]
    return out


def explain(step, r):
    v = r["verdict"]
    step.say("")
    step.say(v["line"].upper() if not v["ok"] else v["line"])
    step.say("Файлы (слушайте своим плеером, скрипт звук не включает):")
    step.say("  собеседник: %s" % r["files"]["down"])
    step.say("  микрофон:   %s" % r["files"]["up"])
    step.say("  оба:        %s" % r["files"]["both"])
    se = r.get("sent") or {}
    if se.get("seconds"):
        step.say("  отправлено телефону: %s" % se["paths"]["sent"])
    step.say("Отчёт: %s" % (Path(r["files"]["down"]).parent / "report.md"))
    lat = r["latency"]["pipeline"]
    step.num("Канал", "%d раз, %.1f с, кодек %s" % (r["sco"]["opens"], r["sco"]["seconds"], r["sco"]["codec"]))
    step.num("Потери S3 -> ПК", "вниз %s %%, вверх %s %%" % (r["stream"]["down"]["loss_pct"], r["stream"]["up"]["loss_pct"]))
    step.num("Уровни в речи, дБFS", "вниз %s, вверх %s" % (r["lines"]["down"]["rms_dbfs"], r["lines"]["up"]["rms_dbfs"]))
    step.num("Задержка живого потока, мс", "медиана %s, макс %s" % (lat.get("median_ms"), lat.get("max_ms")))
    if se.get("seconds"):
        step.num("Отправлено телефону", "%.1f с, потеряно кадров %d (%s %%), уровень %s дБFS, выравнивание %s %s мс" % (
            se["seconds"], se["stats"]["lost_frames"], se["loss_pct"], se["levels"]["rms_dbfs"], se["align"]["method"],
            se["align"]["lag_ms"]))
    elif se:
        step.num("Отправлено телефону", "нет: %s" % (se.get("why") or "кадров нет"))
    mu = r.get("mute") or {}
    if mu.get("count"):
        step.num("Микрофон выключен", "%d раз, %.1f с: %s" % (mu["count"], mu["seconds"], ", ".join(
            "%.1f–%.1f с" % (a, b) for a, b in mu["intervals"])))


# --- the self-run without a call -------------------------------------------------------------------------------------------

class ProbeDriver:
    """sco_probe opens the channel, the phone plays tones for --seconds, then the channel closes. No call (R3)."""

    def __init__(self, step, seconds, tag):
        import round as rnd
        self.rnd, self.step, self.seconds, self.tag = rnd, step, seconds, tag
        self.opened_at = self.closed_at = None
        self.route = None
        self.started = None

    def begin(self):
        self.started = time.monotonic()
        self.rnd.app_command("open", self.tag + "-open")
        end = time.monotonic() + 15.0
        while time.monotonic() < end:
            st = self.rnd.app_status()
            if st and st.get("id") == self.tag + "-open" and (st.get("route_confirmed") or not st.get("ok")):
                self.route = bool(st.get("ok") and st.get("route_confirmed"))
                break
            time.sleep(0.3)
        if self.route:
            self.opened_at = time.monotonic()
            self.rnd.app_command("play", self.tag + "-play", "--es", "wav", self.rnd.RECORD_WAV)
        else:
            self.step.say("приложение не открыло канал")
            self.closed_at = time.monotonic()

    def tick(self, now):
        if self.opened_at and not self.closed_at and now - self.opened_at >= self.seconds:
            self.rnd.app_command("close", self.tag + "-close")
            self.closed_at = now

    def timed_out(self, now):
        return now - self.started > self.seconds + 60.0

    def end(self):
        if not self.closed_at:
            self.rnd.app_command("close", self.tag + "-close")
            self.closed_at = time.monotonic()


def directions(report):
    """Tones from the phone must be in down and not in up: the lines are not swapped or shorted."""
    import audio_check
    out = {}
    sco = report["sco"]["intervals"]
    if not sco:
        return {"ok": False, "why": "канал не открывался"}
    for key in ("down", "up"):
        x = mono(_read(report["files"][key]))
        a, b = int((sco[0][0] + 2) * RATE), int((sco[0][1] - 2) * RATE)
        part = x[a:b]
        res = audio_check.check(list(map(int, part)), RATE, PROBE_TONES, 20.0, 1.0) if len(part) > RATE else {"pass": False, "tones": {}}
        snr = [t.get("snr_db") for t in res["tones"].values() if t.get("snr_db") is not None]
        out[key] = {"tones": bool(res["pass"]), "min_snr_db": min(snr) if snr else None}
    out["ok"] = out["down"]["tones"] and not out["up"]["tones"]
    return out


def run_probe(args):
    import round as rnd
    import s3_uart
    base = Path(args.out) if args.out else RUNS_ROOT / "calls" / ("%s-probe" % stamp())
    step = oc.Step("call_probe", "Прогон call_check без звонка через sco_probe", out=base)
    if rnd.SCO_APP not in rnd.adb("shell", "pm", "path", rnd.SCO_APP):
        return step.finish(NOT_CHECKED, "приложение sco_probe не найдено на телефоне или телефон недоступен по adb",
                           "python lyrat_resend/tools/android/build_apk.py --install")
    calls = rnd.telecom_state()
    if calls is None or calls["calls_now"] or calls["telephony_busy"]:
        return step.finish(NOT_CHECKED, "на телефоне идёт звонок или состояние не читается", "Запустить после разговора.")
    ch = s3_uart.Channel()
    if not ch.find(timeout=5.0):
        ch.close()
        return step.finish(NOT_CHECKED, "S3 не отвечает", "Включить стенд.")
    rnd.app_ready()
    # No sound in the room (R15): the LyraT at vol 0, the bus still carries the phone's tones.
    if ch.command("vol 0", timeout=0.5, retries=5)["status"] != 0:
        ch.close()
        return step.finish(NOT_CHECKED, "LyraT не приняла vol 0 через S3", "owner_check.py uart")
    wav = step.out / rnd.RECORD_WAV
    rnd.make_tone_wav(wav, args.probe_seconds + 30)
    rnd.adb("push", str(wav), rnd.SCO_DIR + "/" + rnd.RECORD_WAV, timeout=120.0)
    wav.unlink(missing_ok=True)
    keep_awake()
    rows = []
    try:
        for i in range(1, args.probe + 1):
            out = base / ("%s-run%02d" % (stamp(), i))
            rows.append(probe_one(step, out, args, "%s-%d" % (time.strftime("%H%M%S"), i)))
            r = rows[-1]
            step.say("Прогон %d: %s. %s" % (i, r["result"], "; ".join(r["reasons"]) or "без замечаний"))
            if r["result"] == DISCARDED:
                step.say("Серия остановлена: на телефоне настоящий звонок. Остальные прогоны не делаются.")
                break
            time.sleep(3.0)
    finally:
        ch.command("vol 100", timeout=0.5, retries=5)
        ch.close()
        rnd.adb("shell", "am", "force-stop", rnd.SCO_APP)
        rnd.adb("shell", "rm", "-f", rnd.SCO_DIR + "/" + rnd.RECORD_WAV)
    write_json(base / "probe.json", rows)
    passed = sum(r["result"] == PASS for r in rows)
    if any(r["result"] == DISCARDED for r in rows):
        return step.finish(NOT_CHECKED, "серия прервана настоящим звонком на телефоне, звук этих прогонов удалён",
                           "Повторить серию, когда на телефоне нет звонков.", extra={"rows": rows})
    step.num("Прогоны", "%d из %d" % (passed, len(rows)))
    for key in ("sco_s", "loss_down", "loss_up", "latency_median_ms", "latency_max_ms", "consumer_median_ms", "align_agree"):
        step.num(key, [r.get(key) for r in rows])
    if passed == len(rows):
        return step.finish(PASS, "команда видит канал, пишет оба направления, отчёт собирается; без звонка", extra={"rows": rows})
    return step.finish(FAIL, "; ".join(sorted({x for r in rows for x in r["reasons"]})) or "прогоны не прошли",
                       "См. report.md прогонов.", extra={"rows": rows})


DISCARDED = "ОТБРОШЕН"


def probe_one(step, out, args, tag, guard_state=None):
    sub = oc.Step("call_check", "Прогон без звонка", out=out)
    cc = CallCheck(out, sub, args)
    reasons = []
    # A run without a call must not keep a real conversation (R3/R6): the phone is watched from before the first packet.
    guard = oc.RealCallGuard(state=guard_state).start()
    if guard.tripped:
        guard.stop()
        sub.finish(NOT_CHECKED, guard.tripped)
        return {"result": DISCARDED, "reasons": [guard.tripped], "dir": str(out)}
    if not cc.prechecks():
        guard.stop()
        if cc.rec:
            cc.rec.stop("проверка стенда не прошла")
            cc.pipe.stop()
        sub.finish(NOT_CHECKED, "стенд не готов")
        return {"result": NOT_CHECKED, "reasons": ["проверка стенда не прошла"], "dir": str(out)}
    consumer = {}
    if cc.server:
        t = threading.Thread(target=lambda: consumer.update(cs.listen(cc.server.port, seconds=args.probe_seconds + 60)), daemon=True)
        t.start()
    reason = cc.run(ProbeDriver(sub, args.probe_seconds, tag), guard=guard)
    s, tap = cc.stop(reason)
    if cc.server:
        t.join(timeout=SOCK_WAIT_S)
    real_call = guard.stop()
    if real_call:
        gone = oc.discard_audio(out)
        sub.say("Прогон отброшен: %s. Звук удалён (%d файлов), не анализируется; остаются только числа потока." % (real_call, len(gone)))
        write_json(out / "discarded.json", {"reason": real_call, "removed": gone, "stop_reason": reason})
        sub.finish(NOT_CHECKED, "прогон отброшен: " + real_call, "Повторить, когда на телефоне нет звонка.")
        return {"result": DISCARDED, "reasons": [real_call], "dir": str(out)}
    report = cc.finish(s, tap, consumer or None)
    dirs = directions(report)
    report["directions"] = dirs
    write_json(out / "report.json", report)
    explain(sub, report)
    if not dirs["ok"]:
        reasons.append("направления: тоны вниз %s, вверх %s" % (dirs.get("down"), dirs.get("up")))
    for key in ("down", "up"):
        if report["lines"][key]["seconds"] < 1 or (key == "down" and report["lines"][key]["silent"]):
            reasons.append("%s пуст" % key)
    if report["events"]["sco_breaks"]:
        reasons.append("канал рвался %d раз" % report["events"]["sco_breaks"])
    sco_s = report["sco"]["seconds"]
    if abs(sco_s - args.probe_seconds) > 3.0:
        reasons.append("канал %.1f с при %d с прогона" % (sco_s, args.probe_seconds))
    if report["latency"]["pipeline"].get("median_ms") is None or report["latency"]["pipeline"]["median_ms"] > cq.MAX_LATENCY_MS:
        reasons.append("задержка %s" % report["latency"]["pipeline"])
    if not report["alignment"]["agree"]:
        reasons.append("выравнивание файлов и живого потока не сходится: %s" % report["alignment"])
    if consumer and (not consumer.get("blocks") or consumer.get("index_gaps")):
        reasons.append("потребитель PCM: %s" % {k: consumer.get(k) for k in ("blocks", "index_gaps")})
    # A rehearsal keeps nothing of the room (R15): the mic line and the stereo go once judged.
    se = report.get("sent")
    if se is not None and not se.get("seconds"):
        reasons.append("_sent.wav пуст: %s" % (se.get("why") or "кадров нет"))
    if not args.keep:
        for key in ("up", "both", "sent", "sent_both"):
            if key in report["files"]:
                Path(report["files"][key]).unlink(missing_ok=True)
        (Path(out) / "sent.bin").unlink(missing_ok=True)
    result = PASS if not reasons else FAIL
    sub.finish(result, "; ".join(reasons) or "прогон без замечаний; %s" % report["verdict"]["line"])
    lat = report["latency"]["pipeline"]
    return {"result": result, "reasons": reasons, "dir": str(out), "sco_s": sco_s,
            "loss_down": report["stream"]["down"]["loss_pct"], "loss_up": report["stream"]["up"]["loss_pct"],
            "lost": [report["stream"]["down"]["lost"], report["stream"]["up"]["lost"]],
            "latency_median_ms": lat.get("median_ms"), "latency_p95_ms": lat.get("p95_ms"), "latency_max_ms": lat.get("max_ms"),
            "consumer_median_ms": (consumer.get("latency") or {}).get("median_ms") if consumer else None,
            "consumer_max_ms": (consumer.get("latency") or {}).get("max_ms") if consumer else None,
            "align_agree": report["alignment"]["agree"], "alignment": report["alignment"], "directions": dirs,
            "verdict": report["verdict"]["line"], "codec": report["sco"]["codec"], "live": report["live"],
            "s3_mark": report["latency"]["s3_mark"], "rtt_min_ms": report["latency"]["rtt_min_ms"],
            "dropouts": [report["lines"]["down"]["dropouts"], report["lines"]["up"]["dropouts"]]}


# --- compare: what left the LyraT mic against what the phone got (goal 099) ----------------------------------------------------

COMPARE_S = 20
COMPARE_SAY = ("ГОВОРИТЕ в микрофон LyraT %d с: считайте вслух от 1 до 20, не спеша, обычным голосом "
               "(можно читать любой текст). Телефон пишет то, что получает по Bluetooth.")


class CompareDriver:
    """sco_probe opens the channel and records what the phone gets for `seconds`; the owner speaks into the LyraT. No call
    (R3), nothing is played by the phone or the PC (R15). The LyraT's packet counters are asked for once a second."""

    def __init__(self, cc, step, seconds, tag):
        import round as rnd
        self.rnd, self.cc, self.step, self.seconds, self.tag = rnd, cc, step, seconds, tag
        self.wav = "compare_%s.wav" % tag
        self.rec_id = tag + "-rec"
        self.started = self.opened_at = self.record_at = self.closed_at = None
        self.record = None
        self.next_pkt = self.next_poll = self.next_say = 0.0
        self.error = None

    def begin(self):
        self.started = time.monotonic()
        self.rnd.app_command("open", self.tag + "-open")
        end = time.monotonic() + 15.0
        route = False
        while time.monotonic() < end:
            st = self.rnd.app_status()
            if st and st.get("id") == self.tag + "-open" and (st.get("route_confirmed") or not st.get("ok")):
                route = bool(st.get("ok") and st.get("route_confirmed"))
                break
            time.sleep(0.3)
        if not route:
            self.error = "приложение не открыло канал"
            self.step.say(self.error)
            self.closed_at = time.monotonic()
            return
        self.opened_at = time.monotonic()
        self.rnd.app_command("record", self.rec_id, "--ei", "seconds", str(self.seconds), "--es", "out", self.wav,
                             "--es", "source", "raw")
        self.record_at = time.monotonic()
        self.step.say(">>> " + COMPARE_SAY % self.seconds)
        self.next_say = self.record_at + 5.0

    def tick(self, now):
        if not self.opened_at or self.closed_at:
            return
        if now >= self.next_pkt:
            self.next_pkt = now + 1.0
            self.cc.rec.s3.want_pkt.set()
        left = self.seconds - (now - self.record_at)
        if left > 0.5 and now >= self.next_say:
            self.next_say = now + 5.0
            self.step.say(">>> говорите, осталось %d с" % round(left))
        if now - self.record_at >= self.seconds + 0.5 and now >= self.next_poll:
            self.next_poll = now + 0.5
            rec = (self.rnd.app_status() or {}).get("record") or {}
            if rec.get("id") == self.rec_id and rec.get("state") not in (None, "recording"):
                self.record = rec
                self.step.say(">>> СТОП, можно молчать. Сравниваю...")
                self._close(now)
                return
        if now - self.record_at > self.seconds + 15.0:
            self.error = "телефон не закончил запись за %d с" % (self.seconds + 15)
            self._close(now)

    def _close(self, now):
        self.rnd.app_command("close", self.tag + "-close")
        self.closed_at = now

    def timed_out(self, now):
        return now - self.started > self.seconds + 60.0

    def end(self):
        if not self.closed_at:
            self._close(time.monotonic())


def bench_without_call(step, need_vol0=True):
    """The common start of a run without a call: the probe app, no call on the phone, the S3, the LyraT at vol 0 (R15).
    Returns the open S3 channel, or the step's finish dict when the bench is not ready."""
    import round as rnd
    import s3_uart
    if rnd.SCO_APP not in rnd.adb("shell", "pm", "path", rnd.SCO_APP):
        return step.finish(NOT_CHECKED, "приложение sco_probe не найдено на телефоне или телефон недоступен по adb",
                           "python lyrat_resend/tools/android/build_apk.py --install")
    calls = rnd.telecom_state()
    if calls is None or calls["calls_now"] or calls["telephony_busy"]:
        return step.finish(NOT_CHECKED, "на телефоне идёт звонок или состояние не читается", "Запустить после разговора.")
    ch = s3_uart.Channel()
    if not ch.find(timeout=5.0):
        ch.close()
        return step.finish(NOT_CHECKED, "S3 не отвечает", "Включить стенд.")
    rnd.app_ready()
    if need_vol0 and ch.command("vol 0", timeout=0.5, retries=5)["status"] != 0:
        ch.close()
        return step.finish(NOT_CHECKED, "LyraT не приняла vol 0 через S3", "owner_check.py uart")
    return ch


def pkt_rows(log):
    """The S3View's timed replies of `pkt` as counter rows {t, rx_total, ..., tx_discarded}."""
    rows = []
    for t, text in log:
        m = call_record.SCO_PKT.search(text or "")
        if m:
            rows.append({"t": t, **dict(zip(call_record.SCO_PKT_KEYS, map(int, m.groups())))})
    return rows


def pkt_rows_console(path, t0):
    """The same rows from the LyraT console log (serial.log, first column seconds since t0 of its watch): the board prints
    its `pkt` reply there, the S3 relays only the status."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = call_record.SCO_PKT.search(line)
        if m:
            try:
                at = float(line.split()[0])
            except (ValueError, IndexError):
                continue
            rows.append({"t": t0 + at, **dict(zip(call_record.SCO_PKT_KEYS, map(int, m.groups())))})
    return rows


def run_compare(args):
    import phone_compare
    import round as rnd
    out = Path(args.out) if args.out else RUNS_ROOT / "calls" / ("%s-compare" % stamp())
    step = oc.Step("call_compare", "Сравнение: микрофон LyraT до Bluetooth и что получил телефон", out=out)
    ch = bench_without_call(step)
    if isinstance(ch, dict):
        return ch
    keep_awake()
    args.grace = min(args.grace, 3.0)
    tag = time.strftime("%H%M%S")
    cc = CallCheck(out, step, args)
    guard = oc.RealCallGuard().start()
    driver = None
    reason = None
    try:
        if guard.tripped:
            return step.finish(NOT_CHECKED, guard.tripped, "Повторить, когда на телефоне нет звонка.")
        if not cc.prechecks():
            if cc.rec:
                cc.rec.stop("проверка стенда не прошла")
                cc.pipe.stop()
            bad = [c for c in cc.checks if not c["ok"] and c["hard"]]
            return step.finish(NOT_CHECKED, "стенд не готов: " + "; ".join(c["text"] for c in bad),
                               "; ".join(c["fix"] for c in bad if c["fix"]) or "см. строки выше")
        driver = CompareDriver(cc, step, args.compare_seconds, tag)
        reason = cc.run(driver, guard=guard)
        s, tap = cc.stop(reason)
    finally:
        ch.command("vol 100", timeout=0.5, retries=5)
        ch.close()
        real_call = guard.stop()
        rnd.adb("shell", "am", "force-stop", rnd.SCO_APP)
    phone_raw = out / "phone_raw.wav"
    if driver.record and driver.record.get("state") == "done":
        rnd.adb("pull", rnd.SCO_DIR + "/" + driver.wav, str(phone_raw), timeout=120.0)
    rnd.adb("shell", "rm", "-f", rnd.SCO_DIR + "/" + driver.wav)
    if real_call:
        gone = oc.discard_audio(out)
        write_json(out / "discarded.json", {"reason": real_call, "removed": gone, "stop_reason": reason})
        return step.finish(NOT_CHECKED, "прогон отброшен: %s; звук удалён, не анализируется" % real_call,
                           "Повторить, когда на телефоне нет звонка.")
    stream = cc.finish(s, tap)
    for name in ("report.json", "report.md"):
        if (out / name).exists():
            (out / name).replace(out / ("stream_" + name))
    files = stream["files"]
    up_full = out / "up_full.wav"
    Path(files["up"]).replace(up_full)
    for key in ("down", "both"):
        Path(files[key]).unlink(missing_ok=True)
    files = {**files, "start_at": cc.files.get("start_at")}
    if driver.error or not phone_raw.exists():
        if not args.keep:
            oc.discard_audio(out)
        return step.finish(NOT_CHECKED, driver.error or "запись телефона не пришла", "Повторить; телефон разблокирован?")
    start_at = files.get("start_at")
    expected = None if start_at is None else (driver.record_at - start_at) * RATE
    watch = cc.rec.watch
    pkt = pkt_rows(cc.rec.s3.pkt_log)
    if not pkt and watch is not None and (out / "serial.log").exists():
        pkt = pkt_rows_console(out / "serial.log", watch.t0)
    meta = {"run": {"tag": tag, "seconds": driver.seconds, "stop": reason, "sco": stream["sco"],
                    "stream_loss_pct": {k: stream["stream"][k]["loss_pct"] for k in ("down", "up")},
                    "stream_gaps_up_s": stream["stream"]["up"].get("gaps_s"), "versions": stream.get("versions"),
                    "phone_record": {k: (driver.record or {}).get(k) for k in ("frames", "peak", "rms", "error")}},
            "expected": expected, "start_at": start_at, "pkt_log": pkt}
    write_json(out / "compare.json", meta)
    rep = phone_compare.analyse(out, phone_compare.read_wav(up_full), phone_compare.read_wav(phone_raw), expected,
                                start_at, pkt, {"run": meta["run"]})
    if not args.keep:
        up_full.unlink(missing_ok=True)
        phone_raw.unlink(missing_ok=True)
    if args.drop_audio:
        gone = oc.discard_audio(out)
        step.say("Звук удалён после оценки (%d файлов): остаются числа." % len(gone))
    step.say("")
    step.say("Итог: " + rep["summary"])
    step.say("Папка: %s" % out)
    step.say("  compare_both.wav: слева до Bluetooth, справа телефон; report.md: таблица дефектов; cut_*.wav: вырезки.")
    step.say("ПК ничего не проигрывает: откройте файлы плеером.")
    if rep["events"] is None:
        return step.finish(NOT_CHECKED, rep["summary"], "Повторить и говорить ближе к микрофону LyraT.",
                           extra={"report": "report.json"})
    return step.finish(PASS, rep["summary"], extra={"report": "report.json", "events": len(rep["events"]),
                                                     "alignment": rep["alignment"]})


# --- main ------------------------------------------------------------------------------------------------------------------

def run_owner(args):
    out = Path(args.out) if args.out else RUNS_ROOT / "calls" / stamp()
    step = oc.Step("call_check", "Проверка звонка: запись и качество", out=out)
    keep_awake()
    cc = CallCheck(out, step, args)
    if not cc.prechecks():
        if cc.rec:
            cc.rec.stop("проверка стенда не прошла")
            cc.pipe.stop()
        bad = [c for c in cc.checks if not c["ok"] and c["hard"]]
        return step.finish(NOT_CHECKED, "стенд не готов: " + "; ".join(c["text"] for c in bad),
                           "; ".join(c["fix"] for c in bad if c["fix"]) or "см. строки выше")
    reason = cc.run()
    step.say("Остановка: %s. Сохраняю файлы..." % reason)
    s, tap = cc.stop(reason)
    report = cc.finish(s, tap)
    explain(step, report)
    if not report["sco"]["intervals"]:
        return step.finish(NOT_CHECKED, report["verdict"]["line"], "Запустите снова и позвоните; на телефоне выберите LyraT_Phone.",
                           extra={"report": "report.json"})
    if report["verdict"]["ok"]:
        return step.finish(PASS, report["verdict"]["line"], extra={"report": "report.json"})
    return step.finish(FAIL, report["verdict"]["line"], "Послушайте файлы; причины в report.md.", extra={"report": "report.json"})


def remake(directory):
    """Files and report again from a run folder that still has call.wav and mic.wav (--keep-raw)."""
    directory = Path(directory)
    s = json.loads((directory / "call_summary.json").read_text(encoding="utf-8"))
    tap = json.loads((directory / "tap.json").read_text(encoding="utf-8"))
    origin = tap["call"].get("first_at") or tap["mic"].get("first_at") or 0.0
    intervals = [[origin + c["from_s"], origin + c["to_s"]] for c in s.get("channel") or []]
    muted = [[origin + c["from_s"], origin + c["to_s"]] for c in s.get("mic_mute") or []]
    files = build_files(directory, directory.name, tap, intervals, keep_raw=True, muted=muted)
    old = json.loads((directory / "report.json").read_text(encoding="utf-8")) if (directory / "report.json").exists() else {}
    pipe = {"align_frames": (old.get("alignment") or {}).get("counter_offset_live"), "rtt_min_ms": (old.get("latency") or {}).get("rtt_min_ms")}
    sent = build_sent(directory, directory.name, files, sent_tap.read_journal(directory / "sent.bin")) \
        if (directory / "sent.bin").exists() else None
    report = build_report(s, tap, files, pipe, (old.get("latency") or {}).get("pipeline") or {}, old.get("versions") or {},
                          old.get("checks") or [], sent=sent)
    write_json(directory / "report.json", report)
    (directory / "report.md").write_text(render_md(report), encoding="utf-8")
    print(report["verdict"]["line"])
    return 0 if report["verdict"]["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="folder; default goals_harness/bench_runs/calls/<stamp>")
    ap.add_argument("--grace", type=float, default=call_record.GRACE_S, help="seconds after the channel closed before it stops")
    ap.add_argument("--only-enter", action="store_true", help="stop only on Enter or Ctrl+C")
    ap.add_argument("--seconds", type=float, help="stop after this long (tests)")
    ap.add_argument("--hold-ms", type=float, default=cs.HOLD_S * 1000, help="jitter buffer of the live stream")
    ap.add_argument("--pcm-port", type=int, default=cs.PCM_PORT, help="live PCM on 127.0.0.1 for a transcriber")
    ap.add_argument("--no-pcm", action="store_true", help="no live PCM output")
    ap.add_argument("--no-phone", action="store_true", help="do not look at the phone over adb (tests on the PC)")
    ap.add_argument("--no-sent", action="store_true", help="do not ask the LyraT for the frames it sends (no _sent.wav)")
    ap.add_argument("--keep-raw", action="store_true", help="keep the recorder's call.wav and mic.wav (needed by --report)")
    ap.add_argument("--probe", type=int, metavar="N", help="N runs without a call, the channel from sco_probe")
    ap.add_argument("--keep", action="store_true", help="--probe: keep the mic and stereo files")
    ap.add_argument("--report", metavar="DIR", help="remake files and report of a run folder kept with --keep-raw")
    ap.add_argument("--compare", action="store_true", help="no call: the LyraT mic before Bluetooth against the phone's "
                    "recording (--seconds, default 20); --keep keeps the raw pair for phone_compare.py DIR")
    ap.add_argument("--drop-audio", action="store_true", help="--compare: delete every sound file once judged (bench runs)")
    args = ap.parse_args(argv)
    oc.console_utf8()
    oc.adb_on_path()
    if args.report:
        return remake(args.report)
    if args.compare:
        # As in a probe, --seconds is how long the phone records, not a stop of the recording.
        args.compare_seconds = int(args.seconds or COMPARE_S)
        args.seconds = None
        return oc.EXIT[run_compare(args)["result"]]
    if args.probe:
        # In a probe --seconds is how long the channel stays open, not a stop of the recording.
        args.probe_seconds = int(args.seconds or PROBE_S)
        args.seconds = None
        data = run_probe(args)
    else:
        data = run_owner(args)
    return oc.EXIT[data["result"]]


if __name__ == "__main__":
    sys.exit(main())
