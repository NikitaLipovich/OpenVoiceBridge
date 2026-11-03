# Goal harness — LyraT как микрофон для Zoom + WiFi-стрим входящего аудио

## Контракт цели

| ID | Цель | Pass-критерий (машинно проверяемый) |
|---|---|---|
| **G1** | Микрофон LyraT доступен как вход в Zoom | Windows endpoint `LyraT_Phone Hands-Free` (Capture) в `DeviceState=1 ACTIVE`; плата рапортует `SCO=active`; RMS с endpoint'а > `RMS_SPEECH` при речи; `sco_drops == 0` за прогон |
| **G2** | Входящее (call) аудио идёт по WiFi на ПК | UDP:5004 принимает ≥ `4.5` уник. пакетов/с, потери по seq < 2%, при активном SCO |
| **G3** | Связка держится сессию | G1+G2 выполняются непрерывно ≥ `SOAK_S` (по умолчанию 600 с) без ресетов платы и разрывов SLC |

Прогон = «trial». Каждый trial пишет строку в `runs/results.jsonl` со всеми метриками.
Луп идёт по лестнице ручек (ниже) и останавливается на первой конфигурации, где G1..G3 = pass.

## Измеряемые метрики (на trial)

```
sco_active_ratio    доля времени SCO=active         (serial)
sco_drops           число CLOSED после OPEN         (serial)
slc_drops           число разрывов HFP conn         (serial)
board_resets        rst:0x / Guru Meditation / abort (serial)
heap_min            минимальный free heap           (serial)
mic_pk_p95          пик уровня микрофона на плате   (serial)
win_capture_state   1=ACTIVE 4=NOTPRESENT 8=UNPLUGGED (registry MMDevices)
bt_connected        fConnected из BLUETOOTH_DEVICE_INFO (bthprops)
cap_rms_p95         RMS сигнала, снятого с Windows-endpoint (winmm waveIn)
udp_call_pps        уник. пакетов/с на 5004
udp_call_loss_pct   потери по seq на 5004
udp_mic_pps         уник. пакетов/с на 5005
udp_mic_loss_pct    потери по seq на 5005
```

Ожидаемые значения потока: 8 кГц, 240 сэмплов/кадр = 30 мс, `UDP_BATCH_FRAMES=6` → 180 мс/пакет
→ **5.56 уник. пакетов/с**; `UDP_DUPLICATE=2` → каждый шлётся 3 раза (~16.7 raw pps).

## Корневая причина G1 — дедлок инициативы

```
Windows (AG)                                  LyraT (HF unit)
     |                                              |
     |  «я AG, к гарнитуре сам не подключаюсь —     |  esp_hf_client_init()
     |   она переподключится, как все наушники»     |  и пассивное ожидание;
     |                                              |  esp_hf_client_connect() НЕТ
     |                 ...ждут оба...               |
```

Пара есть, драйверы Windows встали корректно, COD у платы правильный —
но **SLC не инициирует никто**. Отсюда `fConnected=false`, endpoint вечно
`DeviceState=8 UNPLUGGED`, `waveInGetNumDevs()` не видит устройство, `SCO=idle`.

Реальные гарнитуры (JBL/Mi в этом же логе) переподключаются сами при включении —
поэтому у них endpoint оживает, а у LyraT нет. Чинится ручкой **F4**.

## Установленные факты (замерено 2026-08-25, до первого trial)

