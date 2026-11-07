// Log of U3, U1 and U2 over UDP 5008 (goal 095, docs/PLATFORM_OTA.md). Based on s3_eth_bridge/main/s3_dbglog.c.
//
// Rings: U3's own ESP_LOG lines (dbg_log hook), the records of U1 and U2 as they came in CTL_T_LOG frames (they keep
// their board, line numbers and counters), and the lines of the UART0 console of U1 and U2 (boards 0x83, 0x84, numbered
// here). A PC asks with "LOG1 [u3] [u1] [u2]" (levels 0..5) every 2 s and gets the lines for LEASE_MS after the last
// request. Without a listener nothing leaves the rings, so the lines of the start wait for the first one.
//
// Other requests on the same port (the answer goes to the source port):
//   CON auto|u1|u2|off     which console UART0 reads; answer "CON {json}"
//   CMD u1|u2 <text>       a console command to the node over its control link; answer "UR <node> <status> <text>"
//   MODE u1|u2 <n>         R21, reserved: CTL_T_MODE to the node; answer "MA <node> <status> <mode>" (2 = unsupported)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ctl_frame.h"
#include "dbg_log.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "plat_proto.h"
#include "u3_link.h"

#define OWN_LINES     96
#define NODE_LINES    64
#define CON_LINES     32
#define LEASE_MS      10000
#define DRAIN_MS      50
#define BURST         8
#define ANSWER_MAX    600

static const char *TAG = "u3log";

static dbg_slot_t s_own_slots[OWN_LINES];
static dbg_slot_t s_node_slots[U3_NODES][NODE_LINES];
static dbg_slot_t s_con_slots[U3_NODES][CON_LINES];
static dbg_ring_t s_own, s_node[U3_NODES], s_con[U3_NODES];
static int s_sock = -1;
static struct sockaddr_in s_peer, s_asker[U3_NODES];
static bool s_have_asker[U3_NODES];
static volatile int64_t s_lease_until_us;
static uint8_t s_own_level = DBG_LVL_I;
static volatile uint8_t s_node_level[U3_NODES] = {DBG_LVL_I, DBG_LVL_I};
static uint32_t s_datagrams, s_send_fail, s_requests, s_cmd_seq;

static const uint8_t s_boards[U3_NODES] = {DBG_BOARD_U1, DBG_BOARD_U2};

void u3_log_init(uint16_t boot_id)
{
    dbg_ring_init(&s_own, s_own_slots, OWN_LINES, DBG_BOARD_U3, boot_id);
    for (int i = 0; i < U3_NODES; i++) {
        dbg_ring_init(&s_node[i], s_node_slots[i], NODE_LINES, s_boards[i], 0);
        dbg_ring_init(&s_con[i], s_con_slots[i], CON_LINES, DBG_BOARD_CON | s_boards[i], boot_id);
    }
    dbg_log_install(&s_own);
}

static bool listening(void)
{
    return s_lease_until_us && esp_timer_get_time() < s_lease_until_us;
}

void u3_log_line(const char *fmt, ...)
{
    char line[DBG_TEXT_MAX];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    printf("%s\n", line);
    if (n > 0) dbg_ring_push(&s_own, (uint32_t)(esp_timer_get_time() / 1000), line, strnlen(line, sizeof(line)));
}

void u3_log_relay(int node, const uint8_t *payload, size_t len)
{
    if (node < 0 || node >= U3_NODES) return;
    size_t at = 0;
    while (at < len) {
        size_t used = 0;
        if (dbg_ring_push_record(&s_node[node], payload + at, len - at, &used) == 0) break;
        at += used;
    }
}

void u3_log_console(int node, const char *text, size_t len)
{
    if (node < 0 || node >= U3_NODES) return;
    dbg_ring_push(&s_con[node], (uint32_t)(esp_timer_get_time() / 1000), text, len);
}

size_t u3_log_node_ctl(int node, uint8_t out[2])
{
    out[0] = listening() ? 1 : 0;
    out[1] = node >= 0 && node < U3_NODES ? s_node_level[node] : DBG_LVL_I;
    return 2;
}

