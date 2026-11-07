# Updates over the network and the debug log over UDP

After one flash over USB both boards of the bench take new firmware without USB, roll back by themselves when the new
image does not come up, and send their log to the PC. The LyraT has no Wi-Fi (R13): its image and its log go through the
ESP32-S3-ETH over the control UART of `S3_UART.md`.

```
PC --UDP 5007 (image)----> S3 --UART1 frames 6/7 (image)--> LyraT
PC <-UDP 5008 (log)------- S3 <-UART1 frame 5 (log)-------- LyraT
PC --UDP 5008 (LOG1/LOG0)> S3 --heartbeat ack (on, level)--> LyraT
```

The platform of variant D (U1, U2 through U3 over Wi-Fi, ROM loader, three-board log) uses the same protocol:
`PLATFORM_OTA.md`.

Code: `main/ota_link.[ch]` (image protocol, both ends), `main/ota_sha256.[ch]`, `main/dbg_log.[ch]` (log ring and record),
`main/ctl_frame.h` (UART frame). These files are plain C without platform calls: both firmwares build them, and the PC
tests build the same files with MSVC (goal 090 sandbox). The transport and the flash are behind function tables, so a
third board reached over Wi-Fi uses the same code.

## Partitions

### LyraT, 4 MB

| Name | Type | Offset | Size |
|---|---|---|---|
| nvs | data/nvs | 0x9000 | 0x6000 |
| otadata | data/ota | 0xF000 | 0x2000 |
| phy_init | data/phy | 0x11000 | 0x1000 |
| ota_0 | app | 0x20000 | 0x1F0000 (1984 KB) |
| ota_1 | app | 0x210000 | 0x1F0000 |

`nvs` keeps its place and size, so pairings and the radio calibration survive the change of the table. File:
`partitions_ota.csv`, chosen in `sdkconfig.defaults`. The image was 1.25 MB at goal 090, 63 % of a slot; the limit is
85 %, 1.69 MB.

### S3-ETH, 16 MB

| Name | Type | Offset | Size |
|---|---|---|---|
| nvs | data/nvs | 0x9000 | 0x6000 |
| otadata | data/ota | 0xF000 | 0x2000 |
| phy_init | data/phy | 0x11000 | 0x1000 |
| ota_0 | app | 0x20000 | 0x300000 |
| ota_1 | app | 0x320000 | 0x300000 |
| lyrat_img | data/0x40 | 0x620000 | 0x200000 |

`lyrat_img` holds a LyraT image until it has been checked whole and sent on. File: `s3_eth_bridge/partitions_ota.csv`.

### The first time, over USB (goal 091)

The bootloader with rollback, the table and `otadata` are written by USB once. `sdkconfig` is generated and not in git:
an old one keeps the old table, so delete it (or build with `-D SDKCONFIG=<new file>`) before the first build of this
version. `idf.py flash` then writes bootloader, table, `ota_data_initial.bin` and the app into `ota_0`. NVS stays.
`fw.py` checks the hashes of three images; with `otadata` there are four, see goal 091.

## Rollback and self-test

Both boards build with `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`. A new image starts in `PENDING_VERIFY`. It confirms itself
only after its self-test; until then any restart, crash or watchdog brings the old image back, done by the bootloader.

| Board | Self-test, within 90 s of the start |
|---|---|
| S3-ETH | Ethernet has an IP address; both I2S channels run; the control UART is installed. The LyraT is alive (a valid frame came) and then both I2S lines delivered samples, or the LyraT is silent: no byte on the line at all. Bytes without a valid frame fail. |
| LyraT | The Bluetooth stack came up (`bt_init` returned); a valid frame from the S3 came over the UART. |

A failed self-test calls `esp_ota_mark_app_invalid_rollback_and_reboot()`. The LyraT does not restart during a call: when
the 90 s end with SCO on, it waits for the call to end. An image that was flashed over USB is valid from the start.

`FAILTEST` (flag of BEGIN, `ota.py --test-rollback`) stores a mark in NVS of the receiving board (namespace `ota`, key
`failtest`); the new image finds it at its first start, clears it, and fails its self-test on purpose after 5 s. The board must come back with the old image and report the
new one as `last_invalid`. This is how 091 checks the rollback on the hardware.

## Image protocol (`ota_link`)

The same messages on every hop. Little endian.

Request: `0 u8 op, 1 u32 session`, then by op:

| op | Name | Rest |
|---|---|---|
| 1 | BEGIN | `5 u32 size, 9 sha256[32], 41 u8 flags, 42 u32 baud, 46 auth[32]`, 78 bytes |
| 2 | DATA | `5 u32 offset, 9 data`, 1 to 1008 bytes |
| 3 | END | — |
| 4 | ABORT | — |
| 5 | STATUS | — (the session is ignored) |

Reply: `0 u8 op, 1 u8 status, 2 u32 session, 6 u32 next`, and for STATUS a text `state=... size=... next=... err=...` with
the counters and the version of the running image.

