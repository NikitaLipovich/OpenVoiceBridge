"""What the LyraT sends to the phone, recorded on the PC (goal 112).

The LyraT copies each mSBC frame it hands to Bluetooth up the control UART (command `senttap on [S]`, frames of type 10);
the S3 forwards them to the PC of the stream on UDP 5009 and stamps each with its mic frame counter at arrival, the clock of
the up line. Here they are decoded with the firmware's own codec (msbc.py) and placed on the time line of `_up.wav`:

  <prefix>_sent.wav       what the phone got from the headset: after the board's processing, before the air. 16 kHz mono
  <prefix>_sent_both.wav  stereo: left the up line (mic before processing), right sent, on one time line

UDP packet (little endian): "S3", version 1, stream 2, u32 sequence, u32 mic frame at arrival, u16 S3 boot id, u16 length,
then the LyraT's payload: u32 index of its first frame (frames encoded since the LyraT started), u16 mSBC channels opened,
u8 frames, u8 flags (bit 0 mic muted, bit 1 SCO open), u32 frames the LyraT dropped so far, then frames of 57 bytes.

Placement: a frame's index times 120 samples plus an offset per channel; the offset is the lower envelope of
(arrival stamp - end of the packet's frames) over 3 s, the least delay through the UART. Then the content is matched to the
up line by cross-correlation (lag of the board's processing, about 60 ms of its ring) when the match is clear (both carry
the same voice); with a tone of the board or a muted mic it is not, and the stamps stay. A missing index is a lost frame:
silence in the file, listed in `gaps`.

  sent_tap.py DIR [--prefix P]   build the files again from DIR/sent.bin and DIR/tap.json (and DIR/<prefix>_up.wav)
"""
import argparse
import json
import socket
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import msbc  # noqa: E402
from benchlib import bench_port  # noqa: E402

PORT = 5009
RATE = 16000
SPF = msbc.PCM_SAMPLES          # samples per frame
HEAD = 16
LY_HEAD = 12
ENVELOPE_S = 3.0
XCORR_MIN = 0.3                 # normalized peak needed to trust the content match
XCORR_RANGE_MS = (-100, 400)
XCORR_WINDOW_S = 30.0    # sent later than up: the board's ring and processing
RENEW_S = 300
ON_S = 900


def parse(data):
    """One UDP packet -> dict, or None when it is not one of ours."""
    if len(data) < HEAD + LY_HEAD or data[:2] != b"S3" or data[3] != 2:
        return None
    seq, at, boot, length = struct.unpack_from("<IIHH", data, 4)
    body = data[HEAD:HEAD + length]
    if len(body) < LY_HEAD:
        return None
    first, sco, n, flags, drops = struct.unpack_from("<IHBBI", body, 0)
    frames = body[LY_HEAD:]
    if n == 0 or len(frames) != n * msbc.FRAME_BYTES:
        return None
    return {"seq": seq, "at": at, "boot": boot, "first": first, "sco": sco, "n": n, "muted": bool(flags & 1),
            "sco_open": bool(flags & 2), "drops": drops, "frames": frames}


def make_packet(seq, at, boot, first, sco, frames, muted=False, drops=0):
    """The S3's packet as the firmware builds it (tests)."""
    n = len(frames) // msbc.FRAME_BYTES
    body = struct.pack("<IHBBI", first, sco, n, (1 if muted else 0) | 2, drops) + frames
    return b"S3\x01\x02" + struct.pack("<IIHH", seq, at, boot, len(body)) + body


class SentTap:
    """Listens on UDP 5009 while the recording runs and keeps every packet in DIR/sent.bin as well."""

    def __init__(self, out, port=None):
        self.out = Path(out)
        self.port = port or bench_port(PORT)
        self.packets = []           # (arrival monotonic, bytes)
        self.halt = threading.Event()
        self.sock = None
        self.thread = None
        self.journal = None
        self.enabled = None         # reply of the LyraT to senttap on
        self.renewed = 0.0

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(0.2)
        self.journal = open(self.out / "sent.bin", "wb")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.halt.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except OSError:
                continue
            at = time.monotonic()
            self.packets.append((at, data))
            self.journal.write(struct.pack("<dI", at, len(data)) + data)

    def command(self, text):
        """A console command of the LyraT through the S3: (status, text)."""
        import s3_uart
        ch = s3_uart.Channel()
        try:
            if not ch.find(timeout=3.0):
                return None, "S3 не найдена"
            r = ch.command(text)
        finally:
            ch.close()
        return r["status"], (r["text"] or "").strip()

    def enable(self, seconds=ON_S):
        st, text = self.command("senttap on %d" % seconds)
        self.renewed = time.monotonic()
        self.enabled = st == 0 and text.startswith("senttap on=1")
        return self.enabled, text

    def tick(self, now):
        """Renews the LyraT's timer during a long call."""
        if self.enabled and now - self.renewed >= RENEW_S:
            self.renewed = now
            try:
                self.command("senttap on %d" % ON_S)
            except OSError:
                pass

    def stop(self):
        if self.enabled:
            try:
                self.command("senttap off")
            except OSError:
                pass
        self.halt.set()
        if self.thread:
            self.thread.join(timeout=2.0)
        if self.sock:
            self.sock.close()
        if self.journal:
            self.journal.close()