static void send_text(const struct sockaddr_in *to, const char *text, int n)
{
    if (s_sock >= 0 && n > 0) sendto(s_sock, text, n, MSG_DONTWAIT, (const struct sockaddr *)to, sizeof(*to));
}

void u3_log_node_frame(int node, uint8_t type, const uint8_t *payload, size_t len)
{
    if (node < 0 || node >= U3_NODES || !len) return;
    // Both link tasks call this: a buffer of the caller's own. A node's reply is at most 1 + 240 bytes (node_link.c).
    char text[300];
    int n;
    if (type == CTL_T_REPLY) {
        n = snprintf(text, sizeof(text), "UR %s %u ", u3_node_name(node), payload[0]);
        size_t body = len - 1 < sizeof(text) - (size_t)n - 1 ? len - 1 : sizeof(text) - (size_t)n - 1;
        memcpy(text + n, payload + 1, body);
        n += (int)body;
    } else {
        n = snprintf(text, sizeof(text), "MA %s %u %u", u3_node_name(node), payload[0], len > 1 ? payload[1] : 0);
    }
    text[n] = 0;
    ESP_LOGI(TAG, "%.*s", n > 120 ? 120 : n, text);
    if (s_have_asker[node]) send_text(&s_asker[node], text, n);
}

static int level_at(const char **p, int fallback)
{
    while (**p == ' ') (*p)++;
    if (**p < '0' || **p > '5') return fallback;
    int v = **p - '0';
    while (**p && **p != ' ') (*p)++;
    return v;
}

static int node_arg(const char **p)
{
    while (**p == ' ') (*p)++;
    int node = strncmp(*p, "u1", 2) == 0 ? 0 : strncmp(*p, "u2", 2) == 0 ? 1 : -1;
    if (node >= 0) *p += 2;
    while (**p == ' ') (*p)++;
    return node;
}

static void answer_log(const struct sockaddr_in *to)
{
    static char text[ANSWER_MAX];
    int n = snprintf(text, sizeof(text), "LOG %d {\"u3_level\":%u,\"u1_level\":%u,\"u2_level\":%u,\"own\":{\"pending\":%lu,"
                     "\"lost\":%lu,\"sent\":%lu}", listening(), s_own_level, s_node_level[0], s_node_level[1],
                     (unsigned long)dbg_ring_pending(&s_own), (unsigned long)s_own.lost, (unsigned long)s_own.taken);
    for (int i = 0; i < U3_NODES && n > 0 && n < (int)sizeof(text); i++) {
        n += snprintf(text + n, sizeof(text) - n, ",\"%s\":{\"pending\":%lu,\"lost\":%lu,\"sent\":%lu},"
                      "\"%scon\":{\"pending\":%lu,\"lost\":%lu,\"sent\":%lu}", u3_node_name(i),
                      (unsigned long)dbg_ring_pending(&s_node[i]), (unsigned long)s_node[i].lost,
                      (unsigned long)s_node[i].taken, u3_node_name(i), (unsigned long)dbg_ring_pending(&s_con[i]),
                      (unsigned long)s_con[i].lost, (unsigned long)s_con[i].taken);
    }
    if (n > 0 && n < (int)sizeof(text)) {
        n += snprintf(text + n, sizeof(text) - n, ",\"console\":\"%s\",\"datagrams\":%lu,\"send_fail\":%lu}",
                      u3_port_mode_name(u3_port_mode()), (unsigned long)s_datagrams, (unsigned long)s_send_fail);
    }
    if (n >= (int)sizeof(text)) n = (int)sizeof(text) - 1;
    send_text(to, text, n);
}

static void answer_con(const struct sockaddr_in *to)
{
    char text[160];
    int listen = u3_port_listening();
    int n = snprintf(text, sizeof(text), "CON {\"mode\":\"%s\",\"listening\":\"%s\",\"u1_lines\":%lu,\"u2_lines\":%lu}",
                     u3_port_mode_name(u3_port_mode()), listen >= 0 ? u3_node_name(listen) : "none",
                     (unsigned long)u3_port_con_lines(0), (unsigned long)u3_port_con_lines(1));
    send_text(to, text, n);
}