Flags: `1 FORCE` (also with SLC up or SCO on), `2 FAILTEST`, `4 DRYRUN` (receive and check everything, activate nothing).

Status: `0 ok, 1 busy, 2 refused, 3 no_session, 4 too_big, 5 bad_image, 6 flash, 7 sha, 8 incomplete, 9 auth, 10 bad_req,
11 verify, 12 no_slot` (13 `timeout` exists at the sender only).

### Receiver

- BEGIN opens a session: token proof (network hop only), room in the target slot, the rule of calls, then the slot is erased
  for `size` bytes (seconds: the reply comes late, the sender repeats BEGIN and the repeat is answered without effect).
  A BEGIN with another session replaces an open one: a sender that died leaves nothing behind.
- DATA is taken in order only. `next` is the count of bytes taken, so each reply is a cumulative ack. A repeat or a chunk
  past a gap is answered with the same `next` and dropped. A corrupted chunk never arrives: the UART frame and UDP have
  their checksums, and the SHA-256 at the end covers the rest.
- The first chunk must hold the first 208 bytes: magic `0xE9`, chip (`ESP32` for the LyraT, `ESP32-S3` for the S3), magic
  of the app description and the project name (`lyrat_resend`, `s3_eth_bridge`). Anything else is `bad_image` and the
  session ends.
- END: all bytes there (else `incomplete` with `next`), SHA-256 equal to the one of BEGIN (else `sha`, the image is dropped),
  the platform's own check (`esp_ota_end`, else `verify`). Then the image is set to boot and the board restarts after the
  reply, or with DRYRUN nothing is activated. The answer of the last END is kept, a repeat gets it again.
- 30 s without a request closes the session (`timeout`, the image is dropped).

### Sender

Go-back-N: up to `window` chunks of 1008 bytes in flight. No progress within `rto` sends again from `next`; a reply with
the same `next` while more is in flight (a gap) sends again at once, once per position. `busy` waits 1 s and goes on, for
at most 20 min. `no_session` (the receiver restarted) opens a new session from the start, at most 3 times (not on the
network hop: a new session needs a new nonce, `ota.py` does that itself).

| Hop | window | rto | BEGIN repeat |
|---|---|---|---|
| PC to S3, UDP | 4 | 0.3 s | 1 s, up to 90 s |
| S3 to LyraT, UART | 2 | 0.6 s | 2 s, up to 90 s |

### Calls

The LyraT refuses BEGIN while SLC is up or SCO is on (`refused`), unless FORCE. During a session, writes pause while SCO is
on (`busy`), with FORCE too: a flash write stops both cores for up to tens of ms and the call would hear it. The restart
after END waits for the end of a call. Erasing and writing run in their own task below the audio tasks; the control UART
task keeps running, the heartbeat goes on. The ACL link survives the pauses of erasing, the controller runs from IRAM.

### UART hop

Frame types of `ctl_frame.h`: `5 LOG` (LyraT to S3), `6 OTA` (S3 to LyraT, a request above), `7 OTA_ACK` (LyraT to S3, a
reply above). The frame seq counts per sender and has no meaning here, the session does.

Baud: 115200, the speed `round.py uart` proved on these wires (no series resistors, Dupont). An image of 1.25 MB takes about
2 min. BEGIN can ask for another baud (`ota.py --baud 230400|460800`): the LyraT answers at the old one and switches, the S3
switches on the answer. Without a valid frame within 3 s the LyraT goes back to 115200 and drops the session; the S3 then
starts again at 115200. Both go back to 115200 at the end of a session. A higher baud needs the line check of 091 first.

The receive ring of the LyraT holds 4 KB, two chunks of the window and the heartbeats.

### Network hop

UDP 5007 on the S3. Datagram: `"OT", u8 version 1, u8 target, payload`; target `0` the S3 itself, `1` the LyraT. The reply
comes from 5007 to the source port of the request, with the same four bytes in front.

| op | Name | Reply |
|---|---|---|
| 0x10 | HELLO | status 0, text: 32 hex digits of a nonce |
| 0x11 | QUERY | status 0, text: JSON state of both boards |
| 1-5 | as above | as above |

Token: `auth = HMAC-SHA256(token, nonce || BEGIN bytes 0..45)`. The nonce is random, lives 60 s and serves one BEGIN. The
token never goes over the network, a recorded BEGIN cannot be sent again, and the SHA-256 of the image is under the proof:
a chunk changed on the way ends in `sha`. A BEGIN with the session already open is answered without the check and without
effect.

The token is in `s3_eth_bridge/main/ota_secrets.h` (`#define OTA_TOKEN "..."`, at least 16 random characters), which git
ignores; `ota_secrets.h.example` is the template. Without the file the S3 builds, logs a warning and refuses every BEGIN
from the network with `auth`. `ota.py` reads the token from the same file, or from `BENCH_OTA_TOKEN`.

