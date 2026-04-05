/*
 * ESP32 LyraT — Phone Stream (All Phases)
 *
 * BT HFP + ES8388 + DSP pipeline + WiFi UDP + UART console
 *
 * DSP pipeline stages (each toggleable at runtime via UART console):
 *   1. HPF 80 Hz        — removes hum and vibration
 *   2. Notch 50/150/250 — removes mains hum harmonics
 *   3. Wiener NS        — spectral noise suppression
 *   4. Noise gate       — hysteresis gate for silence
 *   5. AEC              — acoustic echo cancellation
 *   6. Soft limiter     — anti-clip on call audio path
 *
 * UART console (type in idf.py monitor):
 *   help, status, hpf 0/1, notch 0/1, wiener 0/1, gate 0/1,
 *   aec 0/1, limiter 0/1, mic_gain N, call_gain N, wifi 0/1
 *
 * LyraT v4.3 pins:
 *   I2C  : SDA=18, SCL=23
 *   I2S  : MCLK=0, BCLK=5, WS=25, DOUT=26, DIN=35
 *   PA   : GPIO21 (active HIGH)
 */

#include <string.h>
#include <stdlib.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "freertos/event_groups.h"

#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_bt_api.h"
#include "esp_hf_client_api.h"
#include "esp_coexist.h"

#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"

#include "lwip/udp.h"
#include "lwip/tcpip.h"
#include "lwip/ip_addr.h"
#include "lwip/pbuf.h"
#include "lwip/sockets.h"
#include <fcntl.h>

#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_attr.h"
#include "esp_heap_caps.h"

#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "es8388_codec.h"

#include "aec.h"
#include "spectral_ns.h"
#include "adpcm.h"

// ---------------------------------------------------------------------------
// Log tags
// ---------------------------------------------------------------------------

#define TAG       "MAIN"
#define TAG_BT    "BT"
#define TAG_AUDIO "AUDIO"
#define TAG_DSP   "DSP"
#define TAG_WIFI  "WIFI"
#define TAG_CMD   "CMD"

// ---------------------------------------------------------------------------
// LyraT v4.3 — hardware pins
// ---------------------------------------------------------------------------

#define I2C_PORT     I2C_NUM_0
#define I2C_SDA_PIN  18
#define I2C_SCL_PIN  23

#define I2S_PORT     I2S_NUM_0
#define I2S_MCLK_PIN GPIO_NUM_0
#define I2S_BCLK_PIN GPIO_NUM_5
#define I2S_WS_PIN   GPIO_NUM_25
#define I2S_DOUT_PIN GPIO_NUM_26
#define I2S_DIN_PIN  GPIO_NUM_35

#define PA_PIN       21

// ---------------------------------------------------------------------------
// Audio parameters
// ---------------------------------------------------------------------------

#define BT_DEVICE_NAME       "LyraT_Phone"
#define SAMPLE_RATE          16000
#define SAMPLES_PER_FRAME    240            // 15ms @ 16kHz
#define FRAME_BYTES_MONO     (SAMPLES_PER_FRAME * sizeof(int16_t))   // 480
#define FRAME_BYTES_STEREO   (FRAME_BYTES_MONO * 2)                  // 960
#define RINGBUF_FRAMES       8              // BT path ring buffers
#define RINGBUF_UDP_FRAMES   256            // UDP path — 4 sec buffer
#define UDP_BATCH_FRAMES     4              // 4 frames = 60ms per packet, ~490 bytes ADPCM
#define UDP_DUPLICATE        2              // send each packet 3 times (FEC via repetition)

// ---------------------------------------------------------------------------
// WiFi settings (edit for your network)
// ---------------------------------------------------------------------------

#define WIFI_SSID        "lipovich"
#define WIFI_PASS        "Wonsik13_"
#define UDP_REMOTE_IP_A  192
#define UDP_REMOTE_IP_B  168
#define UDP_REMOTE_IP_C  1
#define UDP_REMOTE_IP_D  214
#define UDP_CALL_PORT    5004   // call audio: phone → headphones
#define UDP_MIC_PORT     5005   // mic audio: headset mic → phone

#define WIFI_CONNECTED_BIT BIT0

// ---------------------------------------------------------------------------
// DSP configuration — all toggleable at runtime
// ---------------------------------------------------------------------------

typedef struct {
    volatile int hpf_on;        // HPF 80 Hz
    volatile int notch_on;      // Notch 50/150/250 Hz
    volatile int wiener_on;     // Wiener noise suppression
    volatile int gate_on;       // Noise gate with hysteresis
    volatile int aec_on;        // Acoustic echo cancellation
    volatile int limiter_on;    // Soft limiter on call audio
    volatile int sns_on;        // Spectral noise suppression
    volatile int mic_gain;      // Mic SW gain (1-8)
    volatile int call_gain;     // Call audio SW gain (1-8)
    volatile int wifi_on;       // WiFi + UDP streaming
} dsp_cfg_t;

static dsp_cfg_t s_dsp = {
    .hpf_on     = 0,    // Step 1
    .notch_on   = 0,    // Step 3
    .wiener_on  = 0,    // Step 4
    .gate_on    = 0,    // Step 5
    .aec_on     = 0,    // Step 6
    .limiter_on = 0,    // call audio limiter
    .sns_on     = 1,    // Step 7 — spectral NS active (mode 4 set in init)
    .mic_gain   = 3,    // Step 2: ×3 (hw_gain=24dB × 3 = peak ~15000, no clipping)
    .call_gain  = 2,
    .wifi_on    = 1,
};

// Noise gate parameters
#define NOISE_GATE_OPEN   450   // RMS threshold to open
#define NOISE_GATE_CLOSE  380   // RMS threshold to start hold-down
#define NOISE_GATE_HOLD   4     // frames (~60ms) below CLOSE to actually close

// Soft limiter parameters
#define SW_LIMIT_THRESHOLD  16384   // ~0.5 FS: linear zone
#define SW_LIMIT_MAX        32700   // ceiling

