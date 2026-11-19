# Tests on the PC

Everything of this bench that can be checked without the boards, the phone, a COM port, adb or sound, with one command:

```
python lyrat_resend/tools/run_tests.py            all checks, the firmware builds too (first run about 15 min, then minutes)
python lyrat_resend/tools/run_tests.py --quick    without the builds of the firmwares and of the Android probe
python lyrat_resend/tools/run_tests.py --only s3_sim,bench_tools
python lyrat_resend/tools/run_tests.py --list
python lyrat_resend/tools/run_tests.py --quick --port-offset 100   # BENCH_PORT_OFFSET: another worktree's run does not meet this one
```

It prints PASS, FAIL or SKIP per check and writes `goals_harness/bench_runs/tests/<stamp>/summary.json` (status, time,
counts of tests, notes) with the log of each check beside it. Exit code 0 only when every check that ran passed; a unittest
case that skipped counts as a failure, because the runner provides whatever the case needs. `--quick` and `--only` may skip
what they leave out on purpose.

Needs: Python 3.13 with numpy and pyserial, Visual Studio Build Tools 2022 (`cl`, path of `vcvars64.bat` in `OTA_VCVARS`
if elsewhere), ESP-IDF v5.5.2 (`IDF_PATH`, `IDF_TOOLS_PATH`, as `tools/bench/fw.py`), the Android SDK with a JDK for
`sco_probe` (as `tools/android/build_apk.py`), and `s3_eth_bridge/main/ota_secrets.h` for the secrets check.

