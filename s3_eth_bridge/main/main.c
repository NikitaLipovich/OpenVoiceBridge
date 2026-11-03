// ESP32-S3-ETH, stage 1: passive listener on the I2S bus of the LyraT, plus Ethernet bring-up.
//
// Every pin wired to the LyraT is an input here, so this firmware cannot drive the codec bus.
// The UART pins stay plain inputs until the UART stage. Status goes to the USB console.
//
//   LyraT I2S header         S3
//   SCLK   GPIO5             GPIO15
//   LRCK   GPIO25            GPIO16
//   DSDIN  GPIO26 (to phones) GPIO17
//   ASDOUT GPIO35 (from mic)  GPIO18
//   GND                      GND
//   MCLK and 3.3 V stay unconnected.

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_eth.h"
#include "esp_event.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "s3eth";

#define PIN_I2S_BCK     GPIO_NUM_15
#define PIN_I2S_WS      GPIO_NUM_16
#define PIN_I2S_DSDIN   GPIO_NUM_17
#define PIN_I2S_ASDOUT  GPIO_NUM_18
#define PIN_UART_TX     GPIO_NUM_1     // reserved, input until the UART stage
#define PIN_UART_RX     GPIO_NUM_2     // reserved, input until the UART stage

#define PIN_ETH_MISO    GPIO_NUM_12
#define PIN_ETH_MOSI    GPIO_NUM_11
#define PIN_ETH_SCLK    GPIO_NUM_13
#define PIN_ETH_CS      GPIO_NUM_14
#define PIN_ETH_RST     GPIO_NUM_9
#define PIN_ETH_INT     GPIO_NUM_10

#define SAMPLE_RATE     16000          // same as the LyraT: 16 kHz, 16-bit stereo, Philips
#define DMA_FRAMES      240
#define STATUS_MS       2000

typedef struct {
    const char *name;
    i2s_chan_handle_t rx;
    volatile uint32_t frames;          // stereo frames since the last report
    volatile uint32_t timeouts;
    volatile int peak_l;
    volatile int peak_r;
} listener_t;

static listener_t s_call = {.name = "dsdin"};
static listener_t s_mic = {.name = "asdout"};

static volatile bool s_link_up;
static volatile bool s_eth_ok;
static char s_ip[16] = "-";

static void pins_to_safe_inputs(void)
{
    // No pulls: the lines belong to the LyraT, this board only listens.
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << PIN_I2S_BCK) | (1ULL << PIN_I2S_WS) | (1ULL << PIN_I2S_DSDIN) |
                        (1ULL << PIN_I2S_ASDOUT) | (1ULL << PIN_UART_TX) | (1ULL << PIN_UART_RX),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&cfg));
}

static esp_err_t listener_init(listener_t *l, i2s_port_t port, gpio_num_t din)
{
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(port, I2S_ROLE_SLAVE);
    chan.dma_desc_num = 6;
    chan.dma_frame_num = DMA_FRAMES;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan, NULL, &l->rx), TAG, "%s channel", l->name);

    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_I2S_BCK,
            .ws = PIN_I2S_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = din,
        },
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(l->rx, &std), TAG, "%s std mode", l->name);
    return i2s_channel_enable(l->rx);
}

static void listener_task(void *arg)
{
    listener_t *l = arg;
    static const size_t bytes = DMA_FRAMES * 2 * sizeof(int16_t);
    int16_t *buf = malloc(bytes);
    configASSERT(buf);
    for (;;) {
        size_t got = 0;
        esp_err_t err = i2s_channel_read(l->rx, buf, bytes, &got, pdMS_TO_TICKS(200));
        if (err == ESP_ERR_TIMEOUT && got == 0) {
            l->timeouts++;
            continue;
        }
        int peak_l = l->peak_l, peak_r = l->peak_r;
        size_t samples = got / sizeof(int16_t);
        for (size_t i = 0; i + 1 < samples; i += 2) {
            int a = abs(buf[i]), b = abs(buf[i + 1]);
            if (a > peak_l) peak_l = a;
            if (b > peak_r) peak_r = b;
        }
        l->peak_l = peak_l;
        l->peak_r = peak_r;
        l->frames += samples / 2;
    }
}