// ---------------------------------------------------------------------------
// Global handles
// ---------------------------------------------------------------------------

static esp_codec_dev_handle_t  s_codec_dev   = NULL;
static i2c_master_dev_handle_t s_es8388_i2c  = NULL;
static RingbufHandle_t         s_rb_call_rx  = NULL;  // BT → I2S TX (headphones)
static RingbufHandle_t         s_rb_mic_tx   = NULL;  // I2S RX → BT (phone)
static RingbufHandle_t         s_rb_udp_call = NULL;  // call audio → UDP 5004
static RingbufHandle_t         s_rb_udp_mic  = NULL;  // mic audio → UDP 5005

static volatile bool s_sco_active = false;

// WiFi / UDP
static EventGroupHandle_t s_wifi_event_group = NULL;
static int                s_udp_call_sock    = -1;
static int                s_udp_mic_sock     = -1;
static struct sockaddr_in s_udp_dest_call;
static struct sockaddr_in s_udp_dest_mic;
static volatile bool      s_wifi_ready       = false;

// UDP command listener (raw lwIP — receives infrequently, no coex issue)
static struct udp_pcb    *s_udp_cmd_pcb      = NULL;

// ---------------------------------------------------------------------------
// ES8388 raw I2C access
// ---------------------------------------------------------------------------

static esp_err_t es8388_write_reg_raw(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = {reg, val};
    return i2c_master_transmit(s_es8388_i2c, buf, 2, 100);
}

static esp_err_t es8388_read_reg_raw(uint8_t reg, uint8_t *val)
{
    return i2c_master_transmit_receive(s_es8388_i2c, &reg, 1, val, 1, 100);
}

// ---------------------------------------------------------------------------
// HFP Legacy PCM callbacks
// ---------------------------------------------------------------------------

static void incoming_pcm_cb(const uint8_t *buf, uint32_t sz)
{
    if (s_rb_call_rx) {
        xRingbufferSend(s_rb_call_rx, buf, sz, 0);
    }
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
        if (got < sz) memset(buf + got, 0, sz - got);
        return sz;
    }
    memset(buf, 0, sz);
    return sz;
}

// ---------------------------------------------------------------------------
// i2s_tx_task: BT call audio → ES8388 DAC (headphones)
//   + soft limiter (toggleable)
//   + AEC reference feed
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
        if (!data || received == 0) continue;

        size_t n = received / sizeof(int16_t);
        memcpy(mono_buf, data, received);
        vRingbufferReturnItem(s_rb_call_rx, data);

        // Apply call gain + optional soft limiter
        int gain = s_dsp.call_gain;
        int use_limiter = s_dsp.limiter_on;

        for (size_t i = 0; i < n; i++) {
            int32_t s = (int32_t)mono_buf[i] * gain;

            if (use_limiter) {
                int32_t sign = (s < 0) ? -1 : 1;
                int32_t a = s * sign;
                if (a > SW_LIMIT_THRESHOLD) {
                    int32_t excess   = a - SW_LIMIT_THRESHOLD;
                    int32_t headroom = SW_LIMIT_MAX - SW_LIMIT_THRESHOLD;
                    a = SW_LIMIT_THRESHOLD + (excess * headroom) / (headroom + excess);
                }
                s = sign * a;
            } else {
                if (s > 32767)  s = 32767;
                if (s < -32768) s = -32768;
            }

            mono_buf[i] = (int16_t)s;
            stereo_buf[i * 2]     = (int16_t)s;
            stereo_buf[i * 2 + 1] = (int16_t)s;

            int16_t abs_v = s < 0 ? (int16_t)(-s) : (int16_t)s;
            if (abs_v > peak) peak = abs_v;
        }

        // Feed AEC reference (what goes to headphones = echo source)
        if (s_dsp.aec_on) {
            aec_write_reference(mono_buf, n);
        }

        // Send to DAC
        esp_codec_dev_write(s_codec_dev, stereo_buf, (int)(n * 2 * sizeof(int16_t)));

        // Send to UDP call ringbuf
        if (s_dsp.wifi_on && s_rb_udp_call) {
            xRingbufferSend(s_rb_udp_call, mono_buf, received, pdMS_TO_TICKS(50));
        }

        if (++frame_cnt % 50 == 0) {
            ESP_LOGI(TAG_AUDIO, "TX  frame=%"PRIu32" peak=%d gain=%d lim=%d",
                     frame_cnt, peak, gain, use_limiter);
            peak = 0;
        }
    }
}

// ---------------------------------------------------------------------------
// mic_rx_task: ES8388 ADC → DSP pipeline → BT + UDP
// ---------------------------------------------------------------------------

