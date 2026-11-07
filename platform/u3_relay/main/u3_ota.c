// OTA of U3 itself and of U1/U2 through it (goal 095, docs/PLATFORM_OTA.md). Based on s3_eth_bridge/main/s3_ota.c.
//
// UDP 5007: "OT", version, target, then an ota_link request.
//   target 0      U3's next app slot
//   target 1, 2   U1 / U2 app: staged in u1_img / u2_img, checked, then sent over the node's control UART with the
//                 same protocol (relay task); U3 follows the node until it runs the image or rolled back
//   target 0x11, 0x12   ROM bundle for U1 / U2 (rom_bundle.h): staged, checked, then written through the node's ROM
//                 loader over UART0 with EN and IO0 (rom task), every segment checked by the ROM's MD5
// One staging partition per node: an app transfer and a ROM bundle of the same node exclude each other.
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
#include "plat_proto.h"
#include "rom_bundle.h"
#include "rom_flasher.h"
#include "u3_link.h"

#if __has_include("plat_secrets.h")
#include "plat_secrets.h"
#endif
#ifndef OTA_TOKEN
#define OTA_TOKEN ""
#endif

static const char *TAG = "u3ota";

#define IDLE_MS             30000
#define NONCE_LIFE_MS       60000
#define REBOOT_DELAY_MS     500
#define SELFTEST_MS         90000
#define RELAY_WAIT_TX_MS    30000
#define VERIFY_MS           180000
#define STATUS_EVERY_MS     10000
#define VERIFY_POLL_MS      2000
#define BAUD_PROBE_MS       3000
#define ROM_COME_UP_MS      60000       // after a ROM job: how long to wait for the node's first frame
#define NVS_NS              "ota"
#define NVS_FAILTEST        "failtest"
#define NODE_NVS_FROM       0x9000u     // nvs of U1/U2: pairings and calibration, never in a ROM bundle
#define NODE_NVS_TO         0xF000u
#define NODE_APP_AT         0x20000u    // ota_0 of U1/U2
#define NODE_BOOT_AT        0x1000u

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

static void hex8(const uint8_t *sha, char out[17])
{
    for (int i = 0; i < 8; i++) sprintf(out + 2 * i, "%02x", sha[i]);
}

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
// Nodes

typedef enum { RELAY_IDLE, RELAY_SEND, RELAY_VERIFY, RELAY_DONE, RELAY_ROLLED_BACK, RELAY_FAILED } relay_state_t;
static const char *const s_relay_names[] = {"idle", "send", "verify", "done", "rolled_back", "failed"};
typedef enum { ROMJ_IDLE, ROMJ_RUN, ROMJ_COME_UP, ROMJ_DONE, ROMJ_NO_LINK, ROMJ_FAILED } rom_job_t;
static const char *const s_romj_names[] = {"idle", "run", "come_up", "done", "no_link", "failed"};

typedef struct {
    int node;
    const char *project, *part_label;
    const esp_partition_t *stage;
    ota_rx_t rx_app, rx_rom;
    volatile bool stage_reset;
    // app relay
    volatile bool staged;
    uint32_t size, baud;
    uint8_t sha[OTA_SHA256_BYTES], flags;
    char elf[17];
    volatile relay_state_t relay;
    char err[24];
    ota_tx_t tx;
    QueueHandle_t replies;
    char status[OTA_REPLY_MAX];
    uint32_t status_ms, tx_wait_since, next_status, verify_until, baud_since;
    bool baud_fallback;
    // ROM job
    volatile bool rom_staged;
    uint32_t rom_size, rom_baud;
    uint8_t rom_flags;
    rom_bundle_t bundle;
    rom_flasher_t rom;
    volatile rom_job_t rom_job;
    char rom_err[24];
    uint32_t rom_ms;
} node_ota_t;

static node_ota_t s_nodes[U3_NODES] = {
    {.node = 0, .project = PLAT_PROJECT_U1, .part_label = "u1_img"},
    {.node = 1, .project = PLAT_PROJECT_U2, .part_label = "u2_img"},
};

static bool node_busy(const node_ota_t *n)
{
    return n->staged || n->relay == RELAY_SEND || n->relay == RELAY_VERIFY || n->rom_staged ||
           n->rom_job == ROMJ_RUN || n->rx_app.state == OTA_RX_RECV || n->rx_rom.state == OTA_RX_RECV;
}

static bool any_node_busy(void)
{
    for (int i = 0; i < U3_NODES; i++) {
        if (node_busy(&s_nodes[i])) return true;
    }
    return false;
}

