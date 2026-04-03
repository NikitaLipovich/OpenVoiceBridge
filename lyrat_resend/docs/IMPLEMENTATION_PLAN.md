# План реализации — ESP32 LyraT Phone Stream

## Текущее состояние

| Файл | Статус |
|---|---|
| `main/main.c` | **Пустой** — только `app_main(){}` |
| `sdkconfig.defaults` | Готов (BT HCI, 4MB flash, partitions) |
| `sdkconfig.defaults.esp32` | Готов (PSRAM, QIO 80MHz, CPU 240MHz) |
| `partitions_bt_sink_example.csv` | Готов |

**Что переносим** из `esp32s3_phone_stream/main/main.c`:
- WiFi STA + raw lwIP UDP (порты 5004/5005) — **без изменений**
- DSP pipeline: HPF 80Hz + Notch 50/150/250Hz + Wiener + Noise gate + AEC + Soft limiter — **без изменений**
- L+R averaging петлички — **без изменений**
- Ring buffer архитектура (tasks: mic_rx, i2s_tx, udp_tx, mic_udp_tx) — **без изменений**

**Что меняется**:
- `BT1036-A` (UART AT команды + I2S0) → `ESP32 native BT HFP` (Bluedroid, HCI callbacks)
- `WM8960` (SparkFun breakout, I2C + I2S1) → `ES8388` (встроен на LyraT, другие GPIO и регистры)
- Одна I2S шина вместо двух (ES8388 делает всё)

---

## Архитектура нового кода

```
[Android телефон]
      │ BT SCO (HFP HCI)
      ▼
esp_hf_client_incoming_cb()          esp_hf_client_outgoing_cb()
      │                                         ▲
      │ call audio (8kHz, 16bit, mono)          │ mic audio (после DSP)
      ▼                                         │
[ring buf: rb_call_rx]             [ring buf: rb_mic_tx]
      │                                         │
      ├──────────► [i2s_tx_task]                │
      │            ES8388 DAC → HPOUT           │
      │                                         │
      ├──────────► [udp_tx_task]         [mic_rx_task]
      │            UDP port 5004          ES8388 ADC ← LINE_IN
      │                                         │
      │                                    DSP pipeline
      │                                    (HPF+Notch+Wiener+Gate+AEC+Limiter)
      │                                         │
      │                                    [udp_mic_task]
      │                                    UDP port 5005
      │
[AEC reference] ────────────────────────────────►
                                           aec_write_reference()
```

---

## ФАЗА 1: BT подключение к телефону (без аудио)

**Цель**: увидеть в логах что ESP32 спарилась с Android и SCO callback срабатывает.
**Критерий успеха**: `ESP_HF_CLIENT_AUDIO_STATE_EVT` → `connected` в monitor.

### Шаг 1.1 — Применить конфиг и собрать пустой проект

```bash
cd C:\Users\Admin\workspace\phone_stream\lyra\lyrat_resend
idf.py set-target esp32     # читает sdkconfig.defaults + sdkconfig.defaults.esp32
idf.py build                # убедиться что конфиг собирается
idf.py -p COMX flash monitor
```

Ожидаем: загрузка, нет крашей. Heap > 200KB свободно.

---

### Шаг 1.2 — Минимальный BT HFP init

Написать в `main.c` только инициализацию BT без аудио:

