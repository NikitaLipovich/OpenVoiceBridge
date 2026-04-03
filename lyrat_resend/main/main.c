/*
 * ESP32 LyraT — Phone Stream
 * Фаза 2: ES8388 + HFP Legacy PCM API — двусторонний аудио маршрут
 *
 * Что делает:
 *   1. BT HFP HF Unit — спаривается с Android, открывает SCO (mSBC 16kHz)
 *   2. Legacy PCM callback: Bluedroid декодирует mSBC → PCM 16kHz mono 16-bit
 *   3. incoming_pcm_cb → rb_call_rx → i2s_tx_task → ES8388 DAC → наушники
 *   4. ES8388 ADC → mic_rx_task → L+R avg → rb_mic_tx → outgoing_pcm_cb → BT
 *
 * Проверка фазы:
 *   - Слышим голос собеседника в наушниках (HPOUT)
 *   - Говорим в микрофон → телефон слышит нас (без DSP, пока сырой сигнал)
 *   - В логах: MIC RX peak > 0 когда говорим, I2S TX peak > 0 во время звонка
 *
 * LyraT v4.3 пины:
 *   I2C  : SDA=18, SCL=23
 *   I2S  : MCLK=0, BCLK=5, WS=25, DOUT=26, DIN=35
 *   PA   : GPIO21 (active HIGH)
 */

#include <string.h>
#include <inttypes.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"

#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_bt_api.h"
#include "esp_hf_client_api.h"

#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"

#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "es8388_codec.h"

// ---------------------------------------------------------------------------
// Метки логов
// ---------------------------------------------------------------------------

#define TAG       "MAIN"
#define TAG_BT    "BT"
#define TAG_AUDIO "AUDIO"

// ---------------------------------------------------------------------------
// LyraT v4.3 — аппаратные пины
// ---------------------------------------------------------------------------

#define I2C_PORT     I2C_NUM_0
#define I2C_SDA_PIN  18
#define I2C_SCL_PIN  23

#define I2S_PORT     I2S_NUM_0
#define I2S_MCLK_PIN GPIO_NUM_0
#define I2S_BCLK_PIN GPIO_NUM_5
#define I2S_WS_PIN   GPIO_NUM_25
#define I2S_DOUT_PIN GPIO_NUM_26  // ESP32 → ES8388 DAC
#define I2S_DIN_PIN  GPIO_NUM_35  // ES8388 ADC → ESP32

#define PA_PIN       21           // power amplifier enable, active HIGH

// ---------------------------------------------------------------------------
// Аудио параметры
// ---------------------------------------------------------------------------

#define BT_DEVICE_NAME       "LyraT_Phone"
#define SAMPLE_RATE          16000
#define SAMPLES_PER_FRAME    240            // 15ms @ 16kHz
#define FRAME_BYTES_MONO     (SAMPLES_PER_FRAME * sizeof(int16_t))   // 480
#define FRAME_BYTES_STEREO   (FRAME_BYTES_MONO * 2)                  // 960
#define RINGBUF_FRAMES       8

// ---------------------------------------------------------------------------
// Глобальные хэндлы
// ---------------------------------------------------------------------------

static esp_codec_dev_handle_t s_codec_dev  = NULL;
static i2c_master_dev_handle_t s_es8388_i2c = NULL;  // прямой I2C доступ к ES8388
static RingbufHandle_t        s_rb_call_rx = NULL;  // BT → I2S TX
static RingbufHandle_t        s_rb_mic_tx  = NULL;  // I2S RX → BT

// Прямая запись регистра ES8388 через I2C (обход esp_codec_dev)
static esp_err_t es8388_write_reg_raw(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = {reg, val};
    return i2c_master_transmit(s_es8388_i2c, buf, 2, 100);
}

static esp_err_t es8388_read_reg_raw(uint8_t reg, uint8_t *val)
{
    return i2c_master_transmit_receive(s_es8388_i2c, &reg, 1, val, 1, 100);
}

static volatile bool s_sco_active = false;

// ---------------------------------------------------------------------------
// HFP Legacy PCM callbacks
//
// incoming_pcm_cb: Bluedroid вызывает, когда есть декодированный PCM от телефона.
//   Принимаем 16kHz mono 16-bit, кладём в ring buffer, уведомляем про исходящие.
//
// outgoing_pcm_cb: Bluedroid вызывает, когда нужно отправить PCM на телефон.
//   НЕ БЛОКИРОВАТЬ — вызывается из BT task. Если буфер пуст → тишина.
// ---------------------------------------------------------------------------