static void on_request(char *text, const struct sockaddr_in *from)
{
    s_requests++;
    if (strncmp(text, "LOG1", 4) == 0) {
        const char *p = text + 4;
        int own = level_at(&p, DBG_LVL_I);
        int u1 = level_at(&p, DBG_LVL_I);
        int u2 = level_at(&p, u1);
        if (own != s_own_level) {
            s_own_level = (uint8_t)own;
            esp_log_level_set("*", (esp_log_level_t)own);
        }
        s_node_level[0] = (uint8_t)u1;
        s_node_level[1] = (uint8_t)u2;
        bool fresh = !listening() || s_peer.sin_addr.s_addr != from->sin_addr.s_addr || s_peer.sin_port != from->sin_port;
        s_peer = *from;
        s_lease_until_us = esp_timer_get_time() + LEASE_MS * 1000LL;
        if (fresh) ESP_LOGI(TAG, "log to the PC, levels u3 %d u1 %d u2 %d", own, u1, u2);
        answer_log(from);
    } else if (strncmp(text, "LOG0", 4) == 0) {
        s_lease_until_us = 0;
        answer_log(from);
    } else if (strncmp(text, "CON", 3) == 0) {
        const char *p = text + 3;
        while (*p == ' ') p++;
        if (*p) {
            int mode = strncmp(p, "auto", 4) == 0 ? U3_CON_AUTO : strncmp(p, "u1", 2) == 0 ? U3_CON_U1
                     : strncmp(p, "u2", 2) == 0 ? U3_CON_U2 : strncmp(p, "off", 3) == 0 ? U3_CON_OFF : -1;
            u3_port_set_mode(mode);
        }
        answer_con(from);
    } else if (strncmp(text, "CMD", 3) == 0 || strncmp(text, "MODE", 4) == 0) {
        bool mode = text[1] == 'O';
        const char *p = text + (mode ? 4 : 3);
        int node = node_arg(&p);
        char err[48];
        if (node < 0 || !*p) {
            send_text(from, err, snprintf(err, sizeof(err), "%s bad_request", mode ? "MA" : "UR"));
            return;
        }
        s_asker[node] = *from;
        s_have_asker[node] = true;
        bool sent;
        if (mode) {
            uint8_t m[2] = {(uint8_t)atoi(p), 0};
            sent = u3_node_send(node, CTL_T_MODE, m, sizeof(m));
        } else {
            sent = u3_node_send(node, CTL_T_CMD, p, strlen(p));
        }
        s_cmd_seq++;
        if (!sent) {
            send_text(from, err, snprintf(err, sizeof(err), "%s %s not_connected", mode ? "MA" : "UR",
                                          u3_node_name(node)));
        }
    }
}

// Packs the rings into one datagram: own lines first, then each node's, then the consoles.
static size_t pack(uint8_t *buf)
{
    uint32_t total = 0, c = 0;
    size_t used = DBG_UDP_HDR;
    used += dbg_ring_pack(&s_own, buf + used, DBG_UDP_MAX - used, &c);
    total += c;
    for (int i = 0; i < U3_NODES; i++) {
        c = 0;
        used += dbg_ring_pack(&s_node[i], buf + used, DBG_UDP_MAX - used, &c);
        total += c;
        c = 0;
        used += dbg_ring_pack(&s_con[i], buf + used, DBG_UDP_MAX - used, &c);
        total += c;
    }
    if (!total) return 0;
    buf[0] = 'U';
    buf[1] = 'L';
    buf[2] = DBG_UDP_VERSION;
    buf[3] = (uint8_t)(total > 255 ? 255 : total);
    return used;
}

static void log_task(void *arg)
{
    static uint8_t buf[DBG_UDP_MAX];
    static char text[200];
    for (;;) {
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        int n = recvfrom(s_sock, text, sizeof(text) - 1, 0, (struct sockaddr *)&from, &from_len);
        if (n > 0) {
            text[n] = 0;
            on_request(text, &from);
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

void u3_log_start(void)
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
