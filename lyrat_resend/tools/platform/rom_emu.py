"""Emulator of the ESP32 ROM serial loader on a UART with virtual time, for the PC tests of rom_flasher.c (goal 095).

What it models of the real chip (ESP32 ROM, as esptool documents its protocol):
  - EN and IO0: when EN goes up, IO0 is sampled STRAP_MS later; 0 = download mode (boot text "waiting for download"),
    1 = runs from flash (boot text with SPI_FAST_FLASH_BOOT). Input is ignored outside download mode.
  - the wire: every byte takes 10 bit times at the baud of its sender; a receiver at another baud reads garbage.
  - SLIP frames; request 0x00 op len checksum data; reply 0x01 op len value data + 4 status bytes (status, error, 0, 0).
  - SYNC answered 8 times; READ_REG of the chip magic; SPI_ATTACH needs 8 bytes (ROM), SPI_SET_PARAMS 24; CHANGE_BAUDRATE
    answered at the old baud, then switched; FLASH_BEGIN (16 bytes) erases whole sectors before it answers (time per
    sector); FLASH_DATA checks the checksum and the sequence, programs NOR-like (bits only go 1 -> 0); SPI_FLASH_MD5
    answers 32 hex characters.
Faults, each once unless said: sync_ignore=N (first N SYNCs unanswered), drop_reply={op: n} (processed, answer lost),
corrupt_at=N (host byte N flipped on the wire), cut_at=N + cut_ms=M (line dead from host byte N for M ms; None:
forever), bitflip_block=K (block K reads back with one bit changed), no_download (IO0 not connected), baud_stuck
(CHANGE_BAUDRATE answered but ignored), magic=X (another chip), erase_ms_per_sector.
"""
import hashlib
import random
import struct

ROM_MAGIC_ESP32 = 0x00F01D83
CHIP_MAGIC_REG = 0x40001000
STRAP_MS = 5.0
BOOT_TEXT_DL = (b"ets Jun  8 2016 00:22:57\r\n\r\nrst:0x1 (POWERON_RESET),boot:0x3 (DOWNLOAD_BOOT(UART0/UART1/SDIO_REI_REO_V2))"
                b"\r\nwaiting for download\r\n")
BOOT_TEXT_RUN = (b"ets Jun  8 2016 00:22:57\r\n\r\nrst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n")
SYNC_DATA = bytes([0x07, 0x07, 0x12, 0x20]) + b"\x55" * 32

OP_FLASH_BEGIN, OP_FLASH_DATA, OP_FLASH_END, OP_SYNC, OP_READ_REG = 0x02, 0x03, 0x04, 0x08, 0x0A
OP_SPI_SET_PARAMS, OP_SPI_ATTACH, OP_CHANGE_BAUD, OP_MD5 = 0x0B, 0x0D, 0x0F, 0x13
ERR_BAD_DATA_LEN, ERR_BAD_CHECKSUM, ERR_INVALID_CMD, ERR_FAILED = 0xC0, 0xC1, 0xC5, 0xC6


class Clock:
    def __init__(self):
        self.ms = 0.0


def slip(data):
    return b"\xc0" + data.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc") + b"\xc0"


class SlipIn:
    def __init__(self):
        self.buf, self.in_frame, self.esc = bytearray(), False, False

    def feed(self, b):
        """A whole frame (bytes) when b ends one, else None."""
        if b == 0xC0:
            if self.in_frame and self.buf:
                out = bytes(self.buf)
                self.buf, self.in_frame = bytearray(), False
                return out
            self.in_frame, self.buf, self.esc = True, bytearray(), False
            return None
        if not self.in_frame:
            return None
        if self.esc:
            self.esc = False
            b = 0xC0 if b == 0xDC else 0xDB if b == 0xDD else b
        elif b == 0xDB:
            self.esc = True
            return None
        self.buf.append(b)
        return None


