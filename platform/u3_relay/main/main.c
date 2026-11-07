// U3 of variant D (goal 095): the relay. Wi-Fi to the PC, the control links to U1 (UART1) and U2 (UART2), the console
// and ROM loader port of both (UART0, u3_port.c), new firmware for all three (u3_ota.c), the log of all three to the PC
// (u3_log.c). Sound (I2S listening, RET, recording) and the modes of R21 come in later goals; the pins are in
// plat_pins.h. docs: lyrat_resend/docs/PLATFORM_OTA.md. Based on s3_eth_bridge/main/main.c (control UART part).
//
// R13: nothing here may disturb U1 or U2 when this board fails or restarts. The control TX of a node goes on only after
// a valid frame from it and off after PLAT_TX_RELEASE_MS of silence; EN and IO0 are open drain and stay inputs.
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "ctl_frame.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "plat_pins.h"
#include "plat_proto.h"
#include "u3_link.h"

#if __has_include("plat_secrets.h")
#include "plat_secrets.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif

static const char *TAG = "u3";

#define CTL_RX_RING     4096
#define CTL_TX_RING     4096

// ---------------------------------------------------------------------------------------------------------------------
// Control links

typedef struct {
    const char *name;
    uart_port_t uart;
    int pin_tx, pin_rx;
    ctl_parser_t parser;
    SemaphoreHandle_t tx_lock;
    volatile bool tx_on, hold, ok;
    volatile int baud;
    volatile int64_t last_frame_us;
    bool ever;
    uint16_t seq;
    uint32_t rx_bytes, hb, logs, replies, tx_edges, tx_fail;
    ctl_hb_t last_hb;
} link_t;

static link_t s_links[U3_NODES] = {
    {.name = "u1", .uart = UART_NUM_1, .pin_tx = U3_PIN_C1_TX, .pin_rx = U3_PIN_C1_RX, .baud = CTL_BAUD_DEFAULT},
    {.name = "u2", .uart = UART_NUM_2, .pin_tx = U3_PIN_C2_TX, .pin_rx = U3_PIN_C2_RX, .baud = CTL_BAUD_DEFAULT},
};
static uint16_t s_boot_id;

static bool valid_node(int node) { return node >= 0 && node < U3_NODES; }

const char *u3_node_name(int node) { return valid_node(node) ? s_links[node].name : "?"; }
uint16_t u3_boot_id(void) { return s_boot_id; }

bool u3_node_alive(int node)
{
    if (!valid_node(node)) return false;
    int64_t t = s_links[node].last_frame_us;
    return t && esp_timer_get_time() - t < PLAT_SILENT_MS * 1000LL;
}

bool u3_node_ever_alive(int node) { return valid_node(node) && s_links[node].ever; }
bool u3_node_tx_on(int node) { return valid_node(node) && s_links[node].tx_on; }
int u3_node_get_baud(int node) { return valid_node(node) ? s_links[node].baud : 0; }

int u3_node_set_baud(int node, int baud)
{
    if (!valid_node(node)) return 0;
    link_t *l = &s_links[node];
    if (baud >= 1200 && baud <= 921600 && uart_set_baudrate(l->uart, (uint32_t)baud) == ESP_OK) l->baud = baud;
    return l->baud;
}

