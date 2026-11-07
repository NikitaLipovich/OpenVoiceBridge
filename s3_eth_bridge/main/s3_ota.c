// OTA of the S3 itself and of the LyraT through it (docs/OTA.md).
//
// UDP 5007: "OT", version, target, then an ota_link request. Target 0 writes the next app slot of this board, target 1
// stages a LyraT image in the partition lyrat_img; when that one is complete and its bytes read back with the announced
// SHA-256, the relay task sends it to the LyraT over the control UART with the same protocol and follows the LyraT
// until it runs the image (or rolled back). The protocol and its checks are in ota_link.c, shared with the LyraT and the
// PC tests; this file only gives them flash, the token, the UART and the network.
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "ctl_frame.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "nvs.h"
#include "ota_link.h"
#include "s3_link.h"

#if __has_include("ota_secrets.h")
#include "ota_secrets.h"
#endif
#ifndef OTA_TOKEN
#define OTA_TOKEN ""
#endif

static const char *TAG = "s3ota";

#define IDLE_MS             30000
#define NONCE_LIFE_MS       60000
#define REBOOT_DELAY_MS     500
#define LYRAT_SLOT          0x1F0000u       // the LyraT's ota_0/ota_1, docs/OTA.md
#define LYRAT_PROJECT       "lyrat_resend"
#define S3_PROJECT          "s3_eth_bridge"
#define SELFTEST_MS         90000
#define RELAY_WAIT_TX_MS    30000
#define VERIFY_MS           180000
#define STATUS_EVERY_MS     10000
#define VERIFY_POLL_MS      2000
#define BAUD_PROBE_MS       3000
#define NVS_NS              "ota"
#define NVS_FAILTEST        "failtest"

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

// ---------------------------------------------------------------------------------------------------------------------
// Token and nonce

static uint8_t s_nonce[OTA_NONCE_BYTES];
static uint32_t s_nonce_ms;
static bool s_have_nonce;

static int port_auth(void *ctx, const uint8_t *begin)
{
    const char *token = OTA_TOKEN;
    bool fresh = s_have_nonce && (uint32_t)(now_ms() - s_nonce_ms) < NONCE_LIFE_MS;
    s_have_nonce = false;        // one BEGIN per nonce, good or bad
    if (!token[0] || !fresh) return 0;
    return ota_auth_check((const uint8_t *)token, strlen(token), s_nonce, begin);
}

// ---------------------------------------------------------------------------------------------------------------------
// Target 0: this board's next app slot

static const esp_partition_t *s_next;
static esp_ota_handle_t s_ota;
static bool s_ota_open;
static bool s_relay_busy(void);

static uint32_t self_capacity(void *ctx)
{
    s_next = esp_ota_get_next_update_partition(NULL);
    return s_next ? (uint32_t)s_next->size : 0;
}

static int self_may_begin(void *ctx, uint8_t flags)
{
    return s_relay_busy() ? OTA_ST_REFUSED : OTA_ST_OK;     // a restart now would cut the LyraT's transfer
}

static int self_check_head(void *ctx, const uint8_t *head, size_t len)
{
    return ota_image_check(head, len, OTA_CHIP_ESP32S3, S3_PROJECT);
}

static int self_begin(void *ctx, uint32_t size, uint8_t flags)
{
    if (s_ota_open) {
        esp_ota_abort(s_ota);
        s_ota_open = false;
    }
    esp_err_t err = esp_ota_begin(s_next, size, &s_ota);    // erases size bytes now
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ota begin: %s", esp_err_to_name(err));
        return 1;
    }
    s_ota_open = true;
    ESP_LOGI(TAG, "receiving %" PRIu32 " bytes into %s", size, s_next->label);
    return 0;
}

static int self_write(void *ctx, uint32_t offset, const uint8_t *data, size_t len)
{
    return esp_ota_write(s_ota, data, len) == ESP_OK ? 0 : 1;
}

static void failtest_mark(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u8(h, NVS_FAILTEST, 1);
        nvs_commit(h);
        nvs_close(h);
    }
}

