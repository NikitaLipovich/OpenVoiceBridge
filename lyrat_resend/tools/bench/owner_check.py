#!/usr/bin/env python3
"""The owner's check of the bench: one command per step, each ends in PASS, FAIL or НЕ ПРОВЕРЕНО with its numbers.

  python lyrat_resend/tools/bench/owner_check.py ports        the bench: ports, phone, S3, nothing else holds them
  python lyrat_resend/tools/bench/owner_check.py firmware     which build is on the LyraT, its address, its noise mode; resets the board
  python lyrat_resend/tools/bench/owner_check.py stable       no reset of the LyraT in 2 minutes (#003)
  python lyrat_resend/tools/bench/owner_check.py power        the power indicator, not there until goal 059
  python lyrat_resend/tools/bench/owner_check.py bluetooth    reconnect after reset, channel without a call, codec, the phone's refusals
  python lyrat_resend/tools/bench/owner_check.py audio        tones both ways through Bluetooth, 10 trials
  python lyrat_resend/tools/bench/owner_check.py breaks       breaks of the sound toward the phone per minute, 5 minutes (#004)
  python lyrat_resend/tools/bench/owner_check.py uart         commands through the S3 and its UART, restarts of both boards
  python lyrat_resend/tools/bench/owner_check.py stream       a 5 minute recording of the S3 stream, with tones, against the PC clock
  python lyrat_resend/tools/bench/owner_check.py mic          quiet on the mic line right now (#005)
  python lyrat_resend/tools/bench/owner_check.py rehearsal    call_record end to end on a channel from the app, no call
  python lyrat_resend/tools/bench/owner_check.py voice        the owner speaks, modes before and after; only the ear can judge it
  python lyrat_resend/tools/bench/owner_check.py all          every step above but voice, in order, and a table at the end

These are thin wrappers: round.py, voice_check.py, phone.py, s3_uart.py, udp_record.py do the measuring. What they add is the
judgement against what the owner needs, which the tools leave out on purpose: round.py sco passes on CVSD, round.py btloss
passes with any number of breaks. The result comes from the file the run wrote and its numbers, never from an exit code alone.

Nothing here dials or answers (R3), plays a sound on the PC (R15), or touches other Bluetooth devices (R4). A busy port or a running
series of the chain gives НЕ ПРОВЕРЕНО and leaves them alone. Exit code: 0 PASS, 1 FAIL, 2 НЕ ПРОВЕРЕНО. Output: bench_runs/owner/.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner_common as oc  # noqa: E402
from owner_common import FAIL, NOT_CHECKED, PASS, Step  # noqa: E402
from benchlib import FW_DIR, REPO_DIR, RUNS_ROOT, keep_awake, stamp  # noqa: E402

BREAKS_LIMIT = 0.63         # breaks a minute toward the phone: the top of what goals 044 and 045 measured
BREAKS_BEFORE = "0,37-0,63"
ELF = FW_DIR / "build" / "lyrat_resend.elf"
VARIANT_H = FW_DIR / "main" / "bench_variant.h"
MAC_B1 = 1 * 256 + 1        # the value of BENCH_MAC_PATCH for mac_b1, see fw.py
TAP_MIC_S = 10
MIN_STREAM_SHARE = 0.9      # of a window, in sound really received from the S3
APP_VERSION = re.compile(r"App version:\s+(\S+)")
APP_TIME = re.compile(r"Compile time:\s+(.+)$")
APP_ELF = re.compile(r"ELF file SHA256:\s+([0-9a-f]+)")
BOARD_MAC = re.compile(r"Bluetooth MAC: (\S+)")
ASK_S3 = "S3-ETH включена, кабель в сети, ПК в той же сети? Ответ S3 приходит на широковещательный запрос, адрес искать не нужно."
REHEARSAL_S = 30
TONES = [400, 1000, 1800]


# --- stand and ports -------------------------------------------------------------------------------------------------

def step_ports(args):
    import phone
    import s3_uart
    import round as rnd
    step = Step("ports", "Стенд и порты")
    procs = oc.other_bench_processes()
    if procs is None:
        return step.finish(NOT_CHECKED, "список процессов не читается", "Посмотрите вручную, не идёт ли прогон.")
    if procs:
        return oc.refuse_if_busy(step, need_lyrat=False, need_udp=False)
    faults, busy = [], []
    lyrat, s3 = oc.lyrat_port(), oc.s3_port()
    state_l, state_s = oc.port_state(lyrat), oc.port_state(s3)
    step.num("Порт LyraT", "%s: %s" % (lyrat, state_l))
    step.num("Порт S3", "%s: %s" % (s3, state_s))
    for name, state, port in (("LyraT", state_l, lyrat), ("S3", state_s, s3)):
        if state == "absent":
            faults.append("порта %s нет (%s): плата включена, USB-кабель на месте?" % (name, port))
        elif state == "busy":
            busy.append("порт %s %s занят другой программой (монитор ESP-IDF, терминал), я её не трогаю" % (name, port))
    udp = oc.udp_state()
    step.num("Порты приёма потока 5004, 5005", "заняты: %s" % udp if udp else "свободны")
    if udp:
        busy.append("порты приёма потока заняты: %s, запущена другая запись?" % udp)
    ch = None
    try:
        ch = s3_uart.Channel()
        found = ch.find(timeout=5.0)
    except OSError as e:
        found = False
        step.say("Сеть недоступна: %s" % e)
    if not found:
        faults.append("S3 не отвечает по сети. " + ASK_S3)
        step.num("S3 по сети", "нет ответа")
    else:
        q = ch.query() or {}
        step.num("S3 по сети", "отвечает, выводы в порядке: %s, UART: кадров %s, ошибок CRC %s" % (bool(q.get("pins_ok")), q.get("frames"), q.get("bad_crc")))
        if not q.get("pins_ok"):
            faults.append("S3 сообщает pins_ok=0: выводы к LyraT не входы. Проверьте её прошивку, S3_TAP.md.")
        last = q.get("last_frame_ms")
        step.num("LyraT через S3", "последний кадр %s мс назад, работает %s с, флаги SLC=%s SCO=%s" % (
            last, q.get("lyrat_up_s"), bool((q.get("lyrat_flags") or 0) & 1), bool((q.get("lyrat_flags") or 0) & 2)))
        if last is None or last > 3000:
            faults.append("LyraT молчит на линии UART: плата работает?")
    if ch:
        ch.close()
    try:
        st = phone.status()
    except phone.PhoneError as e:
        st = None
        faults.append("телефон по adb недоступен: %s. USB-кабель, отладка разрешена?" % e)
    if st:
        step.num("Телефон", "adb=%s Bluetooth=%s пара=%s гарнитура=%s аудиосервис=%s заблокирован=%s других пар=%s батарея=%s %%" % (
            st["state"], st["bt_enabled"], st["bonded"], st["hfp_connected"], st["audio_has_headset"], st["keyguard"], st["other_bonds"], st["battery_level"]))
        if st["keyguard"]:
            faults.append("телефон заблокирован: снимите блокировку и держите экран включённым.")
        if not st["bt_enabled"]:
            faults.append("Bluetooth на телефоне выключен.")
        if not st["bonded"]:
            faults.append("нет пары с LyraT_Phone: команды в OWNER_CHECK.md, раздел про пару.")
        calls = rnd.telecom_state()
        if calls is None:
            faults.append("состояние звонков на телефоне не читается.")
        elif calls["calls_now"] or calls["telephony_busy"]:
            busy.append("на телефоне идёт звонок: шаги с каналом не запускаются во время разговора.")
        app = rnd.adb("shell", "pm", "path", rnd.SCO_APP)
        step.num("Приложение sco_probe", "есть" if rnd.SCO_APP in app else "нет")
        if rnd.SCO_APP not in app:
            faults.append("приложение sco_probe не установлено: python lyrat_resend/tools/android/build_apk.py --install")
    running = [g for g in oc.goals_running() if not g.startswith("061")]
    if running:
        step.say("Сейчас помечены как идущие гоалы цепочки: %s. Между сериями порты свободны, но стенд может понадобиться им. "
                 "Поставьте цепочку на паузу (NEEDS-OWNER, пункт 45)." % ", ".join(running))
    if faults:
        return step.finish(FAIL, "; ".join(faults), "Исправить перечисленное и запустить шаг снова.")
    if busy:
        return step.finish(NOT_CHECKED, "; ".join(busy), "Запустить шаг снова, когда стенд свободен.")
    return step.finish(PASS, "порты, сеть S3, телефон и приложение на месте, ничего их не держит")


# --- firmware --------------------------------------------------------------------------------------------------------

def local_build():
    """The ELF of the last build on this PC and the address variant it was built with. Read only."""
    variant = None
    if VARIANT_H.exists():
        m = re.search(r"BENCH_MAC_PATCH (\d+)", VARIANT_H.read_text(encoding="utf-8"))
        variant = int(m.group(1)) if m else 0
    sha = hashlib.sha256(ELF.read_bytes()).hexdigest() if ELF.exists() else None
    return sha, variant


def git_head():
    try:
        return subprocess.run(["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def step_firmware(args):
    import round as rnd
    import s3_uart
    import voice_check
    from serial_watch import SerialWatch
    step = Step("firmware", "Версия прошивки и режим шумоподавления")
    refused = oc.refuse_if_busy(step, need_udp=False)
    if refused:
        return refused
    calls = rnd.telecom_state()
    if calls and (calls["calls_now"] or calls["telephony_busy"]):
        return step.finish(NOT_CHECKED, "на телефоне идёт звонок, плату сейчас не сбрасываю", "Запустить шаг после разговора.")
    ch = s3_uart.Channel()
    before = voice_check.board_status(ch) if ch.find(timeout=5.0) else None
    if before is None:
        step.say("Через S3 нет ответа, режим до сброса узнать нельзя.")
    step.say("Сбрасываю LyraT: версия видна только при загрузке. Канал и связь с телефоном вернутся сами.")
    watch = SerialWatch(oc.lyrat_port(), step.out)
    watch.pump(time.monotonic() + 8, until=lambda: watch.sp is not None)
    if watch.sp is None:
        watch.finish(0)
        ch.close()
        return step.finish(NOT_CHECKED, "порт LyraT не открылся", "Закройте программу, которая его держит, и повторите.")
    ups = watch.slc_ups
    watch.pulse_reset()
    slc = rnd.wait_slc(watch, ups, 30.0)
    lines = [line for _, line in watch.recent]
    watch.close_port()
    serial = watch.finish(0)
    version = next((m.group(1) for m in map(APP_VERSION.search, lines) if m), None)
    built = next((m.group(1).strip() for m in map(APP_TIME.search, lines) if m), None)
    elf = next((m.group(1) for m in map(APP_ELF.search, lines) if m), None)
    mac = next((m.group(1) for m in map(BOARD_MAC.search, lines) if m), None)
    sha, variant = local_build()
    same_build = bool(elf and sha and sha.startswith(elf))
    after = voice_check.wait_board(ch, 20.0)
    ch.close()
    step.num("Сборка на плате", "%s, собрана %s, ELF %s" % (version, built, elf))
    step.num("Git этой копии", git_head())
    step.num("Сборка на ПК", "ELF %s, вариант адреса: %s" % (sha[:9] if sha else None, "mac_b1" if variant == MAC_B1 else variant))
    step.num("Адрес платы", mac)
    step.num("Режим шумоподавления", "до сброса %s, после сброса %s, применён %s" % (
        before and before.get("ns"), after and after.get("ns"), after and after.get("applied")))
    step.num("Телефон после сброса", "SLC за %s с" % slc)
    faults = []
    if version is None or elf is None:
        return step.finish(NOT_CHECKED, "в логе загрузки нет версии: сброс не прошёл или порт терял данные", "Запустить шаг снова.")
    if slc is None:
        faults.append("телефон не подключился за 30 с после сброса")
    if serial["board_resets"] and serial["board_resets"] > 1:
        faults.append("плата сбрасывалась %d раз вместо одного: возможен цикл сбросов #003" % serial["board_resets"])
    if not same_build:
        faults.append("на плате не та сборка, что собрана на этом ПК: адрес mac_b1 подтвердить нечем. Другой гоал мог её сменить (062)")
    elif variant != MAC_B1:
        faults.append("плата собрана не с адресом mac_b1 (вариант %s)" % variant)
    want = oc.WANT_NS
    if before is not None and before.get("ns") != want:
        faults.append("до сброса стоял режим %s вместо %s" % (before.get("ns"), want))
    if after is None:
        faults.append("LyraT не ответила через S3 после сброса: режим шумоподавления не прочитан")
    elif after.get("ns") != want or after.get("applied") != want:
        faults.append("режим шумоподавления после сброса не %s" % want)
    if faults:
        return step.finish(FAIL, "; ".join(faults), "Вернуть рабочую прошивку: %s . Смена адреса требует пары заново, см. OWNER_CHECK.md." % oc.FLASH_BACK)
    return step.finish(PASS, "на плате сборка этого ПК с адресом mac_b1, режим %s" % want)


# --- stability, power ------------------------------------------------------------------------------------------------

def step_stable(args):
    step = Step("stable", "Плата без сбросов")
    refused = oc.refuse_if_busy(step, need_udp=False)
    if refused:
        return refused
    step.say("Смотрю плату %d с, ничего не меняю. Питание для записи в протокол: %s." % (args.seconds, args.power))
    result, rc = oc.run_round(step, "power", "--seconds", str(args.seconds), power=args.power)
    outcome, why = oc.round_outcome(result, rc)
    if result:
        step.num("Сбросы LyraT", result.get("board_resets"))
        step.num("Причины сбросов", result.get("rom_reset_reasons"))
        step.num("Обрывы порта", result.get("port_drops"))
        step.num("Обрывы связи с телефоном", result.get("slc_drops"))
    if outcome == PASS:
        return step.finish(PASS, "за %d с сбросов и обрывов нет. Напряжение этот шаг не меряет." % args.seconds)
    return step.finish(outcome, why, "Если плата перезапускается по кругу: #003, переставьте её в другой порт USB-блока или возьмите другой адаптер, и повторите.")


def step_power(args):
    step = Step("power", "Питание")
    state = REPO_DIR / "goals_harness" / "phone-then-pc" / "059-power-telemetry" / "STATE.md"
    done = False
    try:
        done = state.read_text(encoding="utf-8").startswith("state: done")
    except OSError:
        pass
    step.num("Гоал 059", "готов" if done else "не готов")
    if done:
        return step.finish(NOT_CHECKED, "гоал 059 готов, но этот шаг к нему ещё не подключён.", "Команда индикатора в отчёте гоала 059.")
    return step.finish(NOT_CHECKED, "индикатор питания делает гоал 059, он ещё не готов. Напряжение платы на стенде не измеряется.",
                       "Пока только шаг stable: он покажет перезапуски, но не запас по питанию.")


# --- Bluetooth -------------------------------------------------------------------------------------------------------

class PhoneLog:
    """The phone's Bluetooth log during a step, for the refusals round.py does not judge. No telephony lines are kept."""

    def __init__(self, step):
        import btlog
        self.btlog = btlog
        self.path = step.out / "phone_bt.log"
        self.proc = btlog.start_btlog(self.path)
        time.sleep(1.0)

    def finish(self):
        self.btlog.stop_btlog(self.proc)
        return self.btlog.scan(self.path)


