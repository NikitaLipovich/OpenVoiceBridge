#include <string.h>

#include "lwip/udp.h"
#include "lwip/tcpip.h"
#include "lwip/ip_addr.h"
#include "lwip/pbuf.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"

#include "esp_log.h"
#include "esp_err.h"

#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/event_groups.h"

#include "esp_heap_caps.h"

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "driver/i2c_master.h"

static const char *TAG = "audio_stream";

// -------------------- Settings (edit) --------------------

// I2S0 — BT1036-A (phone side, full-duplex)
#define I2S_BCLK_GPIO   (GPIO_NUM_4)
#define I2S_WS_GPIO     (GPIO_NUM_5)
#define I2S_DIN_GPIO    (GPIO_NUM_6)   // BT1036-A P33 DO → ESP32  (call audio RX)
#define I2S_DOUT_GPIO   (GPIO_NUM_7)   // ESP32 → BT1036-A P32 DI  (mic TX to phone)

// I2S1 — WM8960 DAC (headphone output) + ADC (microphone input)
#define I2S1_MCLK_GPIO  (GPIO_NUM_NC)  // MCLK не нужен — на плате WM8960 SparkFun уже стоит встроенный генератор 2MHz
#define I2S1_BCLK_GPIO  (GPIO_NUM_15)  // BCLK → WM8960 DCLK
#define I2S1_WS_GPIO    (GPIO_NUM_16)  // WS   → WM8960 DLRC + ALRC (перемычка на плате)
#define I2S1_DOUT_GPIO  (GPIO_NUM_17)  // DOUT → WM8960 DDAT
#define I2S1_DIN_GPIO   (GPIO_NUM_18)  // DIN  ← WM8960 ADAT (mic ADC output)

// I2C — WM8960 control
#define WM8960_SDA_GPIO (GPIO_NUM_8)
#define WM8960_SCL_GPIO (GPIO_NUM_9)
#define WM8960_ADDR     (0x1A)

#define SAMPLE_RATE_HZ     (8000)      // OnePlus 13R: CVSD 8 kHz
#define BITS_PER_SAMPLE    (16)

#define FRAME_MS           (20)
#define SAMPLES_PER_FRAME  ((SAMPLE_RATE_HZ * FRAME_MS) / 1000)        // 160
#define BYTES_PER_SAMPLE   (BITS_PER_SAMPLE / 8)
#define FRAME_BYTES_STEREO (SAMPLES_PER_FRAME * BYTES_PER_SAMPLE * 2)  // 640
#define FRAME_BYTES_MONO   (SAMPLES_PER_FRAME * BYTES_PER_SAMPLE)      // 320

#define RINGBUF_CAPACITY_BYTES (FRAME_BYTES_MONO * 8)

#define UDP_REMOTE_IP    "192.168.1.214"
#define UDP_REMOTE_PORT  (5004)   // call audio: phone → headphones
#define UDP_MIC_PORT     (5005)   // mic audio:  headset mic → phone

#define SW_GAIN 8   // software gain for call audio (4=+12dB, 8=+18dB)

// ---------------------------------------------------------

// UDP PCBs (raw lwIP, created on IP_EVENT_STA_GOT_IP)
static struct udp_pcb *s_udp_pcb     = NULL;  // port 5004: call audio
static struct udp_pcb *s_udp_mic_pcb = NULL;  // port 5005: mic audio

// Ring buffers
static RingbufHandle_t s_rb     = NULL;  // call audio → UDP 5004
static RingbufHandle_t s_rb_tx  = NULL;  // call audio → DAC headphones
static RingbufHandle_t s_rb_mic = NULL;  // mic audio  → UDP 5005

// I2S handles
static i2s_chan_handle_t s_i2s_rx      = NULL;  // I2S0 RX: call from BT1036-A
static i2s_chan_handle_t s_i2s_call_tx = NULL;  // I2S0 TX: mic to BT1036-A → phone
static i2s_chan_handle_t s_i2s_tx      = NULL;  // I2S1 TX: DAC → headphones
static i2s_chan_handle_t s_i2s_mic     = NULL;  // I2S1 RX: WM8960 ADC → mic data

