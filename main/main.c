#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "freertos/event_groups.h"

#include "esp_log.h"
#include "esp_err.h"
#include "esp_heap_caps.h"

#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"

#include "lwip/udp.h"
#include "lwip/ip_addr.h"
#include "lwip/tcpip.h"
#include "lwip/pbuf.h"

#include "driver/gpio.h"
#include "driver/i2s_std.h"

static const char *TAG = "audio_stream";

// -------------------- Settings (edit) --------------------

// I2S0 — BT1036-A (phone side, full-duplex)
// ESP32 = master (drives BCLK/WS), BT1036-A = slave
#define I2S0_BCLK_GPIO  (GPIO_NUM_4)
#define I2S0_WS_GPIO    (GPIO_NUM_5)
#define I2S0_DIN_GPIO   (GPIO_NUM_6)   // BT1036-A P33 DO → ESP32  (call audio RX)
#define I2S0_DOUT_GPIO  (GPIO_NUM_7)   // ESP32 → BT1036-A P32 DI  (mic TX to phone)

// I2S1 — BT1036-B (headset side, full-duplex)
// ESP32 = master (drives BCLK/WS), BT1036-B = slave
#define I2S1_BCLK_GPIO  (GPIO_NUM_15)  // BCLK → BT1036-B P30
#define I2S1_WS_GPIO    (GPIO_NUM_16)  // WS   → BT1036-B P31
#define I2S1_DOUT_GPIO  (GPIO_NUM_17)  // ESP32 → BT1036-B P32 DI  (audio to headphones)
#define I2S1_DIN_GPIO   (GPIO_NUM_18)  // BT1036-B P33 DO → ESP32  (mic from headphones)

#define SAMPLE_RATE_HZ     (8000)      // OnePlus 13R: CVSD 8 kHz
#define BITS_PER_SAMPLE    (16)

#define FRAME_MS           (20)
#define SAMPLES_PER_FRAME  ((SAMPLE_RATE_HZ * FRAME_MS) / 1000)        // 160
#define BYTES_PER_SAMPLE   (BITS_PER_SAMPLE / 8)
#define FRAME_BYTES_STEREO (SAMPLES_PER_FRAME * BYTES_PER_SAMPLE * 2)  // 640
#define FRAME_BYTES_MONO   (SAMPLES_PER_FRAME * BYTES_PER_SAMPLE)      // 320

#define RINGBUF_CAPACITY_BYTES (FRAME_BYTES_MONO * 8)

#define UDP_REMOTE_IP   "192.168.1.214"
#define UDP_REMOTE_PORT (5004)

#define SW_GAIN 28   // software gain for call audio (AT+SPKVOL unavailable in I2S mode)

// ---------------------------------------------------------

// Ring buffers
static RingbufHandle_t s_rb    = NULL;  // call audio (BT1036-A RX) → UDP
static RingbufHandle_t s_rb_tx = NULL;  // call audio → BT1036-B TX → headphones

// I2S handles
static i2s_chan_handle_t s_i2s_rx      = NULL;  // I2S0 RX: call audio from BT1036-A
static i2s_chan_handle_t s_i2s_call_tx = NULL;  // I2S0 TX: mic to BT1036-A → phone
static i2s_chan_handle_t s_i2s_tx      = NULL;  // I2S1 TX: call audio to BT1036-B → headphones
static i2s_chan_handle_t s_i2s_mic_rx  = NULL;  // I2S1 RX: mic from BT1036-B headphones

// Raw lwIP UDP PCB (created after WiFi IP assignment, thread-safe via LOCK_TCPIP_CORE)
static struct udp_pcb *s_udp_pcb = NULL;