def phone_log_faults(found):
    """What the log says that no stage measures. An entry on the phone costs mSBC for this address for good."""
    faults = []
    if found["entry_added"] or found["entry_used"] or found["entry_known"]:
        faults.append("телефон запретил mSBC для этого адреса: запись добавлена %d, известна %d, применена %d. Вернуть можно только "
                      "сбросом настроек Bluetooth телефона, это решаете вы (NEEDS-OWNER, пункт 21)" % (found["entry_added"], found["entry_known"], found["entry_used"]))
    if found["audio_refused"]:
        faults.append("аудиосервис телефона не принял гарнитуру %d раз (#001): канал не откроется, настоящий звонок может не перейти в гарнитуру" % found["audio_refused"])
    return faults


def step_bluetooth(args):
    import phone
    step = Step("bluetooth", "Bluetooth и кодек")
    refused = oc.refuse_if_busy(step, need_udp=False)
    if refused:
        return refused
    try:
        st = phone.status()
    except phone.PhoneError as e:
        return step.finish(NOT_CHECKED, "телефон по adb недоступен: %s" % e, "USB-кабель, отладка разрешена? Шаг ports покажет.")
    step.num("До проверки", "пара=%s гарнитура=%s аудиосервис=%s заблокирован=%s" % (st["bonded"], st["hfp_connected"], st["audio_has_headset"], st["keyguard"]))
    if st["keyguard"]:
        return step.finish(NOT_CHECKED, "телефон заблокирован", "Снимите блокировку и повторите.")
    if not st["bonded"]:
        return step.finish(FAIL, "нет пары с LyraT_Phone: канала нет", "Сопрячь: %sfw.py erase-bonds, потом %sphone.py forget и %sphone.py pair." % (oc.TOOLS_CMD, oc.TOOLS_CMD, oc.TOOLS_CMD))
    if not st["hfp_connected"]:
        return step.finish(FAIL, "телефон не подключён к LyraT_Phone как гарнитура: канала нет",
                           "Подключите в меню телефона или %sphone.py connect, потом шаг снова." % oc.TOOLS_CMD)
    if st["audio_has_headset"] is False:
        return step.finish(FAIL, "гарнитура подключена, но аудиосервис телефона её не принял (#001): канал не откроется.",
                           "Перезагрузка телефона вернула это один раз, но решением не считается (R17). Решает гоал 058. Настоящий звонок сейчас может остаться без гарнитуры.")
    log = PhoneLog(step)
    try:
        link, link_rc = oc.run_round(step, "link", "--reboots", str(args.reboots), "--pairs", "0", power=args.power)
        sco, sco_rc = oc.run_round(step, "sco", "--n", str(args.n), power=args.power)
    finally:
        found = log.finish()
    link_out, link_why = oc.round_outcome(link, link_rc)
    sco_out, sco_why = oc.round_outcome(sco, sco_rc)
    if link:
        lat = link.get("slc_latency_s") or {}
        step.num("Сброс и подключение телефона", "%s из %s, за %s-%s с" % (link.get("reboots_passed"), args.reboots, lat.get("min"), lat.get("max")))
    codecs = sco.get("codecs") if sco else None
    per = [c.get("codec") for c in (sco.get("cycles") or [])] if sco else []
    if sco:
        step.num("Открытие канала из приложения", "%s из %s, кодеки по попыткам %s" % (sco.get("cycles_passed"), args.n, per))
    step.num("Лог телефона", "запрет mSBC: добавлен %d, известен %d, применён %d; отказ аудиосервиса %d" % (found["entry_added"], found["entry_known"], found["entry_used"], found["audio_refused"]))
    faults = phone_log_faults(found)
    # round.py sco has no view on the codec: a channel on CVSD passes it.
    if sco and per and any(c != oc.WANT_CODEC for c in per):
        faults.append("кодек %s вместо %s. Голос в разговоре будет хуже (8 кГц). Либо телефон запретил mSBC для этого адреса, либо на плате не mac_b1" % (codecs, oc.WANT_CODEC))
    if link_out == FAIL:
        faults.append("подключение после сброса: %s" % link_why)
    if sco_out == FAIL:
        faults.append("канал: %s" % sco_why)
    if faults:
        return step.finish(FAIL, "; ".join(faults), "Шаг firmware покажет, что на плате. Вернуть адрес: %s , затем пара заново, см. OWNER_CHECK.md." % oc.FLASH_BACK)
    if link_out != PASS or sco_out != PASS or not per:
        return step.finish(NOT_CHECKED, "замер не состоялся: подключение: %s; канал: %s" % (link_why, sco_why), "Шаг ports покажет причину, потом шаг снова.")
    return step.finish(PASS, "подключение после сброса и канал работают, кодек %s во всех попытках, телефон ничего не запретил" % oc.WANT_CODEC)