static void mic_rx_task(void *arg)
{
    static int16_t stereo_buf[SAMPLES_PER_FRAME * 2];
    static int16_t mono_buf[SAMPLES_PER_FRAME];
    static int16_t sns_out[SAMPLES_PER_FRAME];
    static uint32_t frame_cnt = 0;
    static int16_t  peak      = 0;
    int sns_cal_frames = 0;        // noise calibration counter
    const int SNS_CAL_TARGET = 66; // ~1 sec of noise (66 × 15ms)

    // HPF state (persistent across frames)
    int32_t hp_x_prev = 0, hp_y_prev = 0;

    // Notch filter state: 3 biquads (50, 150, 250 Hz)
    int32_t xp1[3] = {0}, xp2[3] = {0};
    int32_t yp1[3] = {0}, yp2[3] = {0};

    // Notch coefficients for 16kHz sample rate, Q14
    // H(z) = (1 + b1*z^-1 + z^-2) / (1 + a1*z^-1 + a2*z^-2), r=0.95
    //  50 Hz: cos(2π×50/16000)  = 0.99981 → b1=-32762, a1=-31124
    // 150 Hz: cos(2π×150/16000) = 0.99827 → b1=-32711, a1=-31076
    // 250 Hz: cos(2π×250/16000) = 0.99518 → b1=-32610, a1=-30979
    static const int32_t notch_b1[3] = {-32762, -32711, -32610};
    static const int32_t notch_a1[3] = {-31124, -31076, -30979};
    static const int32_t notch_a2    =  14786;  // r² × 16384

    // Noise gate state
    int  gate_open    = 0;
    int  close_count  = 0;
    int32_t noise_sq  = 0;
    int  noise_valid  = 0;

    // HPF alpha for 80 Hz @ 16 kHz, Q15
    // α = RC/(RC+dt), RC=1/(2π×80)=0.001989, dt=1/16000=0.0000625
    // α = 0.96953, Q15 = 31770
    static const int32_t HPF_ALPHA_Q15 = 31770;

    ESP_LOGI(TAG, "mic_rx_task started (core %d)", xPortGetCoreID());

    for (;;) {
        int rc = esp_codec_dev_read(s_codec_dev, stereo_buf, (int)FRAME_BYTES_STEREO);
        if (rc != ESP_CODEC_DEV_OK) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        size_t n = SAMPLES_PER_FRAME;

        // === L+R averaging → mono ===
        for (size_t i = 0; i < n; i++) {
            int32_t avg = ((int32_t)stereo_buf[i * 2] +
                           (int32_t)stereo_buf[i * 2 + 1]) >> 1;
            mono_buf[i] = (int16_t)avg;
        }

        // === Step 1: HPF 80 Hz ===
        if (s_dsp.hpf_on) {
            for (size_t i = 0; i < n; i++) {
                int32_t x = mono_buf[i];
                int32_t y = x - hp_x_prev + ((HPF_ALPHA_Q15 * hp_y_prev) >> 15);
                if (y >  32767) y =  32767;
                if (y < -32768) y = -32768;
                hp_x_prev = x;
                hp_y_prev = y;
                mono_buf[i] = (int16_t)y;
            }
        }

        // === Step 2: SW Gain ===
        {
            int mg = s_dsp.mic_gain;
            if (mg > 1) {
                for (size_t i = 0; i < n; i++) {
                    int32_t v = (int32_t)mono_buf[i] * mg;
                    if (v >  32767) v =  32767;
                    if (v < -32768) v = -32768;
                    mono_buf[i] = (int16_t)v;
                }
            }
        }

        // === Step 3: Notch 50/150/250 Hz ===
        if (s_dsp.notch_on) {
            for (size_t i = 0; i < n; i++) {
                int32_t x = mono_buf[i];
                for (int k = 0; k < 3; k++) {
                    int32_t y = x
                        + ((notch_b1[k] * xp1[k]) >> 14)
                        + xp2[k]
                        - ((notch_a1[k] * yp1[k]) >> 14)
                        - ((notch_a2    * yp2[k]) >> 14);
                    if (y >  32767) y =  32767;
                    if (y < -32768) y = -32768;
                    xp2[k] = xp1[k]; xp1[k] = x;
                    yp2[k] = yp1[k]; yp1[k] = y;
                    x = y;
                }
                mono_buf[i] = (int16_t)x;
            }
        }

        // === Step 4+5: Wiener + Noise Gate ===
        {
            // Compute frame energy
            int64_t sum_sq = 0;
            for (size_t i = 0; i < n; i++) {
                int32_t v = mono_buf[i];
                sum_sq += v * v;
            }
            int32_t mean_sq = (int32_t)(sum_sq / (int64_t)n);

            if (s_dsp.gate_on) {
                if (!gate_open) {
                    // Update noise floor estimate
                    if (!noise_valid) {
                        noise_sq    = mean_sq;
                        noise_valid = 1;
                    } else {
                        noise_sq = (int32_t)(((int64_t)noise_sq * 30 +
                                              (int64_t)mean_sq * 2) >> 5);
                    }
                    if (mean_sq >= (int32_t)NOISE_GATE_OPEN * NOISE_GATE_OPEN) {
                        gate_open   = 1;
                        close_count = 0;
                    }
                } else {
                    if (mean_sq < (int32_t)NOISE_GATE_CLOSE * NOISE_GATE_CLOSE) {
                        if (++close_count >= NOISE_GATE_HOLD) {
                            gate_open   = 0;
                            close_count = 0;
                        }
                    } else {
                        close_count = 0;
                    }
                }

                if (!gate_open) {
                    memset(mono_buf, 0, n * sizeof(int16_t));
                    goto after_dsp;
                }
            } else {
                // Even with gate off, track noise floor for Wiener
                if (!noise_valid) {
                    noise_sq    = mean_sq;
                    noise_valid = 1;
                }
            }

            // Wiener gain: g = max(0, 1 - noise/signal), Q14
            // NOTE: mic_gain already applied earlier in the pipeline
            if (s_dsp.wiener_on && noise_valid && mean_sq > noise_sq && mean_sq > 0) {
                int32_t ratio_Q14 = (int32_t)(((int64_t)noise_sq << 14) / mean_sq);
                int32_t g_Q14 = 16384 - ratio_Q14;
                if (g_Q14 < 0)     g_Q14 = 0;
                if (g_Q14 > 16384) g_Q14 = 16384;

                for (size_t i = 0; i < n; i++) {
                    int32_t v = ((int32_t)mono_buf[i] * g_Q14) >> 14;
                    if (v >  32767) v =  32767;
                    if (v < -32768) v = -32768;
                    mono_buf[i] = (int16_t)v;
                }
            }
        }

        // === Step 6: AEC ===
        if (s_dsp.aec_on) {
            aec_process(mono_buf, n);
        }

after_dsp:
        // === Step 7: Spectral NS (calibration + processing) ===
        if (s_dsp.sns_on) {
            if (!spectral_ns_is_calibrated()) {
                // First ~1s: feed noise for calibration
                spectral_ns_feed_noise(mono_buf, n);
                sns_cal_frames++;
                if (sns_cal_frames >= SNS_CAL_TARGET) {
                    spectral_ns_finalize_noise(1.0f);
                    ESP_LOGI(TAG_DSP, "Spectral NS calibrated (%d frames)", sns_cal_frames);
                }
            } else {
                int produced = spectral_ns_process(mono_buf, sns_out, n);
                if (produced > 0) {
                    memcpy(mono_buf, sns_out, produced * sizeof(int16_t));
                    // Zero remaining if produced < n (startup latency)
                    for (int i = produced; i < (int)n; i++) mono_buf[i] = 0;
                }
            }
        }

        // Yield to let UDP task send (SNS FFT is CPU-heavy)
        taskYIELD();

        // Peak measurement
        for (size_t i = 0; i < n; i++) {
            int16_t abs_v = mono_buf[i] < 0 ? -mono_buf[i] : mono_buf[i];
            if (abs_v > peak) peak = abs_v;
        }

        // Send to BT
        if (s_rb_mic_tx) {
            xRingbufferSend(s_rb_mic_tx, mono_buf, FRAME_BYTES_MONO, 0);
        }

        // Send to UDP mic ringbuf
        if (s_dsp.wifi_on && s_rb_udp_mic) {
            xRingbufferSend(s_rb_udp_mic, mono_buf, FRAME_BYTES_MONO, pdMS_TO_TICKS(50));
        }

        if (++frame_cnt % 50 == 0) {
            ESP_LOGI(TAG_AUDIO, "MIC f=%"PRIu32" pk=%d [1:hpf=%d 2:mg=%d 3:notch=%d 4:w=%d 5:gate=%d 6:aec=%d 7:sns=%d]",
                     frame_cnt, peak,
                     s_dsp.hpf_on, s_dsp.mic_gain, s_dsp.notch_on,
                     s_dsp.wiener_on, s_dsp.gate_on, s_dsp.aec_on, s_dsp.sns_on);
            peak = 0;
        }
    }
}