| Факт | Значение | Влияет на |
|---|---|---|
| Windows-драйверы HFP | `LyraT_Phone Hands-Free AG` (BthHFEnum) + `LyraT_Phone Hands-Free` (BthHFAud), ProblemCode=0 | — (в порядке) |
| COD платы | `0x200408` = Audio/Video, minor Hands-free, service-бит Audio | — (в порядке) |
| `fConnected` (bthprops) | **false** — ACL-линка нет, только бонд | **G1 — корень** |
| `BluetoothSetServiceState(0x111E)` | rc=**87** ERROR_INVALID_PARAMETER — HFP управляется профильным драйвером, программно так не подключить | ручка W1 бесполезна |
| `waveInGetNumDevs()` | видит только Realtek — endpoint платы отсутствует, пока нет линка | измеритель G1 |
| SSID платы и ПК | одна сеть, ПК на 5 ГГц (ch 36), плата 2.4 ГГц | G2 — сеть одна, IP просто устарел |
| Capture/Render endpoint | `DeviceState=8` UNPLUGGED (+ дубль со `state=4` NOTPRESENT) | **G1** |
| SCO на плате | `SCO=idle` | **G1** |
| `esp_hf_client_connect()` в прошивке | **отсутствует** — плата никогда не инициирует SLC | **G1** |
| UDP dest в прошивке | `192.168.1.214` (main.c:120-123) | **G2** |
| Реальный IP этого ПК | `192.168.0.248` (Wi-Fi) | **G2 — другой /24, поток не доходит** |
| Firewall для UDP 5004/5005 | правил нет → входящий UDP блокируется | **G2** |
| coex при SCO | `ESP_COEX_PREFER_WIFI` (main.c:777) | **G3 — противоречит docs/KNOWN_ISSUES.md** |
| WiFi TX power | 52 (13 dBm, main.c:600) | **G3** |
| WBS | `CONFIG_BT_HFP_WBS_ENABLE=y` | G1 (mSBC-негоциация) |

## Лестница ручек

Применяются по порядку; после каждой — trial. `auto` = harness применяет сам,
`gated` = требует подтверждения (правит прошивку / шьёт / трогает реестр).

| # | Ручка | Тип | Что делает | Чинит |
|---|---|---|---|---|
| N1 | `firewall` | auto | inbound-правила UDP 5004/5005/5006 | G2 |
| N2 | `destip` | gated-fw | `UDP_REMOTE_IP_*` → фактический IP ПК | G2 |
| W1 | `btconnect` | auto | `BluetoothSetServiceState(HFP, ENABLE)` — включает «Hands-Free Telephony» и инициирует коннект | G1 |
| W2 | `holdsco` | auto | держит capture endpoint открытым через winmm → заставляет Windows поднять SCO | G1 |
| W3 | `stale_ep` | gated | удалить протухший endpoint `DeviceState=4` из MMDevices | G1 |
| F1 | `coex_bt` | gated-fw | main.c:777 `PREFER_WIFI` → `PREFER_BT` | G3 |
| F2 | `txpower` | gated-fw | main.c:600 `52` → `32` (8 dBm) | G3 |
| F3 | `nowbs` | gated-fw | `CONFIG_BT_HFP_WBS_ENABLE=n` | G1 |
| F4 | `hf_connect` | gated-fw | добавить `esp_hf_client_connect()` по peer из NVS | G1 |
| M1 | `repair` | manual | удалить пару и спарить заново | G1 |

## Грабли, на которые harness уже наступил

- **pyserial ресетит плату.** DTR/RTS заведены на EN/IO0; `serial.Serial(port)` дёргает их
  при открытии и перезагружает ESP32, обнуляя SLC и uptime. Открываем порт через
  `Serial()` + `dtr=False`/`rts=False` + `open()`. PowerShell `SerialPort` этим не страдает.
- **Окно замера схлопывалось.** `hold_sco.ps1` возвращается мгновенно, если endpoint'а нет,
  и trial длился 2 с вместо 30. Теперь `hold_sco_windowed()` держит окно фиксированным
  и переоткрывает endpoint, ловя момент его появления.
- **W1 (`BluetoothSetServiceState`) не работает для HFP** — rc=87. Оставлена в лестнице
  как быстрая проверка, но подключение ею не поднять.
- **N1 и W3 требуют админа.** Из обычной сессии `New-NetFirewallRule` и удаление
  MMDevices-ключей не пройдут — запускать harness из elevated-консоли.

## Запуск

```bash
python harness/lyrat_harness.py probe                 # снимок состояния, ничего не меняет
python harness/lyrat_harness.py trial --seconds 60    # один замер + скоринг
python harness/lyrat_harness.py loop                  # лестница: auto-ручки, стоп на первом pass
python harness/lyrat_harness.py loop --apply-gated    # + прошивочные ручки (правит код, билдит, шьёт)
python harness/lyrat_harness.py soak --seconds 600    # финальная проверка G3
```