# --- sound with tones ------------------------------------------------------------------------------------------------

def worst_snr(result, key):
    values = [e.get("snr_min_db") for e in ((result.get("worst") or {}).get(key) or {}).values()]
    return None if not values or None in values else min(values)


def step_audio(args):
    step = Step("audio", "Звук в обе стороны тоном")
    refused = oc.refuse_if_busy(step)
    if refused:
        return refused
    log = PhoneLog(step)
    try:
        step.say("Тоны ходят только через Bluetooth и шину, в наушниках LyraT громкость 0, на ПК звук не включается. Около %d с." % (args.n * 46 + 20))
        extra = ["--codec", oc.WANT_CODEC, "--n", str(args.n), "--criterion-up", "strict", "--criterion-down", "strict", "--min-snr", str(args.min_snr)]
        result, rc = oc.run_round(step, "audio", *extra, power=args.power)
    finally:
        found = log.finish()
    outcome, why = oc.round_outcome(result, rc)
    faults = phone_log_faults(found)
    if result:
        step.num("Попытки", "%s из %s" % (result.get("trials_passed"), args.n))
        step.num("Кодек по попыткам", [t.get("codec") for t in result.get("trials") or []])
        low = {k: worst_snr(result, k) for k in ("down", "up", "duplex_down", "duplex_up")}
        step.num("Худшее SNR, дБ (порог %s)" % args.min_snr, "вниз %s, вверх %s, в обе стороны разом: вниз %s, вверх %s" % (low["down"], low["up"], low["duplex_down"], low["duplex_up"]))
        if outcome == PASS and (None in low.values() or min(low.values()) < args.min_snr):
            outcome, why = FAIL, "худшее SNR ниже порога или не измерено: %s" % low
    if faults:
        outcome = FAIL
        why = "; ".join(faults + ([why] if outcome != PASS else []))
    step.say("Зелёный итог здесь не значит, что обрывов в сторону телефона нет (#004): это мерит шаг breaks.")
    if outcome == PASS:
        return step.finish(PASS, "%d из %d попыток по строгому правилу, кодек %s" % (args.n, args.n, oc.WANT_CODEC))
    return step.finish(outcome, why, "Проверьте шаг bluetooth: кодек и канал. По истории около 2 %% попыток падает из-за #004: это не случайность, а открытый дефект; повтор не делает его зелёным.")


