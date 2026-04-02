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

#include "aec.h"

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

// Frame-level noise gate with hysteresis + hold time.
// Opens when frame RMS exceeds OPEN threshold.
// Closes only after RMS stays below CLOSE threshold for HOLD_FRAMES consecutive frames.
// Prevents choppy stutter on natural phoneme silences (p/t/k stops last ~20ms).
#define NOISE_GATE_OPEN       450   // RMS to open gate
#define NOISE_GATE_CLOSE      380   // RMS to start hold-down counter (raised above noise floor)
#define NOISE_GATE_HOLD       4     // frames (~80ms) RMS must stay low to actually close

#define MIC_SW_GAIN           2     // SW gain after hardware +59dB. Lapel mic is hot; 4 caused 4% clip.

#define UDP_REMOTE_IP    "192.168.1.214"
#define UDP_REMOTE_PORT  (5004)   // call audio: phone → headphones
#define UDP_MIC_PORT     (5005)   // mic audio:  headset mic → phone

#define SW_GAIN     4    // software gain for call audio. 8 caused clipping on peaks >4096.

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
    ESP_ERROR_CHECK(wm8960_write(0x19, 0x0FE));   // PWR1: VMID=50kΩ, VREF, AINL, AINR, ADCL, ADCR, MICB
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
    // Fixed gain; ALC disabled (reset default). ADC digital volume at reset default (0dB).
    ESP_ERROR_CHECK(wm8960_write(0x00, 0x13F));   // Left  PGA: unmute, +30dB, IPVU
    ESP_ERROR_CHECK(wm8960_write(0x01, 0x13F));   // Right PGA: unmute, +30dB, IPVU
    ESP_ERROR_CHECK(wm8960_write(0x20, 0x138));   // L ADC: LINPUT1, boost=+29dB, PGA→boost
    ESP_ERROR_CHECK(wm8960_write(0x21, 0x138));   // R ADC: RINPUT1, boost=+29dB, PGA→boost

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
        i2s_chan_config_t cfg    = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
        cfg.dma_desc_num  = 8;   // more DMA descriptors (default 6)
        cfg.dma_frame_num = 480; // larger per-descriptor buffer (default 240) → ~480ms hardware buffer
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

        // BT1036 always outputs stereo L+R; extract LEFT channel + apply gain + soft limit.
        // Soft limiter: linear below SW_LIMIT_THRESHOLD, hyperbolic above.
        //   y = T + excess*(MAX-T)/(MAX-T+excess)  →  asymptotically approaches MAX, no hard clip.
        #define SW_LIMIT_THRESHOLD  16384   // ~0.5 FS: linear zone
        #define SW_LIMIT_MAX        32700   // ceiling (just below 32767 for safety)
        size_t mono_samples = (bytes_read / 2) / 2;
        for (size_t i = 0; i < mono_samples; i++) {
            int32_t v = (int32_t)stereo[i * 2] * SW_GAIN;
            // Apply soft limiter symmetrically
            int32_t sign = (v < 0) ? -1 : 1;
            int32_t a    = v * sign;  // absolute value
            if (a > SW_LIMIT_THRESHOLD) {
                int32_t excess  = a - SW_LIMIT_THRESHOLD;
                int32_t headroom = SW_LIMIT_MAX - SW_LIMIT_THRESHOLD;
                a = SW_LIMIT_THRESHOLD + (excess * headroom) / (headroom + excess);
            }
            mono[i] = (int16_t)(sign * a);
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
        if (xRingbufferSend(s_rb_tx, mono, mono_bytes, pdMS_TO_TICKS(25)) != pdTRUE) {
            ESP_LOGW(TAG, "s_rb_tx full, dropping headphone frame");
        }
        // Feed AEC reference: the call audio that goes to headphones (echo source)
        aec_write_reference(mono, mono_samples);
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

    uint32_t tx_frame  = 0;
    uint32_t write_err = 0;
    while (1) {
        size_t   item_size = 0;
        int16_t *item      = (int16_t *)xRingbufferReceive(s_rb_tx, &item_size,
                                                            portMAX_DELAY);
        if (!item) continue;

        size_t mono_samples = item_size / 2;
        // Expand mono → stereo (L = R)
        for (size_t i = 0; i < mono_samples; i++) {
            stereo_out[i * 2]     = item[i];
            stereo_out[i * 2 + 1] = item[i];
        }

        size_t    bytes_written = 0;
        esp_err_t err = i2s_channel_write(s_i2s_tx, stereo_out, mono_samples * 4,
                                          &bytes_written, pdMS_TO_TICKS(200));
        if (err != ESP_OK) write_err++;

        tx_frame++;
        if (tx_frame % 50 == 0) {
            int16_t max_val = 0;
            for (size_t i = 0; i < mono_samples; i++) {
                int16_t v = item[i] < 0 ? -item[i] : item[i];
                if (v > max_val) max_val = v;
            }
            ESP_LOGI(TAG, "hp_tx frame=%lu write_err=%lu max=%d",
                     (unsigned long)tx_frame, (unsigned long)write_err, max_val);
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

        size_t samples = bytes_read / 4;

        // Average L and R channels → stereo[i*2] = (L+R)/2.
        // Uncorrelated ADC noise cancels by ~3dB; correlated signal (mic) preserved.
        for (size_t i = 0; i < samples; i++) {
            int32_t avg = ((int32_t)stereo[i * 2] + (int32_t)stereo[i * 2 + 1]) / 2;
            stereo[i * 2] = (int16_t)avg;
        }

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

        // HPF ~80 Hz (1-pole, α=30651 Q15) + biquad notch at 50/150/250 Hz (mains hum harmonics).
        // Notch: H(z)=(1-2cos(w0)z^-1+z^-2)/(1-2r*cos(w0)z^-1+r^2*z^-2), r=0.95
        // Coefficients Q14 (×16384). Stability verified: poles at |z|=0.95 < 1.
        //   50 Hz:  cos=0.99923 → b1=-32728, a1=-31100, a2=14786
        //   150 Hz: cos=0.99307 → b1=-32542, a1=-30905, a2=14786
        //   250 Hz: cos=0.98079 → b1=-32127, a1=-30521, a2=14786
        {
            static int32_t s_hp_x_prev = 0, s_hp_y_prev = 0;
            static int32_t xp1[3]={0,0,0}, xp2[3]={0,0,0};
            static int32_t yp1[3]={0,0,0}, yp2[3]={0,0,0};
            static const int32_t b1[3] = {-32728, -32542, -32127};
            static const int32_t a1[3] = {-31100, -30905, -30521};
            static const int32_t a2    =  14786;

            for (size_t i = 0; i < samples; i++) {
                // HPF ~80 Hz (α=30651 Q15, -3dB @ 80 Hz)
                int32_t x = stereo[i * 2];
                int32_t y = x - s_hp_x_prev + ((30651 * s_hp_y_prev) >> 15);
                if (y >  32767) y =  32767;
                if (y < -32768) y = -32768;
                s_hp_x_prev = x;
                s_hp_y_prev = y;
                x = y;

                // Three notch biquads in series (50 Hz, 150 Hz, 250 Hz)
                for (int k = 0; k < 3; k++) {
                    y = x
                      + ((b1[k] * xp1[k]) >> 14)
                      + xp2[k]
                      - ((a1[k] * yp1[k]) >> 14)
                      - ((a2    * yp2[k]) >> 14);
                    if (y >  32767) y =  32767;
                    if (y < -32768) y = -32768;
                    xp2[k] = xp1[k]; xp1[k] = x;
                    yp2[k] = yp1[k]; yp1[k] = y;
                    x = y;
                }
                stereo[i * 2] = (int16_t)x;
            }
        }

        // Frame-level noise gate with hysteresis + Wiener noise suppression.
        // During silence (gate closed):  update noise floor estimate, mute output.
        // During speech (gate open):     apply Wiener gain g = max(0, 1 - N/S) to
        //                                suppress residual noise proportional to SNR.
        // gate_open: 1 = passing audio, 0 = muted.
        {
            static int     s_gate_open    = 0;
            static int     s_close_count  = 0;
            static int32_t s_noise_sq     = 0;  // noise floor mean_sq estimate
            static int     s_noise_valid  = 0;  // 1 once we have ≥1 silent frame

            int64_t sum_sq = 0;
            for (size_t i = 0; i < samples; i++) {
                int32_t v = stereo[i * 2];
                sum_sq += v * v;
            }
            // mean_sq vs threshold² avoids sqrt; same as comparing RMS to threshold
            int32_t mean_sq = (int32_t)(sum_sq / (int64_t)samples);

            if (!s_gate_open) {
                // Update noise floor with exponential smoothing (τ ≈ 0.3 s at 20ms frames)
                if (!s_noise_valid) {
                    s_noise_sq    = mean_sq;
                    s_noise_valid = 1;
                } else {
                    s_noise_sq = (int32_t)(((int64_t)s_noise_sq * 30 + (int64_t)mean_sq * 2) >> 5);
                }
                // Gate closed: open if RMS exceeds OPEN threshold
                if (mean_sq >= (int32_t)NOISE_GATE_OPEN * NOISE_GATE_OPEN) {
                    s_gate_open   = 1;
                    s_close_count = 0;
                }
            } else {
                // Gate open: count consecutive frames below CLOSE threshold
                if (mean_sq < (int32_t)NOISE_GATE_CLOSE * NOISE_GATE_CLOSE) {
                    if (++s_close_count >= NOISE_GATE_HOLD) {
                        s_gate_open   = 0;
                        s_close_count = 0;
                    }
                } else {
                    s_close_count = 0;  // reset counter on any loud frame
                }
            }

            if (!s_gate_open) {
                for (size_t i = 0; i < samples; i++) {
                    stereo[i * 2]     = 0;
                    stereo[i * 2 + 1] = 0;
                }
            } else {
                // Wiener gain in Q14: g = max(0, 1 - noise_sq/signal_sq)
                // Suppresses residual noise during speech without hard gating.
                int32_t g_Q14 = 16384;  // 1.0 in Q14 (no suppression by default)
                if (s_noise_valid && mean_sq > s_noise_sq && mean_sq > 0) {
                    int32_t ratio_Q14 = (int32_t)(((int64_t)s_noise_sq << 14) / mean_sq);
                    g_Q14 = 16384 - ratio_Q14;
                    if (g_Q14 < 0)     g_Q14 = 0;
                    if (g_Q14 > 16384) g_Q14 = 16384;
                }
                for (size_t i = 0; i < samples; i++) {
                    // Apply SW gain and Wiener gain together to avoid extra multiply stage.
                    // Max intermediate: 32767 × MIC_SW_GAIN × 16384 fits in int32 when gain≤2.
                    int32_t v = (((int32_t)stereo[i * 2] * MIC_SW_GAIN) * g_Q14) >> 14;
                    if (v >  32767) v =  32767;
                    if (v < -32768) v = -32768;
                    stereo[i * 2]     = (int16_t)v;
                    stereo[i * 2 + 1] = stereo[i * 2];
                }
            }
        }

        // Pack mono (L channel) into first half, apply AEC, send to UDP ringbuf.
        for (size_t i = 0; i < samples; i++) {
            stereo[i] = stereo[i * 2];
        }
        aec_process(stereo, samples);
        xRingbufferSend(s_rb_mic, stereo, samples * 2, 0);

        // Rebuild stereo L=R=mic (reverse to avoid overlap)
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

    s_rb_tx = xRingbufferCreate(RINGBUF_CAPACITY_BYTES * 2, RINGBUF_TYPE_NOSPLIT);  // 2× headroom for BT jitter
    if (!s_rb_tx) { ESP_LOGE(TAG, "Failed to create s_rb_tx"); return; }

    s_rb_mic = xRingbufferCreate(RINGBUF_CAPACITY_BYTES, RINGBUF_TYPE_NOSPLIT);
    if (!s_rb_mic) { ESP_LOGE(TAG, "Failed to create s_rb_mic"); return; }

    ESP_ERROR_CHECK(i2s_init());
    wm8960_init();
    aec_init();

    ESP_LOGI(TAG, "Free heap after init: %lu bytes", (unsigned long)esp_get_free_heap_size());

    // Core 0: I2S reads (time-critical)
    xTaskCreatePinnedToCore(i2s_rx_task,   "i2s_rx",  4096, NULL, 20, NULL, 0);
    xTaskCreatePinnedToCore(mic_rx_task,   "mic_rx",  4096, NULL, 19, NULL, 0);

    // Core 1: outputs
    xTaskCreatePinnedToCore(udp_tx_task,     "udp_tx",  8192, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(mic_udp_tx_task, "mic_udp", 8192, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(i2s_tx_task,     "i2s_tx",  4096, NULL, 15, NULL, 1);
}