static void incoming_pcm_cb(const uint8_t *buf, uint32_t sz)
{
    if (s_rb_call_rx) {
        xRingbufferSend(s_rb_call_rx, buf, sz, 0);  // drop on overflow
    }
    // Уведомляем Bluedroid что у нас есть исходящие данные
    esp_hf_client_outgoing_data_ready();
}

static uint32_t outgoing_pcm_cb(uint8_t *buf, uint32_t sz)
{
    if (!s_rb_mic_tx) {
        memset(buf, 0, sz);
        return sz;
    }
    size_t got = 0;
    uint8_t *data = (uint8_t *)xRingbufferReceiveUpTo(s_rb_mic_tx, &got, 0, sz);
    if (data && got > 0) {
        memcpy(buf, data, got);
        vRingbufferReturnItem(s_rb_mic_tx, data);
        if (got < sz) {
            memset(buf + got, 0, sz - got);
        }
        return sz;
    }
    memset(buf, 0, sz);   // тишина если буфер пуст
    return sz;
}

// ---------------------------------------------------------------------------
// i2s_tx_task: ring buffer call_rx → mono→stereo → ES8388 DAC (наушники)
// ---------------------------------------------------------------------------

static void i2s_tx_task(void *arg)
{
    static int16_t mono_buf[SAMPLES_PER_FRAME];
    static int16_t stereo_buf[SAMPLES_PER_FRAME * 2];
    static uint32_t frame_cnt = 0;
    static int16_t  peak      = 0;

    ESP_LOGI(TAG, "i2s_tx_task started (core %d)", xPortGetCoreID());

    for (;;) {
        size_t   received = 0;
        uint8_t *data     = (uint8_t *)xRingbufferReceiveUpTo(
                                s_rb_call_rx, &received,
                                pdMS_TO_TICKS(100), FRAME_BYTES_MONO);
        if (!data || received == 0) {
            continue;
        }

        size_t n = received / sizeof(int16_t);
        memcpy(mono_buf, data, received);
        vRingbufferReturnItem(s_rb_call_rx, data);

        // Дублировать моно → стерео, программное усиление ×2
        for (size_t i = 0; i < n; i++) {
            int32_t s = (int32_t)mono_buf[i] * 2;
            if (s > 32767) s = 32767;
            if (s < -32768) s = -32768;
            stereo_buf[i * 2]     = (int16_t)s;
            stereo_buf[i * 2 + 1] = (int16_t)s;
            int16_t abs_v = s < 0 ? (int16_t)(-s) : (int16_t)s;
            if (abs_v > peak) {
                peak = abs_v;
            }
        }

        esp_codec_dev_write(s_codec_dev, stereo_buf,
                            (int)(n * 2 * sizeof(int16_t)));

        if (++frame_cnt % 50 == 0) {
            ESP_LOGI(TAG_AUDIO, "TX  frame=%"PRIu32" peak=%d", frame_cnt, peak);
            peak = 0;
        }
    }
}

// ---------------------------------------------------------------------------
// mic_rx_task: ES8388 ADC → L+R avg → mono → ring buffer mic_tx
// ---------------------------------------------------------------------------

static void mic_rx_task(void *arg)
{
    static int16_t stereo_buf[SAMPLES_PER_FRAME * 2];
    static int16_t mono_buf[SAMPLES_PER_FRAME];
    static uint32_t frame_cnt = 0;
    static int16_t  peak      = 0;

    ESP_LOGI(TAG, "mic_rx_task started (core %d)", xPortGetCoreID());

    for (;;) {
        int rc = esp_codec_dev_read(s_codec_dev, stereo_buf,
                                    (int)FRAME_BYTES_STEREO);
        if (rc != ESP_CODEC_DEV_OK) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        // Усреднить L + R → моно
        for (int i = 0; i < SAMPLES_PER_FRAME; i++) {
            int32_t avg = ((int32_t)stereo_buf[i * 2] +
                           (int32_t)stereo_buf[i * 2 + 1]) >> 1;
            mono_buf[i] = (int16_t)avg;
            int16_t abs_v = avg < 0 ? (int16_t)(-avg) : (int16_t)avg;
            if (abs_v > peak) {
                peak = abs_v;
            }
        }

        if (s_rb_mic_tx) {
            xRingbufferSend(s_rb_mic_tx, mono_buf, FRAME_BYTES_MONO, 0);
        }

        if (++frame_cnt % 50 == 0) {
            ESP_LOGI(TAG_AUDIO, "MIC frame=%"PRIu32" peak=%d", frame_cnt, peak);
            peak = 0;
        }
    }
}