def stream_seconds(unique):
    """Sound really received: the frame counter of a stream runs on through a pause and is filled with silence, packets do not."""
    return (unique or 0) * 240 / 16000.0


def breaks_judge(up, codec, tap_unique, seconds):
    """The result of a measured window. The down direction comes through the S3: without its stream that half is not measured,
    and a low number up must not read as a whole bench in order."""
    if up is None:
        return NOT_CHECKED, "число сбоев вверх не измерено"
    if codec != oc.WANT_CODEC:
        return FAIL, "кодек %s вместо %s: сбои не сравнимы с прежними" % (codec, oc.WANT_CODEC)
    if up > BREAKS_LIMIT:
        return FAIL, "сбоев вверх %s в минуту, выше прежнего разброса %s. Открытый дефект #004, причина не найдена." % (up, BREAKS_BEFORE)
    got = stream_seconds(tap_unique)
    if got < MIN_STREAM_SHARE * seconds:
        return NOT_CHECKED, "сбоев вверх %s в минуту, но поток S3 дал только %.1f с из %d: сторона вниз не измерена. %s" % (up, got, seconds, ASK_S3)
    return PASS, "сбоев вверх %s в минуту, не выше прежнего разброса %s" % (up, BREAKS_BEFORE)


def step_breaks(args):
    step = Step("breaks", "Обрывы в сторону телефона (#004)")
    refused = oc.refuse_if_busy(step)
    if refused:
        return refused
    log = PhoneLog(step)
    try:
        step.say("Тон в обе стороны %d с, в наушниках LyraT громкость 0, на ПК звук не включается." % args.seconds)
        result, rc = oc.run_round(step, "btloss", "--seconds", str(args.seconds), power=args.power)
    finally:
        found = log.finish()
    outcome, why = oc.round_outcome(result, rc)
    faults = phone_log_faults(found)
    window = ((result or {}).get("windows") or [None])[0] or {}
    up = window.get("up_events_per_min")
    down = window.get("down_events_per_min")
    step.num("Кодек", window.get("codec"))
    step.num("Сбоев вверх в минуту", "%s (прежний разброс %s, предел %s)" % (up, BREAKS_BEFORE, BREAKS_LIMIT))
    step.num("Сбоев вниз в минуту", down)
    step.num("Худшая секунда, дБ", "вверх %s, вниз %s" % (window.get("up_worst_s_db"), window.get("down_worst_s_db")))
    step.num("Bluetooth к плате", "без данных %s %%, с ошибками %s %%" % (window.get("rx_none_pct"), window.get("rx_err_pct")))
    step.say("Окно %d с: оценка грубая, при 0,4 в минуту это всего 2-3 сбоя." % args.seconds)
    if faults:
        return step.finish(FAIL, "; ".join(faults), "См. шаг bluetooth.")
    if outcome == FAIL:
        return step.finish(FAIL, why, "См. шаг bluetooth: канал и кодек.")
    if outcome != PASS:
        return step.finish(NOT_CHECKED, why, "Шаг ports покажет причину, потом шаг снова.")
    tap_unique = None
    try:
        tap_unique = json.loads((Path(result["dir"]) / "asis" / "window.json").read_text(encoding="utf-8"))["tap"]["unique"]
    except (OSError, ValueError, KeyError):
        pass
    step.num("Поток S3 за окно", "%.1f с из %d" % (stream_seconds(tap_unique), args.seconds))
    verdict, text = breaks_judge(up, window.get("codec"), tap_unique, args.seconds)
    if verdict == PASS:
        return step.finish(PASS, text)
    todo = {NOT_CHECKED: "Шаг ports покажет причину, потом шаг снова.",
            FAIL: "Послушайте шагом voice и в звонке, слышно ли это собеседнику. Запись через S3 этого не покажет. Причину ищет гоал 062."}[verdict]
    return step.finish(verdict, text, todo)


