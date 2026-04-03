# Bluetooth — HFP + A2DP

## Стек и роли

| Компонент | Значение |
|---|---|
| Стек | Bluedroid (dual-mode: Classic BT + BLE) |
| Профиль | HFP v1.7 (Hands-Free Profile) |
| Роль ESP32 | **HF Unit** (гарнитура) |
| Роль Android | **AG** (Audio Gateway — телефон) |
| Аудио кодек BT | CVSD (8 кГц, нарративный) |
| Доп. профиль | A2DP Sink (приём музыки) |

---

## Конфигурация menuconfig

Путь: **Component config → Bluetooth**

```
[x] Bluetooth
    [x] Bluedroid - Dual-mode BT stack
        [x] Classic Bluetooth
            [x] A2DP
            [x] HFP
                [x] HFP Client (Hands-Free Unit)
            [x] BT SSP (Secure Simple Pairing)
        BR/EDR Sync (SCO/eSCO) Max Connections: 1

Component config → Bluetooth → Controller Options:
    BT Controller Mode: Both Classic BT and BLE (BTDM)
    BR/EDR max SCO/eSCO connections: 1

HFP → Audio Data Path: HCI   ← ВАЖНО: не PCM (см. ниже)
```

**Почему HCI, а не PCM**: На LyraT v4.3 GPIO0/GPIO5/GPIO25 заняты I2S шиной ES8388.
PCM datapath использует те же пины → физический конфликт. Подробнее: `docs/HARDWARE.md`.

Через `sdkconfig.defaults` (уже настроено в проекте):
```
CONFIG_BT_ENABLED=y
CONFIG_BT_BLUEDROID_ENABLED=y
CONFIG_BT_CLASSIC_ENABLED=y
CONFIG_BT_SSP_ENABLED=y
CONFIG_BT_A2DP_ENABLE=y
CONFIG_BT_HFP_ENABLE=y
CONFIG_BT_HFP_CLIENT_ENABLE=y
CONFIG_BT_HFP_AUDIO_DATA_PATH_HCI=y   # HCI — единственный вариант для LyraT
CONFIG_BTDM_CTRL_MODE_BTDM=y
CONFIG_BTDM_CTRL_BR_EDR_MAX_SYNC_CONN=1
```

---

## Инициализация Bluedroid (порядок вызовов)

```c
// 1. Инициализация NVS (обязательно до BT)
nvs_flash_init();

// 2. Контроллер
esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
esp_bt_controller_init(&bt_cfg);
esp_bt_controller_enable(ESP_BT_MODE_BTDM);

// 3. Bluedroid стек
esp_bluedroid_init();
esp_bluedroid_enable();

// 4. GAP — имя устройства и видимость
esp_bt_dev_set_device_name("LyraT_HF");
esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);
esp_bt_gap_register_callback(gap_callback);

// 5. HFP Client
esp_hf_client_register_callback(hf_client_callback);
esp_hf_client_init();

// 6. A2DP Sink (опционально)
esp_a2d_register_callback(a2dp_callback);
esp_a2d_sink_register_data_callback(a2dp_data_callback);
esp_a2d_sink_init();
```

---

## HFP callback события

Ключевые события в `esp_hf_client_cb_event_t`:

| Событие | Когда | Действие |
|---|---|---|
| `ESP_HF_CLIENT_CONNECTION_STATE_EVT` | Подключение/отключение от телефона | Логировать, обновить LED |
| `ESP_HF_CLIENT_AUDIO_STATE_EVT` | SCO открыт/закрыт | Запустить/остановить аудио pipeline |
| `ESP_HF_CLIENT_CIND_CALL_EVT` | Изменение статуса звонка | Логировать |
| `ESP_HF_CLIENT_RING_IND_EVT` | Входящий звонок (ring) | Опционально: LED мигание |
| `ESP_HF_CLIENT_CLIP_IND_EVT` | Caller ID | Логировать номер |

Критично: `ESP_HF_CLIENT_AUDIO_STATE_EVT` с `state = ESP_HF_CLIENT_AUDIO_STATE_CONNECTED`
означает что SCO активен и аудио идёт через PCM пины → запускать I2S + DSP.

---

## Pairing