// ---------------------------------------------------------------------------
// WM8960 init via I2C
// ---------------------------------------------------------------------------
static i2c_master_bus_handle_t s_i2c_bus = NULL;
static i2c_master_dev_handle_t s_wm8960  = NULL;

static esp_err_t wm8960_write(uint8_t reg, uint16_t val)
{
    // WM8960 protocol: [reg(7-bit) | val_bit8], [val_bits7-0]
    uint8_t buf[2] = {
        (uint8_t)((reg << 1) | ((val >> 8) & 0x01)),
        (uint8_t)(val & 0xFF),
    };
    return i2c_master_transmit(s_wm8960, buf, sizeof(buf), 50);  // 50ms, не pdMS_TO_TICKS
}

static void wm8960_init(void)
{
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port          = I2C_NUM_0,
        .sda_io_num        = WM8960_SDA_GPIO,
        .scl_io_num        = WM8960_SCL_GPIO,
        .clk_source        = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = false,  // на плате уже есть 2.2kΩ
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &s_i2c_bus));

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = WM8960_ADDR,
        .scl_speed_hz    = 100000,
        .scl_wait_us     = 1000,
    };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_i2c_bus, &dev_cfg, &s_wm8960));

    ESP_ERROR_CHECK(i2c_master_probe(s_i2c_bus, WM8960_ADDR, 1000));
    ESP_LOGI(TAG, "WM8960 detected at 0x%02X", WM8960_ADDR);

    esp_err_t err = wm8960_write(0x0F, 0x000);  // Reset
    ESP_LOGI(TAG, "reset write result = %s", esp_err_to_name(err));
    ESP_ERROR_CHECK(err);
    vTaskDelay(pdMS_TO_TICKS(10));

    // Power — VMID должен зарядиться до включения усилителей
    ESP_ERROR_CHECK(wm8960_write(0x19, 0x0EA));   // PWR1: VMID=50kΩ, VREF, AINL(bit5), ADCL(bit3), MICB(bit1)
    vTaskDelay(pdMS_TO_TICKS(500));               // ждём зарядку VMID (50kΩ × C_internal)
    ESP_ERROR_CHECK(wm8960_write(0x1A, 0x1E4));   // PWR2: DACL, DACR, LOUT1, ROUT1, OUT3(bit2)
    ESP_ERROR_CHECK(wm8960_write(0x2F, 0x02C));   // PWR3: LMIC(bit5), LOMIX(bit3), ROMIX(bit2)

    // Audio interface: I2S format, 16-bit, slave (ESP32 is master)
    ESP_ERROR_CHECK(wm8960_write(0x07, 0x002));

    // Clocking: MCLK → SYSCLK, no PLL, no divider
    ESP_ERROR_CHECK(wm8960_write(0x04, 0x000));

    // Route DAC → output mixer → headphone amp
    ESP_ERROR_CHECK(wm8960_write(0x22, 0x100));   // Left  Mix: LD2LO=1
    ESP_ERROR_CHECK(wm8960_write(0x24, 0x100));   // Right Mix: RD2RO=1

    // Unmute DAC — default after reset is DACMU=1 (soft mute ON)
    ESP_ERROR_CHECK(wm8960_write(0x05, 0x000));   // ADC/DAC CTL1: DACMU=0

    // DAC volumes
    ESP_ERROR_CHECK(wm8960_write(0x0A, 0x1FF));   // Left  DAC: 0dB + VU
    ESP_ERROR_CHECK(wm8960_write(0x0B, 0x1FF));   // Right DAC: 0dB + VU
    ESP_ERROR_CHECK(wm8960_write(0x02, 0x179));   // LOUT1: 0dB + VU  (0x7F=+6dB, 0x79=0dB, 0x6D=-12dB, 0x67=-18dB)
    ESP_ERROR_CHECK(wm8960_write(0x03, 0x179));   // ROUT1: 0dB + VU

    // Microphone path: LINPUT1 → PGA → Boost → ADC → ADAT
    ESP_ERROR_CHECK(wm8960_write(0x00, 0x132));   // Left PGA: unmute, +20dB(LINVOL=50), IPVU(bit8)
    ESP_ERROR_CHECK(wm8960_write(0x20, 0x138));   // L ADC path: LMN1(bit8), boost=+29dB(bits5:4=11), LMIC2B(bit3)

    ESP_LOGI(TAG, "WM8960 initialized");
}