bool u3_ota_rom_running(int node)
{
    return node >= 0 && node < U3_NODES && (s_nodes[node].rom_job == ROMJ_RUN || s_nodes[node].rom_staged);
}

// ---------------------------------------------------------------------------------------------------------------------
// Target 0: this board's next app slot

static const esp_partition_t *s_next;
static esp_ota_handle_t s_ota;
static bool s_ota_open;

static uint32_t self_capacity(void *ctx)
{
    s_next = esp_ota_get_next_update_partition(NULL);
    return s_next ? (uint32_t)s_next->size : 0;
}

static int self_may_begin(void *ctx, uint8_t flags)
{
    return any_node_busy() ? OTA_ST_REFUSED : OTA_ST_OK;    // a restart now would cut a node's transfer
}

static int self_check_head(void *ctx, const uint8_t *head, size_t len)
{
    return ota_image_check(head, len, OTA_CHIP_ESP32S3, PLAT_PROJECT_U3);
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
    esp_err_t err = esp_ota_end(s_ota);
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
// Staging, shared by the app and the ROM receivers of a node

static uint32_t stage_room(node_ota_t *n)
{
    if (!n->stage) n->stage = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x40, n->part_label);
    if (!n->stage) return 0;
    return n->stage->size < PLAT_STAGE_SIZE ? (uint32_t)n->stage->size : PLAT_STAGE_SIZE;
}

static int stage_erase(node_ota_t *n, uint32_t size)
{
    uint32_t span = (size + 4095u) & ~4095u;
    esp_err_t err = esp_partition_erase_range(n->stage, 0, span);
    if (err != ESP_OK) ESP_LOGE(TAG, "%s erase: %s", n->part_label, esp_err_to_name(err));
    return err == ESP_OK ? 0 : 1;
}

static int stage_write(void *ctx, uint32_t offset, const uint8_t *data, size_t len)
{
    node_ota_t *n = (node_ota_t *)ctx;
    return esp_partition_write(n->stage, offset, data, len) == ESP_OK ? 0 : 1;
}

static int stage_read(void *ctx, uint32_t offset, uint8_t *buf, size_t len)
{
    node_ota_t *n = (node_ota_t *)ctx;
    return esp_partition_read(n->stage, offset, buf, len) == ESP_OK ? 0 : 1;
}

// The received bytes matched already; this proves what the flash holds, which is what the relay will read.
static int stage_readback(node_ota_t *n, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES])
{
    static uint8_t block[1024];         // the UDP task is the only caller
    ota_sha256_t h;
    ota_sha256_start(&h);
    for (uint32_t at = 0; at < size; at += sizeof(block)) {
        uint32_t k = size - at < sizeof(block) ? size - at : sizeof(block);
        if (esp_partition_read(n->stage, at, block, k) != ESP_OK) return OTA_ST_FLASH;
        ota_sha256_update(&h, block, k);
    }
    uint8_t got[OTA_SHA256_BYTES];
    ota_sha256_finish(&h, got);
    if (memcmp(got, sha, sizeof(got)) != 0) {
        ESP_LOGE(TAG, "%s reads back different", n->part_label);
        return OTA_ST_FLASH;
    }
    return OTA_ST_OK;
}

// --- app of a node --------------------------------------------------------------------------------------------------

static uint32_t app_capacity(void *ctx)
{
    uint32_t room = stage_room((node_ota_t *)ctx);
    return room < PLAT_NODE_SLOT ? room : PLAT_NODE_SLOT;
}

// A relay or a ROM job in progress, or the other receiver of this node open: one thing at a time per staging
// partition. The receiver's own open session does not count: a new sender takes it over.
static bool jobs_busy(const node_ota_t *n)
{
    return n->staged || n->relay == RELAY_SEND || n->relay == RELAY_VERIFY || n->rom_staged || n->rom_job == ROMJ_RUN;
}

static int app_may_begin(void *ctx, uint8_t flags)
{
    node_ota_t *n = (node_ota_t *)ctx;
    return jobs_busy(n) || n->rx_rom.state == OTA_RX_RECV ? OTA_ST_REFUSED : OTA_ST_OK;
}

static int rom_may_begin(void *ctx, uint8_t flags)
{
    node_ota_t *n = (node_ota_t *)ctx;
    return jobs_busy(n) || n->rx_app.state == OTA_RX_RECV ? OTA_ST_REFUSED : OTA_ST_OK;
}

static int app_check_head(void *ctx, const uint8_t *head, size_t len)
{
    node_ota_t *n = (node_ota_t *)ctx;
    int st = ota_image_check(head, len, OTA_CHIP_ESP32, n->project);
    if (st == OTA_ST_OK) hex8(ota_image_elf_sha(head, len), n->elf);
    return st;
}