static int self_finish(void *ctx, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags)
{
    s_ota_open = false;
    esp_err_t err = esp_ota_end(s_ota);         // releases the handle whatever the result
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "image check: %s", esp_err_to_name(err));
        return OTA_ST_VERIFY;
    }
    if (flags & OTA_F_DRYRUN) {
        ESP_LOGI(TAG, "dry run: image good, not activated");
        return OTA_ST_OK;
    }
    if (esp_ota_set_boot_partition(s_next) != ESP_OK) return OTA_ST_FLASH;
    if (flags & OTA_F_FAILTEST) failtest_mark();
    ESP_LOGW(TAG, "new image in %s, restarting%s", s_next->label, flags & OTA_F_FAILTEST ? " (failtest)" : "");
    return OTA_ST_OK;
}

static void self_abort(void *ctx)
{
    if (s_ota_open) esp_ota_abort(s_ota);
    s_ota_open = false;
}

static const ota_rx_port_t s_self_port = {
    .capacity = self_capacity, .may_begin = self_may_begin, .check_head = self_check_head, .auth = port_auth,
    .begin = self_begin, .write = self_write, .finish = self_finish, .abort = self_abort,
};

// ---------------------------------------------------------------------------------------------------------------------
// Target 1: the LyraT's image, staged

typedef enum { RELAY_IDLE, RELAY_SEND, RELAY_VERIFY, RELAY_DONE, RELAY_ROLLED_BACK, RELAY_FAILED } relay_state_t;
static const char *const s_relay_names[] = {"idle", "send", "verify", "done", "rolled_back", "failed"};

static const esp_partition_t *s_stage;
static volatile bool s_staged;              // set by stage_finish, taken by the relay task
static uint32_t s_stage_size;
static uint8_t s_stage_sha[OTA_SHA256_BYTES], s_stage_flags;
static uint32_t s_stage_baud;
static char s_stage_elf[17];
static volatile relay_state_t s_relay = RELAY_IDLE;

static bool s_relay_busy(void)
{
    return s_staged || s_relay == RELAY_SEND || s_relay == RELAY_VERIFY;
}

static uint32_t stage_capacity(void *ctx)
{
    if (!s_stage) s_stage = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x40, "lyrat_img");
    if (!s_stage) return 0;
    return s_stage->size < LYRAT_SLOT ? (uint32_t)s_stage->size : LYRAT_SLOT;
}

static int stage_may_begin(void *ctx, uint8_t flags)
{
    return s_relay_busy() ? OTA_ST_REFUSED : OTA_ST_OK;
}

static int stage_check_head(void *ctx, const uint8_t *head, size_t len)
{
    int st = ota_image_check(head, len, OTA_CHIP_ESP32, LYRAT_PROJECT);
    if (st == OTA_ST_OK) {
        const uint8_t *elf = ota_image_elf_sha(head, len);
        for (int i = 0; i < 8; i++) sprintf(s_stage_elf + 2 * i, "%02x", elf[i]);
    }
    return st;
}

static int stage_begin(void *ctx, uint32_t size, uint8_t flags)
{
    uint32_t span = (size + 4095u) & ~4095u;
    esp_err_t err = esp_partition_erase_range(s_stage, 0, span);
    if (err != ESP_OK) ESP_LOGE(TAG, "stage erase: %s", esp_err_to_name(err));
    return err == ESP_OK ? 0 : 1;
}

static int stage_write(void *ctx, uint32_t offset, const uint8_t *data, size_t len)
{
    return esp_partition_write(s_stage, offset, data, len) == ESP_OK ? 0 : 1;
}

