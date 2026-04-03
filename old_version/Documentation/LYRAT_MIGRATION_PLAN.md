# Миграция на ESP32-LyraT: замена FSC-BT1036x на встроенный BT

## Цель

Убрать внешний Bluetooth модуль FSC-BT1036x и использовать встроенный Classic Bluetooth
ESP32-WROVER-IE на плате ESP32-LyraT для работы в режиме HFP Hands-Free Unit
(прикидываться гарнитурой/наушниками с микрофоном для Android телефона).

---

## Железо

| Компонент | Текущая схема | LyraT схема |
|---|---|---|
| MCU | ESP32-S3 DevKitC v1.1 | ESP32-LyraT (ESP32-WROVER-IE) |
| Bluetooth | FSC-BT1036x (внешний, AT команды) | ESP32 встроенный Classic BT |
| Аудио кодек | WM8960 (SparkFun breakout) | ES8388 (встроен на LyraT) |
| Микрофон | Петличка (TRS, L+R) на LINPUT1/RINPUT1 | Петличка на LINE_IN ES8388 или встроенные микрофоны LyraT |
| WiFi | ESP32-S3 | ESP32 (тот же чип что и BT) |
| PSRAM | 16 MB | 8 MB (WROVER-IE) — достаточно |

**Важно**: ESP32-WROVER-IE имеет внешнюю IPEX-антенну. BT и WiFi делят одну 2.4 ГГц
антенну → RF coexistence. В текущей схеме антенны раздельные.

---

## Почему это возможно

ESP32 (оригинальный, не S3) имеет Classic Bluetooth BR/EDR.
ESP32-S3 — только BLE, Classic BT физически отсутствует.

HFP (Hands-Free Profile) — это Classic BT профиль.
ESP32 реализует роль **HF Unit** (гарнитура), Android реализует роль **AG** (телефон).

Для нативных звонков Android **автоматически** маршрутизирует SCO аудио на подключённое
HFP устройство без действий пользователя.

Для VoIP (WhatsApp, Telegram) — зависит от приложения, большинство поддерживают.

---

## Требования к ESP-IDF / ESP-ADF

- **ESP-IDF версия: v5.3 или новее** (исправлены критические баги SCO)
- **ESP-ADF**: рекомендуется, есть готовый пример

Известные баги исправленные в v5.3:
- Односторонний звук SCO (buffer overflow в Bluedroid)
- Авто-отключение SCO при подключении Android (Redmi Note 7 и др.)

---

## Конфигурация menuconfig (обязательно)

```
Component config → Bluetooth:
  [x] Bluetooth
  [x] Bluedroid - Dual-mode BT stack
  [x] Classic Bluetooth
  [x] BR/EDR Sync (SCO/eSCO) connections support
      BR/EDR Sync (SCO/eSCO) Max Connections: 1  (минимум)
  [x] HFP Client (Hands-Free Unit)
  [x] BT SSP (Secure Simple Pairing)

Component config → Bluetooth → Bluedroid Options:
  BT/BLE MAX Bond Devices: 4
```

---

## Выбор datapath: PCM vs vHCI

### PCM (рекомендуется для нашего случая)
- Аудио идёт аппаратно через PCM GPIO пины прямо на I2S/ES8388
- Кодек CVSD только (8 кГц) — это именно то что нам нужно (телефон всё равно 8 кГц)
- Проще в реализации, меньше CPU нагрузки
- LyraT PCM пины: GPIO0 (CLK), GPIO4 (DOUT), GPIO5 (DIN), GPIO25 (SYNC)

### vHCI (программный)
- Аудио через HCI в software, ESP32 CPU делает декодирование
- Поддерживает mSBC (16 кГц) — но телефон всё равно отдаёт 8 кГц, смысла нет
- Сложнее, больше CPU нагрузки
- Нужен callback `esp_hf_client_outgoing_data_cb_t` для отправки данных

**Вывод: использовать PCM datapath.**

---

## Готовые примеры

### ESP-IDF
```
examples/bluetooth/bluedroid/classic_bt/hfp_hf/
```
Базовый пример HFP Hands-Free Unit. Работает на ESP32.

### ESP-ADF (полный pipeline с аудио кодеком)
```
examples/get-started/pipeline_a2dp_sink_and_hfp/
```
Полный пример: BT HFP + A2DP + I2S кодек. Тестировался именно на LyraT.
ES8388 уже поддерживается в ESP-ADF из коробки.

### Сторонний проект с рабочим bidirectional SCO
```
https://github.com/bsingh19/esp32_hfp_hf_msbc
```

---

## Что нужно реализовать (план)

### 1. Базовая BT HFP связь
- [ ] Инициализация Bluedroid стека
- [ ] Регистрация HFP HF callback-ов (connection, audio state, call state)
- [ ] Pairing с Android телефоном
- [ ] Проверка что SCO audio устанавливается при звонке

### 2. Аудио pipeline (PCM path)
- [ ] Настройка ES8388 кодека (аналог нашего wm8960_init)
  - Микрофон: LINE_IN или встроенные микрофоны LyraT
  - Наушники: HPout ES8388
  - Gain: аналогично WM8960 (+30dB PGA, boost)
- [ ] I2S1 ↔ ES8388 (мастер, 8 кГц, 16 бит, стерео)
- [ ] PCM пины → ES8388 PCM интерфейс (для BT SCO аудио)
  - Или: PCM → ring buffer → I2S (программный микс)

### 3. WiFi + UDP стриминг
- [ ] Порт текущей UDP логики (порты 5004/5005)
- [ ] raw lwIP UDP как сейчас
- [ ] WiFi coexistence: `CONFIG_SW_COEXIST_PREFER_BT` для приоритета BT аудио

### 4. DSP фильтры (порт с текущего кода)
Вся логика переносится без изменений, только другой кодек:
- [ ] L+R averaging для петлички
- [ ] HPF 80 Гц
- [ ] Notch 50/150/250 Гц
- [ ] Wiener noise suppression
- [ ] Hysteresis noise gate
- [ ] AEC (aec.c переносится как есть)
- [ ] Soft limiter для call audio

### 5. WiFi coexistence тестирование
- [ ] Проверить jitter UDP при активном BT SCO звонке
- [ ] Если нужно: `esp_wifi_set_max_tx_power(44)` для снижения помех
- [ ] Буферизация UDP на приёмной стороне

---

## Известные риски

| Риск | Вероятность | Митигация |
|---|---|---|
| BT + WiFi RF jitter (shared antenna) | Средняя | coexist config, буферизация |
| Качество микрофона встроенного LyraT | Средняя | использовать внешнюю петличку через LINE_IN |
| VoIP приложения не маршрутизируют SCO | Низкая | нативные звонки работают гарантированно |
| ES8388 gain chain отличается от WM8960 | Низкая | разные регистры, логика та же |

---

## Текущий статус проекта (ESP32-S3 + FSC-BT1036x)

Для сравнения — что уже работает на текущей плате:

- SNR микрофона: **34 dB** (с повербанком, петличка L+R)
- Клипы: **< 1%** (MIC_SW_GAIN=2)
- DSP pipeline: HPF 80 Гц + Notch 50/150/250 Гц + Wiener + gate + AEC + soft limiter
- DMA underrun-ы: **0** (8×480 frames DMA buffer)
- 50 Гц помеха: **устранена питанием от повербанка**
- Call audio: SW_GAIN=4 + soft limiter, без клиппирования