static int app_begin(void *ctx, uint32_t size, uint8_t flags) { return stage_erase((node_ota_t *)ctx, size); }

static int app_finish(void *ctx, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags)
{
    node_ota_t *n = (node_ota_t *)ctx;
    int st = stage_readback(n, size, sha);
    if (st != OTA_ST_OK) return st;
    n->size = size;
    memcpy(n->sha, sha, sizeof(n->sha));
    n->flags = flags;
    n->staged = true;
    ESP_LOGI(TAG, "%s image staged, %" PRIu32 " bytes, elf %s", u3_node_name(n->node), size, n->elf);
    return OTA_ST_OK;
}

// --- ROM bundle of a node -------------------------------------------------------------------------------------------

static uint32_t rom_capacity(void *ctx) { return stage_room((node_ota_t *)ctx); }

static int rom_check_head(void *ctx, const uint8_t *head, size_t len)
{
    node_ota_t *n = (node_ota_t *)ctx;
    rom_bundle_t b;
    int e = rom_bundle_parse(head, len, n->rom_size, (uint8_t)OTA_CHIP_ESP32, NODE_NVS_FROM, NODE_NVS_TO, &b);
    if (e != ROM_BUNDLE_OK) {
        ESP_LOGE(TAG, "%s bundle: %s", u3_node_name(n->node), rom_bundle_err_name(e));
        return OTA_ST_BAD_IMAGE;
    }
    return OTA_ST_OK;
}

static int rom_begin(void *ctx, uint32_t size, uint8_t flags)
{
    node_ota_t *n = (node_ota_t *)ctx;
    n->rom_size = size;
    return stage_erase(n, size);
}

// The whole bundle is in the flash: parse it again from there, and look into the app and the bootloader it carries.
static int rom_finish(void *ctx, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags)
{
    node_ota_t *n = (node_ota_t *)ctx;
    int st = stage_readback(n, size, sha);
    if (st != OTA_ST_OK) return st;
    static uint8_t head[ROM_BUNDLE_HEAD];
    if (esp_partition_read(n->stage, 0, head, sizeof(head)) != ESP_OK) return OTA_ST_FLASH;
    if (rom_bundle_parse(head, sizeof(head), size, (uint8_t)OTA_CHIP_ESP32, NODE_NVS_FROM, NODE_NVS_TO, &n->bundle)) {
        return OTA_ST_BAD_IMAGE;
    }
    for (uint8_t i = 0; i < n->bundle.count; i++) {
        const rom_segment_t *s = &n->bundle.seg[i];
        if (s->offset != NODE_APP_AT && s->offset != NODE_BOOT_AT) continue;
        static uint8_t img[OTA_HEAD_MIN];
        if (s->size < sizeof(img) || esp_partition_read(n->stage, s->data_at, img, sizeof(img)) != ESP_OK) {
            return OTA_ST_BAD_IMAGE;
        }
        // The app must be this node's project; the bootloader at least an ESP32 image.
        bool ok = s->offset == NODE_APP_AT ? ota_image_check(img, sizeof(img), OTA_CHIP_ESP32, n->project) == OTA_ST_OK
                                           : img[0] == 0xE9 && img[12] == (OTA_CHIP_ESP32 & 0xFF) && img[13] == 0;
        if (!ok) {
            ESP_LOGE(TAG, "%s bundle: segment at 0x%" PRIx32 " is not for this node", u3_node_name(n->node), s->offset);
            return OTA_ST_BAD_IMAGE;
        }
        if (s->offset == NODE_APP_AT) hex8(ota_image_elf_sha(img, sizeof(img)), n->elf);
    }
    if (flags & OTA_F_DRYRUN) {
        ESP_LOGI(TAG, "%s bundle good, dry run: not written", u3_node_name(n->node));
        return OTA_ST_OK;
    }
    n->rom_flags = flags;
    n->rom_staged = true;
    ESP_LOGW(TAG, "%s bundle staged: %u segments, %" PRIu32 " bytes, through the ROM loader next",
             u3_node_name(n->node), n->bundle.count, size);
    return OTA_ST_OK;
}

#define NODE_PORTS(i) \
    {.ctx = &s_nodes[i], .capacity = app_capacity, .may_begin = app_may_begin, .check_head = app_check_head, \
     .auth = port_auth, .begin = app_begin, .write = stage_write, .finish = app_finish}, \
    {.ctx = &s_nodes[i], .capacity = rom_capacity, .may_begin = rom_may_begin, .check_head = rom_check_head, \
     .auth = port_auth, .begin = rom_begin, .write = stage_write, .finish = rom_finish}