// The received bytes matched already; this proves what the flash holds, which is what the relay will read.
static int stage_finish(void *ctx, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags)
{
    static uint8_t block[1024];
    ota_sha256_t h;
    ota_sha256_start(&h);
    for (uint32_t at = 0; at < size; at += sizeof(block)) {
        uint32_t n = size - at < sizeof(block) ? size - at : sizeof(block);
        if (esp_partition_read(s_stage, at, block, n) != ESP_OK) return OTA_ST_FLASH;
        ota_sha256_update(&h, block, n);
    }
    uint8_t got[OTA_SHA256_BYTES];
    ota_sha256_finish(&h, got);
    if (memcmp(got, sha, sizeof(got)) != 0) {
        ESP_LOGE(TAG, "staged image reads back different");
        return OTA_ST_FLASH;
    }
    s_stage_size = size;
    memcpy(s_stage_sha, sha, sizeof(s_stage_sha));
    s_stage_flags = flags;
    s_staged = true;
    ESP_LOGI(TAG, "lyrat image staged, %" PRIu32 " bytes, elf %s", size, s_stage_elf);
    return OTA_ST_OK;
}

static const ota_rx_port_t s_stage_port = {
    .capacity = stage_capacity, .may_begin = stage_may_begin, .check_head = stage_check_head, .auth = port_auth,
    .begin = stage_begin, .write = stage_write, .finish = stage_finish,
};

static ota_rx_t s_rx[2];

// ---------------------------------------------------------------------------------------------------------------------
// Relay to the LyraT over the UART

typedef struct {
    uint16_t len;
    uint8_t data[OTA_REPLY_MAX];
} reply_msg_t;

static QueueHandle_t s_replies;
static ota_tx_t s_tx;
static char s_relay_err[24] = "";
static char s_lyrat_status[OTA_REPLY_MAX];
static uint32_t s_lyrat_status_ms;

void s3_ota_on_lyrat_reply(const uint8_t *payload, size_t len)
{
    if (!s_replies || len < OTA_REPLY_HDR) return;
    reply_msg_t m;
    m.len = (uint16_t)(len < sizeof(m.data) ? len : sizeof(m.data));
    memcpy(m.data, payload, m.len);
    xQueueSend(s_replies, &m, 0);
}

static int stage_read(void *ctx, uint32_t offset, uint8_t *buf, size_t len)
{
    return esp_partition_read(s_stage, offset, buf, len) == ESP_OK ? 0 : 1;
}

static const ota_tx_port_t s_stage_reader = {.read = stage_read};

// Value of "key=" in the LyraT's STATUS text, into out.
static void status_field(const char *text, const char *key, char *out, size_t cap)
{
    out[0] = 0;
    const char *p = strstr(text, key);
    if (!p) return;
    p += strlen(key);
    size_t n = 0;
    while (p[n] && p[n] != ' ' && n + 1 < cap) {
        out[n] = p[n];
        n++;
    }
    out[n] = 0;
}

static void relay_fail(const char *why)
{
    snprintf(s_relay_err, sizeof(s_relay_err), "%s", why);
    s_relay = RELAY_FAILED;
    ESP_LOGE(TAG, "lyrat update failed: %s", why);
}

static volatile bool s_stage_reset;    // the UDP task owns s_rx: it re-arms the stage receiver itself

static void relay_end(void)
{
    if (s3_uart_get_baud() != CTL_BAUD_DEFAULT) s3_uart_set_baud(CTL_BAUD_DEFAULT);
    s_stage_reset = true;               // ready for the next image
}

static void ask_status(void)
{
    uint8_t req[5];
    s3_uart_send(CTL_T_OTA, req, ota_build_short(req, OTA_OP_STATUS, 0));
}

static void take_status(const reply_msg_t *m)
{
    size_t n = m->len - OTA_REPLY_HDR;
    for (size_t i = 0; i < n; i++) {    // it goes into JSON as it is
        char c = (char)m->data[OTA_REPLY_HDR + i];
        s_lyrat_status[i] = (c == '"' || c == '\\' || c < 0x20 || c > 0x7E) ? '_' : c;
    }
    s_lyrat_status[n] = 0;
    s_lyrat_status_ms = now_ms();
}