# --- UART ------------------------------------------------------------------------------------------------------------

def step_uart(args):
    step = Step("uart", "UART через S3")
    refused = oc.refuse_if_busy(step)
    if refused:
        return refused
    state = oc.port_state(oc.s3_port())
    if state != "free":
        # The stage restarts the S3 through its USB port.
        return step.finish(NOT_CHECKED, "порт S3 %s: %s. Шаг сбрасывает S3 через него." % (oc.s3_port(), state), "Закройте программу, которая его держит, или проверьте кабель, и повторите.")
    step.say("Около 3,5 минут: 1000 команд, сбросы LyraT и S3, линия в нуле при открытом канале. Настоящих звонков нет.")
    result, rc = oc.run_round(step, "uart", power=args.power)
    outcome, why = oc.round_outcome(result, rc)
    if result:
        c = result.get("commands") or {}
        step.num("Команды", "ошибок после повтора %s, неверных ответов %s, повторов %s" % (c.get("failed_after_retry"), c.get("wrong_replies"), c.get("retries")))
        step.num("Сбросы LyraT, сбросы S3, линия в нуле", "%s, %s, %s" % (len(result.get("lyrat_resets") or []), len(result.get("s3_restarts") or []), bool(result.get("line_low"))))
    if outcome == PASS:
        return step.finish(PASS, "команды проходят через S3 без потерь, сбросы плат звонку не мешают")
    return step.finish(outcome, why, "Если S3 не найдена, %s Описание канала: lyrat_resend/docs/S3_UART.md." % ASK_S3)


# --- recording -------------------------------------------------------------------------------------------------------

def step_stream(args):
    step = Step("stream", "Запись потока S3")
    refused = oc.refuse_if_busy(step)
    if refused:
        return refused
    step.say("Запись %d с с контрольными тонами и вторым получателем. Если на линии микрофона шум (#005), стадия сначала ждёт его конца, до 15 минут." % args.seconds)
    log = PhoneLog(step)
    try:
        result, rc = oc.run_round(step, "record", "--seconds", str(args.seconds), power=args.power)
    finally:
        found = log.finish()
    outcome, why = oc.round_outcome(result, rc)
    faults = phone_log_faults(found)
    trial = ((result or {}).get("trials") or [None])[0] or {}
    wait = (result or {}).get("mic_wait") or {}
    step.num("Ожидание конца шума на линии микрофона", "%s с" % wait.get("waited_s"))
    summary = trial.get("summary") or {}
    for name in ("call", "mic"):
        s = summary.get(name) or {}
        step.num(name, "длительность %s с при часах ПК %s с, потери %s %%, уровень в тишине %s" % (s.get("seconds"), s.get("wall_s"), s.get("loss_pct"), s.get("floor_rms")))
    tones = (trial.get("call_tones") or {}).get("tones") or {}
    snr = [t.get("snr_db") for t in tones.values() if t.get("snr_db") is not None]
    step.num("Тоны в call.wav", "минимум %s дБ, покрытие %s с" % (min(snr) if snr else None, trial.get("call_tones_cover_s")))
    step.num("Кодек", trial.get("codec"))
    step.say("mic.wav тоном не проверяется: цифрового пути на эту линию нет, звук в комнате не включается (R15). Только живость по шагу усиления. Голос проверяете вы.")
    if outcome == FAIL and result and not (result.get("setup") or {}).get("mic_quiet_at_start", True):
        why = "шум на линии микрофона не кончился за %s с (#005): запись не начиналась. " % wait.get("waited_s") + why
    if faults:
        return step.finish(FAIL, "; ".join(faults + ([why] if outcome != PASS else [])), "См. шаг bluetooth.")
    if outcome == PASS and trial.get("codec") != oc.WANT_CODEC:
        return step.finish(FAIL, "кодек %s вместо %s" % (trial.get("codec"), oc.WANT_CODEC), "См. шаг bluetooth.")
    if outcome == PASS:
        note = " Ждал конца шума %s с: шум был (#005)." % wait.get("waited_s") if (wait.get("waited_s") or 0) > 60 else ""
        return step.finish(PASS, "запись идёт без потерь, длительность сходится, тоны на месте." + note)
    return step.finish(outcome, why, "Шаг mic покажет шум. Если S3 не найдена, %s" % ASK_S3)


