#include <string.h>
#include <errno.h>
#include <sys/socket.h>
#include <netdb.h>
#include <arpa/inet.h>

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

static const char *TAG = "audio_stream";

// -------------------- Settings (edit) --------------------
#define I2S_BCLK_GPIO   (GPIO_NUM_4)
#define I2S_WS_GPIO     (GPIO_NUM_5)
#define I2S_DIN_GPIO    (GPIO_NUM_6)   // BT1036 I2S_OUT -> ESP32 DIN
#define I2S_DOUT_GPIO   (GPIO_NUM_NC)

#define SAMPLE_RATE_HZ  (16000)        // Match AT+HFPSR
#define BITS_PER_SAMPLE (16)
#define CHANNELS_MONO   (1)

// 20ms frames: 16kHz * 0.02s = 320 samples; *2 bytes = 640 bytes (mono, 16-bit)
#define FRAME_MS        (20)
#define SAMPLES_PER_FRAME ((SAMPLE_RATE_HZ * FRAME_MS) / 1000)
#define BYTES_PER_SAMPLE (BITS_PER_SAMPLE / 8)
#define FRAME_BYTES     (SAMPLES_PER_FRAME * BYTES_PER_SAMPLE * CHANNELS_MONO)

#define RINGBUF_CAPACITY_BYTES (FRAME_BYTES * 50)

#define UDP_REMOTE_IP   "192.168.1.50"
#define UDP_REMOTE_PORT (5004)
// ---------------------------------------------------------

static RingbufHandle_t s_rb = NULL;
static i2s_chan_handle_t s_i2s_rx = NULL;

static esp_err_t i2s_rx_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &s_i2s_rx));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = GPIO_NUM_NC,             // Usually not needed for BT1036 slave
            .bclk = I2S_BCLK_GPIO,
            .ws = I2S_WS_GPIO,
            .dout = I2S_DOUT_GPIO,
            .din = I2S_DIN_GPIO,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_i2s_rx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(s_i2s_rx));
    return ESP_OK;
}

/* Wi-Fi STA connect (edit WIFI_SSID and WIFI_PASS) */
#define WIFI_SSID "your_ssid"
#define WIFI_PASS "your_password"

#define WIFI_CONNECTED_BIT BIT0
static EventGroupHandle_t s_wifi_event_group = NULL;

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        esp_wifi_connect();
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        ESP_LOGI(TAG, "wifi disconnected, retrying");
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "got ip:" IPSTR, IP2STR(&event->ip_info.ip));
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
            .ssid = WIFI_SSID,
            .password = WIFI_PASS,
            .pmf_cfg = {
                .capable = true,
                .required = false,
            },
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_connect());

    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                                          pdFALSE, pdTRUE, pdMS_TO_TICKS(15000));
    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "connected to AP");
    } else {
        ESP_LOGW(TAG, "failed to connect to AP within timeout");
    }
}

static int udp_open_socket(struct sockaddr_in *dst)
{
    memset(dst, 0, sizeof(*dst));
    dst->sin_family = AF_INET;
    dst->sin_port = htons(UDP_REMOTE_PORT);
    inet_pton(AF_INET, UDP_REMOTE_IP, &dst->sin_addr);

    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno=%d", errno);
        return -1;
    }
    return sock;
}

static void i2s_rx_task(void *arg)
{
    uint8_t *buf = (uint8_t *)heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_8BIT);
    if (!buf) {
        ESP_LOGE(TAG, "No memory for I2S buffer");
        vTaskDelete(NULL);
        return;
    }

    while (1) {
        size_t bytes_read = 0;
        esp_err_t err = i2s_channel_read(s_i2s_rx, buf, FRAME_BYTES, &bytes_read, portMAX_DELAY);
        if (err != ESP_OK || bytes_read == 0) {
            ESP_LOGW(TAG, "i2s read err=%s bytes=%u", esp_err_to_name(err), (unsigned)bytes_read);
            continue;
        }

        // Copy frame into ring buffer (use SendAcquire with pointer out param)
        void *item_ptr = NULL;
        BaseType_t sent_ok = xRingbufferSendAcquire(s_rb, &item_ptr, bytes_read, pdMS_TO_TICKS(50));
        if (sent_ok == pdTRUE && item_ptr) {
            memcpy(item_ptr, buf, bytes_read);
            xRingbufferSendComplete(s_rb, item_ptr);
        } else {
            // Drop if network is slower (avoid blocking I2S)
            ESP_LOGW(TAG, "Ring buffer full, dropping audio frame");
        }
    }
}

static void udp_tx_task(void *arg)
{
    struct sockaddr_in dst;
    int sock = udp_open_socket(&dst);
    if (sock < 0) {
        vTaskDelete(NULL);
        return;
    }

    while (1) {
        size_t item_size = 0;
        uint8_t *item = (uint8_t *)xRingbufferReceive(s_rb, &item_size, portMAX_DELAY);
        if (!item) {
            continue;
        }

        int sent = sendto(sock, item, item_size, 0, (struct sockaddr *)&dst, sizeof(dst));
        if (sent < 0) {
            ESP_LOGW(TAG, "sendto failed: errno=%d", errno);
        }

        vRingbufferReturnItem(s_rb, item);
    }
}

void app_main(void)
{
    // 1) Connect Wi-Fi
    wifi_connect_sta();

    s_rb = xRingbufferCreate(RINGBUF_CAPACITY_BYTES, RINGBUF_TYPE_BYTEBUF);
    if (!s_rb) {
        ESP_LOGE(TAG, "Failed to create ring buffer");
        return;
    }

    ESP_ERROR_CHECK(i2s_rx_init());

    xTaskCreatePinnedToCore(i2s_rx_task, "i2s_rx", 4096, NULL, 20, NULL, 0);
    xTaskCreatePinnedToCore(udp_tx_task, "udp_tx", 4096, NULL, 10, NULL, 1);
}