static const ota_rx_port_t s_node_ports[U3_NODES][2] = {{NODE_PORTS(0)}, {NODE_PORTS(1)}};
static ota_rx_t s_rx_self;

// ---------------------------------------------------------------------------------------------------------------------
// Relay of an app image to a node over its control UART (the S3 -> LyraT relay of goal 090, per node)

typedef struct {
    uint16_t len;
    uint8_t data[OTA_REPLY_MAX];
} reply_msg_t;

static const ota_tx_port_t s_readers[U3_NODES] = {{.ctx = &s_nodes[0], .read = stage_read},
                                                  {.ctx = &s_nodes[1], .read = stage_read}};

void u3_ota_on_reply(int node, const uint8_t *payload, size_t len)
{
    if (node < 0 || node >= U3_NODES || !s_nodes[node].replies || len < OTA_REPLY_HDR) return;
    reply_msg_t m;
    m.len = (uint16_t)(len < sizeof(m.data) ? len : sizeof(m.data));
    memcpy(m.data, payload, m.len);
    xQueueSend(s_nodes[node].replies, &m, 0);
}

static void status_field(const char *text, const char *key, char *out, size_t cap)
{
    out[0] = 0;
    const char *p = strstr(text, key);
    if (!p) return;
    p += strlen(key);
    size_t k = 0;
    while (p[k] && p[k] != ' ' && k + 1 < cap) {
        out[k] = p[k];
        k++;
    }
    out[k] = 0;
}

static void relay_fail(node_ota_t *n, const char *why)
{
    snprintf(n->err, sizeof(n->err), "%s", why);
    n->relay = RELAY_FAILED;
    ESP_LOGE(TAG, "%s update failed: %s", u3_node_name(n->node), why);
}

static void relay_end(node_ota_t *n)
{
    if (u3_node_get_baud(n->node) != CTL_BAUD_DEFAULT) u3_node_set_baud(n->node, CTL_BAUD_DEFAULT);
    n->stage_reset = true;          // the UDP task owns the receivers: it re-arms them itself
}

static void ask_status(node_ota_t *n)
{
    uint8_t req[5];
    u3_node_send(n->node, CTL_T_OTA, req, ota_build_short(req, OTA_OP_STATUS, 0));
}

static void take_status(node_ota_t *n, const reply_msg_t *m)
{
    size_t k = m->len - OTA_REPLY_HDR;
    for (size_t i = 0; i < k; i++) {        // it goes into JSON as it is
        char c = (char)m->data[OTA_REPLY_HDR + i];
        n->status[i] = (c == '"' || c == '\\' || c < 0x20 || c > 0x7E) ? '_' : c;
    }
    n->status[k] = 0;
    n->status_ms = now_ms();
}

static void start_verify(node_ota_t *n, uint32_t now)
{
    n->relay = RELAY_VERIFY;
    n->verify_until = now + VERIFY_MS;
    n->next_status = now + VERIFY_POLL_MS;
    n->status[0] = 0;
}