static void tx_set(link_t *l, bool on)
{
    if (on == l->tx_on) return;
    xSemaphoreTake(l->tx_lock, portMAX_DELAY);
    if (on) {
        uart_flush(l->uart);
        uart_set_pin(l->uart, l->pin_tx, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
        l->tx_on = true;
    } else {
        uart_wait_tx_done(l->uart, pdMS_TO_TICKS(50));
        gpio_set_direction(l->pin_tx, GPIO_MODE_INPUT);     // back to high impedance (R13)
        l->tx_on = false;
    }
    l->tx_edges++;
    xSemaphoreGive(l->tx_lock);
    ESP_LOGI(TAG, "%s tx %s", l->name, on ? "on" : "off");
}

void u3_node_hold(int node, bool hold)
{
    if (!valid_node(node)) return;
    link_t *l = &s_links[node];
    l->hold = hold;
    if (hold) {
        tx_set(l, false);
        l->last_frame_us = 0;       // whatever comes next is a new start
    }
}

bool u3_node_send(int node, uint8_t type, const void *payload, size_t len)
{
    if (!valid_node(node)) return false;
    link_t *l = &s_links[node];
    uint8_t frame[CTL_MAX_FRAME];
    if (!l->tx_on || l->hold || len > CTL_MAX_PAYLOAD) return false;
    xSemaphoreTake(l->tx_lock, portMAX_DELAY);
    size_t n = ctl_frame_build(frame, type, l->seq++, payload, len);
    // One write per frame: the driver keeps frames of different tasks whole.
    bool ok = l->tx_on && uart_write_bytes(l->uart, frame, n) == (int)n;
    xSemaphoreGive(l->tx_lock);
    if (!ok) l->tx_fail++;
    return ok;
}

static void on_frame(int node, link_t *l, const ctl_parser_t *p)
{
    l->last_frame_us = esp_timer_get_time();
    l->ever = true;
    uint8_t type = ctl_frame_type(p);
    const uint8_t *payload = ctl_frame_payload(p);
    size_t len = ctl_frame_len(p);
    if (type == CTL_T_HB) {
        l->hb++;
        if (len == sizeof(ctl_hb_t)) memcpy(&l->last_hb, payload, sizeof(l->last_hb));
        if (l->tx_on) {
            // The ack carries whether the node sends its log, and at which level.
            uint8_t ctl[2];
            uint8_t frame[CTL_HEADER_BYTES + sizeof(ctl) + CTL_CRC_BYTES];
            size_t n = u3_log_node_ctl(node, ctl);
            xSemaphoreTake(l->tx_lock, portMAX_DELAY);
            if (l->tx_on) uart_write_bytes(l->uart, frame, ctl_frame_build(frame, CTL_T_HB_ACK, ctl_frame_seq(p), ctl, n));
            xSemaphoreGive(l->tx_lock);
        }
    } else if (type == CTL_T_LOG) {
        l->logs++;
        u3_log_relay(node, payload, len);
    } else if (type == CTL_T_OTA_ACK) {
        u3_ota_on_reply(node, payload, len);
    } else if (type == CTL_T_REPLY || type == CTL_T_MODE_ACK) {
        l->replies++;
        u3_log_node_frame(node, type, payload, len);
    }
}

// Whatever comes in, the worst this task does is count it: a node prints garbage on this line at its start.
static void link_task(void *arg)
{
    int node = (int)(intptr_t)arg;
    link_t *l = &s_links[node];
    static uint8_t chunks[U3_NODES][256];
    uint8_t *chunk = chunks[node];
    for (;;) {
        int got = uart_read_bytes(l->uart, chunk, sizeof(chunks[0]), pdMS_TO_TICKS(10));
        if (got > 0 && !l->hold) {
            l->rx_bytes += (uint32_t)got;
            uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
            for (int i = 0; i < got; i++) {
                if (ctl_parser_feed(&l->parser, chunk[i], now)) on_frame(node, l, &l->parser);
            }
        }
        bool alive = l->last_frame_us && esp_timer_get_time() - l->last_frame_us < PLAT_TX_RELEASE_MS * 1000LL;
        tx_set(l, alive && !l->hold);
    }
}

static esp_err_t link_init(link_t *l)
{
    uart_config_t cfg = {
        .baud_rate = CTL_BAUD_DEFAULT,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    l->tx_lock = xSemaphoreCreateMutex();
    if (!l->tx_lock) return ESP_ERR_NO_MEM;
    esp_err_t err = uart_driver_install(l->uart, CTL_RX_RING, CTL_TX_RING, 0, NULL, 0);
    if (err == ESP_OK) err = uart_param_config(l->uart, &cfg);
    // RX only. TX stays away from the pin until the node is proven alive.
    if (err == ESP_OK) err = uart_set_pin(l->uart, UART_PIN_NO_CHANGE, l->pin_rx, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err == ESP_OK) gpio_set_direction(l->pin_tx, GPIO_MODE_INPUT);
    return err;
}

size_t u3_node_json(int node, char *out, size_t cap)
{
    if (!valid_node(node)) return 0;
    link_t *l = &s_links[node];
    const ctl_parser_t *p = &l->parser;
    int64_t age = l->last_frame_us ? (esp_timer_get_time() - l->last_frame_us) / 1000 : -1;
    int n = snprintf(out, cap,
                     "{\"alive\":%d,\"tx_on\":%d,\"hold\":%d,\"baud\":%d,\"rx\":%lu,\"frames\":%lu,\"bad\":%lu,"
                     "\"hb\":%lu,\"logs\":%lu,\"replies\":%lu,\"up_s\":%lu,\"flags\":%u,\"age_ms\":%lld}",
                     u3_node_alive(node), l->tx_on, l->hold, l->baud, (unsigned long)l->rx_bytes,
                     (unsigned long)p->frames, (unsigned long)(p->bad_header + p->bad_crc), (unsigned long)l->hb,
                     (unsigned long)l->logs, (unsigned long)l->replies, (unsigned long)l->last_hb.up_s,
                     l->last_hb.flags, age);
    return n > 0 && (size_t)n < cap ? (size_t)n : 0;
}

// ---------------------------------------------------------------------------------------------------------------------
// Wi-Fi

static volatile bool s_wifi_started, s_got_ip;
static char s_ip[16] = "";

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_got_ip) ESP_LOGW(TAG, "wifi lost, reconnecting");
        s_got_ip = false;
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = (const ip_event_got_ip_t *)data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&e->ip_info.ip));
        s_got_ip = true;
        ESP_LOGI(TAG, "wifi ip %s", s_ip);
    }
}