def noise_source(port, seconds):
    """A control: packets of noise at the level of the noisy periods, in the S3's format, to this PC only. It plays nothing."""
    import random
    import socket
    import struct

    def run():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        seq = frame = 0
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            payload = b"".join(struct.pack("<hh", random.randint(-3000, 3000), random.randint(-3000, 3000)) for _ in range(240))
            header = b"S3" + bytes([1, 1]) + struct.pack("<IIH", seq, frame, 7) + bytes([2, 0]) + struct.pack("<II", 0, 0)
            s.sendto(header + payload, ("127.0.0.1", port))
            seq += 1
            frame += 240
            time.sleep(0.015)
        s.close()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def step_mic(args):
    from udp_record import MIC_FLOOR_WARN, TapRecorder, floor_too_high
    step = Step("mic", "Тишина на линии микрофона (#005)")
    refused = oc.refuse_if_busy(step, need_lyrat=False, need_udp=not args.simulate_noise)
    if refused:
        return refused
    temp = step.out / "excerpt"
    if args.simulate_noise:
        step.say("КОНТРОЛЬ: вместо S3 шум на этом ПК. Звук не воспроизводится.")
        noise_source(6105, args.seconds + 2)
        rec = TapRecorder(("L", "LR"), listen_only=True, ports={"call": 6104, "mic": 6105}).start()
    elif args.control_no_stream:
        step.say("КОНТРОЛЬ: поток у S3 не запрашивается.")
        rec = TapRecorder(("L", "LR"), listen_only=True).start()
    else:
        rec = TapRecorder(("L", "LR")).start()
    time.sleep(args.seconds)
    summary = rec.stop().save(temp)
    mic = summary["mic"]
    # This is the room: only the numbers stay (R15).
    shutil.rmtree(temp, ignore_errors=True)
    floors = mic.get("floor_rms")
    step.num("Уровень в тихие моменты, по каналам", "%s, предел %.0f (тот же, что в round.py record), база около 130" % (floors, MIC_FLOOR_WARN))
    step.num("Длительность и потери", "%s с, %s %%" % (mic.get("seconds"), mic.get("loss_pct")))
    if not mic.get("unique") or not floors or any(f is None for f in floors):
        return step.finish(NOT_CHECKED if summary["call"]["unique"] else FAIL, "нет потока микрофона с S3: уровень не измерен. " + ASK_S3, "Включить S3, проверить кабель, повторить.")
    if stream_seconds(mic.get("unique")) < MIN_STREAM_SHARE * args.seconds:
        return step.finish(NOT_CHECKED, "поток микрофона с S3 шёл с перерывами: %.1f с из %d, уровень не на чем судить. %s" % (
            stream_seconds(mic.get("unique")), args.seconds, ASK_S3), "Проверить S3 и сеть, повторить.")
    if floor_too_high(floors):
        return step.finish(FAIL, "сильный постоянный шум на линии микрофона (#005). Это не провал записи, но разговор сейчас будет испорчен: собеседник слышит то же. "
                                 "Громкий постоянный звук в комнате выглядит так же. Причина не найдена.",
                           "Подождите: прежде проходил сам, через минуты или часы. Шаг повторять. Напишите, что включено рядом и что в USB-блоке (NEEDS-OWNER, пункт 32, 45).")
    return step.finish(PASS, "сейчас шума нет. Шум приходит сам (#005): этот итог верен только на сейчас, запустите шаг прямо перед звонком.")


def step_rehearsal(args):
    """call_record end to end, with the channel from the probe app and tones from the phone. No call."""
    import audio_check
    import numpy as np
    import round as rnd
    import s3_uart
    import wave
    import call_record
    step = Step("rehearsal", "Репетиция записи на канале из приложения (без звонка)")
    refused = oc.refuse_if_busy(step)
    if refused:
        return refused
    if rnd.SCO_APP not in rnd.adb("shell", "pm", "path", rnd.SCO_APP):
        return step.finish(NOT_CHECKED, "приложение sco_probe не найдено на телефоне, или телефон недоступен по adb",
                           "Шаг ports скажет, что именно. Установка: python lyrat_resend/tools/android/build_apk.py --install")
    calls = rnd.telecom_state()
    if calls is None or calls["calls_now"] or calls["telephony_busy"]:
        return step.finish(NOT_CHECKED, "на телефоне идёт звонок или состояние не читается", "Запустить после разговора.")
    ch = s3_uart.Channel()
    if not ch.find(timeout=5.0):
        ch.close()
        return step.finish(FAIL if not args.control_no_stream else NOT_CHECKED, "S3 не отвечает. " + ASK_S3, "Включить S3, проверить кабель, повторить.")
    rnd.app_ready()
    # No sound in the room: the phone's tones stop at the board's volume, the bus still carries them (R15).
    got = ch.command("vol 0", timeout=0.5, retries=5)
    if got["status"] != 0:
        ch.close()
        return step.finish(NOT_CHECKED, "LyraT не приняла команду vol 0 через S3", "Шаг uart покажет причину.")
    wav = step.out / rnd.RECORD_WAV
    rnd.make_tone_wav(wav, args.seconds + 20)
    tag = time.strftime("%H%M%S")
    rnd.app_command("status", tag + "-start")
    time.sleep(1.0)
    rnd.adb("push", str(wav), rnd.SCO_DIR + "/" + rnd.RECORD_WAV, timeout=120.0)
    wav.unlink(missing_ok=True)
    rows, results = [], []
    keep_awake()
    try:
        for i in range(1, args.n + 1):
            one = rehearsal_one(step, i, tag, args, rnd, call_record, audio_check, np, wave)
            rows.append(one)
            results.append(one["result"])
            step.say("Попытка %d: %s. Файл %s с, канал %s с, кодек %s, потери call %s %%, mic %s %%." % (
                i, one["result"], one["call_s"], one["channel_s"], one["codec"], one["loss_call"], one["loss_mic"]))
            for line in one["reasons"]:
                step.say("  - %s" % line)
    finally:
        ch.command("vol 100", timeout=0.5, retries=5)
        ch.close()
        rnd.adb("shell", "am", "force-stop", rnd.SCO_APP)
        rnd.adb("shell", "rm", "-f", rnd.SCO_DIR + "/" + rnd.RECORD_WAV)
    passed = sum(1 for r in rows if r["result"] == PASS)
    step.num("Попытки", "%d из %d" % (passed, args.n))
    step.num("Файл минус часы ПК, с", [r["duration_error_s"] for r in rows])
    step.num("Тоны в call.wav, минимум дБ", [r["tones_min_db"] for r in rows])
    outcome = oc.worst_of(results)
    if outcome == PASS:
        return step.finish(PASS, "запись работает на канале из приложения. Настоящим звонком не проверялось.", extra={"rows": rows})
    why = "; ".join(sorted({line for r in rows for line in r["reasons"]})) or "попытки не прошли"
    return step.finish(outcome, why, "См. шаг bluetooth и шаг mic, затем повторить.", extra={"rows": rows})


