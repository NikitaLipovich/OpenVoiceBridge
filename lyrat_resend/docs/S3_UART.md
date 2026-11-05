# Control channel through the ESP32-S3-ETH

Commands from the PC reach the LyraT over Ethernet and a UART, and a failure of the S3 does not reach the call. The LyraT's
own console on UART0 stays as it was.

```
PC -- UDP 5006 --> S3 -- UART1, 115200 8N1 --> LyraT (UART1)
```

| LyraT | S3 | Direction |
|---|---|---|
| GPIO14, TX | GPIO2, RX | LyraT to S3 |
| GPIO13, RX | GPIO1, TX | S3 to LyraT |

The wiring and its rules: `S3_TAP.md`.

## Frame

Code: `main/ctl_frame.h`, used by both firmwares. Little endian.

| Offset | Field |
|---|---|
| 0 | `A5 5A` |
| 2 | type: 1 command, 2 reply, 3 heartbeat, 4 heartbeat ack |
| 3 | u16 seq |
| 5 | u16 payload length, at most 1024 |
| 7 | xor of bytes 2 to 6 |
| 8 | payload |
| end | u16 CRC16-CCITT (poly 0x1021, init 0xFFFF) of bytes 2 up to the end of the payload |

A reply carries the seq of its command and the payload is a status byte (0 ok, 1 unknown command, 2 not valid text) followed
by the text the command prints. The header has its own check, so noise cannot announce a long frame and swallow what follows.
A frame that stops arriving is dropped after 200 ms: at 9600 baud the driver hands over bytes per 120, about 125 ms apart.

Commands are the ones of the console, and run under the same mutex, one at a time. Their output goes into the reply instead
of the console. Peer addresses are left out of the reply, since it leaves the board over the network. A command repeated with
the same seq is not run again: the LyraT answers from its copy of the last reply. Without this a repeat after a lost reply
would flip a toggle such as `hpf` back.

The LyraT sends a heartbeat every second, payload: u32 uptime in seconds, u8 flags (bit 0 SLC, bit 1 SCO), u8 tone.

## Who drives GPIO1

The line has no series resistors and the LyraT's GPIO13 has a 4.7 kOhm pull-down. GPIO1 of the S3 is an output only while the
LyraT is proven alive: it goes on with the first valid frame and back to an input 5 s after the last one. So an S3 that restarts
or stops leaves the line low at the LyraT, which reads as one break and is counted; it does not raise interrupts continuously.
`pins_ok` reads the registers and allows only this pin to be an output, and only in that state.

An unpowered LyraT with a live S3 can see its pin driven for up to 5 s. The rule that the S3 stays on while the LyraT is on
is unchanged; for a permanent build add 100 to 220 ohm in each signal line.

## Requests on UDP 5006

The answer goes to the source port of the request.

| Request | Meaning | Answer |
|---|---|---|
| `UC <seq> <command>` | One console command | `UR <seq> <status>\n<text>`; status 3 with the TX off |
| `UQ` | S3 counters of the channel, no stream starts | JSON |
| `UT 0\|1\|A` | TX off, on, following the LyraT | `UT <mode>`, mode 2 is `A` |
| `UB <baud>` | Baud rate of the S3's end | `UB <baud>` |
| `UW <hex>` | Raw bytes to the LyraT, for tests | `UW <n>`, -1 with the TX off |

The PC repeats a `UC` on a timeout with the same seq. There is no authentication: anything in the local network can send these,
the same as for the streams.

Console commands of the LyraT that belong to this channel: `ustat` (its counters), `ubaud N` (baud of its end, for the line
check), `ugarb N` (noise toward the S3, for a test).

Noise suppression of the microphone: `ns off|sns|web0|web1|web2` switches the mode with the channel open, the reply is `ns = <mode>`, status 1 for a name that does not exist. It takes effect on the next frame, with a fade of 5 ms on both sides. `status` shows `ns=<asked> applied=<running> switches=<count>` and the time of a frame. `nscal` makes `sns` take a new noise profile. `cpu` prints the load of every task since its last call. Anyone in the local network can send these, as all commands here. How to compare the modes by ear: `VOICE_CHECK.md`.

## Tools

- `tools/bench/s3_uart.py`: `query`, `cmd "tone 1"`, `tx 0|1|A`, `linecheck` (both directions at 9600, GPIO1 stays an input until
  the LyraT was read).
- `tools/bench/round.py uart`: the full check. `round.py audio --uart-load 10`: the audio stage with commands running.