// ---------------------------------------------------------------------------
// HFP Client callback
// ---------------------------------------------------------------------------

static void hf_client_cb(esp_hf_client_cb_event_t    event,
                          esp_hf_client_cb_param_t   *param)
{
    switch (event) {

    case ESP_HF_CLIENT_CONNECTION_STATE_EVT:
        ESP_LOGI(TAG_BT, "HFP conn state=%d", param->conn_stat.state);
        if (param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED) {
            ESP_LOGI(TAG_BT, "*** SLC CONNECTED — готов к звонкам ***");
        }
        break;

    case ESP_HF_CLIENT_AUDIO_STATE_EVT: {
        esp_hf_client_audio_state_t st = param->audio_stat.state;
        bool msbc = (st == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC);
        ESP_LOGI(TAG_BT, "SCO audio state=%d codec=%s",
                 st, msbc ? "mSBC(16kHz)" : "CVSD(8kHz)");

        if (st == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED ||
            st == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC) {
            s_sco_active = true;
            // Первый сигнал Bluedroid что исходящие готовы
            esp_hf_client_outgoing_data_ready();
            ESP_LOGI(TAG_BT, "*** SCO OPEN — аудио маршрут активен ***");
        } else if (st == ESP_HF_CLIENT_AUDIO_STATE_DISCONNECTED) {
            s_sco_active = false;
            ESP_LOGI(TAG_BT, "*** SCO CLOSED ***");
        }
        break;
    }

    case ESP_HF_CLIENT_RING_IND_EVT:
        ESP_LOGI(TAG_BT, "RING — отвечаем автоматически (тест)");
        esp_hf_client_answer_call();
        break;

    case ESP_HF_CLIENT_CLIP_EVT:
        ESP_LOGI(TAG_BT, "CLIP: %s",
                 param->clip.number ? param->clip.number : "(no number)");
        break;

    case ESP_HF_CLIENT_CIND_CALL_EVT:
        ESP_LOGI(TAG_BT, "call active=%d", param->call.status);
        break;

    case ESP_HF_CLIENT_CIND_CALL_SETUP_EVT:
        ESP_LOGI(TAG_BT, "call setup=%d", param->call_setup.status);
        break;

    case ESP_HF_CLIENT_VOLUME_CONTROL_EVT:
        ESP_LOGI(TAG_BT, "volume type=%d val=%d",
                 param->volume_control.type, param->volume_control.volume);
        break;

    case ESP_HF_CLIENT_PROF_STATE_EVT:
        if (param->prof_stat.state == ESP_HF_INIT_SUCCESS) {
            ESP_LOGI(TAG_BT, "HFP профиль инициализирован");
        }
        break;

    default:
        ESP_LOGD(TAG_BT, "HFP event %d", event);
        break;
    }
}

// ---------------------------------------------------------------------------
// GAP callback — SSP / PIN pairing
// ---------------------------------------------------------------------------

static void gap_cb(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param)
{
    switch (event) {

    case ESP_BT_GAP_AUTH_CMPL_EVT:
        if (param->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS) {
            ESP_LOGI(TAG_BT, "Paired: '%s'", param->auth_cmpl.device_name);
        } else {
            ESP_LOGW(TAG_BT, "Pairing failed: %d", param->auth_cmpl.stat);
        }
        break;

    case ESP_BT_GAP_CFM_REQ_EVT:
        ESP_LOGI(TAG_BT, "SSP confirm %06"PRIu32" — auto-accept",
                 param->cfm_req.num_val);
        esp_bt_gap_ssp_confirm_reply(param->cfm_req.bda, true);
        break;

    case ESP_BT_GAP_KEY_NOTIF_EVT:
        ESP_LOGI(TAG_BT, "SSP passkey %06"PRIu32, param->key_notif.passkey);
        break;

    case ESP_BT_GAP_PIN_REQ_EVT:
        ESP_LOGI(TAG_BT, "PIN request — sending 0000");
        {
            esp_bt_pin_code_t pin = {'0', '0', '0', '0'};
            esp_bt_gap_pin_reply(param->pin_req.bda, true, 4, pin);
        }
        break;

    default:
        ESP_LOGD(TAG_BT, "GAP event %d", event);
        break;
    }
}

// ---------------------------------------------------------------------------
// Инициализация ES8388 + I2S
// ---------------------------------------------------------------------------

