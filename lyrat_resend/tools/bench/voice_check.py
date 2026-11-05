#!/usr/bin/env python3
"""The owner's check of the clean voice, and a voice-like test signal with the comparison of a recording with it.

  voice_check.py                       one command for a person: for each mode, speak, then stay quiet; pairs of files
  voice_check.py switches --power P    switches of the mode with the channel open, by console and through the S3
  voice_check.py inject --power P      a known signal in front of the chain, needs the test build nsinj
  voice_check.py make FILE.wav [--seconds 12] [--level 8000]
  voice_check.py check REF.wav REC.wav

make and check need only the standard library, the others numpy and the bench. Nothing here plays a sound on the PC (R15).

The owner's check needs the S3-ETH in the network, the phone over adb with LyraT_Phone connected. It writes to
bench_runs/voice/<stamp>/ for each mode <mode>_before.wav (the line of the codec captured by the S3, before any processing,
L and R averaged and multiplied by the board's mic_gain, so that the level matches) and <mode>_after.wav (what the phone got,
after the processing and the codec), both cut to the same start when they share enough sound, the shift is in voice.json.
Each mode: 3 s to settle, speech, then quiet for the noise of the room.

A pure tone flatters a concealment: a whole number of its periods fits the search window, and a repeat matches the lost
frame exactly. This signal cannot be repeated that way: a pitch that glides between 100 and 210 Hz, harmonics up to
3.4 kHz falling with frequency, syllables at about 4 per second with short pauses.

check finds the delay of the recording against the reference, follows it in windows of 250 ms (the clocks of the phone
and of the bus differ), scales the reference to the recording and cuts the residual into blocks of one frame, 7.5 ms.
Only blocks where the reference is active count. Output (JSON on stdout):
  snr_db             reference power / residual power over all active blocks; lost frames and codec noise both lower it
  block_snr_median_db, block_snr_p02_db   the blocks one by one; the low percentile is where frame losses show
  blocks_under_0db_pct   blocks where the residual is louder than the signal: what is left of a loss is audible there
  zero_runs          stretches of digital silence of at least one concealment frame where the signal is sustained
  aligned_seconds, delay_s, drift_ppm
Exit code: 0 measured, 2 not measurable. As a module: make(), compare(ref, rec, rate).
"""
import argparse
import array
import json
import math
import re
import sys
import time
import wave
from operator import mul
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_check  # noqa: E402

RATE = 16000
WINDOW_S = 0.25
SEARCH_S = 0.001            # the delay moves little from one window to the next
BLOCK_S = 0.0075
ACTIVE_FRACTION = 0.05      # a block counts when its reference rms is above this part of the overall rms
ZERO_MAX = 2                # what counts as digital silence on the bus
ZERO_RUN_S = 0.0045         # a lost CVSD frame gives 75 to 97 zero samples, the half-band filter eats the rest
SUSTAINED_FRACTION = 0.25   # zero runs count only where the signal has been going on: a loss on the very start of a
                            # syllable has nothing to be concealed from, and silence is what a concealment leaves there
DECIMATE = 8


def make(seconds=12.0, level=8000):
    """The samples of the test signal at RATE, peak at level."""
    n = int(seconds * RATE)
    out = [0.0] * n
    phase = 0.0
    for i in range(n):
        t = i / RATE
        f0 = 155 + 55 * math.sin(2 * math.pi * 0.7 * t) * math.cos(2 * math.pi * 0.23 * t)
        phase += 2 * math.pi * f0 / RATE
        env = max(0.0, math.sin(2 * math.pi * 2.1 * t)) ** 0.5 * (0.7 + 0.3 * math.sin(2 * math.pi * 0.37 * t))
        v = 0.0
        if env > 0:
            for k in range(1, int(3400 / f0) + 1):
                v += math.sin(k * phase) / k
        out[i] = env * v
    peak = max(abs(v) for v in out) or 1.0
    return [int(level * v / peak) for v in out]


def write_wav(path, samples, rate=RATE):
    data = array.array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def decimate(x, m):
    return [sum(x[i:i + m]) / m for i in range(0, len(x) - m + 1, m)]


def coarse_delay(ref, rec):
    """Where the start of ref lies in rec, in samples of the full rate; rough, by correlation at a low rate."""
    a, b = decimate(ref, DECIMATE), decimate(rec, DECIMATE)
    size = 1
    while size < len(a) + len(b):
        size *= 2
    fa = audio_check.fft([complex(v) for v in a] + [0j] * (size - len(a)))
    fb = audio_check.fft([complex(v) for v in b] + [0j] * (size - len(b)))
    # Inverse transform through the forward one: conjugate on the way in, the real part stays.
    cc = audio_check.fft([(x.conjugate() * y).conjugate() for x, y in zip(fa, fb)])
    best = max(range(size), key=lambda k: cc[k].real)
    lag = best if best < size // 2 else best - size
    return lag * DECIMATE


def interp(x, pos):
    """Catmull-Rom interpolation at a fractional index."""
    i = int(math.floor(pos))
    if i < 1 or i + 2 >= len(x):
        return 0.0
    t = pos - i
    a, b, c, d = x[i - 1], x[i], x[i + 1], x[i + 2]
    return b + 0.5 * t * (c - a + t * (2 * a - 5 * b + 4 * c - d + t * (3 * (b - c) + d - a)))