// ---------------------------------------------------------------------------
// WiFi
// ---------------------------------------------------------------------------

static int create_udp_socket(uint16_t remote_port)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) return -1;
    // Non-blocking — never stall on coex contention
    int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);
    return sock;
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        s_wifi_ready = false;
        if (s_udp_call_sock >= 0) { close(s_udp_call_sock); s_udp_call_sock = -1; }
        if (s_udp_mic_sock >= 0)  { close(s_udp_mic_sock);  s_udp_mic_sock  = -1; }
        esp_wifi_connect();
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG_WIFI, "disconnected, retrying");
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG_WIFI, "got ip:" IPSTR, IP2STR(&event->ip_info.ip));

        // Setup destination addresses
        memset(&s_udp_dest_call, 0, sizeof(s_udp_dest_call));
        s_udp_dest_call.sin_family = AF_INET;
        s_udp_dest_call.sin_port = htons(UDP_CALL_PORT);
        s_udp_dest_call.sin_addr.s_addr = htonl(
            ((uint32_t)UDP_REMOTE_IP_A << 24) | ((uint32_t)UDP_REMOTE_IP_B << 16) |
            ((uint32_t)UDP_REMOTE_IP_C << 8)  | (uint32_t)UDP_REMOTE_IP_D);

        memcpy(&s_udp_dest_mic, &s_udp_dest_call, sizeof(s_udp_dest_mic));
        s_udp_dest_mic.sin_port = htons(UDP_MIC_PORT);

        // Create non-blocking BSD sockets
        if (s_udp_call_sock < 0) {
            s_udp_call_sock = create_udp_socket(UDP_CALL_PORT);
            ESP_LOGI(TAG_WIFI, "UDP call socket fd=%d → port %d", s_udp_call_sock, UDP_CALL_PORT);
        }
        if (s_udp_mic_sock < 0) {
            s_udp_mic_sock = create_udp_socket(UDP_MIC_PORT);
            ESP_LOGI(TAG_WIFI, "UDP mic socket fd=%d → port %d", s_udp_mic_sock, UDP_MIC_PORT);
        }

        // Reduce WiFi TX power (less coex contention, less power draw)
        esp_wifi_set_max_tx_power(52);  // 13 dBm, enough for same room

        s_wifi_ready = true;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t wifi_init_sta(void)
{
    // NOTE: esp_netif_init + esp_event_loop already called from app_main
    //       before bt_init (required for WiFi+BT coex on ESP32)
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

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
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_connect());

    ESP_LOGI(TAG_WIFI, "WiFi STA init done, connecting to '%s'", WIFI_SSID);
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// UDP TX tasks
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Single merged UDP TX task — BSD sockets, ADPCM, packet duplication
// No LOCK_TCPIP_CORE — uses non-blocking sendto()
// ---------------------------------------------------------------------------

#define ADPCM_FRAME_BYTES  (SAMPLES_PER_FRAME / 2)  // 240 → 120 bytes

// PCM collect buffers in PSRAM
EXT_RAM_BSS_ATTR static int16_t s_pcm_call[SAMPLES_PER_FRAME * UDP_BATCH_FRAMES];
EXT_RAM_BSS_ATTR static int16_t s_pcm_mic[SAMPLES_PER_FRAME * UDP_BATCH_FRAMES];

static int udp_encode_and_send(int sock, struct sockaddr_in *dest,
                                int16_t *pcm, int n_samples, uint32_t *seq)
{
    if (sock < 0 || !s_wifi_ready) return -1;

    int n_frames = n_samples / SAMPLES_PER_FRAME;
    if (n_frames == 0) n_frames = 1;

    // Reset ADPCM state per packet — decoder does the same
    adpcm_state_t enc;
    adpcm_init(&enc);

    uint8_t pkt[8 + ADPCM_FRAME_BYTES * UDP_BATCH_FRAMES];
    size_t adpcm_bytes = adpcm_encode(&enc, pcm, pkt + 8, n_samples);

    uint32_t s = *seq;
    *seq += n_frames;
    pkt[0] = (uint8_t)(s);
    pkt[1] = (uint8_t)(s >> 8);
    pkt[2] = (uint8_t)(s >> 16);
    pkt[3] = (uint8_t)(s >> 24);
    pkt[4] = 0; pkt[5] = 0; pkt[6] = 0;
    pkt[7] = (uint8_t)n_frames;

    size_t total = 8 + adpcm_bytes;

    // Send (non-blocking — returns immediately if WiFi busy)
    // Send original + duplicates (total = 1 + UDP_DUPLICATE times)
    for (int dup = 0; dup <= UDP_DUPLICATE; dup++) {
        sendto(sock, pkt, total, 0, (struct sockaddr *)dest, sizeof(*dest));
    }

    return (int)total;
}