static esp_err_t i2s_init(void)
{
    // I2S0: full-duplex — BT1036-A (phone side)
    {
        i2s_chan_config_t cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
        ESP_ERROR_CHECK(i2s_new_channel(&cfg, &s_i2s_call_tx, &s_i2s_rx));

        i2s_std_config_t std = {
            .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE_HZ),
            .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                            I2S_SLOT_MODE_STEREO),
            .gpio_cfg = {
                .mclk = GPIO_NUM_NC,
                .bclk = I2S_BCLK_GPIO,
                .ws   = I2S_WS_GPIO,
                .dout = I2S_DOUT_GPIO,
                .din  = I2S_DIN_GPIO,
                .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
            },
        };
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_i2s_call_tx, &std));
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_i2s_rx,      &std));
        ESP_ERROR_CHECK(i2s_channel_enable(s_i2s_call_tx));
        ESP_ERROR_CHECK(i2s_channel_enable(s_i2s_rx));
    }

    // I2S1: full-duplex — WM8960 DAC (headphones TX) + ADC (mic RX)
    {
        i2s_chan_config_t cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
        ESP_ERROR_CHECK(i2s_new_channel(&cfg, &s_i2s_tx, &s_i2s_mic));

        i2s_std_config_t std = {
            .clk_cfg = {
                .sample_rate_hz = SAMPLE_RATE_HZ,
                .clk_src        = I2S_CLK_SRC_DEFAULT,
                .mclk_multiple  = I2S_MCLK_MULTIPLE_256,  // не выводится (MCLK_GPIO=NC), используется внутри ESP32 для делителя
            },
            .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                            I2S_SLOT_MODE_STEREO),
            .gpio_cfg = {
                .mclk = I2S1_MCLK_GPIO,
                .bclk = I2S1_BCLK_GPIO,
                .ws   = I2S1_WS_GPIO,
                .dout = I2S1_DOUT_GPIO,
                .din  = I2S1_DIN_GPIO,
                .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
            },
        };
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_i2s_tx,  &std));
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_i2s_mic, &std));
        ESP_ERROR_CHECK(i2s_channel_enable(s_i2s_tx));
        ESP_ERROR_CHECK(i2s_channel_enable(s_i2s_mic));
    }

    return ESP_OK;
}

/* Wi-Fi STA connect */
#define WIFI_SSID "lipovich"
#define WIFI_PASS "Wonsik13_"