def compare(ref, rec, rate=RATE):
    window = int(WINDOW_S * rate)
    search = int(SEARCH_S * rate)
    block = int(round(BLOCK_S * rate))
    delay = coarse_delay(ref, rec)
    start_delay = delay
    residuals = []
    powers = []
    active_ref = []
    zero_runs = 0
    run = 0
    used_windows = 0
    overall = math.sqrt(sum(v * v for v in ref) / max(len(ref), 1)) or 1.0
    threshold = (ACTIVE_FRACTION * overall) ** 2 * block
    sustained = (SUSTAINED_FRACTION * overall) ** 2 * block
    was_sustained = False
    last_delay = float(delay)
    for at in range(0, len(ref) - window, window):
        rec_at = at + delay
        if rec_at - search < 2 or rec_at + window + search + 2 >= len(rec):
            continue
        segment = ref[at:at + window]
        energy = sum(map(mul, segment, segment))
        if energy < threshold:
            continue
        # The window's own delay: strongest correlation, then a parabola for the fraction.
        corr = []
        for d in range(-search, search + 1):
            corr.append(sum(map(mul, segment, rec[rec_at + d:rec_at + d + window])))
        k = max(range(len(corr)), key=corr.__getitem__)
        frac = 0.0
        if 0 < k < len(corr) - 1:
            den = corr[k - 1] - 2 * corr[k] + corr[k + 1]
            frac = 0.5 * (corr[k - 1] - corr[k + 1]) / den if den else 0.0
        shift = k - search + frac
        delay += k - search     # follows the drift, the next window starts from here
        last_delay = delay - (k - search) + shift
        # Reference on the time grid of the recording.
        aligned = [interp(ref, j - shift) for j in range(at, at + window)]
        chunk = rec[rec_at + 0:rec_at + window]
        den = sum(map(mul, aligned, aligned))
        gain = sum(map(mul, aligned, chunk)) / den if den else 0.0
        if gain <= 0:
            continue    # the very end of a track: a whisper of the reference against nothing recorded
        used_windows += 1
        for j in range(0, window - block + 1, block):
            a = [gain * v for v in aligned[j:j + block]]
            b = chunk[j:j + block]
            p = sum(map(mul, a, a))
            strong = p >= sustained * gain * gain
            if not (strong and was_sustained):
                # A pause or a start of the signal says nothing about a lost frame.
                if run >= ZERO_RUN_S * rate:
                    zero_runs += 1
                run = 0
            else:
                for v in b:
                    if -ZERO_MAX <= v <= ZERO_MAX:
                        run += 1
                    else:
                        if run >= ZERO_RUN_S * rate:
                            zero_runs += 1
                        run = 0
            was_sustained = strong
            if p < threshold * gain * gain:
                continue
            r = sum((x - y) ** 2 for x, y in zip(a, b))
            powers.append(p)
            residuals.append(r)
    if not powers:
        return {"error": "no active blocks aligned", "delay_s": round(start_delay / rate, 3)}
    snrs = sorted(10 * math.log10(p / r) if r > 0 else 99.0 for p, r in zip(powers, residuals))
    total = sum(residuals)
    return {
        "snr_db": round(10 * math.log10(sum(powers) / total), 2) if total > 0 else 99.0,
        "block_snr_median_db": round(snrs[len(snrs) // 2], 1),
        "block_snr_p02_db": round(snrs[len(snrs) // 50], 1),
        "blocks_under_0db_pct": round(100.0 * sum(1 for v in snrs if v < 0) / len(snrs), 3),
        "blocks": len(snrs),
        "zero_runs": zero_runs,
        "aligned_seconds": round(used_windows * WINDOW_S, 2),
        "delay_s": round(start_delay / rate, 4),
        "drift_ppm": round((last_delay - start_delay) / max(used_windows, 1) / window * 1e6, 1),
    }


# ---------------------------------------------------------------------------
# The owner's check (no arguments), the switch test and the known signal. These parts need the bench and numpy.
# ---------------------------------------------------------------------------

OWNER_MODES = ("off", "sns", "web1", "web2")    # what a person compares; goal 057 DECISIONS says why these
ALL_MODES = ("off", "sns", "web0", "web1", "web2")
SPEAK_S = 15
QUIET_S = 10
SETTLE_S = 3
PHONE_SOURCE = "raw"        # no processing of the platform, as round.py audio
ALIGN_MIN_SCORE = 0.15      # normalized correlation of before and after; below it there is no shared sound to align on
RUN_DIR = "voice"


def _bench():
    """The bench modules, imported only when the bench is used: make and check stay standard library only."""
    import round as rnd
    import s3_uart
    from benchlib import RUNS_ROOT, stamp
    from udp_record import MIC_FLOOR_WARN, TapRecorder
    import tap_check
    return rnd, s3_uart, RUNS_ROOT, stamp, TapRecorder, MIC_FLOOR_WARN, tap_check


STATUS_NS = re.compile(r"ns=(\S+) applied=(\S+) switches=(\d+) sns_cal=(\d)")
STATUS_TIME = re.compile(r"ns_time frames=(\d+) avg_us=(\d+) max_us=(\d+) under=(\d+) fifo=(-?\d+)\.\.(-?\d+)")


def board_status(ch):
    """What the board says through the S3, with the fields this tool uses; None without an answer."""
    got = ch.command("status", timeout=0.5, retries=5)
    if got["status"] != 0:
        return None
    text = got["text"]
    r = {"text": text}
    m = STATUS_NS.search(text)
    if m:
        r.update(ns=m.group(1), applied=m.group(2), switches=int(m.group(3)), sns_cal=int(m.group(4)))
    m = STATUS_TIME.search(text)
    if m:
        r.update(zip(("ns_frames", "ns_avg_us", "ns_max_us", "ns_under", "fifo_min", "fifo_max"), map(int, m.groups())))
    m = re.search(r"SCO\s+= (\w+) bt_rate=(\d+)", text)
    if m:
        r.update(sco=m.group(1), bt_rate=int(m.group(2)))
    m = re.search(r"SLC\s+= (\w+)", text)
    if m:
        r["slc"] = m.group(1)
    m = re.search(r"mic_gain\s+= (\d+)", text)
    if m:
        r["mic_gain"] = int(m.group(1))
    return r


def wait_board(ch, timeout=30.0):
    """The status of a board that may have just restarted: the S3 opens its TX only after it heard the board, the phone
    connects some 10 s after the boot."""
    end = time.monotonic() + timeout
    while True:
        st = board_status(ch)
        if (st and "ns" in st and st.get("slc") == "up") or time.monotonic() > end:
            return st
        time.sleep(1.0)


def board_cmd(ch, text, expect=None):
    got = ch.command(text, timeout=0.5, retries=5)
    if got["status"] != 0:
        raise RuntimeError("the board did not take '%s': %s" % (text, got))
    if expect and expect not in got["text"]:
        raise RuntimeError("the board answered '%s' to '%s'" % (got["text"].strip(), text))
    return got["text"]


def set_mode(ch, mode):
    board_cmd(ch, "ns " + mode, "ns = " + mode)


def wait_applied(ch, mode, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = board_status(ch)
        if st and st.get("applied") == mode:
            return st
        time.sleep(0.05)
    return None


def app_wait(rnd, cmd_id, done, timeout):
    end = time.monotonic() + timeout
    status = None
    while time.monotonic() < end:
        status = rnd.app_status()
        if status and status.get("id") == cmd_id and (not status.get("ok") or done(status)):
            return status
        time.sleep(0.3)
    return status


def open_channel(rnd, tag):
    rnd.app_ready()
    open_id = tag + "-open"
    rnd.app_command("open", open_id)
    status = app_wait(rnd, open_id, lambda s: s.get("route_confirmed"), 20.0)
    if not status or status.get("id") != open_id or not status.get("route_confirmed"):
        raise RuntimeError("the channel did not open: %s" % (status and status.get("error")))


def close_channel(rnd, tag):
    close_id = tag + "-close"
    rnd.app_command("close", close_id)
    app_wait(rnd, close_id, lambda s: not s.get("opened", True), 10.0)
    rnd.adb("shell", "am", "force-stop", rnd.SCO_APP)


def phone_record_start(rnd, name, seconds, tag):
    rec_id = "%s-%s" % (tag, name)
    rnd.app_command("record", rec_id, "--ei", "seconds", str(seconds), "--es", "out", name, "--es", "source", PHONE_SOURCE)
    status = app_wait(rnd, rec_id, lambda s: (s.get("record") or {}).get("id") == rec_id, 10.0)
    if not status or status.get("id") != rec_id or not status.get("ok"):
        raise RuntimeError("the phone did not start to record: %s" % (status and status.get("error")))
    return rec_id


def phone_record_end(rnd, rec_id, name, seconds, path):
    status = app_wait(rnd, rec_id, lambda s: (s.get("record") or {}).get("state") not in (None, "recording"), seconds + 15.0)
    record = (status or {}).get("record") or {}
    if record.get("state") != "done":
        raise RuntimeError("the phone did not finish the recording: %s" % record)
    rnd.adb("pull", rnd.SCO_DIR + "/" + name, str(path), timeout=60.0)
    rnd.adb("shell", "rm", "-f", rnd.SCO_DIR + "/" + name)
    if not Path(path).exists():
        raise RuntimeError("the recording did not come from the phone")
    return record


def align(before, after):
    """Where the start of `after` lies in `before`, in samples, with the normalized correlation at that point."""
    import numpy as np
    a = np.asarray(after, dtype=float)
    b = np.asarray(before, dtype=float)
    if len(a) < 1000 or len(b) < 1000:
        return None, 0.0
    size = 1
    while size < len(a) + len(b):
        size *= 2
    cc = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    cc = np.concatenate((cc[-(len(a) - 1):], cc[:len(b)]))      # lags from -(len(a)-1) to len(b)-1
    best = int(np.argmax(np.abs(cc)))
    lag = best - (len(a) - 1)
    overlap_b = b[max(lag, 0):lag + len(a)]
    overlap_a = a[max(-lag, 0):max(-lag, 0) + len(overlap_b)]
    denom = float(np.sqrt(np.dot(overlap_a, overlap_a) * np.dot(overlap_b, overlap_b)))
    score = float(abs(np.dot(overlap_a, overlap_b)) / denom) if denom else 0.0
    return lag, score


def rms(x):
    import numpy as np
    x = np.asarray(x, dtype=float)
    return round(float(np.sqrt(np.mean(x * x))), 1) if len(x) else None


def one_mode(mode, ch, rnd, TapRecorder, tap_check, out, tag, speak_s, quiet_s, floor_warn, mic_gain):
    """A person speaks, then is quiet; the S3 and the phone record it at the same time."""
    import numpy as np
    print("\n=== Режим %s. Молчите %d с: подавление привыкает к комнате ===" % (mode, SETTLE_S), flush=True)
    set_mode(ch, mode)
    st = wait_applied(ch, mode)
    if not st:
        raise RuntimeError("the board did not apply mode %s" % mode)
    time.sleep(SETTLE_S)
    seconds = speak_s + quiet_s
    work = out / ("_%s" % mode)
    rec = TapRecorder(("L", "LR")).start()
    time.sleep(0.5)
    name = "voice_%s.wav" % mode
    rec_id = phone_record_start(rnd, name, seconds, tag)
    print(">>> ГОВОРИТЕ %d с (режим %s)" % (speak_s, mode), flush=True)
    time.sleep(speak_s)
    print(">>> МОЛЧИТЕ %d с (режим %s): это замер шума комнаты" % (quiet_s, mode), flush=True)
    time.sleep(quiet_s)
    after_raw = out / ("%s_after.wav" % mode)
    record = phone_record_end(rnd, rec_id, name, seconds, after_raw)
    time.sleep(0.3)
    summary = rec.stop().save(work)
    channels = tap_check.read_channels(work / "mic.wav")
    mono = np.mean(np.asarray(channels[:2], dtype=float), axis=0)  # what the firmware does first
    before = np.clip(mono * mic_gain, -32768, 32767)                # the level of the same sound in "after"
    after, rate = audio_check.read_wav(after_raw)
    after = np.asarray(after, dtype=float)
    lag, score = align(before, after)
    aligned = lag is not None and score >= ALIGN_MIN_SCORE and lag >= 0
    if aligned:
        before = before[lag:]
    else:
        # Without a shared sound: the phone started about this long after the S3, a rough figure from the two clocks.
        before = before[int(0.5 * RATE):]
    length = min(len(before), len(after))
    before, after_cut = before[:length], after[:length]
    write_wav(out / ("%s_before.wav" % mode), [int(v) for v in before], RATE)
    write_wav(after_raw, [int(v) for v in after_cut], RATE)
    speak_end = min(int(speak_s * RATE), length)
    quiet_start = min(speak_end + RATE, length)     # the last words and the delay of the path stay out
    result = {
        "mode": mode, "phone_source": PHONE_SOURCE, "bt_rate": st.get("bt_rate"),
        "codec": {16000: "mSBC", 8000: "CVSD"}.get(st.get("bt_rate")),
        "aligned": aligned, "shift_s": round(lag / RATE, 4) if lag is not None else None, "align_score": round(score, 3),
        "seconds": round(length / RATE, 2),
        "before_speech_rms": rms(before[:speak_end]), "after_speech_rms": rms(after_cut[:speak_end]),
        "before_quiet_rms": rms(before[quiet_start:]), "after_quiet_rms": rms(after_cut[quiet_start:]),
        "after_peak": int(np.max(np.abs(after_cut))) if length else None,
        "before_clip_pct": round(100.0 * float(np.mean(np.abs(before) >= 32767)), 3) if length else None,
        "after_clip_pct": round(100.0 * float(np.mean(np.abs(after_cut) >= 32767)), 3) if length else None,
        "tap_loss_pct": summary["mic"].get("loss_pct"), "tap_floor_rms": summary["mic"].get("floor_rms"),
        "phone_record": {k: record.get(k) for k in ("frames", "peak", "rms")},
    }
    a, b = result["before_quiet_rms"], result["after_quiet_rms"]
    result["quiet_suppression_db"] = round(20 * math.log10(a / b), 1) if a and b else None
    floors = summary["mic"].get("floor_rms") or []
    result["mic_noise_warning"] = any(f is not None and f > floor_warn for f in floors)
    for f in ("mic.wav", "call.wav", "tap.json"):
        (work / f).unlink(missing_ok=True)
    work.rmdir()
    st = board_status(ch) or {}
    result["board"] = {k: st.get(k) for k in ("ns_frames", "ns_avg_us", "ns_max_us", "ns_under", "fifo_min", "fifo_max")}
    return result


def print_table(results):
    head = ("режим", "кодек", "речь до", "речь после", "шум до", "шум после", "подавл. дБ", "пик", "клип %", "сдвиг с", "время кадра мс")
    rows = [head]
    for r in results:
        rows.append((r["mode"], r["codec"], r["before_speech_rms"], r["after_speech_rms"], r["before_quiet_rms"],
                     r["after_quiet_rms"], r["quiet_suppression_db"], r["after_peak"], r["after_clip_pct"],
                     r["shift_s"] if r["aligned"] else "нет", round((r["board"].get("ns_avg_us") or 0) / 1000.0, 1)))
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(head))]
    for row in rows:
        print("  ".join(str(v).rjust(w) for v, w in zip(row, widths)), flush=True)


def owner_run(args):
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    rnd, s3_uart, RUNS_ROOT, stamp, TapRecorder, floor_warn, tap_check = _bench()
    out = Path(args.out) if args.out else RUNS_ROOT / RUN_DIR / stamp()
    out.mkdir(parents=True, exist_ok=True)
    tag = time.strftime("%H%M%S")
    ch = s3_uart.Channel()
    if not ch.find():
        print("S3-ETH не найдена в сети. Плата S3 включена и в кабеле?")
        return 2
    first = wait_board(ch)
    if not first or "ns" not in first:
        print("LyraT не отвечает через S3, или на ней старая прошивка без команды ns. Ответ: %s" % first)
        return 2
    if first.get("slc") != "up":
        print("Телефон не подключён к LyraT_Phone (SLC down). Подключите и запустите снова.")
        return 2
    mode_was = first["ns"]
    results = []
    report = {"modes": args.modes, "speak_s": args.seconds, "quiet_s": args.quiet, "phone_source": PHONE_SOURCE,
              "before_gain": first.get("mic_gain"),
              "before_note": "line of the codec, L and R averaged, times the board's mic_gain so that the level matches after",
              "sns_profile": "kept from the boot" if args.keep_profile else "taken at the start of this run"}
    print("Папка записи: %s" % out, flush=True)
    print("Открываю канал без звонка. Звук на компьютере не включается, слушать файлы будете сами.", flush=True)
    code = 0
    opened = False
    try:
        open_channel(rnd, tag)
        opened = True
        time.sleep(1.0)
        now = board_status(ch)
        if not now or now.get("sco") != "active":
            raise RuntimeError("the channel is open on the phone but not on the board: %s" % (now and now.get("sco")))
        print("Кодек: %s" % {16000: "mSBC", 8000: "CVSD"}.get(now.get("bt_rate")), flush=True)
        if not args.keep_profile:
            print("Молчите 2 с: sns снимает профиль шума", flush=True)
            board_cmd(ch, "nscal", "nscal = 1")
            time.sleep(2.0)
        for mode in args.modes:
            results.append(one_mode(mode, ch, rnd, TapRecorder, tap_check, out, tag, args.seconds, args.quiet, floor_warn,
                                    first.get("mic_gain") or 1))
    except (RuntimeError, OSError) as e:
        report["error"] = str(e)
        print("ОШИБКА: %s" % e, flush=True)
        code = 1
    finally:
        try:
            set_mode(ch, mode_was)
        except RuntimeError as e:
            report["restore_error"] = str(e)
            print("Режим не вернулся: %s" % e, flush=True)
        if opened:
            close_channel(rnd, tag)
        ch.close()
    report["results"] = results
    (out / "voice.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if results:
        print("", flush=True)
        print_table(results)
        for r in results:
            if r["mic_noise_warning"]:
                print("ВНИМАНИЕ: в режиме %s на линии микрофона был сильный постоянный шум (%s). Либо громко в комнате, "
                      "либо период шума из NEEDS-OWNER, пункт 32: эту запись не считать оценкой режима." % (
                          r["mode"], r["tap_floor_rms"]), flush=True)
            if not r["aligned"]:
                print("Режим %s: до и после не выровнены, общего звука мало (совпадение %s)." % (r["mode"], r["align_score"]), flush=True)
        print("\nСлушайте по парам в папке:\n  %s\n<режим>_before.wav это микрофон до обработки, <режим>_after.wav то, что "
              "дошло до телефона." % out, flush=True)
    return code


# ---------------------------------------------------------------------------
# 50 and more switches with the channel open
# ---------------------------------------------------------------------------

def switch_test(args):
    import numpy as np
    from serial_watch import SerialWatch
    rnd, s3_uart, RUNS_ROOT, stamp, TapRecorder, floor_warn, tap_check = _bench()
    out = RUNS_ROOT / RUN_DIR / (stamp() + "-switches" + ("-" + args.label if args.label else ""))
    out.mkdir(parents=True, exist_ok=True)
    tag = time.strftime("%H%M%S")
    ch = s3_uart.Channel()
    if not ch.find():
        print("no S3")
        return 2
    watch = SerialWatch(args.port, out, False)
    watch.pump(time.monotonic() + 10, until=lambda: watch.sp is not None)
    if watch.sp is None:
        print("console port is not free")
        return 2
    report = {"power": args.power, "n": args.n, "period_s": args.period, "modes": args.modes, "steps": [], "checks": {}}
    wrong = []
    resets_before = len(watch.resets)
    slc_drops_before = watch.slc_drops
    first = wait_board(ch)
    if not first or "switches" not in first:
        print("no status: %s" % first)
        return 2
    start_count = first["switches"]
    mode_was = first["ns"]
    open_channel(rnd, tag)
    watch.pump(time.monotonic() + 1.0)
    name = "switches.wav"
    seconds = int(args.n * args.period + 6)
    rec_id = phone_record_start(rnd, name, seconds, tag)
    bus = None
    try:
        if args.inject:     # test build: the bus shows the output to the sample, a step of a switch cannot hide there
            board_cmd(ch, "vol 0", "volume = 0")
            board_cmd(ch, "inj %d 100" % args.inject, "inj = %d" % args.inject)
            bus = TapRecorder(("LR", "L")).start()
        watch.pump(time.monotonic() + 2.0)
        modes = [m for m in args.modes]
        current = first["ns"]
        for i in range(args.n):
            mode = modes[i % len(modes)]
            if mode == current:     # a request for the mode already on is no switch: the counter must not move
                mode = modes[(i + 1) % len(modes)]
            via = "s3" if i % 2 == 0 else "console"
            t0 = time.monotonic()
            if via == "s3":
                reply = ch.command("ns " + mode, timeout=0.5, retries=5)
                ok = reply["status"] == 0 and ("ns = " + mode) in reply["text"]
            else:
                since = watch.now()
                watch.send("ns " + mode)
                seen = rnd.wait_line(watch, since, "ns = " + mode, 3.0)
                ok = seen is not None
            st = None
            if ok:
                st = wait_applied(ch, mode)
            step = {"i": i, "mode": mode, "via": via, "reply_ok": ok, "applied": bool(st), "count": st and st.get("switches"),
                    "sco": st and st.get("sco"), "slc": st and st.get("slc"), "ms": round((time.monotonic() - t0) * 1000.0)}
            report["steps"].append(step)
            if not (ok and st and st.get("sco") == "active" and st.get("slc") == "up"):
                wrong.append(step)
            current = mode
            watch.pump(t0 + args.period)
    finally:
        record = phone_record_end(rnd, rec_id, name, seconds, out / "phone.wav")
        last = board_status(ch)
        if bus:
            bus_summary = bus.stop().save(out / "_bus")
            left, right = tap_check.read_channels(out / "_bus" / "call.wav")
            for f in ("mic.wav", "call.wav", "tap.json"):
                (out / "_bus" / f).unlink(missing_ok=True)
            (out / "_bus").rmdir()
            board_cmd(ch, "inj 0", "inj = 0")
            board_cmd(ch, "vol 100", "volume = 100")
        try:
            set_mode(ch, mode_was)
        except RuntimeError as e:
            report["restore_error"] = str(e)
        close_channel(rnd, tag)
        ch.close()
    watch.pump(time.monotonic() + 2)
    x = np.asarray(audio_check.read_wav(out / "phone.wav")[0], dtype=float)
    (out / "phone.wav").unlink(missing_ok=True)     # the room is in it: only the numbers stay
    dx = np.abs(np.diff(x))
    report["phone"] = {"record": {k: record.get(k) for k in ("frames", "peak", "rms")}, "seconds": round(len(x) / RATE, 1),
                       "peak": int(np.max(np.abs(x))), "max_step": int(np.max(dx)), "full_scale_samples": int(np.sum(np.abs(x) >= 32000))}
    report["last_status"] = {k: last.get(k) for k in ("ns", "applied", "switches", "sco", "slc")} if last else None
    summary = watch.finish(0)
    report["serial"] = {k: summary.get(k) for k in ("board_resets", "panics", "slc_drops", "port_drops")}
    done = last and last.get("switches", 0) - start_count
    c = report["checks"]
    c["all_steps_ok"] = not wrong
    c["count_matches"] = done == args.n
    c["no_reset"] = len(watch.resets) == resets_before
    c["no_slc_drop"] = watch.slc_drops == slc_drops_before
    c["no_full_scale"] = report["phone"]["full_scale_samples"] == 0
    c["at_least_50"] = args.n >= 50
    if bus:
        left, right = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
        # The largest step a linear chain could give. On the signal it is large, on the noise a step of a switch stands out.
        limit = (last.get("mic_gain") or 1) * float(np.max(np.abs(np.diff(left))))
        report["bus"] = {"seconds": round(len(right) / RATE, 1), "gaps": len(bus_summary["call"].get("gaps") or []),
                         "peak": int(np.max(np.abs(right))), "max_step": int(np.max(np.abs(np.diff(right)))), "step_limit": round(limit)}
        c["bus_no_step"] = report["bus"]["max_step"] <= limit
    report["counted"] = done
    report["wrong"] = wrong[:10]
    report["verdict"] = "PASS" if all(c.values()) else "FAIL"
    (out / "round.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    brief = {k: report.get(k) for k in ("verdict", "checks", "counted", "phone", "bus", "serial", "last_status")}
    print(json.dumps(brief, ensure_ascii=False, indent=1))
    print(out)
    return 0 if report["verdict"] == "PASS" else 1


# ---------------------------------------------------------------------------
# The known signal: needs the test build fw.py --variant ...+nsinj, and +afe for the modes afe, webo
# ---------------------------------------------------------------------------

PERIOD = 6 * RATE           # length of the board's table
BLOCK = int(0.020 * RATE)
ONSET = int(0.030 * RATE)
AFTER_END = int(0.100 * RATE)


def inj_capture(ch, TapRecorder, tap_check, what, seconds, skip, work, noise=None):
    """Bus left is what went in, bus right what came out. Returns both after the first `skip` seconds."""
    import numpy as np
    board_cmd(ch, "inj %d%s" % (what, " %d" % noise if noise else ""), "inj = %d" % what)
    rec = TapRecorder(("LR", "L")).start()
    time.sleep(seconds + 0.2)
    summary = rec.stop().save(work)
    left, right = tap_check.read_channels(work / "call.wav")
    stats = summary["call"]
    for f in ("mic.wav", "call.wav", "tap.json"):
        (work / f).unlink(missing_ok=True)
    start = int(skip * RATE)
    return np.asarray(left[start:], dtype=float), np.asarray(right[start:], dtype=float), {
        "loss_pct": stats.get("loss_pct"), "gaps": len(stats.get("gaps") or []), "channels": stats.get("channels")}


def circular_offset(x, table):
    """Offset k with x[t] close to table[(t + k) % PERIOD]."""
    import numpy as np
    n = len(table)
    spec = np.fft.rfft(table) * np.conj(np.fft.rfft(x[:n]))
    return int(np.argmax(np.fft.irfft(spec, n)))


def path_lag(a, b, limit=8000):
    """Samples by which b lags a."""
    import numpy as np
    n = min(len(a), len(b))
    size = 1
    while size < 2 * n:
        size *= 2
    cc = np.fft.irfft(np.conj(np.fft.rfft(a[:n], size)) * np.fft.rfft(b[:n], size), size)
    return int(np.argmax(cc[:limit]))


def analyse_voice(left, right, table):
    """Signal plus noise run: how much of the signal comes through, how much of the noise stays in the pauses."""
    import numpy as np
    n = min(len(left), len(right))
    left, right = left[:n], right[:n]
    k = circular_offset(left, table)
    lag = path_lag(left, right)
    idx = (np.arange(n) - lag + k) % len(table)
    s = table[idx]
    active = np.abs(s) > 0
    # samples within 50 ms of signal do not count as a pause
    win = np.ones(int(0.05 * RATE))
    near = np.convolve(active.astype(float), win, "same") > 0
    use = np.arange(n) >= lag
    a = active & use
    g = float(np.dot(right[a], s[a]) / max(np.dot(s[a], s[a]), 1.0))
    residual = right[a] - g * s[a]
    r = {"lag_samples": lag, "lag_ms": round(lag * 1000.0 / RATE, 1), "signal_gain": round(g, 3),
         "signal_gain_db": round(20 * math.log10(abs(g)), 2) if g else None,
         "residual_db": round(10 * math.log10(float(np.mean(residual ** 2)) / max(float(np.mean((g * s[a]) ** 2)), 1e-9)), 2)}
    # starts of syllables: signal after at least 50 ms without it
    edges = np.flatnonzero(active[1:] & ~active[:-1]) + 1
    starts = [e for e in edges if e >= 800 and not active[e - 800:e].any() and e + ONSET < n]
    if starts:
        on = np.concatenate([np.arange(e, e + ONSET) for e in starts])
        go = float(np.dot(right[on], s[on]) / max(np.dot(s[on], s[on]), 1.0))
        r["onset_gain"] = round(go, 3)
        r["onset_count"] = len(starts)
    pause = (~near) & use
    if pause.any():
        r["pause_rms"] = round(float(np.sqrt(np.mean(right[pause] ** 2))), 1)
        r["pause_samples"] = int(pause.sum())
    ends = np.flatnonzero(~active[1:] & active[:-1]) + 1
    tail = [np.arange(e, e + AFTER_END) for e in ends if e + AFTER_END < n and not active[e:e + AFTER_END].any()]
    if tail:
        t = np.concatenate(tail)
        r["after_end_rms"] = round(float(np.sqrt(np.mean(right[t] ** 2))), 1)
    r["output_peak"] = int(np.max(np.abs(right)))
    return r


def analyse_clean(left, right, table):
    """Signal without noise: the distortion alone."""
    import numpy as np
    n = min(len(left), len(right))
    left, right = left[:n], right[:n]
    k = circular_offset(left, table)
    lag = path_lag(left, right)
    s = table[(np.arange(n) - lag + k) % len(table)]
    active = (np.abs(s) > 0) & (np.arange(n) >= lag)
    g = float(np.dot(right[active], s[active]) / max(np.dot(s[active], s[active]), 1.0))
    e = right[active] - g * s[active]
    return {"gain": round(g, 3), "sdr_db": round(10 * math.log10(float(np.mean((g * s[active]) ** 2)) / max(float(np.mean(e ** 2)), 1e-9)), 2)}


def analyse_noise(right):
    """Noise only: its level, and how much that level moves from one 32 ms to the next (musical noise shows there)."""
    import numpy as np
    frame = int(0.032 * RATE)
    count = len(right) // frame
    levels = np.sqrt(np.mean(right[:count * frame].reshape(count, frame) ** 2, axis=1))
    db = 20 * np.log10(np.maximum(levels, 0.5))
    return {"rms": round(float(np.sqrt(np.mean(right ** 2))), 1), "level_std_db": round(float(np.std(db)), 2),
            "step_std_db": round(float(np.std(np.diff(db))), 2)}


def inject_stage(args):
    import numpy as np
    from serial_watch import SerialWatch
    rnd, s3_uart, RUNS_ROOT, stamp, TapRecorder, floor_warn, tap_check = _bench()
    out = RUNS_ROOT / RUN_DIR / (stamp() + "-inject" + ("-" + args.label if args.label else ""))
    out.mkdir(parents=True, exist_ok=True)
    tag = time.strftime("%H%M%S")
    work = out / "_work"
    ch = s3_uart.Channel()
    if not ch.find():
        print("no S3")
        return 2
    watch = SerialWatch(args.port, out, False)
    watch.pump(time.monotonic() + 10, until=lambda: watch.sp is not None)
    if watch.sp is None:
        print("console port is not free")
        return 2
    first = wait_board(ch)
    if not first or "ns" not in first:
        print("no status: %s" % first)
        return 2
    mode_was = first["ns"]
    report = {"power": args.power, "modes": args.modes, "mic_gain": first.get("mic_gain"), "results": {}}
    resets = len(watch.resets)
    try:
        board_cmd(ch, "vol 0", "volume = 0")    # R15: the bus carries test sound, the volume acts behind it
        open_channel(rnd, tag)
        now = board_status(ch)
        report["bt_rate"] = now and now.get("bt_rate")
        # The clean signal, only to know what went in.
        print("table", flush=True)
        set_mode(ch, "off")
        end = time.monotonic() + 60     # the board builds its table after boot
        while "inj = 3" not in board_cmd(ch, "inj 3") and time.monotonic() < end:
            time.sleep(1.0)
        left, _, stats = inj_capture(ch, TapRecorder, tap_check, 3, 13.0, 1.0, work)
        if stats["gaps"]:
            left, _, stats = inj_capture(ch, TapRecorder, tap_check, 3, 13.0, 1.0, work)
        table = left[:PERIOD]
        report["table"] = {"rms": rms(table), "peak": int(np.max(np.abs(table))), "active_pct": round(100 * float(np.mean(table != 0)), 1), "capture": stats}
        for mode in args.modes:
            r = {}
            print("mode", mode, flush=True)
            set_mode(ch, mode)
            if not wait_applied(ch, mode):
                r["error"] = "not applied"
                report["results"][mode] = r
                continue
            board_cmd(ch, "inj 2 100", "inj = 2")
            time.sleep(1.0)
            board_cmd(ch, "nscal", "nscal = 1")      # sns gets its profile from the noise itself, its best case
            time.sleep(1.5 + args.settle)
            board_cmd(ch, "cpu")
            left, right, stats = inj_capture(ch, TapRecorder, tap_check, 1, 2 * 6.0 + 2.0, 2.0, work)
            r["voice"] = analyse_voice(left, right, table)
            r["voice"]["capture"] = stats
            left, right, stats = inj_capture(ch, TapRecorder, tap_check, 3, 8.0, 1.5, work)
            r["clean"] = analyse_clean(left, right, table)
            r["clean"]["capture"] = stats
            left, right, stats = inj_capture(ch, TapRecorder, tap_check, 2, 11.0, 5.0, work)
            r["noise"] = analyse_noise(right)
            r["noise"]["input_rms"] = rms(left)
            r["noise"]["capture"] = stats
            left, right, stats = inj_capture(ch, TapRecorder, tap_check, 2, 11.0, 5.0, work, noise=200)
            r["noise_up"] = analyse_noise(right)
            r["noise_up"]["input_rms"] = rms(left)
            r["noise_up"]["capture"] = stats
            board_cmd(ch, "inj 2 100", "inj = 2")
            cpu = ch.command("cpu", timeout=0.5, retries=5)
            r["cpu"] = {m.group(1): int(m.group(2)) for m in re.finditer(r"cpu (\S+) (\d+) ", cpu["text"] or "")}
            st = board_status(ch) or {}
            r["time"] = {k: st.get(k) for k in ("ns_frames", "ns_avg_us", "ns_max_us", "ns_under", "fifo_min", "fifo_max")}
            report["results"][mode] = r
            print(json.dumps({"lag_ms": r["voice"]["lag_ms"], "gain": r["voice"]["signal_gain"], "pause": r["voice"].get("pause_rms"),
                              "noise": r["noise"]["rms"], "sdr": r["clean"]["sdr_db"], "avg_us": r["time"]["ns_avg_us"],
                              "under": r["time"]["ns_under"], "mic_rx": r["cpu"].get("mic_rx")}), flush=True)
    finally:
        try:
            board_cmd(ch, "inj 0", "inj = 0")
            set_mode(ch, mode_was)
        except RuntimeError as e:
            report["restore_error"] = str(e)
        close_channel(rnd, tag)
        board_cmd(ch, "vol 100", "volume = 100")
        ch.close()
        if work.exists():
            work.rmdir()
    summary = watch.finish(0)
    report["serial"] = {k: summary.get(k) for k in ("board_resets", "panics", "slc_drops", "port_drops")}
    report["resets_in_run"] = len(watch.resets) - resets
    base = report["results"].get("off")
    if base and "voice" in base:
        for mode, r in report["results"].items():
            if "voice" not in r:
                continue
            g0, o0 = base["voice"]["signal_gain"], base["voice"].get("onset_gain")
            r["relative"] = {
                "signal_loss_db": round(20 * math.log10(g0 / r["voice"]["signal_gain"]), 2),
                "onset_loss_db": round(20 * math.log10(o0 / r["voice"]["onset_gain"]), 2) if o0 and r["voice"].get("onset_gain") else None,
                "pause_suppression_db": round(20 * math.log10(base["voice"]["pause_rms"] / r["voice"]["pause_rms"]), 2),
                "noise_suppression_db": round(20 * math.log10(base["noise"]["rms"] / r["noise"]["rms"]), 2),
                "noise_up_suppression_db": round(20 * math.log10(base["noise_up"]["rms"] / r["noise_up"]["rms"]), 2),
                "sdr_vs_off_db": round(r["clean"]["sdr_db"] - base["clean"]["sdr_db"], 2),
                "level_std_vs_off_db": round(r["noise"]["level_std_db"] - base["noise"]["level_std_db"], 2),
            }
    (out / "inject.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({m: {"rel": r.get("relative"), "lag_ms": r.get("voice", {}).get("lag_ms"), "time": r.get("time"), "mic_rx": r.get("cpu", {}).get("mic_rx")} for m, r in report["results"].items()}, indent=1))
    print(out)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action")
    m = sub.add_parser("make")
    m.add_argument("wav")
    m.add_argument("--seconds", type=float, default=12.0)
    m.add_argument("--level", type=int, default=8000)
    c = sub.add_parser("check")
    c.add_argument("ref")
    c.add_argument("rec")
    r = sub.add_parser("run", help="the owner's check, the same as no arguments at all")
    s = sub.add_parser("switches", help="switches of the mode with the channel open")
    s.add_argument("--power", required=True)
    s.add_argument("--n", type=int, default=60)
    s.add_argument("--period", type=float, default=1.0)
    s.add_argument("--modes", type=lambda t: t.split(","), default=list(ALL_MODES))
    s.add_argument("--inject", type=int, default=0, help="test build nsinj: inj 1 signal and noise, 2 noise only; the bus is judged too")
    s.add_argument("--label", default="")
    s.add_argument("--port", default="COM15")
    i = sub.add_parser("inject", help="the known signal in front of the chain, test build nsinj")
    i.add_argument("--power", required=True)
    i.add_argument("--modes", type=lambda t: t.split(","), default=list(ALL_MODES))
    i.add_argument("--settle", type=float, default=4.0, help="seconds on the noise before the measured runs")
    i.add_argument("--label", default="")
    i.add_argument("--port", default="COM15")
    for p in (ap, r):
        p.add_argument("--modes", type=lambda t: t.split(","), default=list(OWNER_MODES))
        p.add_argument("--seconds", type=int, default=SPEAK_S)
        p.add_argument("--quiet", type=int, default=QUIET_S)
        p.add_argument("--keep-profile", action="store_true", help="sns keeps the profile of the boot")
        p.add_argument("--out", help="directory; the default is <bench runs>/voice/<stamp>")
    args = ap.parse_args()
    if args.action in (None, "run"):
        return owner_run(args)
    if args.action == "switches":
        return switch_test(args)
    if args.action == "inject":
        return inject_stage(args)
    if args.action == "make":
        write_wav(args.wav, make(args.seconds, args.level))
        return 0
    ref, rate = audio_check.read_wav(args.ref)
    rec, rate2 = audio_check.read_wav(args.rec)
    if rate != rate2:
        print(json.dumps({"error": "rates differ: %d %d" % (rate, rate2)}))
        return 2
    result = compare(ref, rec, rate)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
