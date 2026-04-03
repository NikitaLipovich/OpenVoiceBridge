# Аппаратная часть — ESP32 LyraT v4.3

## Плата

ESP32-LyraT v4.3 — официальная плата Espressif для аудио разработки.
MCU: ESP32-WROVER-IE (ESP32 + 8MB PSRAM + внешняя IPEX антенна).

Документация платы: https://docs.espressif.com/projects/esp-adf/en/latest/design-guide/dev-boards/board-esp32-lyrat-v4.3.html

---

## Аудио кодек ES8388

ES8388 — стерео аудио кодек, встроен на LyraT. Подключён к ESP32 через:

### I2C (управление регистрами)
| Сигнал | GPIO |
|---|---|
| SCL | GPIO 23 |
| SDA | GPIO 18 |
| I2C адрес | 0x10 (ADDR pin = LOW) |

### I2S (аудио данные)
| Сигнал | GPIO |
|---|---|
| BCLK (bit clock) | GPIO 5 |
| WS (word select / LRCLK) | GPIO 25 |
| DO (данные кодек → ESP32) | GPIO 35 |
| DI (данные ESP32 → кодек) | GPIO 26 |

**Режим**: ESP32 = I2S Master, ES8388 = Slave.
**Параметры для телефонного аудио**: 8000 Hz, 16 bit, stereo (mono продублировать на оба канала).

### Входы кодека
| Вход | Назначение |
|---|---|
| LINE_IN L (LINPUT1) | Петличка (левый канал) |
| LINE_IN R (RINPUT1) | Петличка (правый канал) |
| MIC1 / MIC2 | Встроенные микрофоны LyraT (не используем) |

Усиление входа: настраивается через регистры ES8388 (аналог PGA +30dB + boost как на WM8960).

### Выходы кодека
| Выход | Назначение |
|---|---|
| HPOUT L/R | Наушники (мониторинг / call audio от телефона) |
| LOUT / ROUT | Линейный выход |

---

## SCO аудио datapath: HCI (vHCI)

На LyraT v4.3 используется **программный HCI datapath** — единственно возможный вариант.

### Почему не PCM

PCM hardware datapath ESP32 физически несовместим с LyraT v4.3:

| PCM сигнал | GPIO (PCM) | GPIO (I2S ES8388) | Конфликт |
|---|---|---|---|
| PCM_CLK | GPIO 0 | MCLK ES8388 | **ПРЯМОЙ КОНФЛИКТ** |
| PCM_DIN | GPIO 5 | BCLK ES8388 | **ПРЯМОЙ КОНФЛИКТ** |
| PCM_SYNC | GPIO 25 | LRCLK ES8388 | **ПРЯМОЙ КОНФЛИКТ** |
| PCM_DOUT | GPIO 4 | MicroSD D1 | **КОНФЛИКТ** |

GPIO 0 дополнительно является strapping pin — если на нём активный выход
при старте, ESP32 входит в режим serial bootloader.

PCM datapath также ограничен **только CVSD** кодеком. mSBC (wideband 16kHz)
возможен исключительно через HCI. Kconfig явно предупреждает:
_"Should disable Wide Band Speech when SCO data path is PCM"_.

Официальный ADF пример `pipeline_a2dp_sink_and_hfp` жёстко прописывает
`CONFIG_HFP_AUDIO_DATA_PATH_HCI=y` в `sdkconfig.defaults` именно по этим причинам.

### Как работает HCI datapath

```
Android SCO (CVSD 8kHz)
        │
  ESP32 BT Controller (Bluedroid)
        │
  HCI → esp_hf_client_incoming_data_cb()   ← аудио ОТ телефона (call audio)
        │
  ring buffer / DSP
        │
  I2S TX → ES8388 DAC → HPOUT (наушники)

  I2S RX ← ES8388 ADC ← LINE_IN (петличка)
        │
  esp_hf_client_outgoing_data_cb()          ← аудио К телефону (mic)
        │
  ESP32 BT Controller → SCO → Android
```

### Сравнение PCM vs HCI

| Критерий | PCM | HCI (выбрано) |
|---|---|---|
| Работает на LyraT v4.3 | **НЕТ** (конфликт GPIO) | **ДА** |
| Кодеки | CVSD только | CVSD + mSBC |
| DSP обработка в коде | Невозможна | Полная |
| UDP стриминг аудио | Невозможен | Полный |
| Официальный ADF пример | Не использует | Использует |
| CPU нагрузка | Минимальная | Умеренная |

---

## Кнопки и индикаторы LyraT

| Элемент | GPIO | Назначение (планируемое) |
|---|---|---|
| KEY1..KEY6 | ADC1 | Управление звонком (ответить / сбросить) |
| LED (PA enable) | GPIO 21 | Усилитель наушников ON/OFF |
| LED зелёный | GPIO 22 | Индикатор BT подключения |

---

## Питание

- USB 5V (разъём на плате) — достаточно для тестирования
- Для продакшн: повербанк (устраняет 50 Hz помеху от сети — проверено на ESP32-S3 схеме)
- Потребление при BT SCO + WiFi TX: ~200-300 mA пиковое

---

## Схема сравнения с предыдущей платой

| Параметр | ESP32-S3 схема (была) | ESP32 LyraT (текущая) |
|---|---|---|
| BT модуль | FSC-BT1036x (UART AT) | Встроенный ESP32 Classic BT |
| Кодек | WM8960 (SparkFun breakout) | ES8388 (на плате) |
| PSRAM | 16 MB | 8 MB |
| Антенна BT | Отдельная | Shared с WiFi (coexistence!) |
| SNR микрофона | 34 dB (проверено) | Ожидается аналогично |