// Per-stream UDP TX task — blocks waiting for data, batches frames, sends ADPCM
static void udp_stream_task(int *sock, struct sockaddr_in *dest,
                             RingbufHandle_t rb, int16_t *pcm_buf,
                             const char *label)
{
    uint32_t seq = 0;
    uint32_t send_count = 0;

    ESP_LOGI(TAG, "udp_%s_tx started (core %d, batch=%d, ADPCM)",
             label, xPortGetCoreID(), UDP_BATCH_FRAMES);

    for (;;) {
        if (!s_wifi_ready) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        int collected = 0;
        int target = SAMPLES_PER_FRAME * UDP_BATCH_FRAMES;

        for (int i = 0; i < UDP_BATCH_FRAMES; i++) {
            // First frame: block until data arrives. Next: short wait to fill batch.
            TickType_t wait = (i == 0) ? portMAX_DELAY : pdMS_TO_TICKS(10);
            size_t item_size = 0;
            uint8_t *item = (uint8_t *)xRingbufferReceiveUpTo(
                rb, &item_size, wait, FRAME_BYTES_MONO);

            if (item && item_size > 0) {
                size_t got = item_size / sizeof(int16_t);
                if (collected + (int)got <= target) {
                    memcpy(&pcm_buf[collected], item, item_size);
                    collected += (int)got;
                }
                vRingbufferReturnItem(rb, item);
            } else {
                break;
            }
        }

        if (collected > 0) {
            udp_encode_and_send(*sock, dest, pcm_buf, collected, &seq);
        }

        if (++send_count % 200 == 0) {
            ESP_LOGI(TAG, "UDP %s: seq=%"PRIu32, label, seq);
        }
    }
}

static void udp_call_tx_task(void *arg)
{
    udp_stream_task(&s_udp_call_sock, &s_udp_dest_call,
                     s_rb_udp_call, s_pcm_call, "call");
}

static void udp_mic_tx_task(void *arg)
{
    udp_stream_task(&s_udp_mic_sock, &s_udp_dest_mic,
                     s_rb_udp_mic, s_pcm_mic, "mic");
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
            ESP_LOGI(TAG_BT, "*** SLC CONNECTED ***");
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
            esp_hf_client_outgoing_data_ready();
            // WiFi priority — BT SCO has built-in retransmission (eSCO + mSBC PLC)
            esp_coex_preference_set(ESP_COEX_PREFER_WIFI);
            ESP_LOGI(TAG_BT, "*** SCO OPEN — coex=WIFI priority ***");
        } else if (st == ESP_HF_CLIENT_AUDIO_STATE_DISCONNECTED) {
            s_sco_active = false;
            esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
            ESP_LOGI(TAG_BT, "*** SCO CLOSED — coex=balance ***");
        }
        break;
    }

    case ESP_HF_CLIENT_RING_IND_EVT:
        ESP_LOGI(TAG_BT, "RING — auto-answer");
        esp_hf_client_answer_call();
        break;

    case ESP_HF_CLIENT_CLIP_EVT:
        ESP_LOGI(TAG_BT, "CLIP: %s",
                 param->clip.number ? param->clip.number : "(none)");
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
            ESP_LOGI(TAG_BT, "HFP profile initialized");
        }
        break;

    default:
        ESP_LOGD(TAG_BT, "HFP event %d", event);
        break;
    }
}

// ---------------------------------------------------------------------------
// GAP callback
// ---------------------------------------------------------------------------

static void gap_cb(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param)
{
    switch (event) {
    case ESP_BT_GAP_AUTH_CMPL_EVT:
        if (param->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS)
            ESP_LOGI(TAG_BT, "Paired: '%s'", param->auth_cmpl.device_name);
        else
            ESP_LOGW(TAG_BT, "Pairing failed: %d", param->auth_cmpl.stat);
        break;

    case ESP_BT_GAP_CFM_REQ_EVT:
        ESP_LOGI(TAG_BT, "SSP confirm %06"PRIu32" — auto-accept",
                 param->cfm_req.num_val);
        esp_bt_gap_ssp_confirm_reply(param->cfm_req.bda, true);
        break;

    case ESP_BT_GAP_KEY_NOTIF_EVT:
        ESP_LOGI(TAG_BT, "SSP passkey %06"PRIu32, param->key_notif.passkey);
        break;

    case ESP_BT_GAP_PIN_REQ_EVT: {
        esp_bt_pin_code_t pin = {'0', '0', '0', '0'};
        esp_bt_gap_pin_reply(param->pin_req.bda, true, 4, pin);
        break;
    }

    default:
        ESP_LOGD(TAG_BT, "GAP event %d", event);
        break;
    }
}

// ---------------------------------------------------------------------------
// Audio init (I2C + I2S + ES8388)
// ---------------------------------------------------------------------------