def rehearsal_one(step, i, tag, args, rnd, call_record, audio_check, np, wave):
    out = step.out / ("try-%02d" % i)
    silent = lambda text: step.log.write(text + "\n")  # noqa: E731
    rec = call_record.CallRecording(out, port=oc.lyrat_port(), grace=args.grace, say=silent, no_stream=args.control_no_stream).start()
    reasons, opened_at, closed_at = [], None, None
    started = rec.wait_first_packets()
    if not started:
        reasons.append("нет потока с S3")
    t_tag = "%s-%d" % (tag, i)
    rnd.app_command("open", t_tag + "-open")
    end = time.monotonic() + 15.0
    status = None
    while time.monotonic() < end:
        status = rnd.app_status()
        if status and status.get("id") == t_tag + "-open" and (status.get("route_confirmed") or not status.get("ok")):
            break
        rec.tick()
        time.sleep(0.2)
    route = bool(status and status.get("id") == t_tag + "-open" and status.get("ok") and status.get("route_confirmed"))
    reason = None
    if not route:
        reasons.append("канал из приложения не открылся: нет канала")
    else:
        opened_at = time.monotonic()
        rnd.app_command("play", t_tag + "-play", "--es", "wav", rnd.RECORD_WAV)
        hold = opened_at + args.seconds
        while time.monotonic() < hold and reason is None:
            reason = rec.tick()
            if args.drop_at and not getattr(rec, "dropped", False) and time.monotonic() - opened_at >= args.drop_at:
                rec.dropped = True
                inject_fault(step, rec, args.fault)
            time.sleep(0.1)
    rnd.app_command("close", t_tag + "-close")
    closed_at = time.monotonic()
    limit = closed_at + args.grace + (call_record.GRACE_AFTER_FAULT_S if args.drop_at else 0) + 15.0
    while reason is None and time.monotonic() < limit:
        reason = rec.tick()
        time.sleep(0.1)
    if reason is None:
        reasons.append("запись сама не остановилась после закрытия канала")
    s = rec.stop(reason or "предел времени репетиции")
    result, judged, _ = call_record.judge(s)
    reasons += judged
    channel_s = sum(c["seconds"] for c in s["channel"]) if s["channel"] else None
    # The app's own clock against what the recorder saw: an independent check of the times in the summary.
    held = closed_at - opened_at if opened_at else None
    if channel_s is not None and held is not None and not args.drop_at and abs(channel_s - held) > call_record.MAX_DURATION_ERROR_S + 1.0:
        reasons.append("канал по записи %.1f с, по приложению %.1f с" % (channel_s, held))
        result = FAIL
    call_path, mic_path = out / "call.wav", out / "mic.wav"
    tones_min = None
    if s["channel"] and call_path.exists() and s["files"]["call"]["bytes"]:
        first = s["channel"][0]
        data = wav_part(call_path, first, wave, np)
        if not data.size:
            # A stream that came in pieces leaves a file shorter than the channel was open.
            reasons.append("в call.wav нет участка, где был открыт канал")
            result = FAIL
        else:
            judged_tones = audio_check.check(list(map(int, data)), 16000, TONES, 20.0, 1.0)
            snr = [t.get("snr_db") for t in judged_tones["tones"].values() if t.get("snr_db") is not None]
            tones_min = min(snr) if snr else None
            if not judged_tones["pass"]:
                reasons.append("тонов нет в call.wav, минимум %s дБ" % tones_min)
                result = FAIL
            # The same check on the other line: it must fail, or the lines are swapped or shorted.
            mic_tones = audio_check.check(list(map(int, wav_part(mic_path, first, wave, np))), 16000, TONES, 20.0, 1.0) if mic_path.exists() else None
            if mic_tones and mic_tones["pass"]:
                reasons.append("тоны есть в mic.wav: линии перепутаны или замкнуты")
                result = FAIL
    if reasons and result == PASS:
        result = FAIL
    tail = s["recorder_ran_s"]
    # This is the room: nothing of the mic stays after a rehearsal (R15), and a rehearsal has no conversation to keep.
    if not args.keep:
        mic_path.unlink(missing_ok=True)
    return {"i": i, "result": result, "reasons": reasons, "call_s": s["files"]["call"]["seconds"], "mic_s": s["files"]["mic"]["seconds"],
            "channel_s": channel_s, "held_s": round(held, 2) if held else None, "codec": s["codec"],
            "loss_call": s["files"]["call"]["loss_pct"], "loss_mic": s["files"]["mic"]["loss_pct"],
            "duration_error_s": round(abs(s["files"]["call"]["seconds"] - s["files"]["call"]["wall_s"]), 3) if s["files"]["call"]["wall_s"] else None,
            "recorder_ran_s": tail, "stop": s["stop_reason"], "tones_min_db": tones_min,
            "peak_call": s["files"]["call"]["peak"], "peak_mic": s["files"]["mic"]["peak"],
            "resets": [s["lyrat_resets_console"], s["lyrat_resets_s3"], s["s3_restarts"]], "port_drops": s["console_port_drops"]}


def wav_part(path, first, wave, np):
    """The first channel of a file while the channel was open, without its first and last 2 s."""
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        start = int((first["from_s"] + 2.0) * 16000)
        if start >= w.getnframes():
            return np.zeros(0, dtype="<i2")
        w.setpos(start)
        data = np.frombuffer(w.readframes(int(max(first["seconds"] - 4.0, 0) * 16000)), dtype="<i2")
    return data[::channels]


def inject_fault(step, rec, fault):
    """Controls of a rehearsal: what a recording has to survive, done to the running recording."""
    step.log.write("fault %s\n" % fault)
    if fault == "lyrat-reset":
        rec.watch.pulse_reset()
    elif fault == "s3-restart":
        # Its USB port stays open through a chip reset on this PC, so this tests the restart, not a port that is lost.
        import s3
        w = s3.watch(step.out / "s3_console")
        w.pump(time.monotonic() + 3, until=lambda: w.sp is not None)
        s3.restart(w)
        w.close_port()
        w.finish(0)
    elif fault == "console-lost":
        # What the reader meets when the port goes: the handle dies under it. Pulling the real USB cable takes hands.
        rec.watch.sp.close()
    elif fault == "app-killed":
        import round as rnd
        rnd.adb("shell", "am", "force-stop", rnd.SCO_APP)


def step_voice(args):
    step = Step("voice", "Голос владельца: режимы до и после")
    refused = oc.refuse_if_busy(step)
    if refused:
        return refused
    cmd = [sys.executable, str(oc.TOOLS / "voice_check.py"), "--modes", args.modes, "--out", str(step.out / "voice")]
    step.say("Команда просит говорить и молчать. Звук на компьютере не включается, слушаете файлы сами.")
    rc = subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode
    report = step.out / "voice" / "voice.json"
    data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
    if rc == 2 or data is None:
        return step.finish(NOT_CHECKED, "запись не началась (код %s)" % rc, "Шаги ports и bluetooth покажут причину.")
    if rc != 0 or data.get("error") or len(data.get("results") or []) != len(args.modes.split(",")):
        return step.finish(FAIL, "запись оборвалась: %s" % data.get("error"), "Шаг bluetooth покажет канал; повторите.")
    for r in data["results"]:
        step.num(r["mode"], "кодек %s, шум до %s, после %s, подавление %s дБ, клип %s %%, шум линии %s" % (
            r["codec"], r["before_quiet_rms"], r["after_quiet_rms"], r["quiet_suppression_db"], r["after_clip_pct"], "ДА" if r["mic_noise_warning"] else "нет"))
    return step.finish(NOT_CHECKED, "пары файлов записаны, но качество голоса никакой скрипт не судит. Голосом это пока никто не проверял.",
                       "Послушайте пары в папке voice: lyrat_resend/docs/VOICE_CHECK.md, что слушать. Ответьте: NEEDS-OWNER, пункт 46.")


