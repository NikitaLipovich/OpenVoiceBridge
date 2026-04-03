# Известные проблемы и решения

## BT / HFP

### SCO не устанавливается при звонке
**Симптом**: соединение BT есть, но при звонке аудио не переключается на ESP32.
**Причины**:
1. `CONFIG_BTDM_CTRL_BR_EDR_MAX_SYNC_CONN=0` — SCO выключено в конфиге
2. Android не видит HFP профиль — убедиться что `CONFIG_BT_HFP_CLIENT_ENABLE=y`
3. Телефон не поддерживает HFP роутинг к данному устройству (редко)

**Решение**: проверить menuconfig → BT → HFP → Client; перепрошить с правильным sdkconfig.

---

### PCM datapath — почему не используется на LyraT
**Вывод**: PCM hardware datapath **физически невозможен** на LyraT v4.3.

Причина — прямой конфликт GPIO между PCM шиной BT контроллера и I2S шиной ES8388:

| PCM сигнал | GPIO | Занят под |
|---|---|---|
| PCM_CLK | GPIO 0 | MCLK ES8388 + strapping pin (boot mode) |
| PCM_DIN | GPIO 5 | BCLK ES8388 |
| PCM_SYNC | GPIO 25 | LRCLK ES8388 |
| PCM_DOUT | GPIO 4 | MicroSD D1 на LyraT |

Дополнительно: PCM datapath поддерживает только CVSD (8kHz). mSBC/wideband невозможен.
Kconfig ESP-IDF явно предупреждает: _"disable Wide Band Speech when SCO data path is PCM"_.

**Проект использует HCI datapath** — как официальный ADF пример `pipeline_a2dp_sink_and_hfp`.
Конфигурация: `CONFIG_BT_HFP_AUDIO_DATA_PATH_HCI=y` (уже в `sdkconfig.defaults`).

---

### Односторонний звук SCO (только приём или только передача)
**Причина**: баг Bluedroid buffer overflow. Исправлен в IDF v5.3.
**Проект использует v5.5.2 — не должно воспроизводиться.**
Если всё же возникает: обновить IDF.

---

### SCO отключается через 2 секунды (Redmi Note 7 и другие Xiaomi)
**Причина**: баг совместимости Bluedroid с MIUI. Исправлен в IDF v5.3.
**Проект использует v5.5.2 — не должно воспроизводиться.**

---

### BT не обнаруживается Android телефоном
**Симптом**: Android не видит устройство при поиске.
**Причина**: `esp_bt_gap_set_scan_mode` не вызван или вызван до `esp_bluedroid_enable`.
**Решение**: вызывать `esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE)` после полной инициализации стека.

---

## Аудио / ES8388

### ES8388 не отвечает на I2C
**Симптом**: `i2c_master_cmd_begin` возвращает `ESP_ERR_TIMEOUT` или `ESP_FAIL`.
**Причины**:
1. Неправильный I2C адрес — ES8388 на LyraT v4.3 = `0x10` (ADDR=LOW)
2. Нет подтяжек на SCL/SDA — на LyraT они встроены на плате
3. GPIO 23/18 захвачены другим драйвером
4. Плата не запитана (PA enable GPIO 21)

---

### Тихий или искажённый микрофон
**Симптом**: SNR < 20 dB, клиппирование.
**Решение**:
1. Начать с PGA 0dB (регистр ES8388 0x0A = 0x00), SW_GAIN=1
2. Постепенно поднимать PGA: 6dB → 12dB → 18dB
3. Целевой SW_GAIN=2 при PGA 0-6dB (аналог WM8960 настройки)
4. Клипы > 1% → снизить PGA или SW_GAIN
5. Питание от USB компьютера может добавлять 50 Hz → использовать повербанк

---

### 50 Hz помеха в аудио
**Симптом**: слышен гул 50 Hz (и гармоники 150, 250 Hz).
**Решение**:
1. Питание от повербанка вместо USB ПК (проверено на S3 схеме — устраняет полностью)
2. Notch фильтры 50/150/250 Hz в DSP pipeline (уже в плане)
3. Убедиться что петличка подключена через балансный вход (L+R averaging убирает синфазную помеху)

---

### DMA underrun / xrun
**Симптом**: треск, артефакты в аудио.
**Причина**: CPU не успевает обработать DMA буфер за 60ms.
**Решение**:
1. Проверить что CPU = 240 MHz (`CONFIG_ESP32_DEFAULT_CPU_FREQ_240=y`)
2. DSP задачу разместить на Core 1 (Core 0 = WiFi/BT)
3. Увеличить `dma_buf_count` с 8 до 16 (увеличивает задержку до ~480ms)
4. Убрать тяжёлые операции из DSP loop (malloc, printf)

---

## WiFi / UDP

### Пакеты UDP теряются при активном BT SCO
**Симптом**: jitter > 50ms, потери > 5% при звонке.
**Решение**:
1. Убедиться что `CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y`
2. Добавить в код: `esp_coex_preference_set(ESP_COEX_PREFER_BT)` при SCO active
3. Снизить WiFi TX мощность: `esp_wifi_set_max_tx_power(44)`
4. Увеличить jitter buffer на приёмной стороне до 200ms

---

### WiFi не подключается при активном BT
**Симптом**: `esp_wifi_connect()` timeout или disconnect.
**Причина**: BT сканирование мешает WiFi association.
**Решение**: инициализировать WiFi и дождаться подключения **до** включения BT discoverable mode.

---

## Сборка / конфигурация

### "Partition too large for flash size"
**Причина**: flash = 2MB в sdkconfig (старый sdkconfig от до настройки).
**Решение**:
```bash
idf.py fullclean
idf.py set-target esp32   # читает sdkconfig.defaults с FLASHSIZE_4MB
idf.py build
```

### "CONFIG_BT_ENABLED not set" при сборке
**Причина**: sdkconfig существует и переопределяет defaults.
**Решение**: удалить `sdkconfig` и пересоздать через `idf.py set-target esp32`.

### PSRAM ошибка при запуске
**Симптом**: `E (xxx) spiram: SPI RAM enabled but initialization failed`
**Причина**: плата не ESP32-WROVER (нет PSRAM).
**Решение**: отключить `CONFIG_SPIRAM=y` в menuconfig если плата без PSRAM.

---

## Риски проекта (из плана миграции)

| Риск | Вероятность | Статус |
|---|---|---|
| BT + WiFi RF jitter | Средняя | Митигирован: SW coexist + jitter buffer |
| Качество встроенного микрофона LyraT | Средняя | Обходим: внешняя петличка на LINE_IN |
| VoIP (WhatsApp) не маршрутизирует SCO | Низкая | Нативные звонки гарантированы |
| ES8388 gain chain отличается от WM8960 | Низкая | Другие регистры, та же логика настройки |