static esp_err_t i2s_init(void)
{
    // I2S0: full-duplex — BT1036-A (phone side)
    // ESP32 = master; BT1036-A configured as slave via AT+I2SCFG=3
    {
        i2s_chan_config_t cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
        ESP_ERROR_CHECK(i2s_new_channel(&cfg, &s_i2s_call_tx, &s_i2s_rx));

        i2s_std_config_t std = {
            .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE_HZ),
            .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                            I2S_SLOT_MODE_STEREO),
            .gpio_cfg = {
                .mclk = GPIO_NUM_NC,
                .bclk = I2S0_BCLK_GPIO,
                .ws   = I2S0_WS_GPIO,
                .dout = I2S0_DOUT_GPIO,
                .din  = I2S0_DIN_GPIO,
                .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
            },
        };
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_i2s_call_tx, &std));
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_i2s_rx,      &std));
        ESP_ERROR_CHECK(i2s_channel_enable(s_i2s_call_tx));
        ESP_ERROR_CHECK(i2s_channel_enable(s_i2s_rx));
    }

    // I2S1: full-duplex — BT1036-B (headset side)
    // ESP32 = master; BT1036-B configured as slave via AT+I2SCFG=3
    {
        i2s_chan_config_t cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
        ESP_ERROR_CHECK(i2s_new_channel(&cfg, &s_i2s_tx, &s_i2s_mic_rx));

        i2s_std_config_t std = {
            .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE_HZ),
            .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                            I2S_SLOT_MODE_STEREO),
            .gpio_cfg = {
                .mclk = GPIO_NUM_NC,
                .bclk = I2S1_BCLK_GPIO,
                .ws   = I2S1_WS_GPIO,
                .dout = I2S1_DOUT_GPIO,
                .din  = I2S1_DIN_GPIO,
                .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
            },
        };
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_i2s_tx,     &std));
        ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_i2s_mic_rx, &std));
        ESP_ERROR_CHECK(i2s_channel_enable(s_i2s_tx));
        ESP_ERROR_CHECK(i2s_channel_enable(s_i2s_mic_rx));
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
        esp_wifi_connect();
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG, "wifi disconnected, retrying");
        // Free UDP PCB on disconnect so it's recreated cleanly after reconnect
        LOCK_TCPIP_CORE();
        if (s_udp_pcb) {
            udp_remove(s_udp_pcb);
            s_udp_pcb = NULL;
        }
        UNLOCK_TCPIP_CORE();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "got ip:" IPSTR, IP2STR(&event->ip_info.ip));
        // Create raw lwIP UDP PCB — safe to call here with LOCK_TCPIP_CORE
        LOCK_TCPIP_CORE();
        if (s_udp_pcb == NULL) {
            s_udp_pcb = udp_new_ip_type(IPADDR_TYPE_V4);
            if (s_udp_pcb) {
                ip_addr_t dest;
                IP4_ADDR(&dest.u_addr.ip4,
                         192, 168, 1, 214);  // UDP_REMOTE_IP
                dest.type = IPADDR_TYPE_V4;
                udp_connect(s_udp_pcb, &dest, UDP_REMOTE_PORT);
                ESP_LOGI(TAG, "UDP PCB ready -> " UDP_REMOTE_IP ":%d", UDP_REMOTE_PORT);
            } else {
                ESP_LOGE(TAG, "udp_new failed");
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
    // Wait until WiFi is up and UDP PCB is created
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
                if (err != ERR_OK) {
                    ESP_LOGW(TAG, "udp_send err=%d", (int)err);
                }
                pbuf_free(p);
            } else {
                ESP_LOGW(TAG, "pbuf_alloc failed (out of pbufs)");
            }
        }
        UNLOCK_TCPIP_CORE();

        vRingbufferReturnItem(s_rb, item);
    }
}

// ---------------------------------------------------------------------------
// TASK: i2s_tx — re-streams call audio via I2S1 to BT1036-B → BT headphones
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
// TASK: mic_task — reads headphone mic from I2S1 RX, writes directly to I2S0 TX → phone
// No ring buffer: avoids cross-core SMP xRingbufferSend assert
// ---------------------------------------------------------------------------
static void mic_task(void *arg)
{
    int16_t *stereo_in  = (int16_t *)heap_caps_malloc(FRAME_BYTES_STEREO, MALLOC_CAP_8BIT);
    int16_t *stereo_out = (int16_t *)heap_caps_malloc(FRAME_BYTES_STEREO, MALLOC_CAP_8BIT);
    if (!stereo_in || !stereo_out) {
        ESP_LOGE(TAG, "No memory for mic buffers");
        vTaskDelete(NULL);
        return;
    }

    while (1) {
        size_t bytes_read = 0;
        esp_err_t err = i2s_channel_read(s_i2s_mic_rx, stereo_in, FRAME_BYTES_STEREO,
                                         &bytes_read, portMAX_DELAY);
        if (err != ESP_OK || bytes_read == 0) continue;

        // BT1036-B always outputs stereo L+R; extract LEFT channel, expand back to stereo
        size_t mono_samples = (bytes_read / 2) / 2;
        for (size_t i = 0; i < mono_samples; i++) {
            int16_t s = stereo_in[i * 2];
            stereo_out[i * 2]     = s;
            stereo_out[i * 2 + 1] = s;
        }

        size_t bytes_written = 0;
        i2s_channel_write(s_i2s_call_tx, stereo_out, mono_samples * 4,
                          &bytes_written, pdMS_TO_TICKS(100));
    }
}

// ---------------------------------------------------------------------------

void app_main(void)
{
    wifi_connect_sta();

    s_rb = xRingbufferCreate(RINGBUF_CAPACITY_BYTES, RINGBUF_TYPE_BYTEBUF);
    if (!s_rb) { ESP_LOGE(TAG, "Failed to create s_rb"); return; }

    s_rb_tx = xRingbufferCreate(RINGBUF_CAPACITY_BYTES, RINGBUF_TYPE_BYTEBUF);
    if (!s_rb_tx) { ESP_LOGE(TAG, "Failed to create s_rb_tx"); return; }

    ESP_ERROR_CHECK(i2s_init());

    ESP_LOGI(TAG, "Free heap after init: %lu bytes", (unsigned long)esp_get_free_heap_size());

    // Core 0: I2S reads (time-critical)
    xTaskCreatePinnedToCore(i2s_rx_task, "i2s_rx", 4096, NULL, 20, NULL, 0);
    xTaskCreatePinnedToCore(mic_task,    "mic",     4096, NULL, 20, NULL, 0);

    // Core 1: outputs
    xTaskCreatePinnedToCore(udp_tx_task, "udp_tx", 8192, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(i2s_tx_task, "i2s_tx", 4096, NULL, 15, NULL, 1);
}
