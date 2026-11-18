// Network part of the LyraT updater (goal 096): Wi-Fi STA with the bench network and the network hop of ota_link on UDP
// 5007, the same as the S3's (HELLO nonce, BEGIN with the token proof, DATA, END). The flash port is the one of
// lyrat_ota.c, so both ways write the image the same way; only one session at a time (upd_net_busy, lyrat_net_rx_busy).
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "lyrat_link.h"
#include "ota_link.h"
#include "upd_mode.h"
#include "upd_net.h"

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif

static const char *TAG = "upd_net";

#define POLL_MS          100
#define RECONNECT_MS     2000
#define NONCE_LIFE_MS    60000
#define NOWIFI_TEST_PASS "wrong-password-of-goal-096"

static volatile bool s_got_ip;
static volatile int s_last_reason;
static volatile bool s_started;
static char s_ip[16] = "-";
static ota_rx_t s_rx;
static ota_rx_port_t s_port;
static uint8_t s_nonce[OTA_NONCE_BYTES];
static uint32_t s_nonce_ms;
static bool s_have_nonce;
static volatile int64_t s_next_connect;

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

const ota_rx_t *upd_net_rx(void) { return &s_rx; }
int lyrat_net_rx_busy(void) { return s_rx.state == OTA_RX_RECV; }

size_t upd_net_text(char *out, size_t cap)
{
    char w[20];
    if (!s_started) snprintf(w, sizeof(w), "off");
    else if (s_got_ip) snprintf(w, sizeof(w), "up");
    else if (s_last_reason) snprintf(w, sizeof(w), "fail:%d", s_last_reason);
    else snprintf(w, sizeof(w), "connecting");
    int n = snprintf(out, cap, "wifi=%s ip=%s port=%d", w, s_ip, OTA_UDP_PORT);
    if (n < 0) return 0;
    return (size_t)n < cap ? (size_t)n : cap - 1;
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *d = data;
        s_last_reason = d ? d->reason : -1;
        if (s_got_ip) ESP_LOGW(TAG, "wifi lost (reason %d)", s_last_reason);
        else ESP_LOGW(TAG, "wifi: no connection (reason %d), again in %d ms", s_last_reason, RECONNECT_MS);
        s_got_ip = false;
        snprintf(s_ip, sizeof(s_ip), "-");
        s_next_connect = esp_timer_get_time() + RECONNECT_MS * 1000LL;
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&e->ip_info.ip));
        s_got_ip = true;
        s_last_reason = 0;
        ESP_LOGI(TAG, "wifi ip %s, image on udp %d", s_ip, OTA_UDP_PORT);
    }
}

static esp_err_t wifi_start(bool wrong_pass)
{
    if (!WIFI_SSID[0]) {
        ESP_LOGE(TAG, "no WIFI_SSID: lyrat_resend/main/wifi_secrets.h missing at build time");
        return ESP_ERR_NOT_FOUND;
    }
    esp_err_t err = esp_netif_init();
    if (err == ESP_OK) err = esp_event_loop_create_default();
    if (err == ESP_OK && !esp_netif_create_default_wifi_sta()) err = ESP_FAIL;
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    if (err == ESP_OK) err = esp_wifi_init(&init);
    if (err == ESP_OK) err = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL);
    if (err == ESP_OK) err = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi_event, NULL);
    if (err == ESP_OK) err = esp_wifi_set_storage(WIFI_STORAGE_RAM);     // the password stays out of nvs
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "wifi init: %s", esp_err_to_name(err));
        return err;
    }
    wifi_config_t cfg = {0};
    snprintf((char *)cfg.sta.ssid, sizeof(cfg.sta.ssid), "%s", WIFI_SSID);
    snprintf((char *)cfg.sta.password, sizeof(cfg.sta.password), "%s", wrong_pass ? NOWIFI_TEST_PASS : WIFI_PASS);
    cfg.sta.threshold.authmode = WIFI_PASS[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    if (wrong_pass) ESP_LOGW(TAG, "test: joining with a wrong password on purpose");
    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err == ESP_OK) err = esp_wifi_set_config(WIFI_IF_STA, &cfg);
    if (err == ESP_OK) err = esp_wifi_start();
    if (err == ESP_OK) esp_wifi_set_ps(WIFI_PS_NONE);      // the image comes in a burst; a dozing radio slows it down
    if (err != ESP_OK) ESP_LOGE(TAG, "wifi start: %s", esp_err_to_name(err));
    s_started = err == ESP_OK;
    return err;
}

static int port_auth(void *ctx, const uint8_t *begin)
{
    const char *token = lyrat_ota_token();
    bool fresh = s_have_nonce && (uint32_t)(now_ms() - s_nonce_ms) < NONCE_LIFE_MS;
    s_have_nonce = false;        // one BEGIN per nonce, good or bad
    if (!token[0] || !fresh) return 0;
    return ota_auth_check((const uint8_t *)token, strlen(token), s_nonce, begin);
}