def read_journal(path):
    out = []
    data = Path(path).read_bytes() if Path(path).exists() else b""
    i = 0
    while i + 12 <= len(data):
        at, n = struct.unpack_from("<dI", data, i)
        out.append((at, data[i + 12:i + 12 + n]))
        i += 12 + n
    return out


def assemble(raw_packets):
    """Packets -> frames on the mic clock of the S3. Returns (pcm by S3 mic frame: dict start -> int16 array, stats)."""
    pk = [p for p in (parse(d) for _, d in raw_packets) if p]
    stats = {"packets": len(pk), "frames": 0, "lost_frames": 0, "lyrat_drops": 0, "boots": 0, "channels": 0,
             "duplicates": 0, "muted_frames": 0}
    if not pk:
        return [], stats
    boots = {}
    for p in pk:
        boots[p["boot"]] = boots.get(p["boot"], 0) + p["n"]
    boot = max(boots, key=boots.get)
    stats["boots"] = len(boots)
    pk = [p for p in pk if p["boot"] == boot]
    stats["lyrat_drops"] = max(p["drops"] for p in pk) - min(p["drops"] for p in pk)
    segments = []
    by_sco = {}
    for p in pk:
        by_sco.setdefault(p["sco"], []).append(p)
    for sco, ps in sorted(by_sco.items()):
        ps.sort(key=lambda p: p["first"])
        # The delay through the UART: the lower envelope over ENVELOPE_S of arrival minus the end of the packet's frames.
        a = np.array([p["at"] - (p["first"] + p["n"]) * SPF for p in ps], dtype=np.int64)
        idx = np.array([p["first"] for p in ps], dtype=np.int64)
        half = int(ENVELOPE_S / 2 * RATE / SPF)
        offs = np.array([a[(idx >= i - half) & (idx <= i + half)].min() for i in idx])
        frames = {}
        for p, off in zip(ps, offs):
            for k in range(p["n"]):
                i = p["first"] + k
                if i in frames:
                    stats["duplicates"] += 1
                    continue
                frames[i] = (p["frames"][k * msbc.FRAME_BYTES:(k + 1) * msbc.FRAME_BYTES], int(off), p["muted"])
        lo, hi = min(frames), max(frames)
        order = list(range(lo, hi + 1))
        lost = [i for i in order if i not in frames]
        stats["lost_frames"] += len(lost)
        stats["frames"] += len(frames)
        stats["muted_frames"] += sum(1 for f in frames.values() if f[2])
        # Decode in order with a fresh decoder per channel; a lost frame is decoded as the zero frame, then zeroed.
        blob = b"".join(frames[i][0] if i in frames else bytes(msbc.FRAME_BYTES) for i in order)
        pcm, _ = msbc.decode(blob, reset_first=True)
        for j, i in enumerate(order):
            if i not in frames:
                pcm[j * SPF:(j + 1) * SPF] = 0
        # A lost frame takes the offset of the frame before it.
        offset, last = [], int(offs[0])
        for i in order:
            last = frames[i][1] if i in frames else last
            offset.append(last)
        segments.append({"sco": sco, "first": lo, "pcm": pcm, "offset": offset, "order": order, "lost": set(lost),
                         "muted": [frames[i][2] if i in frames else False for i in order]})
    stats["channels"] = len(segments)
    return segments, stats


