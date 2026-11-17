# Tests on the PC

Everything of this bench that can be checked without the boards, the phone, a COM port, adb or sound, with one command:

```
python lyrat_resend/tools/run_tests.py            all checks, the firmware builds too (first run about 15 min, then minutes)
python lyrat_resend/tools/run_tests.py --quick    without the builds of the firmwares and of the Android probe
python lyrat_resend/tools/run_tests.py --only s3_sim,bench_tools
python lyrat_resend/tools/run_tests.py --list
```

It prints PASS, FAIL or SKIP per check and writes `goals_harness/bench_runs/tests/<stamp>/summary.json` (status, time,
counts of tests, notes) with the log of each check beside it. Exit code 0 only when every check that ran passed; a unittest
case that skipped counts as a failure, because the runner provides whatever the case needs. `--quick` and `--only` may skip
what they leave out on purpose.

Needs: Python 3.13 with numpy and pyserial, Visual Studio Build Tools 2022 (`cl`, path of `vcvars64.bat` in `OTA_VCVARS`
if elsewhere), ESP-IDF v5.5.2 (`IDF_PATH`, `IDF_TOOLS_PATH`, as `tools/bench/fw.py`), the Android SDK with a JDK for
`sco_probe` (as `tools/android/build_apk.py`), and `s3_eth_bridge/main/ota_secrets.h` for the secrets check.

## Checks

| Name | What it proves | Code |
|---|---|---|
| `host_dll` | The protocol C code of both firmwares (`ota_link`, `ota_sha256`, `dbg_log`, `ctl_frame.h`, `call_limit.h`) builds with MSVC `/W4 /WX` | `tools/ota_test/build_host.cmd` |
| `platform_dll` | The ROM loader C code of the platform builds the same way | `tools/platform/build_host.cmd` |
| `static` | No value of the ignored secret files in any tracked file (values never printed), the secret and local files not tracked and covered by `.gitignore`, the S3 defaults to `S3_NET_ETH 0` | `tools/tests/static_check.py` |
| `fw_build` | S3 and LyraT built into `bench_runs/tests/builds/<board>` with their own sdkconfig: the partition table of the image = `partitions_ota.csv` = the tables of `OTA.md`, rollback on, image within 85 % of the slot and whole (checksum, hash), Wi-Fi name, password and token in the S3 image (bytes compared, never printed; a ccache trap is rebuilt once with `CCACHE_RECACHE=1`), the working `build/`, `sdkconfig` and git status unchanged | `tools/tests/fw_build_check.py` |
| `sco_probe` | The Android probe builds; it is not installed | `tools/android/build_apk.py --out <run>/apk` |
| `c_ctl_frame` | UART framing in C: empty and largest payload, one byte too long, every single bit flip refused, garbage, a frame inside a false header, cut by a pause, clock wrap, repeated seq | `tools/tests/test_ctl_frame.py` |
| `c_dbg_log` | Log ring in C: a line longer than a slot in parts and joined again, slot boundary, ring full mid-line, a repeated datagram; the stat line of old and new firmware; the S3's UDP sender keeps a datagram whose send failed (no IP) and sends it first when the network is back, a gap then counts only what the ring dropped (goal 094 #002) | `tools/tests/test_dbg_log.py` |
| `fw_review` | Findings of the code review of goal 094: the LyraT's soft limiter in C (no sign flip at call gain 8, monotonic), the S3's Wi-Fi retry pause (a few at once, then one per 5 s, reset by an IP), `RESTART_DELAY_MS` in any build, the self-test only for an image in `PENDING_VERIFY` (router off: no rollback loop), the noise mode at start only with its handle | `tools/tests/test_fw_review.py` |
| `ota_protocol` | OTA protocol, C against the Python model (goal 090) | `tools/ota_test/test_protocol.py` |
| `ota_tools` | `ota.py` both boards in every mode and `udplog.py` against `s3_emu.py` (goal 090) | `tools/ota_test/test_tools.py` |
| `s3_sim` | The S3 simulator against the firmware source: numbers, JSON keys of TAP, UQ, LOG and the OTA query, the stream header byte by byte, the stat line, TAP and UC/UT/UB/UW rules, the lease | `tools/tests/test_s3_sim.py` |
| `bench_tools` | `s3_uart.py` (also through a noisy UART: retries, each command runs once), `ota.py query` and `--dry-run` of both boards, `udplog.py --seconds`, `udp_record.py --tap` (WAV files, slots, a 1 kHz tone checked, loss and repeats counted) as processes against the simulator | `tools/tests/test_bench_tools.py` |
| `bench_unit` | `fw.py` variant names, `audio_check` on tone, silence, noise and a wrong rate, `voice_check` noise measures, `phone_mic.level`, the mic floor of `udp_record`, every bench module loads | `tools/tests/test_bench_unit.py` |
| `usb_free` | The bench without USB (goal 093): `first_flash.py --dry-run` (touches no port, file or build; network half passes, fails with its reason without the S3 or the LyraT), the secrets made from `wifi_secrets.h` without printing a value, `fw.current_variant()`; `console.NetWatch` (lines over the log, replies read by `round.py`'s parsers, `reboot` counted as a reset with reason `SW_RESET`, a lost listener comes back, the S3's console and `UX`); `round.reboot_trial` and `pair_trial` with the phone stubbed and `fw.py erase-bonds` over the network; `round.py power` PASS and FAIL on a reboot; `fw.py flash --image` both boards; `s3.py console/reset`; `s3_uart.py power` (`txp=` in stat) and `restart`; `owner_check.py ports` without COM; `call_record.py` with 5 % loss (silence of each lost packet at its frame, loss counters in its report); static: no COM port unless asked, no control characters in the tools | `tools/tests/test_usb_free.py` |
| `platform_rom`, `platform_tools`, `platform_pins` | The three-board platform of goal 095 | `tools/platform/` |