```c
#include "nvs_flash.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_bt_api.h"
#include "esp_hf_client_api.h"
#include "esp_log.h"

static const char *TAG = "HFP_TEST";

// Таблица имён событий для лога
static const char *c_hf_evt_str[] = {
    "CONNECTION_STATE_EVT", "AUDIO_STATE_EVT", "VR_STATE_CHANGE_EVT",
    "CALL_IND_EVT", "CALL_SETUP_IND_EVT", "CALL_HELD_IND_EVT",
    "NETWORK_STATE_EVT", "SIGNAL_STRENGTH_IND_EVT", "ROAMING_STATUS_IND_EVT",
    "BATTERY_LEVEL_IND_EVT", "CURRENT_OPERATOR_EVT", "RESP_AND_HOLD_EVT",
    "CLIP_EVT", "CALL_WAITING_EVT", "CLCC_EVT", "VOLUME_CONTROL_EVT",
    "AT_RESPONSE", "SUBSCRIBER_INFO_EVT", "INBAND_RING_TONE_EVT",
    "LAST_VOICE_TAG_NUMBER_EVT", "RING_IND_EVT",
};
static const char *c_audio_state_str[] = {
    "disconnected", "connecting", "connected", "connected_msbc",
};
static const char *c_connection_state_str[] = {
    "disconnected", "connecting", "connected", "slc_connected", "disconnecting",
};

static void hf_client_cb(esp_hf_client_cb_event_t event,
                         esp_hf_client_cb_param_t *param)
{
    ESP_LOGI(TAG, "HFP event: %s",
             event <= ESP_HF_CLIENT_RING_IND_EVT ? c_hf_evt_str[event] : "UNKNOWN");

    switch (event) {
    case ESP_HF_CLIENT_CONNECTION_STATE_EVT:
        ESP_LOGI(TAG, "  connection: %s",
                 c_connection_state_str[param->conn_stat.state]);
        break;
    case ESP_HF_CLIENT_AUDIO_STATE_EVT:
        ESP_LOGI(TAG, "  SCO audio: %s",
                 c_audio_state_str[param->audio_stat.state]);
        // СЮДА придёт "connected" когда телефон зазвонит → SCO открыт
        if (param->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED ||
            param->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC) {
            ESP_LOGI(TAG, "  *** SCO OPEN — audio callbacks should fire ***");
        }
        break;
    case ESP_HF_CLIENT_RING_IND_EVT:
        ESP_LOGI(TAG, "  *** RING RING ***");
        break;
    case ESP_HF_CLIENT_CLIP_EVT:
        ESP_LOGI(TAG, "  Caller ID: %s",
                 param->clip.number ? param->clip.number : "NULL");
        break;
    default:
        break;
    }
}

static void gap_cb(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param)
{
    if (event == ESP_BT_GAP_CFM_REQ_EVT) {
        // Auto-confirm SSP pairing (для разработки)
        ESP_LOGI(TAG, "SSP confirm request, auto-accepting");
        esp_bt_gap_ssp_confirm_reply(param->cfm_req.bda, true);
    }
}

void app_main(void)
{
    // NVS (bonding данные хранятся здесь)
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // BT controller
    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_BTDM));

    // Bluedroid стек
    ESP_ERROR_CHECK(esp_bluedroid_init());
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    // GAP — имя + видимость
    esp_bt_dev_set_device_name("LyraT_Phone");
    esp_bt_gap_register_callback(gap_cb);
    esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);

    // HFP Client
    ESP_ERROR_CHECK(esp_hf_client_register_callback(hf_client_cb));
    ESP_ERROR_CHECK(esp_hf_client_init());

    ESP_LOGI(TAG, "BT ready — find 'LyraT_Phone' on Android");
}
```

**Проверка**:
1. На Android: BT поиск → найти `LyraT_Phone` → сопрячь
2. В логах должно появиться: `connection: slc_connected`
3. Позвонить на телефон → `SCO audio: connected` → `*** SCO OPEN ***`

> **Если `slc_connected` нет**: проверить `CONFIG_BTDM_CTRL_BR_EDR_MAX_SYNC_CONN=1`
> в menuconfig, пересобрать.

---

### Шаг 1.3 — Проверить HCI audio callbacks

Добавить stub-коллбеки чтобы убедиться что данные приходят:

```c
static uint32_t s_incoming_count = 0;
static uint32_t s_outgoing_count = 0;

static void hf_incoming_cb(const uint8_t *buf, uint32_t sz)
{
    s_incoming_count++;
    if (s_incoming_count % 100 == 0) {
        ESP_LOGI(TAG, "incoming SCO: %lu pkts, sz=%lu", s_incoming_count, sz);
    }
}

static int32_t hf_outgoing_cb(uint8_t *buf, int32_t sz)
{
    s_outgoing_count++;
    memset(buf, 0, sz);  // тишина — просто проверяем что callback вызывается
    return sz;
}
```

Зарегистрировать при `AUDIO_STATE_EVT` → `connected`:
```c
esp_hf_client_register_data_callback(hf_incoming_cb, hf_outgoing_cb);
```

**Критерий**: в логах регулярно `incoming SCO: N pkts, sz=120` во время звонка.
sz=120 → CVSD, sz=240 → mSBC. Запомнить какой sz приходит от конкретного телефона.