static esp_err_t audio_init(void)
{
    esp_err_t ret;

    // I2C master bus
    i2c_master_bus_config_t i2c_bus_cfg = {
        .clk_source                   = I2C_CLK_SRC_DEFAULT,
        .i2c_port                     = I2C_PORT,
        .sda_io_num                   = I2C_SDA_PIN,
        .scl_io_num                   = I2C_SCL_PIN,
        .glitch_ignore_cnt            = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t i2c_bus;
    ret = i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C bus init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    // Raw I2C access to ES8388
    i2c_device_config_t es_dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = 0x10,
        .scl_speed_hz    = 100000,
    };
    ret = i2c_master_bus_add_device(i2c_bus, &es_dev_cfg, &s_es8388_i2c);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C add ES8388 failed: %s", esp_err_to_name(ret));
        return ret;
    }

    // I2S full-duplex
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num  = 8;
    chan_cfg.dma_frame_num = SAMPLES_PER_FRAME;

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
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx_handle, &i2s_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_handle, &i2s_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(tx_handle));
    ESP_ERROR_CHECK(i2s_channel_enable(rx_handle));
    ESP_LOGI(TAG, "I2S OK — %dHz stereo 16-bit", SAMPLE_RATE);

    // ES8388 via esp_codec_dev
    audio_codec_i2c_cfg_t i2c_codec_cfg = {
        .port       = I2C_PORT,
        .addr       = ES8388_CODEC_DEFAULT_ADDR,
        .bus_handle = i2c_bus,
    };
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&i2c_codec_cfg);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();

    es8388_codec_cfg_t es8388_cfg = {
        .ctrl_if     = ctrl_if,
        .gpio_if     = gpio_if,
        .codec_mode  = ESP_CODEC_DEV_WORK_MODE_BOTH,
        .master_mode = false,
        .pa_pin      = PA_PIN,
        .pa_reverted = false,
        .hw_gain     = { .pa_voltage = 5.0f, .codec_dac_voltage = 3.3f },
    };
    const audio_codec_if_t *codec_if = es8388_codec_new(&es8388_cfg);

    audio_codec_i2s_cfg_t i2s_codec_cfg = {
        .port      = I2S_PORT,
        .rx_handle = rx_handle,
        .tx_handle = tx_handle,
    };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_codec_cfg);

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

    esp_codec_dev_set_out_vol(s_codec_dev, 100);
    esp_codec_dev_set_in_gain(s_codec_dev, 24.0f);  // 24dB — stronger signal for spectral NS

    ESP_LOGI(TAG, "ES8388 OK — vol=100 gain=24dB PA=GPIO%d", PA_PIN);
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Bluetooth init
// ---------------------------------------------------------------------------