## The S3 simulator

`BENCH_NO_ADB=1` is set for every check: `round.adb()` answers nothing and `phone.adb()` refuses, so no test reaches the
phone (goal 093).

`tools/tests/s3_sim.py` is the Wi-Fi firmware of `s3_eth_bridge` with a LyraT behind it, on 127.0.0.1, on the real ports
5004 to 5008. The bench tools reach it with `BENCH_S3_ADDR=127.0.0.1` (requests then leave from the loopback only and go
to that address only). Its numbers, JSON keys, header layout and stat fields are parsed from `s3_eth_bridge/main/*.c`
(`s3_sim.facts()`), so a change of the firmware that the simulator does not follow fails `s3_sim`. The OTA receivers,
the UART frames and the log rings are the firmware's own C code (`otahost.dll`); OTA and log are `ota_test/s3_emu.py`.

```
python lyrat_resend/tools/tests/s3_sim.py [--uart-ber 1e-4] [--stream-loss 0.05] [--stream-dup 0.05] [--lyrat-silent]
```

Images for OTA: `tools/tests/images.py prepare DIR [--s3 BIN] [--lyrat BIN]` makes the four images `ota_test` wants; the
newer one is the same build with another version and ELF SHA, checksum and hash made again. Without a build it makes
synthetic images (no code in them: for the protocol only).

Since goal 093 it also answers `UX <boot>` (restart, new boot id, new log ring) and `UP [qdbm]` (`txp=` in stat), and its
LyraT console knows `s` (the status of the firmware with bonds, SLC and peers by their last two bytes), `reboot [1]`
(restart after 1 s with the boot line `BOOT reset_reason=SW code=3`, then `SLC CONNECTED` while it has a bond), `unpair`
and `ota`; `_pair <passkey>` is not a firmware command: it plays the phone's pairing (`SSP confirm`, bond, SLC) for the
tests of `round.py link`.

## What is not checked here

- Anything of the radio, the I2S bus, the GPIO registers, the flash and `esp_ota_*`: the glue of `main.c`, `s3_ota.c`,
  `lyrat_ota.c`, `lyrat_dbglog.c`, Bluedroid. Those run on the bench (goal 091 and later).
- ESP-IDF QEMU: not installed on this PC, and it emulates neither the Wi-Fi of the S3 nor the Bluetooth of the LyraT, which
  both self-tests need (goal 092 DECISIONS D2).
- The DSP code of the LyraT (`aec`, `spectral_ns`, `voice_ns`, `plc`, `msbc_codec`) only through its build.
- `tools/bench/break_shape.py`: a script of goal 062 that runs on import with an argument.
- Without USB (goal 093), only on the bench: the S3 on real Wi-Fi, the LyraT's log at 2 KB/s through the real UART (lines
  lost in a burst), `reboot`, `unpair` and `UX` of the real firmwares, `esp_wifi_set_max_tx_power` at run time, and
  `first_flash.py` without `--dry-run` (the USB flash of both boards, the backup of `sdkconfig`, the builds). The stages of
  `round.py` that need the phone (`link`, `sco`, `audio`, `tap`, `uart`, `btloss`, `plc`, `record`) run here only in
  parts: the trials of `link` with the phone stubbed; the rest is the same code with the console swapped.