#define WIFI_CONNECTED_BIT BIT0
static EventGroupHandle_t s_wifi_event_group = NULL;

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        LOCK_TCPIP_CORE();
        if (s_udp_pcb)     { udp_remove(s_udp_pcb);     s_udp_pcb     = NULL; }
        if (s_udp_mic_pcb) { udp_remove(s_udp_mic_pcb); s_udp_mic_pcb = NULL; }
        UNLOCK_TCPIP_CORE();
        esp_wifi_connect();
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG, "wifi disconnected, retrying");
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "got ip:" IPSTR, IP2STR(&event->ip_info.ip));
        ip_addr_t dest;
        IP4_ADDR(&dest.u_addr.ip4, 192, 168, 1, 214);
        dest.type = IPADDR_TYPE_V4;
        LOCK_TCPIP_CORE();
        if (s_udp_pcb == NULL) {
            s_udp_pcb = udp_new_ip_type(IPADDR_TYPE_V4);
            if (s_udp_pcb) {
                udp_connect(s_udp_pcb, &dest, UDP_REMOTE_PORT);
                ESP_LOGI(TAG, "UDP call PCB created (port %d)", UDP_REMOTE_PORT);
            }
        }
        if (s_udp_mic_pcb == NULL) {
            s_udp_mic_pcb = udp_new_ip_type(IPADDR_TYPE_V4);
            if (s_udp_mic_pcb) {
                udp_connect(s_udp_mic_pcb, &dest, UDP_MIC_PORT);
                ESP_LOGI(TAG, "UDP mic PCB created (port %d)", UDP_MIC_PORT);
            }
        }
        UNLOCK_TCPIP_CORE();
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_connect_sta(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                       &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                       &wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid     = WIFI_SSID,
            .password = WIFI_PASS,
            .pmf_cfg  = { .capable = true, .required = false },
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));  // отключить power save — иначе lwIP крашится в sendto
    ESP_ERROR_CHECK(esp_wifi_connect());

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                                          pdFALSE, pdTRUE, pdMS_TO_TICKS(15000));
    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "connected to AP");
    } else {
        ESP_LOGW(TAG, "failed to connect to AP within timeout");
    }
}

// (BSD socket helper removed — now using raw lwIP UDP)

// ---------------------------------------------------------------------------
// TASK: i2s_rx — reads call audio from BT1036-A, fans out to UDP and headphones
// ---------------------------------------------------------------------------
static void i2s_rx_task(void *arg)
{
    int16_t *stereo = (int16_t *)heap_caps_malloc(FRAME_BYTES_STEREO, MALLOC_CAP_8BIT);
    int16_t *mono   = (int16_t *)heap_caps_malloc(FRAME_BYTES_MONO,   MALLOC_CAP_8BIT);
    if (!stereo || !mono) {
        ESP_LOGE(TAG, "No memory for i2s_rx buffers");
        vTaskDelete(NULL);
        return;
    }

    uint32_t frame_count = 0;
    while (1) {
        size_t bytes_read = 0;
        esp_err_t err = i2s_channel_read(s_i2s_rx, stereo, FRAME_BYTES_STEREO,
                                         &bytes_read, portMAX_DELAY);
        if (err != ESP_OK || bytes_read == 0) {
            ESP_LOGW(TAG, "i2s_rx read err=%s bytes=%u", esp_err_to_name(err),
                     (unsigned)bytes_read);
            continue;
        }

        // BT1036 always outputs stereo L+R; extract LEFT channel + apply gain
        size_t mono_samples = (bytes_read / 2) / 2;
        for (size_t i = 0; i < mono_samples; i++) {
            int32_t v = (int32_t)stereo[i * 2] * SW_GAIN;
            if (v >  32767) v =  32767;
            if (v < -32768) v = -32768;
            mono[i] = (int16_t)v;
        }
        size_t mono_bytes = mono_samples * 2;

        frame_count++;
        if (frame_count % 50 == 0) {
            int16_t max_val = 0;
            for (size_t i = 0; i < mono_samples; i++) {
                int16_t v = mono[i] < 0 ? -mono[i] : mono[i];
                if (v > max_val) max_val = v;
            }
            ESP_LOGI(TAG, "call frame=%lu mono_bytes=%u max=%d %s",
                     (unsigned long)frame_count, (unsigned)mono_bytes, max_val,
                     max_val > 100 ? "<<< AUDIO" : "(silence)");
        }

        if (xRingbufferSend(s_rb, mono, mono_bytes, pdMS_TO_TICKS(50)) != pdTRUE) {
            ESP_LOGW(TAG, "s_rb full, dropping frame");
        }
        if (xRingbufferSend(s_rb_tx, mono, mono_bytes, pdMS_TO_TICKS(10)) != pdTRUE) {
            // headphone path drop is non-fatal
        }
    }
}