Target 1: the S3 receives the image into `lyrat_img` with the same checks (chip ESP32, project `lyrat_resend`), then sends
it to the LyraT over the UART with the flags and baud of the BEGIN. While that runs, a new BEGIN for target 1 is
`refused`. After the LyraT's END it asks the LyraT for STATUS every 2 s until the LyraT runs the image (its ELF SHA-256)
and has confirmed it, or ran the old one again (rolled back), for 180 s at most.

QUERY, JSON:

```
{"fw":"s3tap","boot":<id>,"up_ms":..,"ver":"..","elf":"<16 hex>","run":"ota_0","img":"valid|pending|undefined|...",
 "selftest":"pass|wait|fail|off","last_invalid":{"ver":"..","elf":"<16 hex>"}|null,"token":0|1,
 "rx":[{"target":0,"state":"idle|recv|done","size":..,"next":..,"err":".."},{"target":1,...}],
 "lyrat":{"relay":"idle|send|verify|done|rolled_back|failed","err":"..","sent":..,"acked":..,"size":..,
          "resent":..,"busy":..,"baud":..,"elf_want":"<16 hex>","status":"<STATUS text of the LyraT>","status_age_ms":..}}
```

## Debug log

Every `ESP_LOG` line goes to the console as before and into a ring of fixed slots (`dbg_log.c`, hook
`esp_log_set_vprintf`). Writing never waits: a full ring drops the new line, counts it, and the line number goes on. The
oldest lines stay, so the lines of the start, before any network, wait in the ring for the first listener.

| Board | Ring | Where |
|---|---|---|
| S3-ETH | 128 lines of up to 160 characters, plus 128 for the LyraT's lines | internal RAM |
| LyraT | 96 lines | PSRAM |

Record (the same in a UART frame and in a UDP datagram): `0 u8 board (0 S3, 1 LyraT), 1 u8 level (1 E .. 5 V, 0 other),
2 u16 boot id, 4 u32 line number since boot, 8 u32 ms since boot, 12 u32 lines dropped on the board so far, 16 u16 length,
18 text`. UDP datagram: `"UL", u8 version 1, u8 count`, then records, at most 1400 bytes.

Losses: a gap in the line numbers of one board whose `dropped` grew by the same count was lost on the board; the rest of
the gap was lost on the way (UDP, or the S3's relay ring, counted in its `stat` line as `lrelay_lost`). A new boot id is a
restart of that board.

Control on UDP 5008 of the S3, the answer goes to the source port, the log too:

| Request | Meaning |
|---|---|
| `LOG1 [s3_level] [lyrat_level]` | send the log here for 10 s, levels 0..5 (none, E, W, I, D, V), default 3. Repeat every 2 s. |
| `LOG0` | stop |

The answer is `LOG <on> {json}` with the counters of both rings. One listener: the last one to ask. Debug levels above I
need `CONFIG_LOG_MAXIMUM_LEVEL` in the build.

The S3 tells the LyraT in each heartbeat ack (payload `u8 on, u8 level`, an old S3 sends none, that is off) whether to send
its log and at which level. The LyraT then drains its ring in frames of type 5, at most 2 KB/s, a sixth of the line.

## Commands

- `python tools/bench/ota.py s3 build/.../s3_eth_bridge.bin [--dry-run] [--test-rollback]`: image to the S3.
- `python tools/bench/ota.py lyrat build/.../lyrat_resend.bin [--force] [--baud N] [--dry-run] [--test-rollback]`: image to
  the LyraT through the S3.
- `python tools/bench/ota.py query`: state of both boards.
- `python tools/bench/udplog.py [--seconds N] [--board s3|lyrat] [--level W] [--grep RE]`: log of both boards to
  `goals_harness/bench_runs/udplog/<stamp>/`.
- LyraT console: `ota` prints the running image, its state and the receiver's counters; `ulog` the log ring (listener,
  level, lines pending and dropped). Both also over `s3_uart.py cmd ota`.
- STATUS text of the LyraT (the S3 follows an update with it): `state= size= next= err= ... run=ota_0 ver=.. elf=<16 hex>
  img=valid|pending|.. inv=<16 hex of the last image that failed, or -> selftest=off|wait|pass|fail baud=..`.

`ota.py` checks the image before sending (chip, project, size), waits for the restart, compares the ELF SHA-256 of the
running image with the file, waits for `valid`. Exit code: 0 done, 1 refused or failed, 2 S3 not found, 3 rolled back
(with `--test-rollback`: 0 when it rolled back as asked), 4 timeout.

## When everything is broken: back over USB

Over USB, as before: `idf.py -C s3_eth_bridge flash` (S3, its own USB) or `fw.py flash` (LyraT, COM15). A board that rolls
back has its old image and needs nothing. A board whose both slots fail: `idf.py erase-otadata` then `flash`; `nvs` stays.
`erase_flash` is never needed and would take the pairings.