---

## ФАЗА 2: ES8388 — инициализация кодека

**Цель**: слышать тон в наушниках и видеть данные с микрофона.
**Критерий**: I2C probe ES8388 без ошибок, I2S write/read без underrun.

### Шаг 2.1 — I2C probe

```c
#include "driver/i2c_master.h"

#define ES8388_ADDR    0x10
#define I2C_SCL_GPIO   23
#define I2C_SDA_GPIO   18

// Проверить что ES8388 отвечает на I2C
i2c_master_bus_config_t bus_cfg = {
    .i2c_port = I2C_NUM_0,
    .scl_io_num = I2C_SCL_GPIO,
    .sda_io_num = I2C_SDA_GPIO,
    .clk_source = I2C_CLK_SRC_DEFAULT,
    .glitch_ignore_cnt = 7,
};
i2c_master_bus_handle_t i2c_bus;
ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &i2c_bus));
ESP_ERROR_CHECK(i2c_master_probe(i2c_bus, ES8388_ADDR, 1000));
ESP_LOGI(TAG, "ES8388 found at 0x%02X", ES8388_ADDR);
```

**Если probe ошибка**: проверить GPIO 23/18 не конфликтуют, питание платы.

---

### Шаг 2.2 — Инициализация ES8388 через esp_codec_dev

ES8388 **отсутствует в ESP-IDF**. Используем компонент из IDF Component Registry:
`main/idf_component.yml` уже содержит `espressif/esp_codec_dev: ">=1.3.4"`.

Это **не ADF** — standalone компонент с готовым ES8388 драйвером.

```bash
# Скачать компонент (автоматически при первом idf.py build)
idf.py build
# или явно:
idf.py update-dependencies
```

```c
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"

// I2C шина уже инициализирована в шаге 2.1
// I2S шина инициализирована в шаге 2.3

// ES8388 через esp_codec_dev
const audio_codec_data_if_t *i2s_if = audio_codec_new_i2s_data(&i2s_cfg);
const audio_codec_ctrl_if_t *i2c_if = audio_codec_new_i2c_ctrl(&i2c_cfg);
const audio_codec_if_t *codec_if   = es8388_codec_new(&es8388_cfg);  // ES8388 драйвер

esp_codec_dev_cfg_t dev_cfg = {
    .codec_if = codec_if,
    .data_if  = i2s_if,
};
esp_codec_dev_handle_t spk = esp_codec_dev_new(&dev_cfg);  // DAC (наушники)
esp_codec_dev_handle_t mic = esp_codec_dev_new(&dev_cfg);  // ADC (петличка)

// Настройка: 8kHz, 16bit, mono
esp_codec_dev_sample_info_t fs = {
    .sample_rate = 8000,
    .channel     = 1,
    .bits_per_sample = 16,
};
esp_codec_dev_open(spk, &fs);
esp_codec_dev_open(mic, &fs);
esp_codec_dev_set_vol(spk, 80.0);  // громкость 80%
esp_codec_dev_set_mute(spk, false);
```

---

### Шаг 2.3 — I2S loopback тест

```c
#include "driver/i2s_std.h"

// I2S для ES8388: BCLK=5, WS=25, DO=26, DI=35, MCLK=0
// dma_frame_num = 480 (60ms @ 8kHz), dma_desc_num = 8

// Тест: читать с ADC (LINE_IN), писать в DAC (HPOUT)
// Подключить петличку, говорить — слышать себя в наушниках
```

**Критерий**: нет `i2s_channel_read` ошибок, нет DMA underrun в логах.

---

## ФАЗА 3: BT + ES8388 — полный аудио путь

**Цель**: телефонный звонок слышен в наушниках, голос с петлички уходит в телефон.
**Критерий**: звонок с Android → двустороннее аудио без артефактов.

### Шаг 3.1 — Call audio: телефон → наушники