def _xcorr_lag(up, sent):
    """Lag in samples of sent after up by normalized cross-correlation, and its peak; (None, 0) when there is nothing."""
    n = min(len(up), len(sent))
    if n < RATE:
        return None, 0.0
    # At most XCORR_WINDOW_S around the loudest second of sent: a long call stays cheap.
    w = int(XCORR_WINDOW_S * RATE)
    start = 0
    if n > w:
        e = np.add.reduceat(sent[:n].astype(np.float64) ** 2, np.arange(0, n, RATE))
        start = min(max(int(np.argmax(e)) * RATE - w // 2, 0), n - w)
        n = w
    a = up[start:start + n].astype(np.float64)
    b = sent[start:start + n].astype(np.float64)
    if a.std() < 1 or b.std() < 1:
        return None, 0.0
    a -= a.mean()
    b -= b.mean()
    size = 1 << int(np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    lo, hi = (int(v * RATE / 1000) for v in XCORR_RANGE_MS)
    lags = np.arange(lo, hi + 1)
    vals = c[lags % size]
    k = int(np.argmax(np.abs(vals)))
    lag = int(lags[k])
    # Normalize on the overlap of that lag.
    if lag >= 0:
        x, y = a[:n - lag], b[lag:n]
    else:
        x, y = a[-lag:n], b[:n + lag]
    peak = float(abs(np.dot(x, y)) / (np.sqrt(np.dot(x, x) * np.dot(y, y)) + 1e-9))
    return lag, peak


def levels(x):
    """RMS and peak in dBFS of the samples, None for an empty or silent part."""
    if len(x) == 0:
        return {"rms_dbfs": None, "peak_dbfs": None}
    f = x.astype(np.float64)
    rms = float(np.sqrt(np.mean(f * f)))
    peak = float(np.max(np.abs(f)))
    db = lambda v: round(20 * np.log10(v / 32768.0), 1) if v > 0 else None  # noqa: E731
    return {"rms_dbfs": db(rms), "peak_dbfs": db(peak)}


def _spans(flags, unit_s):
    """[[from_s, to_s]] where flags are true."""
    out, start = [], None
    for i, f in enumerate(list(flags) + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append([round(start * unit_s, 2), round(i * unit_s, 2)])
            start = None
    return out


def build(out, prefix, raw_packets, mic_first_frame, file_start, length, up=None, write=True):
    """_sent.wav and _sent_both.wav on the time line of the up file.

    mic_first_frame: the S3 mic frame of sample 0 of the recorder's mic.wav (tap.json mic.first_frame); file_start: the
    sample of mic.wav that is sample 0 of the files (build_files: sm); length: samples of the files; up: the up file's
    samples, for the content match and for _sent_both.wav."""
    out = Path(out)
    segments, stats = assemble(raw_packets)
    sent = np.zeros(length, np.int16)
    have = np.zeros(length, bool)
    muted = np.zeros(length, bool)
    gaps = []
    base = (mic_first_frame or 0) + (file_start or 0)
    placed = 0
    for seg in segments:
        for j, i in enumerate(seg["order"]):
            off = seg["offset"][j]
            pos = i * SPF + off - base
            if pos < 0 or pos + SPF > length:
                continue
            if i in seg["lost"]:
                gaps.append([int(pos), SPF])
                continue
            sent[pos:pos + SPF] = seg["pcm"][j * SPF:(j + 1) * SPF]
            have[pos:pos + SPF] = True
            muted[pos:pos + SPF] = seg["muted"][j]
            placed += 1
    stats["placed_frames"] = placed
    align = {"method": "stamps", "lag_ms": 0.0, "xcorr_peak": None}
    if up is not None and placed:
        lag, peak = _xcorr_lag(np.asarray(up)[:length], sent)
        align["xcorr_peak"] = round(peak, 3)
        if lag is not None and peak >= XCORR_MIN:
            align = {"method": "xcorr", "lag_ms": round(lag / RATE * 1000, 1), "xcorr_peak": round(peak, 3)}
            sent, have, muted = (np.roll(v, -lag) for v in (sent, have, muted))
            if lag > 0:
                sent[-lag:] = 0
                have[-lag:] = False
                muted[-lag:] = False
            elif lag < 0:
                sent[:-lag] = 0
                have[:-lag] = False
                muted[:-lag] = False
            gaps = [[g[0] - lag, g[1]] for g in gaps]
    part = sent[have]
    lost_ms = round(stats["lost_frames"] * SPF / RATE * 1000, 1)
    total = stats["frames"] + stats["lost_frames"]
    res = {"stats": stats, "align": align, "gaps": gaps, "lost_ms": lost_ms,
           "loss_pct": round(100.0 * stats["lost_frames"] / total, 3) if total else None,
           "seconds": round(placed * SPF / RATE, 2), "levels": levels(part),
           "levels_unmuted": levels(sent[have & ~muted]),
           "muted": _spans(muted[::SPF], SPF / RATE), "silent": bool(placed and (levels(part)["rms_dbfs"] or -200) < -70)}
    if write:
        paths = {"sent": out / ("%s_sent.wav" % prefix), "sent_both": out / ("%s_sent_both.wav" % prefix)}
        _write(paths["sent"], sent, 1)
        upx = np.zeros(length, np.int16) if up is None else np.asarray(up, np.int16)[:length]
        if len(upx) < length:
            upx = np.concatenate([upx, np.zeros(length - len(upx), np.int16)])
        _write(paths["sent_both"], np.stack([upx, sent], axis=1), 2)
        res["paths"] = {k: str(v) for k, v in paths.items()}
    res["pcm"] = sent
    return res


def _write(path, data, channels):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.ascontiguousarray(data, dtype="<i2").tobytes())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir")
    ap.add_argument("--prefix")
    args = ap.parse_args(argv)
    d = Path(args.dir)
    rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
    st = (rep.get("sent") or {}).get("file_start")
    if st is None:
        print("report.json без sent.file_start: прогон до гоала 112")
        return 1
    prefix = args.prefix or Path(rep["files"]["up"]).name.rsplit("_up.wav", 1)[0]
    with wave.open(str(d / ("%s_up.wav" % prefix)), "rb") as w:
        up = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    r = build(d, prefix, read_journal(d / "sent.bin"), st["mic_first_frame"], st["sm"], len(up), up)
    r.pop("pcm")
    print(json.dumps({k: r[k] for k in ("seconds", "loss_pct", "levels", "align", "muted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