Processes of a run end with it (goal 107). Each check runs in a Windows job object of its own (`tools/tests/procjob.py`,
ctypes, KILL_ON_JOB_CLOSE): when the check ends, passed or not, whatever it started and left running is killed and named in
`summary.json` (`leftover`) and at the end of its log; at its timeout the whole tree goes, not only the first process;
Ctrl+C (or Ctrl+Break) kills the running check's tree, writes `summary.json` with `"interrupted": true`, runs nothing more
and exits with 130; and if the runner itself is killed, Windows closes the job and the tree goes with it. Only processes
of the run's own jobs are killed — never by name or port, so a live `benchd` and other runs are not touched. Test modules
that start simulators (`simproc`, `ota_test/test_tools.py`, `platform/test_platform_tools.py`) also tie themselves with
`procjob.tie_this_process()`, so a test run alone that is killed takes its simulators with it. A simulator left alive
used to hold `ota_test/build/otahost.dll` and the next build failed with LNK1104 (goal 106 #001).

## Checks

| Name | What it proves | Code |
|---|---|---|
| `host_dll` | The protocol C code of both firmwares (`ota_link`, `ota_sha256`, `dbg_log`, `ctl_frame.h`, `call_limit.h`) builds with MSVC `/W4 /WX` | `tools/ota_test/build_host.cmd` |
| `platform_dll` | The ROM loader C code of the platform builds the same way | `tools/platform/build_host.cmd` |
| `static` | No value of the ignored secret files in any tracked file (values never printed), the secret and local files not tracked and covered by `.gitignore`, the S3 defaults to `S3_NET_ETH 0` | `tools/tests/static_check.py` |
| `fw_build` | S3 and LyraT (`lyrat`: the phone mode at a clean start, `lyrat_pc` with `-D LYRAT_DEFAULT_MODE=pc`, checked from the compile flags) built into `bench_runs/tests/builds/<board>` with their own sdkconfig: the partition table of the image = `partitions_ota.csv` = the tables of `OTA.md`, rollback on, image within 85 % of the slot and whole (checksum, hash), Wi-Fi name, password and token in the S3 image (bytes compared, never printed; a ccache trap is rebuilt once with `CCACHE_RECACHE=1`), the working `build/`, `sdkconfig` and git status unchanged | `tools/tests/fw_build_check.py` |
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
| `call_check` | The owner's test call against the simulator (the channel opened by `simsco`, the mic counter behind, clicks at one bus frame): checks, files aligned within 1 ms, report, latency; quality numbers on synthetic lines (tone, silence, cut pieces, clipping, noise); the live pipeline with offset, jitter, loss, an S3 restart and a stopped bus; the PCM output read by its client; the guard against a real call in a run without one (goal 097) | `tools/tests/test_call_check.py` |
| `call_listen` | The live stream cut into utterances (goal 101): synthetic speech (glottal pulses through formants, known borders) in quiet and in noise down to 8 dB SNR, borders within 60 ms; silence, clicks and digital zeros make nothing; a +26 dB background step gives at most one short "noise" utterance; overlap of the two directions; the 30 s cut without a hole; a stream that starts in speech; missing sound short (kept as zeros) and long (ends it), blocks lost by the client, a jump, the break flag; files bit for bit with the stream; the recogniser hook (call order, what it hears = the file, a failing and a slow engine, `--stt file.py:Class`); load; over TCP a dropped stream and its reconnect, a foreign format; end to end `s3_sim.py --play-call/--play-mic` (a real tone recording of a past probe run when the PC has one, else a synthetic tone; synthetic speech up) through `call_check.py` to `call_listen.py`, with and without 3 % packet loss: utterances where the scenario put them, both directions on one time line, decision latency. Not covered: a real call with a human voice | `tools/tests/test_call_listen.py` |
| `phone_compare` | The LyraT mic before Bluetooth against the phone's recording (goal 099): a synthetic pair (delay 37.4 samples, clock 80 ppm off, slow gain, a filter, codec noise 25 dB under) aligns with nothing found; lost frames knocked out the ways the phone shows them (repeat, repeat with fade, zeros, click, dip) found within one mSBC frame with their type; a jump of the time line found at its place as one defect; an unrelated pair does not converge and no defect is looked for; the steps and the outlier of two real bench runs (offsets only); files and report; a recording at 8 kHz stereo from the command line; the detector against `tone_blocks` on a tone with losses; the LyraT counters from the console log; `--compare` takes `--seconds` as the recording length. Not covered: the bench path itself (phone, adb, S3), checked on the bench, see goal 099 REPORT | `tools/tests/test_phone_compare.py` |
| `benchd` | The bench service (goal 103, `BENCHD.md`) against the simulator: event patterns of both firmwares, rotation by size, hour and day, the disk limit, the lock kept over a restart; the log of both boards in files with MAC prefixes hidden, a start (`reboot 1`, reason SW, SLC back), SCO open/close and commands as events; the relay: two `udplog.py` and a `NetWatch` at once while the service keeps its own log; `s3_uart.py` finds the S3 through the service; the lock (refusal with the holder, the queue, expiry, a dead pid, `bench.py lock run`, 423 and `force`, `bench_busy`); an update as a job (`fw.py flash --image`); the admin page (no external URL) and the POST rules, `--lan` read only; the tools without the service; two simulators on two `BENCH_PORT_OFFSET` | `tools/tests/test_benchd.py` |
| `bench_ui_check` | `gen_bench_ui.py --check --strict` (goal 104, `BENCHD.md` «Как добавить кнопку»): the generated web page and phone screen are what `bench_ui_spec.json` gives, every action and feed names a route, parameter and choice benchd has, every status path is in `/status` of benchd against the simulator, every control route has a button | `tools/bench/gen_bench_ui.py` |
| `bench_ui` | The bench UI generator (goal 104): deterministic; `--check` catches (a) hand edits of the generated files or the template, (b) unknown endpoint, parameter, choice, action name, LAN control on a local route, a route taken out of benchd, (c) a status path that is not there (also in a list), (d) a route without a button (warning, error with `--strict`) on broken copies; the template language gives the same text in Python, the page's JS (node) and the phone's Java (JVM); the page renders in headless Chrome against benchd + simulator; LAN token 401/200, `/shutdown` never from the LAN; UDP discovery | `tools/tests/test_bench_ui.py` |
| `bench_find` | The phone finds benchd and the S3 itself (goal 105, `BENCHD.md` «Как телефон находит ПК»): the phone's Java (`BenchFind`, `S3Direct`) on the JVM against real benchd processes — signed answer = `benchd_client`, a foreign service (other token), an unsigned and an old (goal 104) one not taken, a cache that is only a hint, the same service on another port found anew — and direct mode against the simulator (UQ, log by LOG1/LOG0, UC, UX); its status = `s3_direct.py`, log parsing = `udplog`, redact = `benchlib`; the direct ops agree in generator, spec and app; every write to the S3 only in direct mode | `tools/tests/test_bench_find.py`, `tools/tests/BenchNetMain.java` |
| `mic_mute` | The mic mute of the LyraT (goal 111): the C rules of `main/mic_mute.c` through `otahost.dll` — the key Rec debounced (3 samples of 10 ms), a short press toggles on release, a long one and a stuck-low input never do, single and double low samples of the GPIO36 erratum ignored, bounce on both edges, clock wrap; the command `mute on|off|toggle|?`; the ramp: bit exact while on, 10 ms to zeros and back, never above the input, no jump, full scale safe; main.c keeps the key polled (no interrupt), GPIO36/22, the mute after the tone and before the ring to Bluetooth, heartbeat bit 2, the log line; `bench_events` `mic_mute`, `call_record.mic_mute_of`, benchd `flag_muted` and the action, `s3_direct.py` = `S3Direct.java`, the panel's indicator; `call_check`: mute spans, the live mark, up zeroed and cut out of its quality, a whole muted call no defect; against the simulator `s3_uart.py mute` with the heartbeat bit and `call_check` over a muted stretch (live mark, report, zeroed file). Not covered: the real key, LED and ramp on the board (bench) | `tools/tests/test_mic_mute.py` |
| `dual_mode` | The two modes of the LyraT, phone and PC (goal 102, `DUAL_MODE.md`): the C rules of `main/hf_mode.c` through `otahost.dll` (start mode, migration from phone-bringup, the SLC table keep/adopt/drop, a new pairing takes the slot, whom to call, the command `mode`); scenarios on a model clock with a one-link Bluedroid model, a phone (connects by itself, calls, pushy re-connects) and Windows (never connects by itself except after pairing, audio only while an app holds it): switch while idle with the drop waking the reconnect task, refusal during SCO and the forced switch, 40 app audio opens and closes, the PC asleep with a pushy phone, reboot keeps the mode, first Windows pairing with and without its own connect, an unknown AG, a clean board of the `pc` build, `unpair`, the HFP service of Windows off; the phone mode line for line and time for time against the reconnect logic of phone-bringup (reset, call, loss, reboot); the audio functions byte for byte against `origin/phone-bringup`; every format string of the model and every pattern of `hfp_states.py` is a line of `main.c`; `hfp_states.py` on console and udplog lines and its CLI; `pc_link.py` verdicts on saved answers. Mutants of the rules (other AG kept, switch during SCO, no drop, the other AG called) all fail it. Not covered: a real Windows and phone, radio | `tools/tests/test_dual_mode.py`, `hfp_sim.py` |
| `platform_rom`, `platform_tools`, `platform_pins` | The three-board platform of goal 095 | `tools/platform/` |
| `child_cleanup` | What a run starts ends with it (goal 107): the real `run_tests.py` on fake checks whose children and grandchildren load a DLL and ignore Ctrl+C — after a check that passes, fails, crashes or times out, after Ctrl+C and after the runner is killed from outside, no process of theirs runs and the DLL opens for writing; a process outside the run and the bench service on 5080 untouched; a test process run alone (`procjob.tie_this_process()`) takes its children with it. Runs last: then `ota_test/build/otahost.dll` is free too | `tools/tests/test_child_cleanup.py`, `tools/tests/procjob.py` |

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