static void relay_step(node_ota_t *n, uint32_t now)
{
    static uint8_t out[OTA_REQ_MAX];
    int node = n->node;
    reply_msg_t m;
    while (xQueueReceive(n->replies, &m, 0) == pdTRUE) {
        if (m.data[0] == OTA_OP_STATUS) {
            take_status(n, &m);
            continue;
        }
        if (n->relay != RELAY_SEND) continue;
        if (m.data[0] == OTA_OP_BEGIN && m.data[1] == OTA_ST_OK && n->tx.state == OTA_TX_BEGIN && n->tx.baud &&
            (int)n->tx.baud != u3_node_get_baud(node)) {
            u3_node_set_baud(node, (int)n->tx.baud);   // the node switched after its reply
            n->baud_since = now;
        }
        ota_tx_on_reply(&n->tx, m.data, m.len, now);
    }

    if (n->staged && n->relay != RELAY_SEND && n->relay != RELAY_VERIFY) {
        n->staged = false;
        ota_tx_init(&n->tx, &s_readers[node]);
        n->tx.window = 2;
        ota_tx_start(&n->tx, esp_random(), n->size, n->sha, n->flags, n->baud, now);
        n->err[0] = 0;
        n->baud_fallback = false;
        n->baud_since = 0;
        n->tx_wait_since = now;
        n->relay = RELAY_SEND;
        ESP_LOGI(TAG, "sending the image to %s, baud %" PRIu32, u3_node_name(node), n->baud ? n->baud : CTL_BAUD_DEFAULT);
    }

    if (n->relay == RELAY_SEND) {
        if (n->baud_since && n->tx.acked == 0 && (uint32_t)(now - n->baud_since) > BAUD_PROBE_MS && !n->baud_fallback) {
            ESP_LOGW(TAG, "%s: no answer at %" PRIu32 " baud, again at %d", u3_node_name(node), n->tx.baud,
                     CTL_BAUD_DEFAULT);
            u3_node_set_baud(node, CTL_BAUD_DEFAULT);
            n->baud_fallback = true;
            n->baud_since = 0;
            ota_tx_start(&n->tx, n->tx.session + 1, n->size, n->sha, n->flags, 0, now);
        }
        if (!u3_node_tx_on(node)) {
            if ((uint32_t)(now - n->tx_wait_since) > RELAY_WAIT_TX_MS) {
                relay_fail(n, "node_silent");
                relay_end(n);
            }
            return;
        }
        n->tx_wait_since = now;
        size_t k;
        while ((k = ota_tx_poll(&n->tx, now, out, sizeof(out))) > 0) {
            if (!u3_node_send(node, CTL_T_OTA, out, k)) break;
        }
        if (n->tx.state == OTA_TX_DONE) {
            if (u3_node_get_baud(node) != CTL_BAUD_DEFAULT) u3_node_set_baud(node, CTL_BAUD_DEFAULT);
            if (n->flags & OTA_F_DRYRUN) {
                n->relay = RELAY_DONE;
                ESP_LOGI(TAG, "%s dry run: image good, not activated", u3_node_name(node));
                relay_end(n);
            } else {
                start_verify(n, now);
                ESP_LOGI(TAG, "%s took the image, waiting for it to run it", u3_node_name(node));
            }
        } else if (n->tx.state == OTA_TX_FAILED) {
            if (u3_node_get_baud(node) != CTL_BAUD_DEFAULT) u3_node_set_baud(node, CTL_BAUD_DEFAULT);
            // A lost answer to END may hide a node that already restarted with the image: look before failing.
            bool maybe = (n->tx.fail_status == OTA_ST_TIMEOUT || n->tx.fail_status == OTA_ST_NO_SESSION) &&
                         n->tx.acked == n->tx.size && !(n->flags & OTA_F_DRYRUN);
            if (maybe) {
                start_verify(n, now);
            } else {
                relay_fail(n, ota_status_name(n->tx.fail_status));
                relay_end(n);
            }
        }
        return;
    }

    if (n->relay == RELAY_VERIFY) {
        char elf[17], img[16], inv[17];
        status_field(n->status, "elf=", elf, sizeof(elf));
        status_field(n->status, "img=", img, sizeof(img));
        status_field(n->status, "inv=", inv, sizeof(inv));
        if (n->status[0] && strcmp(elf, n->elf) == 0 && strcmp(img, "valid") == 0) {
            n->relay = RELAY_DONE;
            ESP_LOGI(TAG, "%s runs the new image, confirmed", u3_node_name(node));
            relay_end(n);
        } else if (n->status[0] && strcmp(elf, n->elf) != 0 && strcmp(inv, n->elf) == 0) {
            n->relay = RELAY_ROLLED_BACK;
            ESP_LOGW(TAG, "%s rolled back to %s", u3_node_name(node), elf);
            relay_end(n);
        } else if (n->status[0] && strstr(n->status, "state=done")) {
            n->verify_until = now + VERIFY_MS;      // image taken, the restart waits for the end of a call
            if ((int32_t)(now - n->next_status) >= 0) {
                n->next_status = now + VERIFY_POLL_MS;
                n->status[0] = 0;
                if (u3_node_tx_on(node)) ask_status(n);
            }
        } else if ((int32_t)(now - n->verify_until) > 0) {
            relay_fail(n, "verify_timeout");
            relay_end(n);
        } else if ((int32_t)(now - n->next_status) >= 0) {
            n->next_status = now + VERIFY_POLL_MS;
            n->status[0] = 0;
            if (u3_node_tx_on(node)) ask_status(n);
        }
        return;
    }

    if ((int32_t)(now - n->next_status) >= 0 && n->rom_job != ROMJ_RUN) {   // keep the node's version at hand
        n->next_status = now + STATUS_EVERY_MS;
        if (u3_node_tx_on(node)) ask_status(n);
    }
}

static void relay_task(void *arg)
{
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(5));
        uint32_t now = now_ms();
        for (int i = 0; i < U3_NODES; i++) relay_step(&s_nodes[i], now);
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// ROM jobs: one at a time, UART0 is shared