static int (*s_flash_may_begin)(void *ctx, uint8_t flags);

static int port_may_begin(void *ctx, uint8_t flags)
{
    if (lyrat_ota_uart_busy()) return OTA_ST_REFUSED;      // the S3 is sending one over the UART
    return s_flash_may_begin ? s_flash_may_begin(ctx, flags) : OTA_ST_OK;
}

static size_t query_json(char *out, size_t cap)
{
    char st[300];
    lyrat_ota_status(st, sizeof(st));
    for (char *c = st; *c; c++) {
        if (*c == '"' || *c == '\\' || *c < 0x20) *c = '_';
    }
    int n = snprintf(out, cap, "{\"fw\":\"lyrat_updater\",\"ip\":\"%s\",\"rx\":{\"state\":\"%s\",\"size\":%lu,\"next\":%lu,"
                     "\"err\":\"%s\",\"done\":%lu},\"status\":\"%s\"}", s_ip, ota_rx_state_name(s_rx.state),
                     (unsigned long)s_rx.size, (unsigned long)s_rx.next, ota_status_name(s_rx.last_err),
                     (unsigned long)s_rx.done, st);
    if (n < 0) return 0;
    return (size_t)n < cap ? (size_t)n : cap - 1;
}

static void serve_task(void *arg)
{
    int sock = (int)(intptr_t)arg;
    static uint8_t in[OTA_UDP_HDR + OTA_REQ_MAX + 8];
    static uint8_t out[OTA_UDP_HDR + 700];
    for (;;) {
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        int n = recvfrom(sock, in, sizeof(in), 0, (struct sockaddr *)&from, &from_len);
        uint32_t now = now_ms();
        ota_rx_tick(&s_rx, now);
        if (!s_got_ip && s_next_connect && esp_timer_get_time() >= s_next_connect) {
            s_next_connect = 0;
            esp_wifi_connect();
        }
        if (n < OTA_UDP_HDR + 1 || in[0] != 'O' || in[1] != 'T' || in[2] != OTA_UDP_VERSION ||
            in[3] != OTA_TARGET_SELF) {
            continue;
        }
        uint8_t op = in[4];
        memcpy(out, in, OTA_UDP_HDR);
        uint8_t *rep = out + OTA_UDP_HDR;
        size_t len;
        if (op == OTA_OP_HELLO) {
            esp_fill_random(s_nonce, sizeof(s_nonce));
            s_nonce_ms = now;
            s_have_nonce = true;
            memset(rep, 0, OTA_REPLY_HDR);
            rep[0] = op;
            for (int i = 0; i < OTA_NONCE_BYTES; i++) sprintf((char *)rep + OTA_REPLY_HDR + 2 * i, "%02x", s_nonce[i]);
            len = OTA_REPLY_HDR + 2 * OTA_NONCE_BYTES;
        } else if (op == OTA_OP_QUERY) {
            memset(rep, 0, OTA_REPLY_HDR);
            rep[0] = op;
            len = OTA_REPLY_HDR + query_json((char *)rep + OTA_REPLY_HDR, sizeof(out) - OTA_UDP_HDR - OTA_REPLY_HDR);
        } else {
            len = ota_rx_handle(&s_rx, in + OTA_UDP_HDR, (size_t)n - OTA_UDP_HDR, now, rep, OTA_REPLY_MAX);
            ota_rx_take_events(&s_rx);
        }
        sendto(sock, out, OTA_UDP_HDR + len, 0, (struct sockaddr *)&from, from_len);
    }
}

void upd_net_start(bool wrong_pass)
{
    lyrat_ota_port_copy(&s_port);
    s_flash_may_begin = s_port.may_begin;
    s_port.auth = port_auth;
    s_port.may_begin = port_may_begin;
    s_port.busy = NULL;                 // no call can be on: no Bluetooth here
    s_port.ctx = LYRAT_OTA_NET_CTX;
    ota_rx_init(&s_rx, &s_port, UPD_AFTER_SESSION_MS);
    if (wifi_start(wrong_pass) != ESP_OK) return;     // the UART way and the window go on without it
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in me = {.sin_family = AF_INET, .sin_port = htons(OTA_UDP_PORT), .sin_addr.s_addr = htonl(INADDR_ANY)};
    if (sock < 0 || bind(sock, (struct sockaddr *)&me, sizeof(me)) != 0) {
        ESP_LOGE(TAG, "udp %d not available", OTA_UDP_PORT);
        if (sock >= 0) close(sock);
        return;
    }
    struct timeval tv = {.tv_sec = 0, .tv_usec = POLL_MS * 1000};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    if (xTaskCreatePinnedToCore(serve_task, "upd_net", 6144, (void *)(intptr_t)sock, 5, NULL, 0) != pdPASS) {
        ESP_LOGE(TAG, "no network task");
    }
}
