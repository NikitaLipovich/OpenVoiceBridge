# Два режима гарнитуры: телефон и Windows (ветка `dual-mode`, гоал 102)

LyraT — гарнитура HFP и для телефона, и для ПК с Windows (Zoom видит её микрофон и динамик). Одновременно — нельзя:
стек Bluedroid держит одно HFP-соединение, поэтому **режим** выбирает, к кому гарнитура подключается и чьё подключение
принимает. По умолчанию — телефон; режим сохраняется в NVS и переживает перезагрузку.

| Что | Как |
|---|---|
| Показать режим | `python lyrat_resend/tools/bench/s3_uart.py mode` → `mode=phone src=nvs phone=..:aa:bb pc=..:cc:dd link=phone ..:aa:bb` |
| На ПК | `python lyrat_resend/tools/bench/s3_uart.py mode pc` → `mode pc saved, was phone, dropping the link to phone` |
| На телефон | `python lyrat_resend/tools/bench/s3_uart.py mode phone` |
| Во время звонка/Zoom | отказ `mode refused: SCO active, 'mode pc 1' forces`; `--force` рвёт звук и переключает |
| Забыть устройство режима | `s3_uart.py cmd "mode forget pc"` — следующее подключённое/сопряжённое в этом режиме займёт слот |
| Прошивка, стартующая в режиме ПК | `fw.py flash --variant mac_b1+pc` (только для платы без записанного режима; `mode` в NVS сильнее) |

Как устроено (подробно — `goals_harness/phone-then-pc/102-dual-mode-phone-windows/DECISIONS.md`):
у каждого режима свой «слот» с адресом его устройства. Устройство попадает в слот режима, в котором впервые подключилось
или сопряглось. В режиме `pc` телефон, который сам стучится, сразу отключается (строка `mode slc phone ..: drop_other`),
и наоборот. Windows сама к гарнитуре не подключается — LyraT зовёт свой AG каждые 8 с, а после смены режима сразу.
Кодек (mSBC или CVSD) выбирает телефон/Windows; звуковой тракт тот же, что в `phone-bringup`, байт в байт.

## План проверки на стенде (≈ 27 минут)

Все команды — из корня worktree `C:/Users/Admin/workspace/phone_stream/esp32s3_phone_stream-dual` (ветка `dual-mode`;
`wifi_secrets.h` и `ota_secrets.h` там уже лежат, вне git). Сначала — ветка телефона (`phone-bringup`) как обычно;
эта проверка — после неё. Стенд как обычно: обе платы включены, S3 в сети, телефон по USB.

### 1. Прошить `dual-mode` (≈ 3 мин)

```
python lyrat_resend/tools/bench/fw.py flash --variant mac_b1
```
Ожидается: `flash lyrat over the network (wifi): ... in N s`, код 0. Адрес тот же `mac_b1`, телефон пару не теряет.

### 2. Режим после первого старта (≈ 30 с)

```
python lyrat_resend/tools/bench/s3_uart.py mode
```
Ожидается: `mode=phone src=nvs phone=..:xx:xx pc=none link=phone ..:xx:xx` — первый старт перенёс телефон из старого
списка переподключения в слот `phone` (в логе платы `mode boot: ... migrated`). Если `link=down` — подождать 10 с и
повторить. Если `phone=` не тот (два последних байта адреса телефона) — `s3_uart.py cmd "mode forget phone"`, затем
`phone.py connect`.

### 3. Регрессия режима телефона (≈ 12 мин) — главное

```
python lyrat_resend/tools/bench/round.py link --power battery --reboots 3 --pairs 0
python lyrat_resend/tools/bench/round.py sco  --power battery --n 3 --active 15
python lyrat_resend/tools/bench/call_check.py --probe 1 --seconds 20
```
Ожидается то же, что на `phone-bringup`: `link` и `sco` — PASS, SLC после сброса ≤ 30 с, кодек mSBC; `call_check` —
`качество: норм`. Любое отличие от `phone-bringup` — стоп, режим ПК не проверять, прислать папки прогонов.

### 4. Сопряжение с Windows (≈ 5 мин)

```
python lyrat_resend/tools/bench/s3_uart.py mode pc
```
Ожидается: `mode pc saved, was phone, dropping the link to phone` (телефон отпустит гарнитуру — это нормально).