static esp_err_t bt_init(void)
{
    // NOTE: do NOT call esp_bt_controller_mem_release(BLE) — it conflicts
    // with WiFi coexistence on ESP32 (shared radio needs BTDM controller)

    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT));

    esp_bluedroid_config_t bd_cfg = BT_BLUEDROID_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bluedroid_init_with_cfg(&bd_cfg));
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    ESP_ERROR_CHECK(esp_bt_gap_set_device_name(BT_DEVICE_NAME));
    ESP_ERROR_CHECK(esp_bt_gap_register_callback(gap_cb));

    esp_bt_sp_param_t param_type = ESP_BT_SP_IOCAP_MODE;
    esp_bt_io_cap_t   iocap      = ESP_BT_IO_CAP_IO;
    esp_bt_gap_set_security_param(param_type, &iocap, sizeof(iocap));

    esp_bt_pin_type_t pin_type = ESP_BT_PIN_TYPE_FIXED;
    esp_bt_pin_code_t pin_code = {'0', '0', '0', '0'};
    esp_bt_gap_set_pin(pin_type, 4, pin_code);

    ESP_ERROR_CHECK(esp_hf_client_register_callback(hf_client_cb));
    ESP_ERROR_CHECK(esp_hf_client_init());
    esp_hf_client_register_data_callback(incoming_pcm_cb, outgoing_pcm_cb);

    esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);

    const uint8_t *mac = esp_bt_dev_get_address();
    ESP_LOGI(TAG_BT, "BT ready: '%s' MAC=%02x:%02x:%02x:%02x:%02x:%02x",
             BT_DEVICE_NAME, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// UART console — runtime DSP control from idf.py monitor
// ---------------------------------------------------------------------------

#define UART_NUM       UART_NUM_0
#define UART_BUF_SIZE  256
#define CMD_LINE_MAX   128

static void print_status(void)
{
    ESP_LOGI(TAG_CMD, "=== DSP Pipeline Status ===");
    ESP_LOGI(TAG_CMD, "  hpf     = %d", s_dsp.hpf_on);
    ESP_LOGI(TAG_CMD, "  notch   = %d", s_dsp.notch_on);
    ESP_LOGI(TAG_CMD, "  wiener  = %d", s_dsp.wiener_on);
    ESP_LOGI(TAG_CMD, "  gate    = %d", s_dsp.gate_on);
    ESP_LOGI(TAG_CMD, "  aec     = %d", s_dsp.aec_on);
    ESP_LOGI(TAG_CMD, "  limiter = %d", s_dsp.limiter_on);
    ESP_LOGI(TAG_CMD, "  mic_gain  = %d", s_dsp.mic_gain);
    ESP_LOGI(TAG_CMD, "  call_gain = %d", s_dsp.call_gain);
    ESP_LOGI(TAG_CMD, "  wifi    = %d", s_dsp.wifi_on);
    ESP_LOGI(TAG_CMD, "  SCO     = %s", s_sco_active ? "active" : "idle");
    ESP_LOGI(TAG_CMD, "  heap    = %"PRIu32, esp_get_free_heap_size());
}

static void print_help(void)
{
    ESP_LOGI(TAG_CMD, "=== MIC Pipeline (toggle, or 0/1) ===");
    ESP_LOGI(TAG_CMD, "  Step 0: hw N   — ES8388 analog gain (0-40 dB)");
    ESP_LOGI(TAG_CMD, "  Step 1: hpf    — HPF 80Hz");
    ESP_LOGI(TAG_CMD, "  Step 2: mg N   — SW gain (1-8)");
    ESP_LOGI(TAG_CMD, "  Step 3: notch  — Notch 50/150/250Hz");
    ESP_LOGI(TAG_CMD, "  Step 4: w      — Wiener NS");
    ESP_LOGI(TAG_CMD, "  Step 5: g      — Noise gate");
    ESP_LOGI(TAG_CMD, "  Step 6: aec    — Echo cancel");
    ESP_LOGI(TAG_CMD, "  Step 7: sns    — Spectral NS (FFT)");
    ESP_LOGI(TAG_CMD, "=== Other ===");
    ESP_LOGI(TAG_CMD, "  lim    — Soft limiter (call)");
    ESP_LOGI(TAG_CMD, "  cg N   — Call gain (1-8)");
    ESP_LOGI(TAG_CMD, "  vol N  — ES8388 output vol (0-100)");
    ESP_LOGI(TAG_CMD, "  wifi   — UDP stream on/off");
    ESP_LOGI(TAG_CMD, "  all    — ALL DSP on/off");
    ESP_LOGI(TAG_CMD, "  s      — Status");
    ESP_LOGI(TAG_CMD, "  h|?    — This help");
}

// Toggle helper: no value = flip, 0 = off, 1 = on
static int toggle(volatile int *flag, int val)
{
    if (val < 0)
        *flag = !(*flag);    // no arg → toggle
    else
        *flag = val ? 1 : 0;
    return *flag;
}

static void process_cmd(char *line)
{
    // Trim trailing whitespace/newline
    size_t len = strlen(line);
    while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r' || line[len-1] == ' '))
        line[--len] = '\0';

    if (len == 0) return;

    char cmd[32] = {0};
    int  val = -1;
    sscanf(line, "%31s %d", cmd, &val);

    if (strcmp(cmd, "help") == 0 || strcmp(cmd, "h") == 0 || strcmp(cmd, "?") == 0) {
        print_help();
    } else if (strcmp(cmd, "status") == 0 || strcmp(cmd, "s") == 0) {
        print_status();
    } else if (strcmp(cmd, "hpf") == 0) {
        ESP_LOGI(TAG_CMD, "hpf = %d", toggle(&s_dsp.hpf_on, val));
    } else if (strcmp(cmd, "notch") == 0) {
        ESP_LOGI(TAG_CMD, "notch = %d", toggle(&s_dsp.notch_on, val));
    } else if (strcmp(cmd, "wiener") == 0 || strcmp(cmd, "w") == 0) {
        ESP_LOGI(TAG_CMD, "wiener = %d", toggle(&s_dsp.wiener_on, val));
    } else if (strcmp(cmd, "gate") == 0 || strcmp(cmd, "g") == 0) {
        ESP_LOGI(TAG_CMD, "gate = %d", toggle(&s_dsp.gate_on, val));
    } else if (strcmp(cmd, "aec") == 0) {
        ESP_LOGI(TAG_CMD, "aec = %d", toggle(&s_dsp.aec_on, val));
    } else if (strcmp(cmd, "sns") == 0) {
        // sns 0=off, 1=FFT-IFFT, 2=mask-log, 3=soft-mask, 4=hard-mask
        // sns without arg = cycle 0→1→2→3→4→0
        if (val >= 0 && val <= 4) {
            spectral_ns_set_mode(val);
            s_dsp.sns_on = (val > 0) ? 1 : 0;
        } else {
            int next = (spectral_ns_get_mode() + 1) % 5;
            spectral_ns_set_mode(next);
            s_dsp.sns_on = (next > 0) ? 1 : 0;
        }
        static const char *mn[] = {"OFF","FFT-IFFT","mask-LOG","soft-MASK","hard-MASK"};
        int m = spectral_ns_get_mode();
        ESP_LOGI(TAG_CMD, "sns=%d (%s) cal=%d", m,
                 (m >= 0 && m <= 4) ? mn[m] : "?", spectral_ns_is_calibrated());
    } else if (strcmp(cmd, "snsg") == 0) {
        // snsg N — set floor gain in percent (1-100, default 2 = 0.02)
        if (val >= 1 && val <= 100) {
            spectral_ns_set_floor_gain((float)val / 100.0f);
        }
        ESP_LOGI(TAG_CMD, "sns floor gain — use 'snsg N' where N=1..100 (percent)");
    } else if (strcmp(cmd, "limiter") == 0 || strcmp(cmd, "lim") == 0) {
        ESP_LOGI(TAG_CMD, "limiter = %d", toggle(&s_dsp.limiter_on, val));
    } else if (strcmp(cmd, "wifi") == 0) {
        int r = toggle(&s_dsp.wifi_on, val);
        ESP_LOGI(TAG_CMD, "wifi = %d (UDP %s)", r, r ? "ON" : "OFF");
    } else if (strcmp(cmd, "mic_gain") == 0 || strcmp(cmd, "mg") == 0) {
        if (val >= 1 && val <= 8) {
            s_dsp.mic_gain = val;
        }
        ESP_LOGI(TAG_CMD, "mic_gain = %d", s_dsp.mic_gain);
    } else if (strcmp(cmd, "call_gain") == 0 || strcmp(cmd, "cg") == 0) {
        if (val >= 1 && val <= 8) {
            s_dsp.call_gain = val;
        }
        ESP_LOGI(TAG_CMD, "call_gain = %d", s_dsp.call_gain);
    } else if (strcmp(cmd, "all") == 0) {
        // no val → toggle all based on majority: if most are on → turn off, else on
        int v;
        if (val < 0) {
            int sum = s_dsp.hpf_on + s_dsp.notch_on + s_dsp.wiener_on +
                      s_dsp.gate_on + s_dsp.aec_on + s_dsp.limiter_on;
            v = (sum > 3) ? 0 : 1;
        } else {
            v = val ? 1 : 0;
        }
        s_dsp.hpf_on = v; s_dsp.notch_on = v; s_dsp.wiener_on = v;
        s_dsp.gate_on = v; s_dsp.aec_on = v; s_dsp.limiter_on = v;
        ESP_LOGI(TAG_CMD, "ALL DSP = %d", v);
    } else if (strcmp(cmd, "hw") == 0) {
        // hw N — set ES8388 analog input gain (0-40 dB)
        if (val >= 0 && val <= 40 && s_codec_dev) {
            esp_codec_dev_set_in_gain(s_codec_dev, (float)val);
        }
        ESP_LOGI(TAG_CMD, "hw_gain = %d dB", val >= 0 ? val : -1);
    } else if (strcmp(cmd, "vol") == 0) {
        // vol N — set ES8388 output volume (0-100)
        if (val >= 0 && val <= 100 && s_codec_dev) {
            esp_codec_dev_set_out_vol(s_codec_dev, val);
        }
        ESP_LOGI(TAG_CMD, "volume = %d", val >= 0 ? val : -1);
    } else {
        ESP_LOGW(TAG_CMD, "Unknown: '%s' (type 'help')", line);
    }
}

static void console_task(void *arg)
{
    uart_config_t uart_cfg = {
        .baud_rate  = 115200,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM, UART_BUF_SIZE, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_NUM, &uart_cfg));

    char line[CMD_LINE_MAX];
    int  pos = 0;

    ESP_LOGI(TAG_CMD, "UART console ready");

    for (;;) {
        uint8_t ch;
        int len = uart_read_bytes(UART_NUM, &ch, 1, portMAX_DELAY);
        if (len <= 0) continue;

        if (ch == '\n' || ch == '\r') {
            if (pos > 0) {
                line[pos] = '\0';
                process_cmd(line);
                pos = 0;
            }
        } else if (pos < CMD_LINE_MAX - 1) {
            line[pos++] = (char)ch;
        }
    }
}

