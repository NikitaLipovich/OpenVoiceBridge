#!/usr/bin/env python3
"""
lyrat_harness.py — goal-harness для LyraT: микрофон в Zoom (G1) + WiFi-стрим call-аудио (G2),
стабильные на длине сессии (G3).

Контракт целей и лестница ручек — в GOAL.md рядом.

    python harness/lyrat_harness.py probe
    python harness/lyrat_harness.py trial --seconds 60
    python harness/lyrat_harness.py loop --apply
    python harness/lyrat_harness.py soak --seconds 600
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# консоль Windows по умолчанию cp1252 — форсим UTF-8, иначе кириллица падает
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
RUNS = HERE / "runs"
WIN = HERE / "win"

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
SERIAL_PORT = "COM15"
SERIAL_BAUD = 115200
UDP_CALL_PORT = 5004
UDP_MIC_PORT = 5005
MATCH = "LyraT"

# Из прошивки (main.c:104-111): 16 кГц, 240 сэмплов/кадр = 15 мс -> 66.7 кадров/с.
# UDP_BATCH_FRAMES=6 — это ПОТОЛОК: udp_stream_task забирает из ringbuf сколько
# накопилось и выходит из цикла на пустом буфере, поэтому на практике в пакете
# обычно 1 кадр. Считаем не пакеты, а кадры — n_frames лежит в pkt[7].
FRAME_SAMPLES = 240
SAMPLE_RATE = 16000
BATCH_FRAMES_MAX = 6
NOMINAL_FPS = SAMPLE_RATE / FRAME_SAMPLES   # 66.67 кадров/с
UDP_DUPLICATE = 2                            # каждый пакет шлётся 1+2 раза

# Пороги pass/fail
TH = {
    "sco_active_ratio": 0.95,
    "cap_rms_idle": 5.0,      # выше этого — линк реально несёт звук
    "cap_rms_speech": 200.0,  # ориентир «в Zoom слышно»
    "udp_fps_min": NOMINAL_FPS * 0.85,   # ~56.7 кадров/с
    "udp_loss_max": 2.0,
    "sco_drops_max": 0,
    "resets_max": 0,
}

RE = {
    "hb": re.compile(r"SCO=(\w+)\s+heap=(\d+)\s+wifi=(\d+)"),
    "sco_state": re.compile(r"SCO audio state=(\d+)\s+codec=(\S+)"),
    "sco_open": re.compile(r"\*\*\* SCO OPEN"),
    "sco_closed": re.compile(r"\*\*\* SCO CLOSED"),
    "slc": re.compile(r"\*\*\* SLC CONNECTED"),
    "hfp_conn": re.compile(r"HFP conn state=(\d+)"),
    "mic": re.compile(r"MIC f=(\d+)\s+pk=(\d+)"),
    "udpmic": re.compile(r"UDP mic: seq=(\d+)"),
    "gotip": re.compile(r"got ip:([\d.]+)"),
    "peer": re.compile(r"peer\[(\d)\]\s*=\s*([0-9a-fA-F:]{17})"),
    "slc_status": re.compile(r"SLC\s+=\s+(up|down)"),
    "connect_try": re.compile(r"HFP connect try peer"),
    "seeded": re.compile(r"seeded (\d+) peer"),
    "reset": re.compile(r"rst:0x|Guru Meditation|abort\(\) was called|assert failed|Backtrace:"),
}


def now() -> float:
    return time.monotonic()


def ps(script: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(WIN / script), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def pct(xs, p: float):
    if not xs:
        return None
    s = sorted(xs)
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return float(s[k])


# ---------------------------------------------------------------------------
# Serial probe — владеет COM-портом единолично
# ---------------------------------------------------------------------------
class SerialProbe(threading.Thread):
    def __init__(self, port: str = SERIAL_PORT, log: Path | None = None):
        super().__init__(daemon=True)
        self.port = port
        self.log = log
        self._stop = threading.Event()
        self.error: str | None = None

        self.sco_samples: list[int] = []   # 1 = active
        self.sco_opens = 0
        self.sco_closes = 0
        self.slc_connects = 0
        self.hfp_states: list[int] = []
        self.codecs: list[str] = []
        self.mic_pk: list[int] = []
        self.heaps: list[int] = []
        self.wifi: list[int] = []
        self.udp_mic_seq: list[int] = []
        self.resets = 0
        self.board_ip: str | None = None
        self.peers: set[str] = set()
        self.slc_status: str | None = None
        self.connect_tries = 0
        self.lines = 0

    def run(self) -> None:
        try:
            import serial  # pyserial
        except ImportError:
            self.error = "pyserial_missing"
            return
        try:
            # DTR/RTS заведены на EN/IO0 — их подъём при open() ресетит ESP32.
            # Открываем порт с явно снятыми линиями, чтобы не сбивать состояние платы.
            sp = serial.Serial()
            sp.port = self.port
            sp.baudrate = SERIAL_BAUD
            sp.timeout = 0.5
            sp.dtr = False
            sp.rts = False
            sp.open()
        except Exception as e:  # порт занят монитором / нет платы
            self.error = "open_failed: %s" % e
            return

        fh = self.log.open("a", encoding="utf-8", errors="replace") if self.log else None
        last_poll = 0.0
        try:
            while not self._stop.is_set():
                if now() - last_poll > 5.0:
                    try:
                        sp.write(b"s\n")   # попросить status у консоли платы
                    except Exception:
                        pass
                    last_poll = now()
                raw = sp.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").rstrip()
                self.lines += 1
                if fh:
                    fh.write(line + "\n")
                self._parse(line)
        finally:
            if fh:
                fh.close()
            try:
                sp.close()
            except Exception:
                pass

    def _parse(self, line: str) -> None:
        m = RE["hb"].search(line)
        if m:
            self.sco_samples.append(1 if m.group(1) == "active" else 0)
            self.heaps.append(int(m.group(2)))
            self.wifi.append(int(m.group(3)))
        m = RE["sco_state"].search(line)
        if m:
            self.codecs.append(m.group(2))
        if RE["sco_open"].search(line):
            self.sco_opens += 1
        if RE["sco_closed"].search(line):
            self.sco_closes += 1
        if RE["slc"].search(line):
            self.slc_connects += 1
        m = RE["hfp_conn"].search(line)
        if m:
            self.hfp_states.append(int(m.group(1)))
        m = RE["mic"].search(line)
        if m:
            self.mic_pk.append(int(m.group(2)))
        m = RE["udpmic"].search(line)
        if m:
            self.udp_mic_seq.append(int(m.group(1)))
        m = RE["gotip"].search(line)
        if m:
            self.board_ip = m.group(1)
        m = RE["peer"].search(line)
        if m:
            self.peers.add(m.group(2).lower())
        m = RE["slc_status"].search(line)
        if m:
            self.slc_status = m.group(1)
        if RE["connect_try"].search(line):
            self.connect_tries += 1
        if RE["reset"].search(line):
            self.resets += 1

    def stop(self) -> None:
        self._stop.set()

    def summary(self) -> dict:
        n = len(self.sco_samples)
        return {
            "serial_error": self.error,
            "serial_lines": self.lines,
            "sco_active_ratio": (sum(self.sco_samples) / n) if n else None,
            "sco_opens": self.sco_opens,
            "sco_drops": self.sco_closes,
            "slc_connects": self.slc_connects,
            "codecs": sorted(set(self.codecs)),
            "board_resets": self.resets,
            "heap_min": min(self.heaps) if self.heaps else None,
            "wifi_up": bool(self.wifi and self.wifi[-1]),
            "mic_pk_p95": pct(self.mic_pk, 95),
            "board_ip": self.board_ip,
            "board_peers": sorted(self.peers),
            "board_slc": self.slc_status,
            "connect_tries": self.connect_tries,
        }


# ---------------------------------------------------------------------------
# UDP probe — приём mu-law потока платы, дедуп по seq (прошивка шлёт каждый 3 раза)
# ---------------------------------------------------------------------------
class UdpProbe(threading.Thread):
    def __init__(self, port: int, label: str, wav: Path | None = None):
        super().__init__(daemon=True)
        self.port = port
        self.label = label
        self.wav = wav
        self._stop = threading.Event()
        self.error: str | None = None
        self.seen: set[int] = set()
        self.frames: dict[int, int] = {}   # seq -> n_frames
        self.raw_packets = 0
        self.bytes = 0
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.senders: set[str] = set()
        self._pcm = bytearray()

    def run(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.settimeout(0.5)
            s.bind(("0.0.0.0", self.port))
        except Exception as e:
            self.error = "bind_failed: %s" % e
            return
        try:
            while not self._stop.is_set():
                try:
                    data, addr = s.recvfrom(4096)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if len(data) < 9:
                    continue
                self.raw_packets += 1
                self.bytes += len(data)
                self.senders.add(addr[0])
                t = now()
                if self.first_ts is None:
                    self.first_ts = t
                self.last_ts = t
                seq = int.from_bytes(data[0:4], "little")
                if seq not in self.seen:
                    self.seen.add(seq)
                    # pkt[7] = n_frames в этом пакете; seq инкрементится на n_frames
                    self.frames[seq] = data[7] or 1
                    if self.wav is not None:
                        self._pcm += ulaw_decode(data[8:])
        finally:
            s.close()
            if self.wav is not None and self._pcm:
                write_wav(self.wav, bytes(self._pcm), SAMPLE_RATE)

    def stop(self) -> None:
        self._stop.set()

    def summary(self) -> dict:
        dur = (self.last_ts - self.first_ts) if (self.first_ts and self.last_ts) else 0.0
        uniq = len(self.seen)
        got_frames = sum(self.frames.values())
        loss = None
        if uniq >= 2:
            lo = min(self.seen)
            hi = max(self.seen)
            span = (hi + self.frames[hi]) - lo      # ожидаемое число кадров в окне
            loss = max(0.0, (span - got_frames) / span * 100.0) if span > 0 else 0.0
        return {
            "udp_%s_error" % self.label: self.error,
            "udp_%s_raw" % self.label: self.raw_packets,
            "udp_%s_uniq" % self.label: uniq,
            "udp_%s_frames" % self.label: got_frames,
            "udp_%s_fps" % self.label: round(got_frames / dur, 1) if dur > 0.5 else 0.0,
            "udp_%s_dup_ratio" % self.label: round(self.raw_packets / uniq, 2) if uniq else None,
            "udp_%s_loss_pct" % self.label: round(loss, 2) if loss is not None else None,
            "udp_%s_senders" % self.label: sorted(self.senders),
        }


_ULAW_TABLE: list[int] = []


def _build_ulaw() -> None:
    for i in range(256):
        u = ~i & 0xFF
        t = ((u & 0x0F) << 3) + 0x84
        t <<= (u & 0x70) >> 4
        t = t - 0x84
        _ULAW_TABLE.append(-t if (u & 0x80) else t)


_build_ulaw()


def ulaw_decode(payload: bytes) -> bytes:
    out = bytearray(len(payload) * 2)
    for i, b in enumerate(payload):
        v = _ULAW_TABLE[b] & 0xFFFF
        out[2 * i] = v & 0xFF
        out[2 * i + 1] = (v >> 8) & 0xFF
    return bytes(out)


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# ---------------------------------------------------------------------------
# Windows-сторона
# ---------------------------------------------------------------------------
def win_probe() -> dict:
    r = ps("probe_win.ps1", "-Match", MATCH, timeout=90)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": "probe_win_failed",
                "stderr": r.stderr[-400:], "stdout": r.stdout[-400:]}


def hold_sco_windowed(seconds: int) -> dict:
    """
    Держит окно измерения ровно `seconds`, независимо от того, доступен ли endpoint.

    Если endpoint ещё не поднят (BT не подключён), hold_sco.ps1 возвращается мгновенно —
    тогда ждём и пробуем снова, чтобы поймать момент появления endpoint'а.
    """
    deadline = now() + seconds
    agg = {"cap_opened": False, "cap_error": None, "cap_mmrc": None, "cap_devices": [],
           "cap_rms_p95": None, "cap_rms_max": None, "cap_peak_max": None,
           "cap_samples": 0, "cap_attempts": 0}
    while True:
        left = int(deadline - now())
        if left <= 1:
            break
        r = hold_sco(left)
        agg["cap_attempts"] += 1
        agg["cap_devices"] = r["cap_devices"] or agg["cap_devices"]
        if r["cap_opened"]:
            agg["cap_opened"] = True
            for k in ("cap_rms_p95", "cap_rms_max", "cap_peak_max"):
                if r[k] is not None:
                    agg[k] = max(agg[k] or 0.0, r[k])
            agg["cap_samples"] += r["cap_samples"] or 0
            agg["cap_error"] = None
        else:
            agg["cap_error"] = agg["cap_error"] or r["cap_error"]
            agg["cap_mmrc"] = agg["cap_mmrc"] if agg["cap_mmrc"] is not None else r["cap_mmrc"]
            time.sleep(min(3.0, max(0.0, deadline - now())))
    return agg


def hold_sco(seconds: int) -> dict:
    """Открывает capture-endpoint LyraT — это и есть то, что заставляет Windows поднять SCO."""
    seconds = max(1, seconds)
    r = ps("hold_sco.ps1", "-Match", MATCH, "-Seconds", str(seconds), timeout=seconds + 60)
    levels, ev = [], []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        ev.append(o)
        if o.get("ev") == "level":
            levels.append(o)
    rms = [l["rms"] for l in levels]
    peaks = [l["peak"] for l in levels]
    err = next((o for o in ev if o.get("ev") == "error"), None) or {}
    return {
        "cap_opened": any(o.get("ev") == "open" for o in ev),
        "cap_error": err.get("why"),
        "cap_mmrc": err.get("mmrc"),
        "cap_devices": next((o.get("names") for o in ev if o.get("ev") == "devices"), []),
        "cap_rms_p95": pct([float(x) for x in rms], 95) if rms else None,
        "cap_rms_max": max(rms) if rms else None,
        "cap_peak_max": max(peaks) if peaks else None,
        "cap_samples": next((o.get("samples") for o in ev if o.get("ev") == "done"), 0),
    }


def local_ipv4() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Скоринг
# ---------------------------------------------------------------------------
def score(m: dict) -> dict:
    g1 = {
        "bt_connected": bool(m.get("bt_connected")),
        "endpoint_active": bool(m.get("capture_active")),
        "capture_opened": bool(m.get("cap_opened")),
        "sco_active": (m.get("sco_active_ratio") or 0) >= TH["sco_active_ratio"],
        "audio_flowing": (m.get("cap_rms_p95") or 0) > TH["cap_rms_idle"],
        "no_drops": (m.get("sco_drops") or 0) <= TH["sco_drops_max"],
    }
    g2 = {
        "call_fps": (m.get("udp_call_fps") or 0) >= TH["udp_fps_min"],
        "call_loss": (m.get("udp_call_loss_pct")
                      if m.get("udp_call_loss_pct") is not None else 100.0) <= TH["udp_loss_max"],
    }
    g3 = {
        "no_resets": (m.get("board_resets") or 0) <= TH["resets_max"],
        "no_slc_flap": (m.get("sco_drops") or 0) <= TH["sco_drops_max"],
    }
    return {
        "G1_mic_for_zoom": {"pass": all(g1.values()), "parts": g1},
        "G2_wifi_call_stream": {"pass": all(g2.values()), "parts": g2},
        "G3_stable": {"pass": all(g3.values()), "parts": g3},
    }


def first_blocker(sc: dict, m: dict) -> str:
    g1 = sc["G1_mic_for_zoom"]["parts"]
    if not g1["bt_connected"]:
        peers = m.get("board_peers") or []
        tries = m.get("connect_tries") or 0
        if not peers:
            return ("F4 работает, но у платы ПУСТОЙ bond list: пара односторонняя — "
                    "Windows считает устройство сопряжённым, плата про него не знает. "
                    "Нужно удалить пару в Windows и спарить заново (ручка M1)")
        if tries == 0:
            return ("пиры есть (%s), но попыток connect нет — проверить, что "
                    "hf_reconnect_task поднялся" % ", ".join(peers))
        return ("плата зовёт esp_hf_client_connect (%d попыток к %s), но линк не встаёт — "
                "смотреть HFP conn state в runs/*.log" % (tries, ", ".join(peers)))
    if not g1["endpoint_active"] or not g1["capture_opened"]:
        return "BT есть, но HFP endpoint не активируется -> ручки W3 (stale endpoint) / F3 (WBS off)"
    if not g1["sco_active"]:
        return "endpoint открыт, но плата не видит SCO -> ручка F3 (WBS off), смотреть коды в serial"
    if not g1["audio_flowing"]:
        return "SCO есть, звука нет -> смотреть outgoing_pcm_cb / s_rb_mic_tx"
    if not g1["no_drops"]:
        return "SCO рвётся -> ручки F1 (coex PREFER_BT) / F2 (txpower)"
    g2 = sc["G2_wifi_call_stream"]["parts"]
    if not g2["call_fps"]:
        return ("UDP:%d не даёт %.0f кадров/с -> call-аудио идёт только при активном SCO; "
                "проверить dest IP в main.c и firewall" % (UDP_CALL_PORT, TH["udp_fps_min"]))
    if not g2["call_loss"]:
        return "UDP приходит с потерями -> ручки F1 (coex) / F2 (txpower)"
    if not sc["G3_stable"]["pass"]:
        return "нестабильно: ресеты платы -> смотреть backtrace в runs/*.log"
    return "все цели достигнуты"


# ---------------------------------------------------------------------------
# Trial
# ---------------------------------------------------------------------------
@dataclass
class Trial:
    seconds: int = 60
    knob: str = "baseline"
    capture_wav: bool = True
    metrics: dict = field(default_factory=dict)

    def run(self) -> dict:
        RUNS.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tag = "%s-%s" % (stamp, self.knob)
        log = RUNS / ("%s.log" % tag)

        print("[trial:%s] %ds -> runs/%s.log" % (self.knob, self.seconds, tag))

        before = win_probe()
        ser = SerialProbe(log=log)
        call = UdpProbe(UDP_CALL_PORT, "call",
                        RUNS / ("%s-call.wav" % tag) if self.capture_wav else None)
        mic = UdpProbe(UDP_MIC_PORT, "mic",
                       RUNS / ("%s-mic.wav" % tag) if self.capture_wav else None)
        for t in (ser, call, mic):
            t.start()
        time.sleep(1.0)

        cap = hold_sco_windowed(self.seconds)  # держит окно ровно seconds

        for t in (ser, call, mic):
            t.stop()
        time.sleep(1.5)
        after = win_probe()

        m: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "knob": self.knob,
            "seconds": self.seconds,
            "pc_ip": local_ipv4(),
            "win_capture_state": before.get("win_capture_state"),
            "stale_endpoints": before.get("stale_endpoints"),
            "capture_active": bool(after.get("capture_active") or before.get("capture_active")),
            "bt_connected": bool(after.get("bt_connected") or before.get("bt_connected")),
        }
        m.update(cap)
        m.update(ser.summary())
        m.update(call.summary())
        m.update(mic.summary())
        m["score"] = score(m)
        m["blocker"] = first_blocker(m["score"], m)
        self.metrics = m

        with (RUNS / "results.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
        return m


def render(m: dict) -> str:
    s = m["score"]

    def mark(b):
        return "PASS" if b else "FAIL"

    def parts(k):
        return " ".join("%s=%s" % (n, "+" if v else "-") for n, v in s[k]["parts"].items())

    out = [
        "",
        "=== trial %s @ %s (%ds) ===" % (m["knob"], m["ts"], m["seconds"]),
        "  G1 микрофон в Zoom : %s   %s" % (mark(s["G1_mic_for_zoom"]["pass"]), parts("G1_mic_for_zoom")),
        "  G2 WiFi call-стрим : %s   %s" % (mark(s["G2_wifi_call_stream"]["pass"]), parts("G2_wifi_call_stream")),
        "  G3 стабильность    : %s   %s" % (mark(s["G3_stable"]["pass"]), parts("G3_stable")),
        "",
        "  bt_connected=%s  win_capture_state=%s  cap_opened=%s (%s, попыток %s)"
        % (m.get("bt_connected"), m.get("win_capture_state"), m.get("cap_opened"),
           m.get("cap_error") or "ok", m.get("cap_attempts")),
        "  sco_ratio=%s  sco_opens=%s  sco_drops=%s  codecs=%s  resets=%s"
        % (m.get("sco_active_ratio"), m.get("sco_opens"), m.get("sco_drops"),
           m.get("codecs"), m.get("board_resets")),
        "  cap_rms_p95=%s  mic_pk_p95=%s  heap_min=%s  board_ip=%s  pc_ip=%s"
        % (m.get("cap_rms_p95"), m.get("mic_pk_p95"), m.get("heap_min"),
           m.get("board_ip"), m.get("pc_ip")),
        "  board: SLC=%s peers=%s connect_tries=%s"
        % (m.get("board_slc"), m.get("board_peers"), m.get("connect_tries")),
        "  udp call: fps=%s (норма %.1f) кадров=%s dup=%s loss=%s%% from=%s"
        % (m.get("udp_call_fps"), NOMINAL_FPS, m.get("udp_call_frames"),
           m.get("udp_call_dup_ratio"), m.get("udp_call_loss_pct"), m.get("udp_call_senders")),
        "  udp mic : fps=%s (норма %.1f) кадров=%s dup=%s loss=%s%% from=%s"
        % (m.get("udp_mic_fps"), NOMINAL_FPS, m.get("udp_mic_frames"),
           m.get("udp_mic_dup_ratio"), m.get("udp_mic_loss_pct"), m.get("udp_mic_senders")),
        "",
        "  -> %s" % m["blocker"],
        "",
    ]
    if m.get("serial_error"):
        out.insert(1, "  !! serial: %s" % m["serial_error"])
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_probe(_a) -> int:
    w = win_probe()
    print(json.dumps(w, ensure_ascii=False, indent=2))
    print("\npc_ip = %s   (прошивка шлёт UDP на 192.168.1.214)" % local_ipv4())
    return 0


def cmd_trial(a) -> int:
    m = Trial(seconds=a.seconds, knob=a.knob).run()
    print(render(m))
    return 0 if all(g["pass"] for g in m["score"].values()) else 1


def cmd_soak(a) -> int:
    m = Trial(seconds=a.seconds, knob="soak").run()
    print(render(m))
    return 0 if all(g["pass"] for g in m["score"].values()) else 1


def cmd_loop(a) -> int:
    from knobs import ladder
    for knob in ladder(include_gated=a.apply_gated):
        print("\n--- ручка %s: %s ---" % (knob.id, knob.title))
        print("    %s" % knob.apply(dry_run=not a.apply))
        m = Trial(seconds=a.seconds, knob=knob.id).run()
        print(render(m))
        if all(g["pass"] for g in m["score"].values()):
            print("\n*** ЦЕЛИ ДОСТИГНУТЫ на ручке %s ***" % knob.id)
            return 0
    print("\nЛестница исчерпана. Текущий блокер см. выше; ручные шаги — в GOAL.md")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="снимок состояния, ничего не меняет")
    sp.set_defaults(fn=cmd_probe)

    st = sub.add_parser("trial", help="один замер + скоринг")
    st.add_argument("--seconds", type=int, default=60)
    st.add_argument("--knob", default="baseline")
    st.set_defaults(fn=cmd_trial)

    sl = sub.add_parser("loop", help="лестница ручек")
    sl.add_argument("--seconds", type=int, default=45)
    sl.add_argument("--apply", action="store_true", help="реально применять ручки (иначе dry-run)")
    sl.add_argument("--apply-gated", action="store_true", help="включить прошивочные ручки")
    sl.set_defaults(fn=cmd_loop)

    ss = sub.add_parser("soak", help="длинная проверка G3")
    ss.add_argument("--seconds", type=int, default=600)
    ss.set_defaults(fn=cmd_soak)

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