В Windows: **Параметры → Bluetooth и устройства → Добавить устройство → Bluetooth → `LyraT_Phone`**. Если
`LyraT_Phone` там уже есть со старых проверок — сначала «Удалить устройство» только у неё (R10), потом добавить.
Подтвердить код, если Windows спросит (LyraT соглашается сама). Через 10–20 с:

```
python lyrat_resend/tools/bench/s3_uart.py mode
python lyrat_resend/tools/bench/pc_link.py
```
Ожидается: `... pc=..:xx:xx link=pc ..:xx:xx` и `verdict: connected` (строки `mic OK Headset Microphone (LyraT_Phone
Hands-Free)` и `speaker OK ...`). `pc_link.py` только читает список устройств Windows, ничего не меняет.

### 5. Звук в Zoom или «Звукозаписи» (≈ 3 мин)

Zoom → Настройки → Звук: Динамик и Микрофон — `LyraT_Phone Hands-Free`, «Проверить микрофон» (или «Звукозапись»:
запись 15 с). Пока идёт проверка, в другом окне:

```
python lyrat_resend/tools/bench/udplog.py --seconds 20 --board lyrat --grep "SCO|mode"
```
Ожидается: `*** SCO OPEN ***` и `SCO audio state=2 codec=CVSD(8kHz)` (или `state=3 codec=mSBC(16kHz)`, если Windows
включила широкополосную речь), после закрытия проверки — `*** SCO CLOSED ***`. В Zoom слышно себя.
Переключение во время проверки должно отказать: `s3_uart.py mode phone` → `mode refused: SCO active ...`, код 1.

### 6. Возврат к телефону (≈ 1 мин)

```
python lyrat_resend/tools/bench/s3_uart.py mode phone
python lyrat_resend/tools/bench/s3_uart.py mode
```
Ожидается: `mode phone saved, was pc, dropping the link to pc`, через ≤ 10 с — `link=phone`. Windows покажет
`LyraT_Phone` «Сопряжено», не «Подключено» — это нормально. Перезагрузка (`s3_uart.py reboot`) режим не меняет.

### Если застряло

Записать 20 с лога с перезагрузкой и разобрать:

```
python lyrat_resend/tools/bench/udplog.py --seconds 20 --board lyrat      (в другом окне: s3_uart.py reboot)
python lyrat_resend/tools/bench/hfp_states.py goals_harness/bench_runs/udplog/<время>/udplog.log
```
`hfp_states.py` печатает ход связи (boot → connecting → acl → slc → sco) и подсказку:

| Подсказка начинается с | Значит | Что делать |
|---|---|---|
| `LyraT calls the pc ... does not answer` | ПК не отвечает: Bluetooth выключен, ПК спит, или Windows забыла пару | включить Bluetooth / сопрячь заново (шаг 4) |
| `the radio link comes up but HFP does not` | радиосвязь есть, профиль гарнитуры нет | Панель управления → Устройства и принтеры → LyraT_Phone → Службы: включить «Hands-free telephony» |
| `the phone keeps connecting while LyraT is in the pc mode` | телефон стучится в режиме ПК, его отключают | так и задумано; или выключить Bluetooth телефона |
| `nobody to call` | в этом режиме никого нет | сопрячь устройство этого режима |
| `an AG in neither slot` | подключился незнакомый AG | `mode forget pc` (или `phone`) и подключить нужный |
| `connected to the PC; Windows opens audio only while an app uses ...` | связь есть, звука нет, пока приложение не взяло устройство | выбрать `LyraT_Phone Hands-Free` в Zoom |

## Что проверено без стенда (гоал 102)

`python lyrat_resend/tools/run_tests.py --only dual_mode,bench_tools,fw_build` (полный набор — `run_tests.py`):
правила `hf_mode.c` собраны на ПК; модель стека (одно соединение), телефона и Windows: смена режима в простое и отказ во
время звука, 40 открытий/закрытий звука приложением, ПК уснул и проснулся при телефоне, который непрерывно стучится,
первое сопряжение Windows (с подключением и без), незнакомый AG, перезагрузка, сборка `pc`; режим телефона — строка в
строку как логика `phone-bringup` (сброс, звонок, потеря связи, перезагрузка); звуковой тракт — байт в байт; сборки
`lyrat` (phone) и `lyrat_pc` в пределах слота. **Не проверено**: настоящая Windows (когда именно она открывает звук,
выберет ли mSBC), настоящий телефон в режиме ПК (как часто он стучится), радио.