static esp_err_t audio_init(void)
{
    esp_err_t ret;

    // ── I2C master bus ───────────────────────────────────────────────────────
    i2c_master_bus_config_t i2c_bus_cfg = {
        .clk_source                     = I2C_CLK_SRC_DEFAULT,
        .i2c_port                       = I2C_PORT,
        .sda_io_num                     = I2C_SDA_PIN,
        .scl_io_num                     = I2C_SCL_PIN,
        .glitch_ignore_cnt              = 7,
        .flags.enable_internal_pullup   = true,
    };
    i2c_master_bus_handle_t i2c_bus;
    ret = i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C bus init failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ESP_LOGI(TAG, "I2C bus OK (SDA=%d SCL=%d)", I2C_SDA_PIN, I2C_SCL_PIN);

    // Прямой I2C доступ к ES8388 (7-bit addr = 0x10)
    i2c_device_config_t es_dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = 0x10,
        .scl_speed_hz    = 100000,
    };
    ret = i2c_master_bus_add_device(i2c_bus, &es_dev_cfg, &s_es8388_i2c);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C add ES8388 device failed: %s", esp_err_to_name(ret));
        return ret;
    }

    // ── I2S full-duplex (TX + RX одним каналом) ──────────────────────────────
    i2s_chan_config_t chan_cfg          = I2S_CHANNEL_DEFAULT_CONFIG(
                                             I2S_NUM_AUTO, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num  = 8;
    chan_cfg.dma_frame_num = SAMPLES_PER_FRAME;  // 240 сэмплов = 15ms

    i2s_chan_handle_t tx_handle, rx_handle;
    ret = i2s_new_channel(&chan_cfg, &tx_handle, &rx_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S channel create failed: %s", esp_err_to_name(ret));
        return ret;
    }

    i2s_std_config_t i2s_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
                        I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_MCLK_PIN,
            .bclk = I2S_BCLK_PIN,
            .ws   = I2S_WS_PIN,
            .dout = I2S_DOUT_PIN,
            .din  = I2S_DIN_PIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv   = false,
            },
        },
    };

    ret = i2s_channel_init_std_mode(tx_handle, &i2s_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S TX init failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = i2s_channel_init_std_mode(rx_handle, &i2s_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S RX init failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = i2s_channel_enable(tx_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S TX enable failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = i2s_channel_enable(rx_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2S RX enable failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ESP_LOGI(TAG, "I2S OK — %dHz stereo 16-bit Philips", SAMPLE_RATE);

    // ── ES8388 codec ─────────────────────────────────────────────────────────
    audio_codec_i2c_cfg_t i2c_codec_cfg = {
        .port       = I2C_PORT,
        .addr       = ES8388_CODEC_DEFAULT_ADDR,  // 0x20
        .bus_handle = i2c_bus,
    };
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&i2c_codec_cfg);
    if (!ctrl_if) {
        ESP_LOGE(TAG, "I2C ctrl interface failed");
        return ESP_FAIL;
    }

    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();
    if (!gpio_if) {
        ESP_LOGE(TAG, "GPIO interface failed");
        return ESP_FAIL;
    }

    es8388_codec_cfg_t es8388_cfg = {
        .ctrl_if     = ctrl_if,
        .gpio_if     = gpio_if,
        .codec_mode  = ESP_CODEC_DEV_WORK_MODE_BOTH,
        .master_mode = false,      // ES8388 — I2S slave, ESP32 — master
        .pa_pin      = PA_PIN,     // GPIO21, active HIGH
        .pa_reverted = false,
        .hw_gain     = {
            .pa_voltage        = 5.0f,
            .codec_dac_voltage = 3.3f,
        },
    };
    const audio_codec_if_t *codec_if = es8388_codec_new(&es8388_cfg);
    if (!codec_if) {
        ESP_LOGE(TAG, "es8388_codec_new failed");
        return ESP_FAIL;
    }

    audio_codec_i2s_cfg_t i2s_codec_cfg = {
        .port      = I2S_PORT,
        .rx_handle = rx_handle,
        .tx_handle = tx_handle,
    };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_codec_cfg);
    if (!data_if) {
        ESP_LOGE(TAG, "I2S data interface failed");
        return ESP_FAIL;
    }

    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN_OUT,
        .codec_if = codec_if,
        .data_if  = data_if,
    };
    s_codec_dev = esp_codec_dev_new(&dev_cfg);
    if (!s_codec_dev) {
        ESP_LOGE(TAG, "esp_codec_dev_new failed");
        return ESP_FAIL;
    }

    esp_codec_dev_sample_info_t fs = {
        .sample_rate     = SAMPLE_RATE,
        .channel         = 2,
        .bits_per_sample = 16,
    };
    ret = esp_codec_dev_open(s_codec_dev, &fs);
    if (ret != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG, "esp_codec_dev_open failed: %d", ret);
        return ESP_FAIL;
    }

    // ADC вход: LIN1/RIN1 = бортовые MEMS микрофоны (reg 0x0A = 0x00)
    // AUX IN (LIN2/RIN2 = 0x50) не подходит для пассивной петлички — нет mic bias
    // Оставляем 0x00 (default от драйвера)

    // Начальная громкость: 90% на DAC, 30dB усиления микрофона
    esp_codec_dev_set_out_vol(s_codec_dev, 100);
    esp_codec_dev_set_in_gain(s_codec_dev, 30.0f);

    ESP_LOGI(TAG, "ES8388 OK — vol=90 mic=LIN1/RIN1(MEMS) gain=30dB PA=GPIO%d", PA_PIN);
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Инициализация Bluetooth
// ---------------------------------------------------------------------------

