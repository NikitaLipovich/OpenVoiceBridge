# Bus capture by the ESP32-S3-ETH

The S3-ETH board listens to the I2S bus of the LyraT and sends both lines to the PC over Ethernet. The LyraT does not
depend on it: every S3 pin wired to the LyraT is an input, and a restart of the S3 does not reach the call.

## Wiring

| LyraT | ESP32-S3-ETH | Direction |
|---|---|---|
| J21 `SCLK`, GPIO5 | GPIO15 | LyraT to S3 |
| J21 `LRCK`, GPIO25 | GPIO16 | LyraT to S3 |
| J21 `DSDIN`, GPIO26 (to the headphones) | GPIO17 | LyraT to S3 |
| J21 `ASDOUT`, GPIO35 (from the mic) | GPIO18 | LyraT to S3 |
| J21 `GND` | GND | common |
| J19 `MTMS`, GPIO14 (the board prints `WTWS`) | GPIO2, RX | LyraT to S3 |
| J19 `MTCK`, GPIO13 (the board prints `WTCK`) | GPIO1, TX | S3 to LyraT |

`MCLK` and 3.3 V stay unconnected. DIP switches on the LyraT: 4 and 6 ON, the rest OFF, no card.

Rules of this wiring, there are no series resistors on the lines:

- GPIO15-18 and GPIO2 are bare inputs without pulls. GPIO1 too, except while the LyraT is proven alive, see `S3_UART.md`.
  The firmware reads this back from the registers (`pins_ok`) at boot and every 2 s.
- The S3 must not be powered off while the LyraT is on. A restart is fine.
- For a permanent build add 100 to 220 ohm in each signal line.

## Bus format

16 kHz, 16 bit, stereo, Philips, the LyraT is the master. `DSDIN` carries the same samples in both slots. `ASDOUT`
carries two different channels: the two ADC channels of the codec.

## Stream

No address or password is built in. The PC sends `TAP1 <call> <mic>` to UDP port 5006, by broadcast until it knows the
board, and repeats it each second. The streams go to the sender, port 5004 for `DSDIN` and 5005 for `ASDOUT`, for 5 s
after the last request or until `TAP0`. `<call>` and `<mic>` pick the slots: `L`, `R` or `LR`. PCM without compression,
one packet per 240 frames. Header, little endian:

| Offset | Field |
|---|---|
| 0 | `S3`, version 1, stream (0 call, 1 mic) |
| 4 | u32 sequence, per stream |
| 8 | u32 frame of the first sample |
| 12 | u16 boot id, u8 channels, u8 frames / 8 |
| 16 | u32 I2S overflows, u32 send failures |

A sequence gap is a loss on the way to the PC. Growth of the overflow counter is a loss on the board. A new boot id is a
restart of the S3, and the recorder returns to broadcast by itself.

## Tools

- `tools/bench/s3.py`: build, flash, console, restart; finds the port by USB VID.
- `tools/bench/udp_record.py --tap`: records `call.wav`, `mic.wav`, `tap.json`.
- `tools/bench/tap_check.py`: sample by sample comparison with the LyraT's own stream; mic liveness.
- `tools/bench/round.py tap`: the full check.

## What can be proved about `ASDOUT` without playing sound

The ES8388 has no path from the DAC to the ADC and no test pattern, and the firmware tone replaces the mic after the
I2S read, so no known tone can be put on this line digitally. Two things are checked instead:

- The capture is exact: with the LyraT's DSP off, its own UDP stream of the same line is compared with the capture,
  sample by sample.
- The mic is alive: the level and spectrum of room sound change over time, unlike a stationary copy of the same
  recording, and the level follows the gain of the codec. This says nothing about quality. No SNR and no frequency
  response of the mic were measured here.