// ---------------------------------------------------------------------------
// TASK: udp_tx — sends call audio to PC over UDP (raw lwIP, no BSD sockets)
// ---------------------------------------------------------------------------
static void udp_tx_task(void *arg)
{
    // Wait until wifi_event_handler creates the PCB after IP assignment
    while (s_udp_pcb == NULL) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    while (1) {
        size_t item_size = 0;
        uint8_t *item = (uint8_t *)xRingbufferReceive(s_rb, &item_size, portMAX_DELAY);
        if (!item) continue;

        LOCK_TCPIP_CORE();
        if (s_udp_pcb) {
            struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, (u16_t)item_size, PBUF_RAM);
            if (p) {
                memcpy(p->payload, item, item_size);
                err_t err = udp_send(s_udp_pcb, p);
                pbuf_free(p);
                if (err != ERR_OK) {
                    ESP_LOGW(TAG, "udp_send err=%d", (int)err);
                }
            } else {
                ESP_LOGW(TAG, "pbuf_alloc failed");
            }
        }
        UNLOCK_TCPIP_CORE();

        vRingbufferReturnItem(s_rb, item);
    }
}

// ---------------------------------------------------------------------------
// TASK: mic_udp_tx — sends headset mic audio to PC over UDP port 5005
// ---------------------------------------------------------------------------
static void mic_udp_tx_task(void *arg)
{
    while (s_udp_mic_pcb == NULL) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    while (1) {
        size_t item_size = 0;
        uint8_t *item = (uint8_t *)xRingbufferReceive(s_rb_mic, &item_size, portMAX_DELAY);
        if (!item) continue;

        LOCK_TCPIP_CORE();
        if (s_udp_mic_pcb) {
            struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, (u16_t)item_size, PBUF_RAM);
            if (p) {
                memcpy(p->payload, item, item_size);
                udp_send(s_udp_mic_pcb, p);
                pbuf_free(p);
            }
        }
        UNLOCK_TCPIP_CORE();

        vRingbufferReturnItem(s_rb_mic, item);
    }
}

// ---------------------------------------------------------------------------
// TASK: i2s_tx — sends call audio to BT1036-B → headphones
// ---------------------------------------------------------------------------
static void i2s_tx_task(void *arg)
{
    int16_t *stereo_out = (int16_t *)heap_caps_malloc(FRAME_BYTES_STEREO, MALLOC_CAP_8BIT);
    if (!stereo_out) {
        ESP_LOGE(TAG, "No memory for i2s_tx buffer");
        vTaskDelete(NULL);
        return;
    }

    uint32_t tx_frame = 0;
    while (1) {
        size_t item_size = 0;
        int16_t *item = (int16_t *)xRingbufferReceive(s_rb_tx, &item_size, portMAX_DELAY);
        if (!item) continue;

        // Expand mono → stereo (L = R)
        size_t mono_samples = item_size / 2;
        for (size_t i = 0; i < mono_samples; i++) {
            stereo_out[i * 2]     = item[i];
            stereo_out[i * 2 + 1] = item[i];
        }

        size_t bytes_written = 0;
        esp_err_t err = i2s_channel_write(s_i2s_tx, stereo_out, mono_samples * 4,
                                          &bytes_written, pdMS_TO_TICKS(100));
        tx_frame++;
        if (tx_frame % 50 == 0) {
            int16_t max_val = 0;
            for (size_t i = 0; i < mono_samples; i++) {
                int16_t v = item[i] < 0 ? -item[i] : item[i];
                if (v > max_val) max_val = v;
            }
            ESP_LOGI(TAG, "hp_tx frame=%lu written=%u max=%d err=%s",
                     (unsigned long)tx_frame, (unsigned)bytes_written, max_val,
                     esp_err_to_name(err));
        }
        vRingbufferReturnItem(s_rb_tx, item);
    }
}