static bool wifi_start(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    if (!WIFI_SSID[0]) {
        ESP_LOGW(TAG, "no main/plat_secrets.h: no Wi-Fi, updates only over USB");
        return false;
    }
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    wifi_config_t cfg = {0};
    snprintf((char *)cfg.sta.ssid, sizeof(cfg.sta.ssid), "%s", WIFI_SSID);
    snprintf((char *)cfg.sta.password, sizeof(cfg.sta.password), "%s", WIFI_PASS);
    cfg.sta.threshold.authmode = WIFI_PASS[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    s_wifi_started = true;
    return true;
}

void u3_health(u3_health_t *h)
{
    memset(h, 0, sizeof(*h));
    h->wifi_started = s_wifi_started;
    h->got_ip = s_got_ip;
    snprintf(h->ip, sizeof(h->ip), "%s", s_ip);
    wifi_ap_record_t ap;
    if (s_got_ip && esp_wifi_sta_get_ap_info(&ap) == ESP_OK) h->rssi = ap.rssi;
    for (int i = 0; i < U3_NODES; i++) {
        h->uart_ok[i] = s_links[i].ok;
        h->rx_bytes[i] = s_links[i].rx_bytes;
        h->frames[i] = s_links[i].parser.frames;
    }
}

// ---------------------------------------------------------------------------------------------------------------------

void app_main(void)
{
    s_boot_id = (uint16_t)esp_random();
    u3_log_init(s_boot_id);         // from here every log line also waits in the ring for the PC
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "nvs: %s, erased", esp_err_to_name(err));
        nvs_flash_erase();
        nvs_flash_init();
    }
    ESP_LOGI(TAG, "U3 relay, boot %04x, reset %d", s_boot_id, (int)esp_reset_reason());
    u3_port_init();                 // EN and IO0 of both nodes released first thing
    for (int i = 0; i < U3_NODES; i++) {
        link_t *l = &s_links[i];
        err = link_init(l);
        l->ok = err == ESP_OK;
        if (!l->ok) {
            ESP_LOGE(TAG, "%s control uart: %s", l->name, esp_err_to_name(err));
            continue;
        }
        xTaskCreatePinnedToCore(link_task, l->name, 4096, (void *)(intptr_t)i, 5, NULL, 0);
    }
    bool network = wifi_start();
    u3_ota_start(network);
    if (network) u3_log_start();
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(60000));
        u3_health_t h;
        u3_health(&h);
        ESP_LOGI(TAG, "up %" PRId64 " s, ip %s rssi %d, u1 alive %d frames %" PRIu32 ", u2 alive %d frames %" PRIu32
                 ", console %s", esp_timer_get_time() / 1000000, h.got_ip ? h.ip : "-", h.rssi, u3_node_alive(0),
                 h.frames[0], u3_node_alive(1), h.frames[1], u3_port_mode_name(u3_port_mode()));
    }
}