```c
// incoming_cb → ring_buf → i2s_tx_task → ES8388 DAC → HPOUT

static RingbufHandle_t rb_call_rx;

static void hf_incoming_cb(const uint8_t *buf, uint32_t sz) {
    xRingbufferSend(rb_call_rx, buf, sz, pdMS_TO_TICKS(10));
    esp_hf_client_outgoing_data_ready();  // ОБЯЗАТЕЛЬНО — сигналим BT стеку
}

static void i2s_tx_task(void *arg) {
    int16_t *stereo = malloc(sz_stereo);
    while (1) {
        size_t item_size;
        uint8_t *item = xRingbufferReceive(rb_call_rx, &item_size, portMAX_DELAY);
        // mono → stereo (L=R)
        // i2s_channel_write(es8388_tx_chan, ...)
        vRingbufferReturnItem(rb_call_rx, item);
    }
}
```

> **Важно**: `esp_hf_client_outgoing_data_ready()` должен вызываться в incoming_cb,
> иначе BT стек прекратит присылать данные.

---

### Шаг 3.2 — Mic audio: петличка → телефон

Используем **новый push API** (`CONFIG_BT_HFP_USE_EXTERNAL_CODEC=y`) — проще legacy:

```c
// mic_rx_task читает с ES8388, обрабатывает DSP, сама отправляет в BT

static void mic_rx_task(void *arg) {
    int16_t *buf = malloc(SCO_PKT_BYTES);  // 120 байт = 60 семплов @ 8kHz
    while (1) {
        // Читать с ES8388 ADC через esp_codec_dev или i2s_channel_read
        esp_codec_dev_read(mic_dev, buf, SCO_PKT_BYTES);

        // DSP (после фазы 4): HPF + Notch + Wiener + Gate + AEC + Limiter

        // Push в BT стек — вызываем сами когда данные готовы
        esp_hf_client_audio_data_send(sco_handle, (uint8_t*)buf);
    }
}

// Incoming audio (телефон → наушники) — callback только для приёма
static void hf_audio_cb(esp_hf_audio_buff_t *audio_buff) {
    if (audio_buff->is_bad_frame) return;
    esp_codec_dev_write(spk_dev, audio_buff->data, audio_buff->len);
    // или через ring buffer если нужна буферизация
}
```

> Legacy API тоже работает (см. Шаг 1.3) — но push API удобнее: нет требования
> non-blocking, нет `outgoing_data_ready()`, task сама контролирует темп.

---

### Шаг 3.3 — Проверка двустороннего аудио

Тест без DSP (чистые данные):
1. Позвонить с другого телефона на Android с LyraT
2. Говорить в петличку → слышно на другом телефоне?
3. Говорить в другой телефон → слышно в наушниках LyraT?

Логировать уровни:
```c
// Каждые 50 фреймов
if (++frame_count % 50 == 0) {
    ESP_LOGI(TAG, "call_rx max=%d, mic_tx max=%d", call_max, mic_max);
}
```

Если `call_rx max > 100` и `mic_tx max > 100` — аудио идёт в обоих направлениях.

---

## ФАЗА 4: DSP pipeline

**Цель**: перенести весь DSP из `esp32s3_phone_stream/main/main.c` без изменений.
**Критерий**: SNR > 30dB, клипы < 1%, Wiener+gate убирают шум в паузах.

### Шаг 4.1 — Скопировать DSP блоки из S3 кода

Из `esp32s3_phone_stream/main/main.c` скопировать в mic_rx_task:

1. **L+R averaging** (строки ~529-531) — без изменений
2. **HPF 80 Hz + Notch 50/150/250 Hz** (строки ~553-586) — без изменений
3. **Wiener + noise gate** (строки ~588+) — без изменений

Из `i2s_rx_task` (строки ~352-366):
4. **Soft limiter** для call audio — без изменений

### Шаг 4.2 — AEC

Скопировать `aec.c` / `aec.h` из `esp32s3_phone_stream/main/`:

```c
// В incoming_cb или i2s_tx_task: после воспроизведения call audio
aec_write_reference(call_mono, call_samples);  // reference = то что уходит в уши

// В mic_rx_task: после HPF/Notch, перед отправкой
aec_process(mic_mono, mic_samples);            // убирает эхо наушников
```

### Шаг 4.3 — Gain калибровка ES8388

По аналогии с WM8960 настройкой на S3:
- Начать: PGA 0dB в ES8388 (регистр 0x0A = 0x00), SW_GAIN = 1
- Целевые параметры (как на S3): SNR > 30dB, клипы < 1%
- SW_GAIN = 2 — проверенный компромисс для петлички с PGA 0-6dB
- Логировать max уровень каждые 50 фреймов для калибровки