static esp_err_t bt_init(void)
{
    esp_err_t ret;

    // Освободить память BLE — используем только Classic BT
    ret = esp_bt_controller_mem_release(ESP_BT_MODE_BLE);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG_BT, "BLE mem release: %s", esp_err_to_name(ret));
    }

    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT));

    esp_bluedroid_config_t bd_cfg = BT_BLUEDROID_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bluedroid_init_with_cfg(&bd_cfg));
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    // GAP
    ESP_ERROR_CHECK(esp_bt_gap_set_device_name(BT_DEVICE_NAME));
    ESP_ERROR_CHECK(esp_bt_gap_register_callback(gap_cb));

    // SSP IO capability
    esp_bt_sp_param_t param_type = ESP_BT_SP_IOCAP_MODE;
    esp_bt_io_cap_t   iocap      = ESP_BT_IO_CAP_IO;
    esp_bt_gap_set_security_param(param_type, &iocap, sizeof(iocap));

    // PIN для legacy устройств
    esp_bt_pin_type_t pin_type = ESP_BT_PIN_TYPE_FIXED;
    esp_bt_pin_code_t pin_code = {'0', '0', '0', '0'};
    esp_bt_gap_set_pin(pin_type, 4, pin_code);

    // HFP client
    ESP_ERROR_CHECK(esp_hf_client_register_callback(hf_client_cb));
    ESP_ERROR_CHECK(esp_hf_client_init());

    // Legacy PCM callbacks: Bluedroid декодирует mSBC/CVSD → сырой PCM
    esp_hf_client_register_data_callback(incoming_pcm_cb, outgoing_pcm_cb);

    // Видимость
    esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);

    const uint8_t *mac = esp_bt_dev_get_address();
    ESP_LOGI(TAG_BT, "BT ready: '%s'  MAC=%02x:%02x:%02x:%02x:%02x:%02x",
             BT_DEVICE_NAME,
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// app_main
// ---------------------------------------------------------------------------

void app_main(void)
{
    ESP_LOGI(TAG, "=== LyraT Phone Stream — Phase 2: ES8388 + HFP Audio ===");

    // NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_LOGI(TAG, "Free heap: %"PRIu32" bytes", esp_get_free_heap_size());

    // Ring buffers
    s_rb_call_rx = xRingbufferCreate(FRAME_BYTES_MONO * RINGBUF_FRAMES,
                                     RINGBUF_TYPE_BYTEBUF);
    s_rb_mic_tx  = xRingbufferCreate(FRAME_BYTES_MONO * RINGBUF_FRAMES,
                                     RINGBUF_TYPE_BYTEBUF);
    if (!s_rb_call_rx || !s_rb_mic_tx) {
        ESP_LOGE(TAG, "Ring buffer alloc failed");
        abort();
    }

    // Аудиоподсистема: I2C + I2S + ES8388
    ESP_ERROR_CHECK(audio_init());

    // Bluetooth
    ESP_ERROR_CHECK(bt_init());

    // Задачи на Core 1 (BT стек крутится на Core 0)
    xTaskCreatePinnedToCore(i2s_tx_task, "i2s_tx",
                            4096, NULL, 15, NULL, 1);
    xTaskCreatePinnedToCore(mic_rx_task, "mic_rx",
                            4096, NULL, 14, NULL, 1);

    ESP_LOGI(TAG, "Free heap after init: %"PRIu32" bytes", esp_get_free_heap_size());
    ESP_LOGI(TAG, "Pair Android with '%s', make a call to test audio",
             BT_DEVICE_NAME);

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        ESP_LOGI(TAG, "SCO=%s heap=%"PRIu32,
                 s_sco_active ? "active" : "idle",
                 esp_get_free_heap_size());
    }
}