Используется SSP (Secure Simple Pairing) — пользователь подтверждает цифровой код на обоих устройствах.

Для автоматического подтверждения (тест/разработка):
```c
// В GAP callback при ESP_BT_GAP_CFM_REQ_EVT:
esp_bt_gap_ssp_confirm_reply(param->cfm_req.bda, true);
```

В продакшн: показывать код на дисплее или подтверждать кнопкой.

NVS хранит bonding данные — после первого сопряжения переподключение автоматическое.
Количество bond устройств: `CONFIG_BT_BLE_MAX_BOND_DEVICES=4` (рекомендовано в плане).

---

## SCO аудио — HCI datapath (callbacks)

При HCI datapath аудио проходит через Bluedroid и попадает в callback функции:

```c
// Регистрация audio callbacks (до esp_hf_client_init)
esp_hf_client_register_data_callback(
    bt_hf_client_incoming_cb,   // аудио ОТ телефона → ESP32 (call audio)
    bt_hf_client_outgoing_cb    // аудио ОТ ESP32 → телефону (mic)
);
```

### Incoming callback (телефон → наушники)

```c
// Вызывается Bluedroid когда пришёл SCO пакет от Android
// buf: CVSD PCM данные, 8kHz 16bit mono
// len: обычно 120 или 240 байт
void bt_hf_client_incoming_cb(const uint8_t *buf, uint32_t len) {
    // 1. Опционально: DSP обработка (AEC reference, soft limiter)
    // 2. Передать на I2S для воспроизведения через ES8388
    i2s_write(I2S_NUM_0, buf, len, &written, portMAX_DELAY);
    // 3. Опционально: отправить по UDP порт 5004
    udp_send_call_audio(buf, len);
}
```

### Outgoing callback (петличка → телефон)

```c
// Вызывается Bluedroid когда нужно отправить SCO пакет на Android
// buf: буфер для заполнения, len: сколько байт нужно
// ВАЖНО: вызывается из BT task, не блокировать надолго
int32_t bt_hf_client_outgoing_cb(uint8_t *buf, int32_t len) {
    // Взять данные из ring buffer (заполняется I2S RX task)
    int32_t got = ring_buf_read(mic_ring_buf, buf, len);
    if (got < len) {
        // Недостаточно данных — заполнить тишиной
        memset(buf + got, 0, len - got);
    }
    return len;
}
```

### Размер SCO буфера

Bluedroid использует `BTM_SCO_DATA_SIZE_MAX`. По умолчанию 120 байт (= 60 семплов при 16bit).
Если возникают `SCO xmit Q overflow` предупреждения → увеличить до 240 (известный баг #3550).

---

## WiFi + BT Coexistence

ESP32 имеет одну 2.4 GHz антенну. BT и WiFi делят RF.

### Настройки (уже в sdkconfig.defaults):
```
CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y
```

### Дополнительно в коде:
```c
// Приоритет BT аудио при SCO активен
esp_coex_preference_set(ESP_COEX_PREFER_BT);

// При неактивном SCO — вернуть баланс
esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);

// Снизить TX мощность WiFi если много помех
esp_wifi_set_max_tx_power(44);  // 11 dBm (по умолчанию ~20 dBm)
```

### Ожидаемый эффект coexistence на UDP:
- При SCO активен: jitter UDP +5..15 ms (буферизировать на приёмной стороне)
- Пакеты: <1% потерь при использовании SW coexist

---

## Известные баги ESP-IDF (исправлены в v5.3+)

| Баг | Симптом | Версия fix |
|---|---|---|
| SCO buffer overflow (Bluedroid) | Односторонний звук | v5.3 |
| Авто-отключение SCO (Redmi Note 7) | Звук пропадает через 2 сек | v5.3 |

Проект использует IDF v5.5.2 — оба бага закрыты.

---

## Ссылки

- Пример ESP-IDF: `$IDF_PATH/examples/bluetooth/bluedroid/classic_bt/hfp_hf/`
- Пример ESP-ADF: `pipeline_a2dp_sink_and_hfp` (протестирован на LyraT)
- Сторонний проект с рабочим bidirectional SCO: https://github.com/bsingh19/esp32_hfp_hf_msbc
- API: `esp_hf_client.h` в ESP-IDF