static void relay_task(void *arg)
{
    static uint8_t out[OTA_REQ_MAX];
    uint32_t tx_wait_since = 0, next_status = 0, verify_until = 0, baud_since = 0;
    bool baud_fallback = false;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(5));
        uint32_t now = now_ms();
        reply_msg_t m;
        while (xQueueReceive(s_replies, &m, 0) == pdTRUE) {
            if (m.data[0] == OTA_OP_STATUS) {
                take_status(&m);
                continue;
            }
            if (s_relay != RELAY_SEND) continue;
            if (m.data[0] == OTA_OP_BEGIN && m.data[1] == OTA_ST_OK && s_tx.state == OTA_TX_BEGIN && s_tx.baud &&
                (int)s_tx.baud != s3_uart_get_baud()) {
                s3_uart_set_baud((int)s_tx.baud);      // the LyraT switched after its reply
                baud_since = now;
            }
            ota_tx_on_reply(&s_tx, m.data, m.len, now);
        }

        if (s_staged && s_relay != RELAY_SEND && s_relay != RELAY_VERIFY) {
            s_staged = false;
            ota_tx_init(&s_tx, &s_stage_reader);
            s_tx.window = 2;
            ota_tx_start(&s_tx, esp_random(), s_stage_size, s_stage_sha, s_stage_flags, s_stage_baud, now);
            s_relay_err[0] = 0;
            baud_fallback = false;
            baud_since = 0;
            tx_wait_since = now;
            s_relay = RELAY_SEND;
            ESP_LOGI(TAG, "sending the image to the lyrat, baud %" PRIu32, s_stage_baud ? s_stage_baud : CTL_BAUD_DEFAULT);
        }

        if (s_relay == RELAY_SEND) {
            // Agreed baud that carries nothing: both ends go back to the default and start again.
            if (baud_since && s_tx.acked == 0 && (uint32_t)(now - baud_since) > BAUD_PROBE_MS && !baud_fallback) {
                ESP_LOGW(TAG, "no answer at %" PRIu32 " baud, again at %d", s_tx.baud, CTL_BAUD_DEFAULT);
                s3_uart_set_baud(CTL_BAUD_DEFAULT);
                baud_fallback = true;
                baud_since = 0;
                ota_tx_start(&s_tx, s_tx.session + 1, s_stage_size, s_stage_sha, s_stage_flags, 0, now);
            }
            if (!s3_uart_tx_on()) {
                if ((uint32_t)(now - tx_wait_since) > RELAY_WAIT_TX_MS) {
                    relay_fail("lyrat_silent");
                    relay_end();
                }
                continue;
            }
            tx_wait_since = now;
            size_t n;
            while ((n = ota_tx_poll(&s_tx, now, out, sizeof(out))) > 0) {
                if (!s3_uart_send(CTL_T_OTA, out, n)) break;
            }
            if (s_tx.state == OTA_TX_DONE) {
                if (s3_uart_get_baud() != CTL_BAUD_DEFAULT) s3_uart_set_baud(CTL_BAUD_DEFAULT);
                if (s_stage_flags & OTA_F_DRYRUN) {
                    s_relay = RELAY_DONE;
                    ESP_LOGI(TAG, "lyrat dry run: image good, not activated");
                    relay_end();
                } else {
                    s_relay = RELAY_VERIFY;
                    verify_until = now + VERIFY_MS;
                    next_status = now + VERIFY_POLL_MS;
                    s_lyrat_status[0] = 0;
                    ESP_LOGI(TAG, "lyrat took the image, waiting for it to run it");
                }
            } else if (s_tx.state == OTA_TX_FAILED) {
                if (s3_uart_get_baud() != CTL_BAUD_DEFAULT) s3_uart_set_baud(CTL_BAUD_DEFAULT);
                // A lost answer to END may hide a LyraT that already restarted with the image: look before failing.
                bool maybe = (s_tx.fail_status == OTA_ST_TIMEOUT || s_tx.fail_status == OTA_ST_NO_SESSION) &&
                             s_tx.acked == s_tx.size && !(s_stage_flags & OTA_F_DRYRUN);
                if (maybe) {
                    s_relay = RELAY_VERIFY;
                    verify_until = now + VERIFY_MS;
                    next_status = now + VERIFY_POLL_MS;
                    s_lyrat_status[0] = 0;
                } else {
                    relay_fail(ota_status_name(s_tx.fail_status));
                    relay_end();
                }
            }
            continue;
        }

        if (s_relay == RELAY_VERIFY) {
            char elf[17], img[16], inv[17];
            status_field(s_lyrat_status, "elf=", elf, sizeof(elf));
            status_field(s_lyrat_status, "img=", img, sizeof(img));
            status_field(s_lyrat_status, "inv=", inv, sizeof(inv));
            if (s_lyrat_status[0] && strcmp(elf, s_stage_elf) == 0 && (strcmp(img, "valid") == 0)) {
                s_relay = RELAY_DONE;
                ESP_LOGI(TAG, "lyrat runs the new image, confirmed");
                relay_end();
            } else if (s_lyrat_status[0] && strcmp(elf, s_stage_elf) != 0 && strcmp(inv, s_stage_elf) == 0) {
                s_relay = RELAY_ROLLED_BACK;
                ESP_LOGW(TAG, "lyrat rolled back to %s", elf);
                relay_end();
            } else if (s_lyrat_status[0] && strstr(s_lyrat_status, "state=done")) {
                verify_until = now + VERIFY_MS;     // image taken, the restart waits for the end of a call
                if ((int32_t)(now - next_status) >= 0) {
                    next_status = now + VERIFY_POLL_MS;
                    s_lyrat_status[0] = 0;
                    if (s3_uart_tx_on()) ask_status();
                }
            } else if ((int32_t)(now - verify_until) > 0) {
                relay_fail("verify_timeout");
                relay_end();
            } else if ((int32_t)(now - next_status) >= 0) {
                next_status = now + VERIFY_POLL_MS;
                s_lyrat_status[0] = 0;
                if (s3_uart_tx_on()) ask_status();
            }
            continue;
        }

        if ((int32_t)(now - next_status) >= 0) {      // keep the LyraT's version at hand for QUERY
            next_status = now + STATUS_EVERY_MS;
            if (s3_uart_tx_on()) ask_status();
        }
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// Self-test of a new image of this board