static void on_eth_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (id == ETHERNET_EVENT_CONNECTED) {
        s_link_up = true;
        ESP_LOGI(TAG, "ethernet link up");
    } else if (id == ETHERNET_EVENT_DISCONNECTED) {
        s_link_up = false;
        strcpy(s_ip, "-");
        ESP_LOGW(TAG, "ethernet link down");
    }
}

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    const ip_event_got_ip_t *e = data;
    snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&e->ip_info.ip));
    ESP_LOGI(TAG, "ethernet ip %s", s_ip);
}

static esp_err_t ethernet_init(void)
{
    ESP_RETURN_ON_ERROR(gpio_install_isr_service(0), TAG, "isr service");

    spi_bus_config_t bus = {
        .miso_io_num = PIN_ETH_MISO,
        .mosi_io_num = PIN_ETH_MOSI,
        .sclk_io_num = PIN_ETH_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "spi bus");

    spi_device_interface_config_t dev = {
        .mode = 0,
        .clock_speed_hz = 20 * 1000 * 1000,
        .spics_io_num = PIN_ETH_CS,
        .queue_size = 20,
    };
    eth_w5500_config_t w5500 = ETH_W5500_DEFAULT_CONFIG(SPI2_HOST, &dev);
    w5500.int_gpio_num = PIN_ETH_INT;

    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.reset_gpio_num = PIN_ETH_RST;

    esp_eth_mac_t *mac = esp_eth_mac_new_w5500(&w5500, &mac_cfg);
    esp_eth_phy_t *phy = esp_eth_phy_new_w5500(&phy_cfg);
    ESP_RETURN_ON_FALSE(mac && phy, ESP_FAIL, TAG, "w5500 driver objects");

    esp_eth_config_t cfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth = NULL;
    ESP_RETURN_ON_ERROR(esp_eth_driver_install(&cfg, &eth), TAG, "driver install, is the W5500 responding?");

    // The W5500 has no MAC address of its own.
    uint8_t addr[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(addr, ESP_MAC_ETH), TAG, "read mac");
    ESP_RETURN_ON_ERROR(esp_eth_ioctl(eth, ETH_CMD_S_MAC_ADDR, addr), TAG, "set mac");

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "event loop");
    esp_netif_config_t netif_cfg = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *netif = esp_netif_new(&netif_cfg);
    ESP_RETURN_ON_FALSE(netif, ESP_FAIL, TAG, "netif new");
    ESP_RETURN_ON_ERROR(esp_netif_attach(netif, esp_eth_new_netif_glue(eth)), TAG, "netif attach");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, on_eth_event, NULL), TAG, "eth events");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_got_ip, NULL), TAG, "ip events");
    return esp_eth_start(eth);
}

static void report(listener_t *l, uint32_t dt_ms)
{
    uint32_t frames = l->frames;
    l->frames = 0;
    int peak_l = l->peak_l, peak_r = l->peak_r;
    l->peak_l = 0;
    l->peak_r = 0;
    printf(" %s rate=%" PRIu32 " peak=%d/%d", l->name, frames * 1000 / dt_ms, peak_l, peak_r);
}

void app_main(void)
{
    pins_to_safe_inputs();

    esp_err_t a = listener_init(&s_call, I2S_NUM_0, PIN_I2S_DSDIN);
    esp_err_t b = listener_init(&s_mic, I2S_NUM_1, PIN_I2S_ASDOUT);
    if (a == ESP_OK) xTaskCreatePinnedToCore(listener_task, "i2s_call", 4096, &s_call, 10, NULL, 1);
    if (b == ESP_OK) xTaskCreatePinnedToCore(listener_task, "i2s_mic", 4096, &s_mic, 10, NULL, 1);

    esp_err_t e = ethernet_init();
    s_eth_ok = (e == ESP_OK);
    if (!s_eth_ok) ESP_LOGE(TAG, "ethernet init failed: %s", esp_err_to_name(e));

    printf("s3eth stage1 i2s0=%s i2s1=%s eth=%s\n", esp_err_to_name(a), esp_err_to_name(b), esp_err_to_name(e));

    int64_t last = esp_timer_get_time();
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(STATUS_MS));
        int64_t now = esp_timer_get_time();
        uint32_t dt_ms = (uint32_t)((now - last) / 1000);
        last = now;
        printf("stat up=%" PRIu32 " eth=%s link=%d ip=%s", (uint32_t)(now / 1000000), s_eth_ok ? "ok" : "fail",
               s_link_up, s_ip);
        report(&s_call, dt_ms);
        report(&s_mic, dt_ms);
        printf(" timeouts=%" PRIu32 "/%" PRIu32 "\n", s_call.timeouts, s_mic.timeouts);
    }
}
