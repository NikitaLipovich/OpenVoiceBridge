#!/usr/bin/env python3
"""Passive LyraT serial watcher. Never asserts DTR/RTS and sends nothing.

Survives the port disappearing and counts port drops and board resets.
Writes serial.log, events.jsonl and summary.json into --out.
Based on goals_harness/tools/seeds/watch_power.py and SerialProbe from lyrat_resend/harness.

With --no-open the port is never opened, only its presence is polled: this is the
"port closed" bench variant, so reset counters stay null in that mode.

Exit code: 0 measured, 2 port busy for the whole window (another program holds it).
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import serial
from serial.tools import list_ports

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import DEFAULT_PORT, redact, write_json  # noqa: E402

ROM_RST = re.compile(r"rst:0x([0-9a-f]+) \(([A-Z0-9_]+)\)")
FW_RST = re.compile(r"BOOT reset_reason=([A-Z0-9_]+)")
UPTIME = re.compile(r"^[IWED] \((\d+)\) ")
SLC_UP = re.compile(r"SLC CONNECTED")
HFP_DOWN = re.compile(r"HFP conn state=0\b")
PANIC = re.compile(r"Guru Meditation|abort\(\) was called|Backtrace:|Brownout detector|stack overflow", re.I)


class SerialWatch:
    def __init__(self, port, out, no_open=False, baud=115200, echo=False):
        self.port, self.out, self.no_open, self.baud, self.echo = port, Path(out), no_open, baud, echo
        self.out.mkdir(parents=True, exist_ok=True)
        self.log = (self.out / "serial.log").open("a", encoding="utf-8", errors="replace")
        self.events = (self.out / "events.jsonl").open("a", encoding="utf-8")
        self.t0 = time.monotonic()
        self.port_opens = self.port_drops = self.open_denied = 0
        self.lines = self.panics = 0
        self.resets = []            # one dict per detected reset
        self.rom_reasons = Counter()
        self.fw_reasons = Counter()
        self.last_uptime = None
        self.max_uptime = 0
        self.boot_seen = False      # a ROM banner was seen and no uptime line followed yet
        self.slc_ups = self.slc_drops = 0
        self.slc_up = False
        self.sp = None
        self.recent = []            # redacted lines with their time, for the caller to inspect

    def now(self):
        return round(time.monotonic() - self.t0, 3)

    def event(self, kind, **kw):
        rec = {"t": self.now(), "wall": datetime.now().isoformat(timespec="milliseconds"), "ev": kind}
        rec.update(kw)
        self.events.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.events.flush()
        if self.echo:
            print(json.dumps(rec, ensure_ascii=False), flush=True)

    def add_reset(self, source, reason=None):
        self.resets.append({"t": self.now(), "source": source, "reason": reason, "uptime_before_ms": self.last_uptime})
        self.event("board_reset", source=source, reason=reason, uptime_before_ms=self.last_uptime)
        self.slc_up = False

    def feed(self, raw: bytes):
        line = redact(raw.decode("utf-8", "replace").rstrip())
        self.lines += 1
        self.log.write("%9.3f %s\n" % (self.now(), line))
        self.log.flush()
        self.recent.append((self.now(), line))
        del self.recent[:-4000]
        m = ROM_RST.search(line)
        if m:
            self.rom_reasons[m.group(2)] += 1
            self.add_reset("rom_banner", m.group(2))
            self.boot_seen = True
        m = FW_RST.search(line)
        if m:
            self.fw_reasons[m.group(1)] += 1
            self.event("fw_reset_reason", reason=m.group(1))
        m = UPTIME.match(line)
        if m:
            up = int(m.group(1))
            # A log timestamp going backwards means a reset whose ROM banner was lost.
            if self.last_uptime is not None and up < self.last_uptime - 1000 and not self.boot_seen:
                self.add_reset("uptime_regress")
            self.boot_seen = False
            self.last_uptime = up
            self.max_uptime = max(self.max_uptime, up)
        if SLC_UP.search(line):
            self.slc_ups += 1
            self.slc_up = True
            self.event("slc_up")
        if HFP_DOWN.search(line) and self.slc_up:
            self.slc_drops += 1
            self.slc_up = False
            self.event("slc_drop")
        if PANIC.search(line):
            self.panics += 1
            self.event("panic", line=line[:160])

    def present(self):
        return any(p.device.upper() == self.port.upper() for p in list_ports.comports())

    def run_no_open(self, seconds):
        was = self.present()
        self.event("port_present" if was else "port_absent")
        end = self.t0 + seconds
        while time.monotonic() < end:
            time.sleep(0.5)
            now = self.present()
            if was and not now:
                self.port_drops += 1
                self.event("port_drop")
            elif now and not was:
                self.event("port_back")
            was = now

    def pump(self, end, until=None):
        """Read lines until the monotonic time `end` or until `until()` is true. Opens the port as needed."""
        while time.monotonic() < end and not (until and until()):
            if self.sp is None:
                try:
                    # DTR/RTS are wired to EN/IO0: keep them released before open().
                    sp = serial.Serial()
                    sp.port, sp.baudrate, sp.timeout, sp.dtr, sp.rts = self.port, self.baud, 0.3, False, False
                    sp.open()
                    self.sp = sp
                    self.port_opens += 1
                    self.event("port_open")
                except Exception as e:
                    denied = "PermissionError" in str(e)
                    self.open_denied += 1 if denied else 0
                    self.event("open_failed", denied=denied, err=str(e)[:80])
                    time.sleep(0.5)
                continue
            try:
                raw = self.sp.readline()
            except Exception as e:
                self.port_drops += 1
                self.event("port_drop", err=str(e)[:80])
                self.close_port()
                continue
            if raw:
                self.feed(raw)

    def close_port(self):
        """Release the port, for example so that esptool can use it."""
        if self.sp is not None:
            try:
                self.sp.close()
            except Exception:
                pass
            self.sp = None

    def send(self, text):
        """Send one console command, False if the port is gone. The passive `run` modes never call this."""
        if self.sp is None:
            self.event("send_no_port", text=text)
            return False
        try:
            self.sp.write((text + "\n").encode())
        except (OSError, serial.SerialException) as e:
            self.event("send_failed", text=text, err=str(e)[:80])
            return False
        self.event("sent", text=text)
        return True

    def pulse_reset(self, hold_s=0.15):
        """Hardware reset through EN: RTS low while DTR stays released keeps IO0 high, so the app boots."""
        self.sp.rts = True
        time.sleep(hold_s)
        self.sp.rts = False
        self.event("reset_pulse")

    def run_open(self, seconds):
        self.pump(self.t0 + seconds)
        self.close_port()

    def summary(self, seconds):
        closed = self.no_open
        return {
            "port": self.port,
            "no_open": closed,
            "seconds": seconds,
            "elapsed_s": self.now(),
            "port_opens": self.port_opens,
            "port_drops": self.port_drops,
            "open_denied": self.open_denied,
            "lines": self.lines,
            "board_resets": None if closed else len(self.resets),
            "rom_reset_reasons": None if closed else dict(self.rom_reasons),
            "fw_reset_reasons": None if closed else dict(self.fw_reasons),
            "uptime_before_reset_ms": None if closed else [r["uptime_before_ms"] for r in self.resets],
            "reset_times_s": None if closed else [r["t"] for r in self.resets],
            "panics": None if closed else self.panics,
            "max_uptime_ms": None if closed else self.max_uptime,
            "last_uptime_ms": None if closed else self.last_uptime,
            "slc_ups": None if closed else self.slc_ups,
            "slc_drops": None if closed else self.slc_drops,
            "slc_up_at_end": None if closed else self.slc_up,
        }

    def finish(self, seconds):
        result = self.summary(seconds)
        write_json(self.out / "summary.json", result)
        self.log.close()
        self.events.close()
        return result

    def run(self, seconds):
        if self.no_open:
            self.run_no_open(seconds)
        else:
            self.run_open(seconds)
        return self.finish(seconds)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--out", required=True, help="output directory, created if missing")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--no-open", action="store_true", help="only poll port presence, never open it")
    ap.add_argument("--echo", action="store_true", help="print events live")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = SerialWatch(args.port, args.out, args.no_open, echo=args.echo).run(args.seconds)
    print(json.dumps(result, ensure_ascii=False))
    busy = not args.no_open and result["port_opens"] == 0 and result["open_denied"] > 0
    return 2 if busy else 0


if __name__ == "__main__":
    sys.exit(main())