typedef enum { ST_OFF, ST_WAIT, ST_PASS, ST_FAIL } selftest_t;
static volatile selftest_t s_selftest = ST_OFF;
static const char *const s_selftest_names[] = {"off", "wait", "pass", "fail"};

const char *s3_ota_selftest_name(void) { return s_selftest_names[s_selftest]; }

static bool failtest_take(void)
{
    nvs_handle_t h;
    uint8_t v = 0;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return false;
    if (nvs_get_u8(h, NVS_FAILTEST, &v) == ESP_OK) {
        nvs_erase_key(h, NVS_FAILTEST);
        nvs_commit(h);
    }
    nvs_close(h);
    return v == 1;
}

static void selftest_task(void *arg)
{
    bool fail_on_purpose = failtest_take();
    uint32_t start = now_ms();
    s_selftest = ST_WAIT;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        s3_health_t h;
        s3_health(&h);
        bool lyrat_alive = h.uart_frames > 0;
        bool lyrat_silent = h.uart_rx_bytes == 0;
        bool i2s = h.i2s_call_ok && h.i2s_mic_ok && (!lyrat_alive || (h.call_frames > 0 && h.mic_frames > 0));
        bool ok = h.eth_ok && h.got_ip && h.uart_ok && i2s && (lyrat_alive || lyrat_silent);
        if (fail_on_purpose && (uint32_t)(now_ms() - start) > 5000) {
            ESP_LOGW(TAG, "self-test fails on purpose (failtest), back to the old image");
            ok = false;
            start -= SELFTEST_MS;
        }
        if (ok) {
            esp_ota_mark_app_valid_cancel_rollback();
            s_selftest = ST_PASS;
            ESP_LOGI(TAG, "self-test passed: image confirmed");
            vTaskDelete(NULL);
        }
        if ((uint32_t)(now_ms() - start) > SELFTEST_MS) {
            s_selftest = ST_FAIL;
            ESP_LOGE(TAG, "self-test failed: eth=%d ip=%d uart=%d i2s=%d/%d frames=%" PRIu32 "/%" PRIu32
                     " lyrat frames=%" PRIu32 " bytes=%" PRIu32 ", rolling back", h.eth_ok, h.got_ip, h.uart_ok,
                     h.i2s_call_ok, h.i2s_mic_ok, h.call_frames, h.mic_frames, h.uart_frames, h.uart_rx_bytes);
            vTaskDelay(pdMS_TO_TICKS(500));
            esp_ota_mark_app_invalid_rollback_and_reboot();
            vTaskDelete(NULL);
        }
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// UDP 5007

static const char *img_state_name(const esp_partition_t *p)
{
    esp_ota_img_states_t st;
    if (!p || esp_ota_get_state_partition(p, &st) != ESP_OK) return "undefined";
    switch (st) {
    case ESP_OTA_IMG_NEW: return "new";
    case ESP_OTA_IMG_PENDING_VERIFY: return "pending";
    case ESP_OTA_IMG_VALID: return "valid";
    case ESP_OTA_IMG_INVALID: return "invalid";
    case ESP_OTA_IMG_ABORTED: return "aborted";
    default: return "undefined";
    }
}

static void elf_hex(const uint8_t *sha, char out[17])
{
    for (int i = 0; i < 8; i++) sprintf(out + 2 * i, "%02x", sha[i]);
}

static int query_json(char *out, size_t cap)
{
    const esp_partition_t *run = esp_ota_get_running_partition();
    const esp_app_desc_t *desc = esp_app_get_description();
    char elf[17], inv_json[96] = "null";
    elf_hex(desc->app_elf_sha256, elf);
    const esp_partition_t *inv = esp_ota_get_last_invalid_partition();
    esp_app_desc_t inv_desc;
    if (inv && esp_ota_get_partition_description(inv, &inv_desc) == ESP_OK) {
        char inv_elf[17];
        elf_hex(inv_desc.app_elf_sha256, inv_elf);
        snprintf(inv_json, sizeof(inv_json), "{\"ver\":\"%.31s\",\"elf\":\"%s\"}", inv_desc.version, inv_elf);
    }
    int n = snprintf(out, cap,
        "{\"fw\":\"s3tap\",\"boot\":%u,\"up_ms\":%lld,\"ver\":\"%.31s\",\"elf\":\"%s\",\"run\":\"%s\",\"img\":\"%s\","
        "\"selftest\":\"%s\",\"last_invalid\":%s,\"token\":%d,\"rx\":[",
        s3_boot_id(), esp_timer_get_time() / 1000, desc->version, elf, run ? run->label : "?", img_state_name(run),
        s3_ota_selftest_name(), inv_json, OTA_TOKEN[0] ? 1 : 0);
    for (int t = 0; t < 2 && n > 0 && (size_t)n < cap; t++) {
        const ota_rx_t *rx = &s_rx[t];
        n += snprintf(out + n, cap - n, "%s{\"target\":%d,\"state\":\"%s\",\"size\":%lu,\"next\":%lu,\"err\":\"%s\"}",
                      t ? "," : "", t, ota_rx_state_name(rx->state), (unsigned long)rx->size, (unsigned long)rx->next,
                      ota_status_name(rx->last_err));
    }
    if (n > 0 && (size_t)n < cap) {
        n += snprintf(out + n, cap - n,
            "],\"lyrat\":{\"relay\":\"%s\",\"err\":\"%s\",\"tx\":\"%s\",\"sent\":%lu,\"acked\":%lu,\"size\":%lu,"
            "\"resent\":%lu,\"busy\":%lu,\"baud\":%d,\"elf_want\":\"%s\",\"status\":\"%s\",\"status_age_ms\":%ld}}",
            s_relay_names[s_relay], s_relay_err, ota_tx_state_name(s_tx.state), (unsigned long)s_tx.sent,
            (unsigned long)s_tx.acked, (unsigned long)s_tx.size, (unsigned long)s_tx.resent,
            (unsigned long)s_tx.busy_replies, s3_uart_get_baud(), s_stage_elf, s_lyrat_status,
            s_lyrat_status_ms ? (long)(now_ms() - s_lyrat_status_ms) : -1L);
    }
    return n;
}

static void udp_task(void *arg)
{
    int sock = (int)(intptr_t)arg;
    static uint8_t in[OTA_UDP_HDR + OTA_REQ_MAX + 8];
    static uint8_t out[OTA_UDP_HDR + 1400];
    int64_t reboot_at = 0;
    for (;;) {
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        int n = recvfrom(sock, in, sizeof(in), 0, (struct sockaddr *)&from, &from_len);
        uint32_t now = now_ms();
        if (s_stage_reset) {
            s_stage_reset = false;
            ota_rx_init(&s_rx[OTA_TARGET_LYRAT], &s_stage_port, IDLE_MS);
        }
        for (int t = 0; t < 2; t++) ota_rx_tick(&s_rx[t], now);
        if (reboot_at && esp_timer_get_time() >= reboot_at) esp_restart();
        if (n < OTA_UDP_HDR + 1 || in[0] != 'O' || in[1] != 'T' || in[2] != OTA_UDP_VERSION) continue;
        uint8_t target = in[3], op = in[4];
        memcpy(out, in, OTA_UDP_HDR);
        uint8_t *rep = out + OTA_UDP_HDR;
        size_t len = 0;
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
            int q = query_json((char *)rep + OTA_REPLY_HDR, sizeof(out) - OTA_UDP_HDR - OTA_REPLY_HDR);
            len = OTA_REPLY_HDR + (q > 0 ? (size_t)q : 0);
            if (len > sizeof(out) - OTA_UDP_HDR) len = sizeof(out) - OTA_UDP_HDR;
        } else if (target <= OTA_TARGET_LYRAT) {
            len = ota_rx_handle(&s_rx[target], in + OTA_UDP_HDR, (size_t)n - OTA_UDP_HDR, now, rep, OTA_REPLY_MAX);
            if (target == OTA_TARGET_LYRAT && op == OTA_OP_BEGIN && s_rx[1].state == OTA_RX_RECV) {
                s_stage_baud = s_rx[1].baud;
            }
            uint8_t ev = ota_rx_take_events(&s_rx[target]);
            if (target == OTA_TARGET_SELF && (ev & OTA_EV_DONE)) {
                reboot_at = esp_timer_get_time() + REBOOT_DELAY_MS * 1000LL;
            }
        } else {
            continue;
        }
        sendto(sock, out, OTA_UDP_HDR + len, 0, (struct sockaddr *)&from, from_len);
    }
}

