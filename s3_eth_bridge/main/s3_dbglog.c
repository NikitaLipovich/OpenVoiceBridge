// Debug log of the S3 and, relayed, of the LyraT over UDP 5008 (docs/OTA.md, "Debug log").
//
// Every ESP_LOG line of this board goes into s_own (dbg_log.c hook), the LyraT's records into s_relay as they came. A PC
// asks with "LOG1 [s3_level] [lyrat_level]" every 2 s; the lines go to it for LEASE_MS after the last request. Nothing is
// taken from the rings without a listener, so the lines of the start wait for the first one.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "dbg_log.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "s3_link.h"

#define OWN_LINES     128
#define RELAY_LINES   128
#define LEASE_MS      10000
#define DRAIN_MS      50
#define BURST         8         // datagrams per round at most

static const char *TAG = "s3log";

static dbg_slot_t s_own_slots[OWN_LINES];
static dbg_slot_t s_relay_slots[RELAY_LINES];
static dbg_ring_t s_own, s_relay;
static int s_sock = -1;
static struct sockaddr_in s_peer;
static volatile int64_t s_lease_until_us;
static volatile uint8_t s_lyrat_level = DBG_LVL_I;
static uint8_t s_s3_level = DBG_LVL_I;
static uint32_t s_datagrams, s_send_fail, s_requests;

void s3_log_init(uint16_t boot_id)
{
    dbg_ring_init(&s_own, s_own_slots, OWN_LINES, DBG_BOARD_S3, boot_id);
    dbg_ring_init(&s_relay, s_relay_slots, RELAY_LINES, DBG_BOARD_LYRAT, 0);
    dbg_log_install(&s_own);
}

static bool listening(void)
{
    return s_lease_until_us && esp_timer_get_time() < s_lease_until_us;
}

void s3_log_line(const char *fmt, ...)
{
    char line[DBG_TEXT_MAX];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    // The console sees the whole line, the ring keeps what fits.
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    printf("\n");
    if (n > 0) dbg_ring_push(&s_own, (uint32_t)(esp_timer_get_time() / 1000), line, (size_t)n);
}

void s3_log_relay(const uint8_t *payload, size_t len)
{
    size_t at = 0;
    while (at < len) {
        size_t used = 0;
        if (dbg_ring_push_record(&s_relay, payload + at, len - at, &used) == 0) break;
        at += used;
    }
}

size_t s3_log_lyrat_ctl(uint8_t out[2])
{
    out[0] = listening() ? 1 : 0;
    out[1] = s_lyrat_level;
    return 2;
}

uint32_t s3_log_relay_lost(void)
{
    return s_relay.lost;
}

static int level_arg(const char *p, int fallback)
{
    while (*p == ' ') p++;
    if (*p < '0' || *p > '5') return fallback;
    return *p - '0';
}

static void answer(const struct sockaddr_in *to)
{
    char text[200];
    int n = snprintf(text, sizeof(text),
                     "LOG %d {\"s3_level\":%u,\"lyrat_level\":%u,\"own\":{\"pending\":%lu,\"lost\":%lu,\"sent\":%lu},"
                     "\"relay\":{\"pending\":%lu,\"lost\":%lu,\"sent\":%lu},\"datagrams\":%lu,\"send_fail\":%lu}",
                     listening(), s_s3_level, s_lyrat_level, (unsigned long)dbg_ring_pending(&s_own),
                     (unsigned long)s_own.lost, (unsigned long)s_own.taken, (unsigned long)dbg_ring_pending(&s_relay),
                     (unsigned long)s_relay.lost, (unsigned long)s_relay.taken, (unsigned long)s_datagrams,
                     (unsigned long)s_send_fail);
    sendto(s_sock, text, n, MSG_DONTWAIT, (const struct sockaddr *)to, sizeof(*to));
}

static void on_request(char *text, int n, const struct sockaddr_in *from)
{
    s_requests++;
    if (strncmp(text, "LOG1", 4) == 0) {
        const char *p = text + 4;
        int s3 = level_arg(p, DBG_LVL_I);
        while (*p == ' ') p++;
        while (*p && *p != ' ') p++;
        int ly = level_arg(p, DBG_LVL_I);
        if (s3 != s_s3_level) {
            s_s3_level = (uint8_t)s3;
            esp_log_level_set("*", (esp_log_level_t)s3);
        }
        s_lyrat_level = (uint8_t)ly;
        bool fresh = !listening() || s_peer.sin_addr.s_addr != from->sin_addr.s_addr || s_peer.sin_port != from->sin_port;
        s_peer = *from;
        s_lease_until_us = esp_timer_get_time() + LEASE_MS * 1000LL;
        if (fresh) ESP_LOGI(TAG, "log to the PC, s3 level %d, lyrat level %d", s3, ly);
        answer(from);
    } else if (strncmp(text, "LOG0", 4) == 0) {
        s_lease_until_us = 0;
        answer(from);
    }
}

// Packs both rings into one datagram: own lines first, then the LyraT's.
static size_t pack(uint8_t *buf)
{
    uint32_t a = 0, b = 0;
    size_t used = DBG_UDP_HDR;
    used += dbg_ring_pack(&s_own, buf + used, DBG_UDP_MAX - used, &a);
    used += dbg_ring_pack(&s_relay, buf + used, DBG_UDP_MAX - used, &b);
    if (a + b == 0) return 0;
    buf[0] = 'U';
    buf[1] = 'L';
    buf[2] = DBG_UDP_VERSION;
    buf[3] = (uint8_t)(a + b);
    return used;
}

static void log_task(void *arg)
{
    static uint8_t buf[DBG_UDP_MAX];
    static char text[64];
    for (;;) {
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        int n = recvfrom(s_sock, text, sizeof(text) - 1, 0, (struct sockaddr *)&from, &from_len);
        if (n > 0) {
            text[n] = 0;
            on_request(text, n, &from);
        }
        if (!listening()) continue;
        for (int i = 0; i < BURST; i++) {
            size_t len = pack(buf);
            if (!len) break;
            struct sockaddr_in to = s_peer;
            if (sendto(s_sock, buf, len, 0, (struct sockaddr *)&to, sizeof(to)) < 0) s_send_fail++;
            else s_datagrams++;
        }
    }
}

void s3_log_start(void)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in me = {.sin_family = AF_INET, .sin_port = htons(DBG_UDP_PORT), .sin_addr.s_addr = htonl(INADDR_ANY)};
    if (sock < 0 || bind(sock, (struct sockaddr *)&me, sizeof(me)) != 0) {
        ESP_LOGE(TAG, "udp %d not available", DBG_UDP_PORT);
        if (sock >= 0) close(sock);
        return;
    }
    struct timeval tv = {.tv_sec = 0, .tv_usec = DRAIN_MS * 1000};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    s_sock = sock;
    xTaskCreatePinnedToCore(log_task, "udplog", 4096, NULL, 3, NULL, 0);
}