static void rom_task(void *arg)
{
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(100));
        for (int i = 0; i < U3_NODES; i++) {
            node_ota_t *n = &s_nodes[i];
            if (n->rom_job == ROMJ_COME_UP) {
                if (u3_node_alive(i)) {
                    n->rom_job = ROMJ_DONE;
                    ESP_LOGI(TAG, "%s talks again after the ROM job", u3_node_name(i));
                    n->stage_reset = true;
                } else if ((uint32_t)(now_ms() - n->rom_ms) > ROM_COME_UP_MS) {
                    n->rom_job = ROMJ_NO_LINK;     // written and checked, but its app does not answer
                    ESP_LOGW(TAG, "%s written, no frame from it within %d s", u3_node_name(i), ROM_COME_UP_MS / 1000);
                    n->stage_reset = true;
                }
                continue;
            }
            if (!n->rom_staged) continue;
            n->rom_job = ROMJ_RUN;
            n->rom_err[0] = 0;
            u3_node_hold(i, true);          // the node is about to be in the ROM: no frames to it
            const rom_port_t *port = u3_port_rom_take(i, stage_read, n);
            int r = ROM_E_IO;
            if (port) {
                rom_init(&n->rom, port);
                n->rom.baud_high = n->rom_baud;
                ESP_LOGW(TAG, "%s: ROM loader, %u segments, %" PRIu32 " bytes, baud %" PRIu32, u3_node_name(i),
                         n->bundle.count, n->bundle.total, n->rom_baud ? n->rom_baud : ROM_BAUD_DEFAULT);
                r = rom_flash_bundle(&n->rom, &n->bundle);
                u3_port_rom_release();
            }
            u3_node_hold(i, false);
            n->rom_staged = false;
            n->rom_ms = now_ms();
            if (r == ROM_OK) {
                n->rom_job = ROMJ_COME_UP;
                ESP_LOGI(TAG, "%s: ROM job done in %" PRIu32 " ms, %" PRIu32 " blocks, restarts %" PRIu32 "/%" PRIu32,
                         u3_node_name(i), n->rom.ms, n->rom.blocks, n->rom.session_restarts, n->rom.segment_restarts);
            } else {
                snprintf(n->rom_err, sizeof(n->rom_err), "%s", rom_err_name(r));
                n->rom_job = ROMJ_FAILED;
                ESP_LOGE(TAG, "%s: ROM job failed: %s (rom status %u error %u, phase %s)", u3_node_name(i),
                         rom_err_name(r), n->rom.rom_status, n->rom.rom_error, rom_phase_name(n->rom.phase));
                n->stage_reset = true;
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// Self-test of a new image of this board

typedef enum { ST_OFF, ST_WAIT, ST_PASS, ST_FAIL } selftest_t;
static volatile selftest_t s_selftest = ST_OFF;
static const char *const s_selftest_names[] = {"off", "wait", "pass", "fail"};

const char *u3_ota_selftest_name(void) { return s_selftest_names[s_selftest]; }

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

// Wi-Fi with an address, both control UARTs installed, and each node alive (a valid frame) or silent (no byte at all,
// e.g. U2 not fitted yet). Bytes without a valid frame fail: a wrong baud or wrong wiring.
static void selftest_task(void *arg)
{
    bool fail_on_purpose = failtest_take();
    uint32_t start = now_ms();
    s_selftest = ST_WAIT;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        u3_health_t h;
        u3_health(&h);
        bool nodes = true;
        for (int i = 0; i < U3_NODES; i++) nodes &= h.uart_ok[i] && (h.frames[i] > 0 || h.rx_bytes[i] == 0);
        bool ok = h.got_ip && nodes;
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
            ESP_LOGE(TAG, "self-test failed: ip=%d u1 uart=%d frames=%" PRIu32 " bytes=%" PRIu32 " u2 uart=%d frames=%"
                     PRIu32 " bytes=%" PRIu32 ", rolling back", h.got_ip, h.uart_ok[0], h.frames[0], h.rx_bytes[0],
                     h.uart_ok[1], h.frames[1], h.rx_bytes[1]);
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

static int rx_json(char *out, size_t cap, int target, const ota_rx_t *rx)
{
    return snprintf(out, cap, "{\"target\":%d,\"state\":\"%s\",\"size\":%lu,\"next\":%lu,\"err\":\"%s\"}", target,
                    ota_rx_state_name(rx->state), (unsigned long)rx->size, (unsigned long)rx->next,
                    ota_status_name(rx->last_err));
}

#define ADD(...) do { if (n > 0 && (size_t)n < cap) n += snprintf(out + n, cap - n, __VA_ARGS__); } while (0)

// QUERY to target 0: U3 and a line per node. To target 1/2 (or 0x11/0x12): the node in detail.
static int query_json(int target, char *out, size_t cap)
{
    int n = 0;
    if (target == PLAT_TARGET_U3) {
        const esp_partition_t *run = esp_ota_get_running_partition();
        const esp_app_desc_t *desc = esp_app_get_description();
        char elf[17], inv_json[96] = "null";
        hex8(desc->app_elf_sha256, elf);
        const esp_partition_t *inv = esp_ota_get_last_invalid_partition();
        esp_app_desc_t inv_desc;
        if (inv && esp_ota_get_partition_description(inv, &inv_desc) == ESP_OK) {
            char inv_elf[17];
            hex8(inv_desc.app_elf_sha256, inv_elf);
            snprintf(inv_json, sizeof(inv_json), "{\"ver\":\"%.31s\",\"elf\":\"%s\"}", inv_desc.version, inv_elf);
        }
        u3_health_t h;
        u3_health(&h);
        n = snprintf(out, cap,
            "{\"fw\":\"u3relay\",\"boot\":%u,\"up_ms\":%lld,\"ver\":\"%.31s\",\"elf\":\"%s\",\"run\":\"%s\",\"img\":\"%s\","
            "\"selftest\":\"%s\",\"last_invalid\":%s,\"token\":%d,\"ip\":\"%s\",\"rssi\":%d,\"console\":\"%s\",\"rx\":",
            u3_boot_id(), esp_timer_get_time() / 1000, desc->version, elf, run ? run->label : "?", img_state_name(run),
            u3_ota_selftest_name(), inv_json, OTA_TOKEN[0] ? 1 : 0, h.ip, h.rssi, u3_port_mode_name(u3_port_mode()));
        if (n > 0 && (size_t)n < cap) n += rx_json(out + n, cap - n, 0, &s_rx_self);
        for (int i = 0; i < U3_NODES; i++) {
            node_ota_t *x = &s_nodes[i];
            ADD(",\"%s\":{\"alive\":%d,\"relay\":\"%s\",\"rom\":\"%s\"}", u3_node_name(i), u3_node_alive(i),
                s_relay_names[x->relay], s_romj_names[x->rom_job]);
        }
        ADD("}");
        return n;
    }
    int i = (target & 0x0F) - 1;
    if (i < 0 || i >= U3_NODES) return 0;
    node_ota_t *x = &s_nodes[i];
    n = snprintf(out, cap, "{\"node\":\"%s\",\"link\":", u3_node_name(i));
    if (n > 0 && (size_t)n < cap) n += (int)u3_node_json(i, out + n, cap - n);
    ADD(",\"rx\":[");
    if (n > 0 && (size_t)n < cap) n += rx_json(out + n, cap - n, i + 1, &x->rx_app);
    ADD(",");
    if (n > 0 && (size_t)n < cap) n += rx_json(out + n, cap - n, PLAT_TARGET_ROM | (i + 1), &x->rx_rom);
    ADD("],\"relay\":\"%s\",\"err\":\"%s\",\"tx\":\"%s\",\"sent\":%lu,\"acked\":%lu,\"size\":%lu,\"resent\":%lu,"
        "\"busy\":%lu,\"baud\":%d,\"elf_want\":\"%s\",\"status\":\"%s\",\"status_age_ms\":%ld,", s_relay_names[x->relay],
        x->err, ota_tx_state_name(x->tx.state), (unsigned long)x->tx.sent, (unsigned long)x->tx.acked,
        (unsigned long)x->tx.size, (unsigned long)x->tx.resent, (unsigned long)x->tx.busy_replies,
        u3_node_get_baud(i), x->elf, x->status, x->status_ms ? (long)(now_ms() - x->status_ms) : -1L);
    ADD("\"rom\":{\"job\":\"%s\",\"phase\":\"%s\",\"err\":\"%s\",\"seg\":%lu,\"done\":%lu,\"total\":%lu,\"syncs\":%lu,"
        "\"session_restarts\":%lu,\"segment_restarts\":%lu,\"blocks\":%lu,\"baud\":%lu,\"rom_status\":%u,"
        "\"rom_error\":%u,\"ms\":%lu}}", s_romj_names[x->rom_job], rom_phase_name(x->rom.phase), x->rom_err,
        (unsigned long)x->rom.seg, (unsigned long)x->rom.done, (unsigned long)x->rom.total,
        (unsigned long)x->rom.syncs, (unsigned long)x->rom.session_restarts, (unsigned long)x->rom.segment_restarts,
        (unsigned long)x->rom.blocks, (unsigned long)x->rom.baud, x->rom.rom_status, x->rom.rom_error,
        (unsigned long)x->rom.ms);
    return n;
}

static ota_rx_t *rx_for(int target)
{
    if (target == PLAT_TARGET_U3) return &s_rx_self;
    int i = (target & 0x0F) - 1;
    if (i < 0 || i >= U3_NODES || (target & ~(PLAT_TARGET_ROM | 0x0F))) return NULL;
    return target & PLAT_TARGET_ROM ? &s_nodes[i].rx_rom : &s_nodes[i].rx_app;
}

static void arm_node(int i)
{
    node_ota_t *x = &s_nodes[i];
    ota_rx_init(&x->rx_app, &s_node_ports[i][0], IDLE_MS);
    ota_rx_init(&x->rx_rom, &s_node_ports[i][1], IDLE_MS);
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
        for (int i = 0; i < U3_NODES; i++) {
            if (s_nodes[i].stage_reset) {
                s_nodes[i].stage_reset = false;
                arm_node(i);
            }
            ota_rx_tick(&s_nodes[i].rx_app, now);
            ota_rx_tick(&s_nodes[i].rx_rom, now);
        }
        ota_rx_tick(&s_rx_self, now);
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
            int q = query_json(target, (char *)rep + OTA_REPLY_HDR, sizeof(out) - OTA_UDP_HDR - OTA_REPLY_HDR);
            len = OTA_REPLY_HDR + (q > 0 ? (size_t)q : 0);
            if (len > sizeof(out) - OTA_UDP_HDR) len = sizeof(out) - OTA_UDP_HDR - 1;
        } else {
            ota_rx_t *rx = rx_for(target);
            if (!rx) continue;
            len = ota_rx_handle(rx, in + OTA_UDP_HDR, (size_t)n - OTA_UDP_HDR, now, rep, OTA_REPLY_MAX);
            if (op == OTA_OP_BEGIN && rx->state == OTA_RX_RECV && target != PLAT_TARGET_U3) {
                node_ota_t *x = &s_nodes[(target & 0x0F) - 1];
                if (target & PLAT_TARGET_ROM) x->rom_baud = rx->baud;
                else x->baud = rx->baud;
            }
            uint8_t ev = ota_rx_take_events(rx);
            if (target == PLAT_TARGET_U3 && (ev & OTA_EV_DONE)) {
                reboot_at = esp_timer_get_time() + REBOOT_DELAY_MS * 1000LL;
            }
        }
        sendto(sock, out, OTA_UDP_HDR + len, 0, (struct sockaddr *)&from, from_len);
    }
}

void u3_ota_start(bool network)
{
    ota_rx_init(&s_rx_self, &s_self_port, IDLE_MS);
    for (int i = 0; i < U3_NODES; i++) {
        arm_node(i);
        ota_tx_init(&s_nodes[i].tx, &s_readers[i]);
    }
    if (!OTA_TOKEN[0]) ESP_LOGW(TAG, "no OTA_TOKEN in main/plat_secrets.h: every update from the network is refused");

    const esp_partition_t *run = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    if (run && esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        xTaskCreatePinnedToCore(selftest_task, "selftest", 3072, NULL, 2, NULL, 0);
    }
    if (!network) return;
    for (int i = 0; i < U3_NODES; i++) s_nodes[i].replies = xQueueCreate(4, sizeof(reply_msg_t));
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in me = {.sin_family = AF_INET, .sin_port = htons(OTA_UDP_PORT), .sin_addr.s_addr = htonl(INADDR_ANY)};
    if (!s_nodes[0].replies || !s_nodes[1].replies || sock < 0 || bind(sock, (struct sockaddr *)&me, sizeof(me)) != 0) {
        ESP_LOGE(TAG, "udp %d not available", OTA_UDP_PORT);
        if (sock >= 0) close(sock);
        return;
    }
    struct timeval tv = {.tv_sec = 0, .tv_usec = 100 * 1000};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    xTaskCreatePinnedToCore(udp_task, "ota_udp", 6144, (void *)(intptr_t)sock, 4, NULL, 0);
    xTaskCreatePinnedToCore(relay_task, "ota_relay", 4096, NULL, 4, NULL, 0);
    xTaskCreatePinnedToCore(rom_task, "ota_rom", 4096, NULL, 3, NULL, 1);
}
