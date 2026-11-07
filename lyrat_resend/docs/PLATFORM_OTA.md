# Platform of variant D: updates and logs of U1, U2, U3

Three controllers (goal 095; boards and wires: goals 066-068). Code in `platform/`, built without the boards; nothing here
has run on hardware yet. The image protocol, the log record and the UART frame are those of goal 090 (`docs/OTA.md`):
this file only says what is new.

| Board | Module | Role | Project |
|---|---|---|---|
| U1 | ESP32-WROVER DevKitC | HF to the phone, I2S master | `platform/u1_hf` |
| U2 | ESP32-WROVER DevKitC | HFP AG to the Bluetooth headphones | `platform/u2_ag` |
| U3 | ESP32-S3-DevKitC-1U N16R16VA | relay: Wi-Fi to the PC, recording, OTA | `platform/u3_relay` |

U1 and U2 are skeletons now: control link, new firmware with rollback, log. Bluetooth, sound and the modes of R21 come
in later goals; their pins are already in `plat_pins.h`.

```
PC --Wi-Fi UDP 5007 (images, ROM bundles)--> U3 --UART1 C1 frames 6/7--> U1      (normal path)
                                                --UART2 C2 frames 6/7--> U2
                                                --UART0 + EN1/IO0 (ROM loader)--> U1 or U2   (emergency path)
PC <-Wi-Fi UDP 5008 (log)------------------- U3 <-C1/C2 frame 5 (log records)-- U1, U2
                                                <-UART0 RX (console lines)----- U1 or U2
```

## Pins

One source: `platform/components/plat_core/include/plat_pins.h`, a table of every wire end with the wire name of the
goal 068 table. `lyrat_resend/tools/platform/plat_pins_check.py --mutate` compares it with
`goals_harness/.../068-node-placement/sandbox/scripts/asm_data.py` both ways, checks the pin rules of both chips and R13
in the code of U3. Change a pin in both places.

| U3 GPIO | Signal | To | Use |
|---|---|---|---|
| 1 / 2 | C1RX_D / C1TX | U1 GPIO13 / GPIO14 | UART1, control frames of U1 |
| 4 / 5 | C2RX_D / C2TX | U2 GPIO13 / GPIO14 | UART2, control frames of U2 |
| 39 / 40 | F1RX_D / F1TX | U1 RX0 / TX0 | UART0 when it serves U1 |
| 41 / 42 | F2RX_D / F2TX | U2 RX0 / TX0 | UART0 when it serves U2 |
| 21 / 12 | EN1_D / B01_D | U1 EN / GPIO0 | open drain; IO0 on **GPIO12**, not 47 (1.8 V on N16R16VA) |
| 13 / 14 | EN2_D / B02_D | U2 EN / GPIO0 | open drain |
| 15, 16, 17, 18 / 8 | BCLK, WS, DL, UL / RET_D | I2S bus / U1 GPIO34 | not used yet |
| 9 | VBATS | cell divider | ADC, not used yet |

Every wire has its 1 kOhm series resistor on a node board (R6-R17). The console of U3 itself is the native USB
(USB-Serial-JTAG); GPIO43/44 (the USB-UART bridge) carry nothing of interest.

R13 on these pins: the control TX of a node goes on only after a valid frame from it and off 5 s after the last one; the
UART0 TX reaches a node's RX0 only during a ROM job (otherwise input, R31/R32 hold RX0 high); EN and IO0 are inputs
without pulls unless pulled low on purpose, and nothing drives them high. At reset of U3 all of them are inputs, so a
restart or an update of U3 does not reset U1 or U2.

## Partitions

| Board | Flash | Table |
|---|---|---|
| U1, U2 | 4 MB | nvs 0x9000/24K, otadata 0xF000, phy 0x11000, ota_0 0x20000/0x1F0000, ota_1 0x210000/0x1F0000 |
| U3 | 16 MB | nvs 0x9000/24K, otadata 0xF000, phy 0x11000, ota_0 0x20000/3M, ota_1 0x320000/3M, u1_img 0x620000/2M, u2_img 0x820000/2M; 0xA20000 up free |

The flash size of the WROVER boards is not stated by the seller: 4 MB is the smallest module and works on all. A board
with more flash still uses 4 MB until the table changes. Image limit: 85 % of a slot, U1/U2 1 684 KB, U3 2 611 KB.
At goal 095: U1 and U2 skeletons 244 496 B (12 %), U3 407 968 B (13 %); the full HF firmware of the LyraT is 1.27 MB (64 %).

All three build with `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`. Self-test of a new image, within 90 s:

| Board | Passes when |
|---|---|
| U3 | Wi-Fi has an address; UART1 and UART2 are installed; each node sent a valid frame, or nothing at all (U2 not fitted) |
| U1, U2 | U3 answered a heartbeat (and the app's own hook, none yet). Never restarts during a call (hook `sco_active`). |

`FAILTEST` works as in `docs/OTA.md` on all three.

## Images over the network: targets on U3

UDP 5007, datagram `"OT", 1, target, ota_link request` exactly as `docs/OTA.md`, HELLO and HMAC token too.

| Target | What | Where it goes |
|---|---|---|
| 0 | U3's own app (`u3_relay`) | U3's next slot, restart after END |
| 1 / 2 | app of U1 (`u1_hf`) / U2 (`u2_ag`) | staged in `u1_img` / `u2_img`, read back, then sent over C1 / C2 with `ota_link` (window 2, 115200 or the BEGIN's baud), U3 follows the node until it runs the image (`done`) or rolled back |
| 0x11 / 0x12 | ROM bundle of U1 / U2 | staged, checked, then written through the node's ROM loader |

One thing at a time per node (its app and its ROM bundle share the staging partition); U3 refuses its own image while a
node's job runs. QUERY to target 0: U3 and one line per node (`alive`, `relay`, `rom`); to 1/2: the node in detail
(link counters, receiver states, relay, the node's STATUS text, ROM job with phase, bytes, syncs, restarts, ms).

The node side (`components/plat_node/node_link.c`) is the LyraT's receiver of goal 090 with the app's hooks: BEGIN refused
while `link_up` or `sco_active` without FORCE, writes paused during SCO, no restart during a call, STATUS text
`run= ver= elf= img= inv= selftest= baud=`.

## Emergency: the ROM loader

For a node whose bootloader, table or both app slots are broken, or which never got a firmware. The ROM loader cannot
be erased, so a node always comes back this way. `rom_flasher.c` speaks the ESP32 ROM protocol of esptool (no stub):
SLIP frames, SYNC, READ_REG of the chip magic (0x00F01D83), SPI_ATTACH, SPI_SET_PARAMS, CHANGE_BAUDRATE, FLASH_BEGIN
(erases before it answers, 30 s per MB allowed), FLASH_DATA in 1 KB blocks with checksum, SPI_FLASH_MD5 per segment.

ROM bundle (`rom_bundle.h`): header of 256 bytes `"RBND"`, version 1, chip 0, count, flash size, total, per segment
offset, size, MD5; then the data. `platform_ota.py bundle <build>` makes it from `flasher_args.json`: bootloader 0x1000,
table 0x8000, `ota_data_initial` 0xF000 (boots ota_0), app 0x20000. U3 checks it before anything is written: magic,
chip, sectors, order, inside the flash, sizes, **no segment in NVS** (0x9000-0xEFFF: pairings and calibration stay), the
app at 0x20000 is this node's project, the bootloader an ESP32 image.

The job (`u3_ota.c` rom task, `u3_port.c` port):

1. The node's control link is held (no frames to it), UART0 is taken from the console: TX onto the node's RX0, 115200.
2. Download mode: IO0 = 0, EN = 0, 100 ms, EN released, 250 ms, IO0 released (EN of a DevKitC rises through 10 kOhm ×
   1 µF). Then SYNC up to 10 times, READ_REG of the magic (another chip: stop, nothing written).
3. Optional baud from BEGIN (`--baud 460800`): CHANGE_BAUDRATE, then READ_REG at the new speed; no answer: the next
   session stays at 115200.
4. Each segment: FLASH_BEGIN, blocks. A failure the ROM reports (checksum): the segment again, up to 3 times. A timeout
   (cut line, dead target): the whole session again from the reset, 2 sessions in all.
5. SPI_FLASH_MD5 of each segment against the bundle; a difference: the session again.
6. Always at the end: IO0 released, EN pulsed: the node starts from its flash; TX back to input, the console back.
   Then U3 waits up to 60 s for the node's first frame: `done`, or `no_link` (written and checked, but the app is silent).

Time at 115200 for a skeleton bundle (282 KB): about 29 s plus the erase; at 460800 about 9 s (emulator, goal 095).
A 1.3 MB app: about 2.5 min at 115200, 40 s at 460800.

## Log of three boards

UDP 5008 of U3, the protocol of `docs/OTA.md` with more boards:

| board | Name in `udplog.py` | Lines |
|---|---|---|
| 2 | u3 | U3's own ESP_LOG |
| 3 / 4 | u1 / u2 | the node's ESP_LOG through its control frames (`CTL_T_LOG`), numbered by the node |
| 0x83 / 0x84 | u1con / u2con | the node's UART0 console read by U3: ROM boot text, panic, anything before its control UART; numbered by U3 |

Board numbers 0 and 1 (S3-ETH, LyraT) are those of the bench of direction 2: `udplog.py` tells all of them apart.

| Request (text) | Answer | |
|---|---|---|
| `LOG1 [u3] [u1] [u2]` | `LOG <on> {json}` | levels 0..5; repeat every 2 s; an S3-ETH reads the first two |
| `LOG0` | `LOG 0 {json}` | stop |
| `CON auto\|u1\|u2\|off` | `CON {"mode","listening",...}` | which console UART0 reads. `auto`: U1, but a node whose control link was alive and went silent for 3 s is followed for 30 s (its panic and its start) |
| `CMD u1\|u2 <text>` | `UR <node> <status> <text>` | a console command of the node: `ver`, `ota`, `ulog`, `link` |
| `MODE u1\|u2 <n>` | `MA <node> <status> <mode>` | R21, reserved, see below |

## Room for the modes of R21

Not implemented in goal 095, only the place:

- UART frame types `CTL_T_MODE = 8` (U3 to a node: `u8 mode, u8 flags`, more may follow) and `CTL_T_MODE_ACK = 9` (node
  to U3: `u8 status, u8 mode`; status 0 ok, 2 unsupported) in `ctl_frame.h`. The skeletons answer every MODE with
  status 2 and the mode asked, so the path PC → U3 → node → U3 → PC can be checked now.
- PC side: `MODE u1|u2 <n>` on UDP 5008 (`platform_ota.py mode u1 3`).
- The modes (R21): 1 BT headphones whole (U2 talks to the headphones, their microphone to the phone); 2 BT headphones +
  board microphone; 3 wired headphones (PCM5102A → TDA1308) + board microphone; 4 agent without headphones (the PC
  answers over RET: PC → Wi-Fi → U3 → U1). In every mode U1 is the headset of the phone and the call goes to the PC.
  The next goal fills in the handler on U1/U2 (sound routes), the command on U3 (to both nodes, the jitter buffer of RET)
  and may add more fields after `flags`; a node must keep answering unknown modes with status 2.

## Secrets

`platform/u3_relay/main/plat_secrets.h` (git ignores it; template `plat_secrets.h.example`): `WIFI_SSID`, `WIFI_PASS`,
`OTA_TOKEN` (at least 16 random characters). Without the file U3 builds, starts no Wi-Fi and can only be flashed over USB.
`platform_ota.py` reads the token from the same file or from `BENCH_OTA_TOKEN`.

## Commands

- Build: `idf.py -C platform/u1_hf build` (and `u2_ag`, `u3_relay`). The first flash of each board is over its USB, with
  its 3V3 jumper off (only one supply at a time): `idf.py -C platform/<p> -p COMx flash`. Delete an old `sdkconfig` first.
- `python lyrat_resend/tools/platform/platform_ota.py u3|u1|u2 <image> [--dry-run] [--test-rollback] [--force] [--baud N]`
- `python lyrat_resend/tools/platform/platform_ota.py u1-rom|u2-rom <build dir or bundle> [--baud 460800] [--dry-run]`
- `python lyrat_resend/tools/platform/platform_ota.py bundle <build dir> [-o file]`, `query [u1|u2]`, `cmd u1 ver`,
  `mode u1 2`, `con u2`
- `python lyrat_resend/tools/bench/udplog.py --host <U3> [--board u1|u2|u3|u1con|u2con] [--u2-level 3]`
- Address of U3: `--host`, `BENCH_U3_ADDR`, else a broadcast QUERY on 5007.

## Checks on the PC (no boards)

`lyrat_resend/tools/platform/`: `build_host.cmd` (MSVC: `plathost.dll` from `rom_flasher.c`, `rom_bundle.c`), then
`test_rom.py` (the C flasher against `rom_emu.py`, an emulator of the ESP32 ROM with virtual time: download mode by IO0 at
EN, SLIP, erase, NOR writes, MD5; faults: lost SYNCs, cut line, corrupt byte, lost answer, bit flip, stuck baud, other chip,
slow erase), `test_platform_tools.py` (`platform_ota.py` and `udplog.py` against `u3_emu.py`, whose receivers, sender,
frames and log rings are the C of goal 090 and whose ROM jobs are the C of goal 095), `plat_pins_check.py --mutate`.
Not covered there: the ESP-IDF glue of `u3_*.c` and `node_link.c` (drivers, `esp_ota_*`, Wi-Fi, sockets, tasks); it is
built and read only, and rewritten in Python in the emulator.