// ---------------------------------------------------------------------------
// UDP command listener (port 5006) — works without USB serial
// ---------------------------------------------------------------------------

#define CMD_UDP_PORT  5006

static void udp_cmd_recv(void *arg, struct udp_pcb *pcb, struct pbuf *p,
                          const ip_addr_t *addr, u16_t port)
{
    if (!p) return;

    char line[CMD_LINE_MAX];
    size_t len = (p->len < CMD_LINE_MAX - 1) ? p->len : CMD_LINE_MAX - 1;
    memcpy(line, p->payload, len);
    line[len] = '\0';
    pbuf_free(p);

    // Trim newline
    while (len > 0 && (line[len-1] == '\n' || line[len-1] == '\r'))
        line[--len] = '\0';

    if (len > 0) {
        ESP_LOGI(TAG_CMD, "UDP cmd from " IPSTR ": '%s'", IP2STR(&addr->u_addr.ip4), line);
        process_cmd(line);
    }
}

static void udp_cmd_init(void)
{
    struct udp_pcb *pcb = udp_new();
    if (!pcb) {
        ESP_LOGE(TAG_CMD, "udp_cmd: pcb alloc failed");
        return;
    }
    udp_bind(pcb, IP_ADDR_ANY, CMD_UDP_PORT);
    udp_recv(pcb, udp_cmd_recv, NULL);
    ESP_LOGI(TAG_CMD, "UDP command listener on port %d", CMD_UDP_PORT);
}

// ---------------------------------------------------------------------------
// app_main
// ---------------------------------------------------------------------------

void app_main(void)
{
    ESP_LOGI(TAG, "=== LyraT Phone Stream — All Phases ===");

    // NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_LOGI(TAG, "Free heap: %"PRIu32" bytes", esp_get_free_heap_size());

    // Ring buffers — BT path
    s_rb_call_rx = xRingbufferCreate(FRAME_BYTES_MONO * RINGBUF_FRAMES,
                                     RINGBUF_TYPE_BYTEBUF);
    s_rb_mic_tx  = xRingbufferCreate(FRAME_BYTES_MONO * RINGBUF_FRAMES,
                                     RINGBUF_TYPE_BYTEBUF);
    // Ring buffers — UDP path (larger to survive WiFi/BT coex pauses)
    s_rb_udp_call = xRingbufferCreate(FRAME_BYTES_MONO * RINGBUF_UDP_FRAMES,
                                      RINGBUF_TYPE_BYTEBUF);
    s_rb_udp_mic  = xRingbufferCreate(FRAME_BYTES_MONO * RINGBUF_UDP_FRAMES,
                                      RINGBUF_TYPE_BYTEBUF);

    if (!s_rb_call_rx || !s_rb_mic_tx || !s_rb_udp_call || !s_rb_udp_mic) {
        ESP_LOGE(TAG, "Ring buffer alloc failed");
        abort();
    }

    // AEC
    aec_init();
    spectral_ns_init();

    // Event loop + netif
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    // WiFi MUST init before BT on ESP32 — coex arbitrator needs WiFi stack
    // ready before BT controller touches the shared 2.4GHz radio
    ESP_ERROR_CHECK(wifi_init_sta());

    // UDP command listener (port 5006) — control without USB serial
    udp_cmd_init();

    // Audio: I2C + I2S + ES8388
    ESP_ERROR_CHECK(audio_init());

    // Bluetooth — after WiFi so coex is ready
    ESP_ERROR_CHECK(bt_init());

    // Tasks on Core 1 (Core 0 = BT + WiFi stacks)
    xTaskCreatePinnedToCore(i2s_tx_task,       "i2s_tx",    4096, NULL, 15, NULL, 1);
    xTaskCreatePinnedToCore(mic_rx_task,        "mic_rx",    8192, NULL, 14, NULL, 1);
    xTaskCreatePinnedToCore(udp_call_tx_task,   "udp_call",  4096, NULL, 12, NULL, 1);
    xTaskCreatePinnedToCore(udp_mic_tx_task,    "udp_mic",   4096, NULL, 12, NULL, 1);
    xTaskCreatePinnedToCore(console_task,       "console",   4096, NULL,  5, NULL, 1);

    ESP_LOGI(TAG, "Free heap after init: %"PRIu32" bytes", esp_get_free_heap_size());
    ESP_LOGI(TAG, "Pair Android with '%s', type 'help' in monitor for DSP control",
             BT_DEVICE_NAME);

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(10000));
        ESP_LOGI(TAG, "SCO=%s heap=%"PRIu32" wifi=%d",
                 s_sco_active ? "active" : "idle",
                 esp_get_free_heap_size(),
                 s_dsp.wifi_on);
    }
}