void s3_ota_start(bool network)
{
    ota_rx_init(&s_rx[OTA_TARGET_SELF], &s_self_port, IDLE_MS);
    ota_rx_init(&s_rx[OTA_TARGET_LYRAT], &s_stage_port, IDLE_MS);
    ota_tx_init(&s_tx, &s_stage_reader);
    if (!OTA_TOKEN[0]) ESP_LOGW(TAG, "no main/ota_secrets.h: every update from the network is refused (auth)");

    const esp_partition_t *run = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    if (run && esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        xTaskCreatePinnedToCore(selftest_task, "selftest", 3072, NULL, 2, NULL, 0);
    }

    if (!network) return;
    s_replies = xQueueCreate(4, sizeof(reply_msg_t));
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in me = {.sin_family = AF_INET, .sin_port = htons(OTA_UDP_PORT), .sin_addr.s_addr = htonl(INADDR_ANY)};
    if (!s_replies || sock < 0 || bind(sock, (struct sockaddr *)&me, sizeof(me)) != 0) {
        ESP_LOGE(TAG, "udp %d not available", OTA_UDP_PORT);
        if (sock >= 0) close(sock);
        return;
    }
    struct timeval tv = {.tv_sec = 0, .tv_usec = 100 * 1000};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    xTaskCreatePinnedToCore(udp_task, "ota_udp", 6144, (void *)(intptr_t)sock, 4, NULL, 0);
    xTaskCreatePinnedToCore(relay_task, "ota_relay", 4096, NULL, 4, NULL, 0);
}