# --- the whole set ---------------------------------------------------------------------------------------------------

STEPS = [("ports", step_ports), ("firmware", step_firmware), ("stable", step_stable), ("power", step_power),
         ("bluetooth", step_bluetooth), ("audio", step_audio), ("breaks", step_breaks), ("uart", step_uart), ("stream", step_stream),
         ("mic", step_mic), ("rehearsal", step_rehearsal)]


def step_all(args):
    out = RUNS_ROOT / "owner" / ("%s-all" % stamp())
    out.mkdir(parents=True, exist_ok=True)
    print("Набор по порядку, около 30 минут. Каждый шаг пишет свой итог и папку. В конце таблица.", flush=True)
    rows = []
    first_ok = True
    for name, func in STEPS:
        if name != "ports" and not first_ok and name != "power":
            rows.append({"step": name, "result": NOT_CHECKED, "why": "стенд не готов: шаг ports не прошёл", "seconds": 0, "dir": ""})
            print("\n=== %s: не запускаю, шаг ports не прошёл ===" % name, flush=True)
            continue
        step_args = defaults_for(name, args)
        try:
            data = func(step_args)
        except Exception as e:      # a tool's crash is this step's answer, and the steps after it still run
            data = {"step": name, "result": NOT_CHECKED, "why": "сбой инструмента: %s: %s" % (type(e).__name__, str(e)[:160]), "seconds": 0, "dir": ""}
            print("\nИТОГ %s: %s\n  Почему: %s" % (name, NOT_CHECKED, data["why"]), flush=True)
        rows.append(data)
        if name == "ports" and data["result"] != PASS:
            first_ok = False
        print(flush=True)
    rows.append({"step": "voice", "result": NOT_CHECKED, "why": "нужен ваш голос, запускается отдельно", "seconds": 0, "dir": ""})
    rows.append({"step": "звонок", "result": NOT_CHECKED, "why": "настоящий звонок только ваш: call_record.py", "seconds": 0, "dir": ""})
    table = render_table(rows)
    print(table, flush=True)
    (out / "summary.md").write_text(table, encoding="utf-8")
    (out / "rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("Таблица: %s" % (out / "summary.md"), flush=True)
    worst = oc.worst_of([r["result"] for r in rows])
    return {"result": worst}


def render_table(rows):
    lines = ["# Итог набора %s" % stamp(), "", "| Шаг | Итог | С | Почему |", "|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %s | %s |" % (r["step"], r["result"], round(r.get("seconds") or 0), str(r["why"]).replace("|", "/")[:300]))
    lines += ["", "Открытые дефекты, зелёные шаги их не закрывают:", ""]
    lines += ["- %s: %s" % issue for issue in oc.OPEN_ISSUES]
    lines += ["", "Настоящим звонком стенд не проверялся (R3)."]
    return "\n".join(lines) + "\n"


def defaults_for(name, args):
    ns = argparse.Namespace(**vars(args))
    for key, value in DEFAULTS.get(name, {}).items():
        if getattr(ns, key, None) is None:
            setattr(ns, key, value)
    return ns


DEFAULTS = {
    "stable": {"seconds": 120},
    "bluetooth": {"reboots": 3, "n": 5},
    "audio": {"n": 10, "min_snr": 20.0},
    "breaks": {"seconds": 300},
    "stream": {"seconds": 300},
    "mic": {"seconds": TAP_MIC_S, "simulate_noise": False, "control_no_stream": False},
    "rehearsal": {"n": 1, "seconds": REHEARSAL_S, "grace": 10.0, "keep": False, "drop_at": None, "fault": None, "control_no_stream": False},
    "voice": {"modes": "off,sns,web1,web2"},
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=[name for name, _ in STEPS] + ["voice", "all"])
    ap.add_argument("--power", default="adapter", help="what feeds the LyraT, for the protocol (R11)")
    ap.add_argument("--n", type=int, help="trials: bluetooth 5, audio 10, rehearsal 1")
    ap.add_argument("--seconds", type=int, help="length: stable 120, breaks 300, stream 300, mic 10, rehearsal 30")
    ap.add_argument("--reboots", type=int, help="bluetooth: resets of the LyraT, default 3")
    ap.add_argument("--min-snr", type=float, help="audio: the limit, 20 dB; a control may raise it, never lower it")
    ap.add_argument("--modes", help="voice: the modes, default off,sns,web1,web2")
    ap.add_argument("--grace", type=float, help="rehearsal: seconds after the channel closed")
    ap.add_argument("--keep", action="store_true", default=None, help="rehearsal: keep mic.wav")
    ap.add_argument("--simulate-noise", action="store_true", default=None, help="mic, control: noise from this PC instead of the S3")
    ap.add_argument("--control-no-stream", action="store_true", default=None, help="mic, rehearsal, control: nothing asks the S3 for the stream")
    ap.add_argument("--fault", choices=["lyrat-reset", "s3-restart", "console-lost", "app-killed"], help="rehearsal, control: done to the running recording")
    ap.add_argument("--drop-at", type=float, help="rehearsal, control: seconds after the channel opened")
    args = ap.parse_args()
    oc.console_utf8()
    oc.adb_on_path()
    if args.min_snr is not None and args.min_snr < 20.0:
        print("Порог 20 дБ снижать нельзя (R12).")
        return 2
    if args.step == "all":
        return oc.EXIT[step_all(args)["result"]]
    func = dict(STEPS).get(args.step) or step_voice
    try:
        data = func(defaults_for(args.step, args))
    except Exception as e:  # a crash of a tool is no result: say so in the same form as any other, the trace stays for whoever repairs it
        import traceback
        traceback.print_exc()
        print("\nИТОГ %s: %s\n  Почему: сбой инструмента, замера нет: %s: %s\n  Что делать: запустить шаг снова; повторится, значит дефект в скрипте." % (
            args.step, NOT_CHECKED, type(e).__name__, str(e)[:200]), flush=True)
        return oc.EXIT[NOT_CHECKED]
    return oc.EXIT[data["result"]]


if __name__ == "__main__":
    sys.exit(main())