class Esp32Rom:
    def __init__(self, clock, flash_size=0x400000, seed=1, **faults):
        self.clock = clock
        rnd = random.Random(seed)
        # Old content everywhere: a write that misses its erase shows up in the MD5.
        self.flash = bytearray(rnd.getrandbits(8) for _ in range(flash_size))
        self.faults = dict(faults)
        self.erase_ms_per_sector = faults.get("erase_ms_per_sector", 20.0)
        self.write_ms_per_block = 3.0
        self.mode = "run"
        self.en_low = self.boot_low = False
        self.sample_at = None
        self.baud = 115200
        self.out = []               # [time_ms, byte, baud]
        self.slip = SlipIn()
        self.host_bytes = 0
        self.cut_from = self.cut_until = None
        self.events = []
        self.counts = {}
        self.syncs_ignored = 0
        self._reset_state()

    # --- state -------------------------------------------------------------------------------------------------------
    def _reset_state(self):
        self.attached = False
        self.params = None
        self.begin = None
        self.slip = SlipIn()

    def _now(self):
        return self.clock.ms

    def _sample(self):
        if self.sample_at is not None and self._now() >= self.sample_at:
            at = self.sample_at
            self.sample_at = None
            self.baud = 115200
            if self.boot_low and not self.faults.get("no_download"):
                self.mode = "download"
                self.events.append(("download", at))
                self._emit(BOOT_TEXT_DL, at)
            else:
                self.mode = "run"
                self.events.append(("run", at))
                self._emit(BOOT_TEXT_RUN, at)

    def advance_to(self, t):
        if self.sample_at is not None and self.sample_at <= t:
            self.clock.ms = max(self.clock.ms, self.sample_at)
            self._sample()
        self.clock.ms = max(self.clock.ms, t)

    def pins(self, en_low, boot_low):
        self._sample()
        if en_low and not self.en_low:
            self.mode = "reset"
            self.sample_at = None
            self.out = [o for o in self.out if o[0] <= self._now()]
            self._reset_state()
            self.events.append(("reset", self._now()))
        if self.en_low and not en_low:
            self.sample_at = self._now() + STRAP_MS
        self.en_low, self.boot_low = bool(en_low), bool(boot_low)

    def _line_dead(self, t):
        return self.cut_from is not None and t >= self.cut_from and (self.cut_until is None or t < self.cut_until)

    # --- wire --------------------------------------------------------------------------------------------------------
    def _emit(self, data, at=None, baud=None):
        t = max(self._now() if at is None else at, self.out[-1][0] if self.out else 0.0)
        baud = baud or self.baud
        for b in data:
            t += 10_000.0 / baud
            if not self._line_dead(t):
                self.out.append([t, b, baud])

    def host_write(self, data, host_baud):
        """The host sends data; returns when the last byte is out (the clock moves)."""
        self._sample()
        bit_ms = 10_000.0 / host_baud
        for b in data:
            self.host_bytes += 1
            self.clock.ms += bit_ms
            self._sample()
            n = self.host_bytes
            if self.faults.get("cut_at") == n:
                self.cut_from = self._now()
                ms = self.faults.get("cut_ms")
                self.cut_until = None if ms is None else self._now() + ms
                self.faults.pop("cut_at")
                self.events.append(("cut", self._now()))
            if self._line_dead(self._now()):
                continue
            if self.faults.get("corrupt_at") == n:
                b ^= 0x10
                self.faults.pop("corrupt_at")
            if host_baud != self.baud:
                b = (b * 7 + 3) & 0xFF      # framing at the wrong speed: garbage
            if self.mode != "download":
                continue
            frame = self.slip.feed(b)
            if frame is not None:
                self._command(frame)

    def host_read(self, cap, timeout_ms, host_baud):
        """Bytes that arrived by now; else waits (moves the clock) up to timeout_ms for the first one."""
        end = self._now() + timeout_ms
        while True:
            self._sample()
            if self.out and self.out[0][0] <= self._now():
                break
            times = [t for t in ((self.out[0][0] if self.out else None), self.sample_at) if t is not None]
            nxt = min(times) if times else None
            if nxt is None or nxt > end:
                self.advance_to(end)
                if self.out and self.out[0][0] <= self._now():
                    break
                return b""
            self.advance_to(nxt)
        got = bytearray()
        while self.out and self.out[0][0] <= self._now() and len(got) < cap:
            t, b, baud = self.out.pop(0)
            got.append(b if baud == host_baud else (b ^ 0x5A))
        return bytes(got)

    def host_flush(self):
        self._sample()
        self.out = [o for o in self.out if o[0] > self._now()]

    # --- commands ----------------------------------------------------------------------------------------------------
    def _reply(self, op, value=0, data=b"", status=0, error=0, delay_ms=0.1, count=1):
        drop = self.faults.get("drop_reply", {})
        if drop.get(op):
            drop[op] -= 1
            self.events.append(("reply_dropped", op))
            return
        pkt = struct.pack("<BBHI", 1, op, len(data) + 4, value) + data + bytes([status, error, 0, 0])
        at = self._now() + delay_ms
        for _ in range(count):
            self._emit(slip(pkt), at)
            at = None

    def _command(self, frame):
        if len(frame) < 8 or frame[0] != 0:
            return
        op, size, chk = struct.unpack_from("<BHI", frame, 1)
        data = frame[8:]
        self.counts[op] = self.counts.get(op, 0) + 1
        if len(data) != size:
            return self._reply(op, status=1, error=ERR_BAD_DATA_LEN)
        if op == OP_SYNC:
            if data != SYNC_DATA:
                return
            if self.syncs_ignored < self.faults.get("sync_ignore", 0):
                self.syncs_ignored += 1
                return
            return self._reply(op, value=0x20120707, count=8)
        if op == OP_READ_REG:
            addr, = struct.unpack("<I", data)
            magic = self.faults.get("magic", ROM_MAGIC_ESP32)
            return self._reply(op, value=magic if addr == CHIP_MAGIC_REG else 0)
        if op == OP_SPI_ATTACH:
            if size != 8:
                return self._reply(op, status=1, error=ERR_BAD_DATA_LEN)
            self.attached = True
            return self._reply(op)
        if op == OP_SPI_SET_PARAMS:
            if size != 24:
                return self._reply(op, status=1, error=ERR_BAD_DATA_LEN)
            self.params = struct.unpack("<6I", data)
            return self._reply(op)
        if op == OP_CHANGE_BAUD:
            new, old = struct.unpack("<II", data)
            self._reply(op)
            if not self.faults.get("baud_stuck"):
                # The answer is queued at the old speed already; the ROM listens at the new one from now on.
                self.baud = new
            return
        if op == OP_FLASH_BEGIN:
            if size != 16:
                return self._reply(op, status=1, error=ERR_BAD_DATA_LEN)
            erase, blocks, bsize, off = struct.unpack("<4I", data)
            if not self.attached or off + erase > len(self.flash):
                return self._reply(op, status=1, error=ERR_FAILED)
            first, last = off // 4096, (off + erase + 4095) // 4096
            self.flash[first * 4096:last * 4096] = b"\xff" * ((last - first) * 4096)
            self.begin = {"off": off, "blocks": blocks, "bsize": bsize, "seq": 0, "written": 0}
            return self._reply(op, delay_ms=(last - first) * self.erase_ms_per_sector)
        if op == OP_FLASH_DATA:
            if not self.begin:
                return self._reply(op, status=1, error=ERR_FAILED)
            dlen, seq, _, _ = struct.unpack_from("<4I", data)
            block = data[16:16 + dlen]
            c = 0xEF
            for x in block:
                c ^= x
            if len(block) != dlen or c != chk:
                return self._reply(op, status=1, error=ERR_BAD_CHECKSUM)
            b = self.begin
            if seq != b["seq"] or seq >= b["blocks"]:
                return self._reply(op, status=1, error=ERR_FAILED)
            at = b["off"] + seq * b["bsize"]
            for i, x in enumerate(block):
                if at + i < len(self.flash):
                    self.flash[at + i] &= x
            if self.faults.get("bitflip_block") == b["written"] and "bitflip_done" not in self.faults:
                self.flash[at + 17] ^= 0x04
                self.faults["bitflip_done"] = True
            b["seq"] += 1
            b["written"] += 1
            return self._reply(op, delay_ms=self.write_ms_per_block)
        if op == OP_FLASH_END:
            return self._reply(op)
        if op == OP_MD5:
            addr, n, _, _ = struct.unpack("<4I", data)
            digest = hashlib.md5(bytes(self.flash[addr:addr + n])).hexdigest().encode()
            return self._reply(op, data=digest, delay_ms=1 + n / 0x100000 * 300)
        return self._reply(op, status=1, error=ERR_INVALID_CMD)

    # --- for the tests -----------------------------------------------------------------------------------------------
    def region(self, off, n):
        return bytes(self.flash[off:off + n])