---

## ФАЗА 5: WiFi + UDP стриминг

**Цель**: аудио дублируется по UDP на ПК.
**Критерий**: Python-скрипт на ПК воспроизводит звонок и микрофон с задержкой < 200ms.

### Шаг 5.1 — Скопировать WiFi код из S3

Из `esp32s3_phone_stream/main/main.c`:
- `wifi_event_handler` (строки ~240-275) — без изменений
- `wifi_connect_sta()` (строки ~277-321) — изменить SSID/PASS

```c
#define WIFI_SSID "YOUR_SSID"
#define WIFI_PASS "YOUR_PASS"
```

### Шаг 5.2 — Скопировать UDP tasks из S3

- `udp_tx_task` (строки ~395-425) — без изменений (port 5004, call audio)
- `mic_udp_tx_task` (строки ~430-454) — без изменений (port 5005, mic audio)

### Шаг 5.3 — BT + WiFi coexistence

```c
#include "esp_coexist.h"

// В hf_client_cb при AUDIO_STATE_EVT → connected:
esp_coex_preference_set(ESP_COEX_PREFER_BT);   // BT SCO приоритет

// При AUDIO_STATE_EVT → disconnected:
esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
```

Тест jitter: запустить `ping 192.168.1.xxx` во время звонка.
Если jitter > 50ms → снизить WiFi TX power: `esp_wifi_set_max_tx_power(44)`.

---

## ФАЗА 6: Финальная интеграция и tuning

### Checklist

- [ ] BT паринг с Android (SSP)
- [ ] SCO открывается при входящем звонке
- [ ] HCI incoming callback: call audio → наушники
- [ ] HCI outgoing callback: петличка → телефон
- [ ] ES8388 I2C probe без ошибок
- [ ] ES8388 I2S без DMA underrun
- [ ] DSP: HPF + Notch + Wiener + Gate + AEC + Limiter активны
- [ ] SNR > 30dB (логировать max каждые 50 фреймов)
- [ ] Клипы < 1%
- [ ] WiFi подключается параллельно с BT
- [ ] UDP port 5004: call audio на ПК
- [ ] UDP port 5005: mic audio на ПК
- [ ] SW coexist: `ESP_COEX_PREFER_BT` при SCO active
- [ ] 0 DMA underrun за 10 минут звонка
- [ ] Питание от повербанка (устраняет 50Hz помеху)

### Задачи на Core 0 / Core 1

```c
// Core 0 — WiFi + BT стек (зарезервирован системой)
// Core 1 — все пользовательские задачи

xTaskCreatePinnedToCore(mic_rx_task,    "mic_rx",    4096, NULL, 5, NULL, 1);
xTaskCreatePinnedToCore(i2s_tx_task,    "i2s_tx",    4096, NULL, 5, NULL, 1);
xTaskCreatePinnedToCore(udp_tx_task,    "udp_tx",    4096, NULL, 4, NULL, 1);
xTaskCreatePinnedToCore(mic_udp_task,   "mic_udp",   4096, NULL, 4, NULL, 1);
```

---

## Файловая структура итогового проекта

```
lyrat_resend/
├── main/
│   ├── main.c          ← основной код (все фазы)
│   ├── es8388.c/.h     ← инициализация ES8388 (выделить из main.c)
│   ├── aec.c/.h        ← копия из esp32s3_phone_stream
│   └── CMakeLists.txt
├── sdkconfig.defaults
├── sdkconfig.defaults.esp32
├── partitions_bt_sink_example.csv
├── CMakeLists.txt
└── docs/
    ├── IMPLEMENTATION_PLAN.md  ← этот файл
    ├── PROJECT_OVERVIEW.md
    ├── SETUP.md
    ├── HARDWARE.md
    ├── BT_HFP.md
    ├── AUDIO_PIPELINE.md
    ├── WIFI_UDP.md
    └── KNOWN_ISSUES.md
```

---

## Ссылки

- ADF пример (структура HCI callbacks): `a2dp_sink_and_hfp_example.c` (release/v2.x)
- ES8388 регистры: `esp-adf/components/audio_hal/driver/es8388/es8388.c`
- S3 код для переноса: `esp32s3_phone_stream/main/main.c`
- ESP-IDF HFP API: `esp_hf_client.h`