// ---------------------------------------------------------------------------
// TASK: mic_rx — reads mic from WM8960 ADC, sends to BT1036-A → phone
// ---------------------------------------------------------------------------
static void mic_rx_task(void *arg)
{
    // WM8960 ADC outputs stereo I2S even when only left channel (mic) is active
    int16_t *stereo = (int16_t *)heap_caps_malloc(FRAME_BYTES_STEREO, MALLOC_CAP_8BIT);
    if (!stereo) {
        ESP_LOGE(TAG, "No memory for mic_rx buffer");
        vTaskDelete(NULL);
        return;
    }

    uint32_t mic_frame = 0;
    while (1) {
        size_t bytes_read = 0;
        esp_err_t err = i2s_channel_read(s_i2s_mic, stereo, FRAME_BYTES_STEREO,
                                         &bytes_read, portMAX_DELAY);
        if (err != ESP_OK || bytes_read == 0) {
            ESP_LOGW(TAG, "mic_rx read err=%s bytes=%u", esp_err_to_name(err), (unsigned)bytes_read);
            continue;
        }

        // WM8960 ADC: left channel = mic, right = 0 (only ADCL enabled)
        // Extract left, duplicate to right for BT1036-A I2S0 TX (expects stereo)
        size_t samples = bytes_read / 4;

        mic_frame++;
        if (mic_frame % 50 == 0) {
            int16_t max_val = 0;
            for (size_t i = 0; i < samples; i++) {
                int16_t v = stereo[i * 2];
                if (v < 0) v = -v;
                if (v > max_val) max_val = v;
            }
            ESP_LOGI(TAG, "mic frame=%lu bytes=%u max=%d %s",
                     (unsigned long)mic_frame, (unsigned)bytes_read, max_val,
                     max_val > 100 ? "<<< MIC" : "(silence)");
        }

        for (size_t i = 0; i < samples; i++) {
            int16_t left = stereo[i * 2];
            stereo[i * 2]     = left;
            stereo[i * 2 + 1] = left;
        }

        // Pack mono (left channel) into first half of buffer, then send to UDP
        for (size_t i = 0; i < samples; i++) {
            stereo[i] = stereo[i * 2];  // compact: L0,L1,L2...
        }
        xRingbufferSend(s_rb_mic, stereo, samples * 2, 0);  // mono 320 bytes

        // Rebuild stereo for I2S0 TX (L=R=mic)
        for (size_t i = samples; i-- > 0; ) {
            stereo[i * 2]     = stereo[i];
            stereo[i * 2 + 1] = stereo[i];
        }

        size_t bytes_written = 0;
        i2s_channel_write(s_i2s_call_tx, stereo, samples * 4,
                          &bytes_written, pdMS_TO_TICKS(100));
    }
}

// ---------------------------------------------------------------------------

void app_main(void)
{
    wifi_connect_sta();

    s_rb = xRingbufferCreate(RINGBUF_CAPACITY_BYTES, RINGBUF_TYPE_NOSPLIT);
    if (!s_rb) { ESP_LOGE(TAG, "Failed to create s_rb"); return; }

    s_rb_tx = xRingbufferCreate(RINGBUF_CAPACITY_BYTES, RINGBUF_TYPE_NOSPLIT);
    if (!s_rb_tx) { ESP_LOGE(TAG, "Failed to create s_rb_tx"); return; }

    s_rb_mic = xRingbufferCreate(RINGBUF_CAPACITY_BYTES, RINGBUF_TYPE_NOSPLIT);
    if (!s_rb_mic) { ESP_LOGE(TAG, "Failed to create s_rb_mic"); return; }

    ESP_ERROR_CHECK(i2s_init());
    wm8960_init();
    ESP_LOGI(TAG, "Free heap after init: %lu bytes", (unsigned long)esp_get_free_heap_size());

    // Core 0: I2S reads (time-critical)
    xTaskCreatePinnedToCore(i2s_rx_task,     "i2s_rx",  4096, NULL, 20, NULL, 0);
    xTaskCreatePinnedToCore(mic_rx_task,     "mic_rx",  4096, NULL, 19, NULL, 0);

    // Core 1: outputs
    xTaskCreatePinnedToCore(udp_tx_task,     "udp_tx",  8192, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(mic_udp_tx_task, "mic_udp", 8192, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(i2s_tx_task,     "i2s_tx",  4096, NULL, 15, NULL, 1);
}
