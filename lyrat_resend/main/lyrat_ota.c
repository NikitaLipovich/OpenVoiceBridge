// New firmware for the LyraT over the control UART, from the S3 (docs/OTA.md).
//
// Goal 096: the receiver runs in the updater (factory) only. The headset firmware in ota_0 has no slot to write (no
// second copy), it calls lyrat_ota_set_app(): every BEGIN is no_slot, its self-test runs at every start and decides
// through the NVS marks of upd_mode.h whether the updater starts it again. The text below describes the receiver.
//
// Frames CTL_T_OTA carry ota_link requests; the control UART task only queues them, this task erases and writes the
// flash and answers with CTL_T_OTA_ACK. It runs below the audio tasks and below the UART task, so the heartbeat goes on
// while a slot is erased (seconds) and the S3 keeps driving the line. The rules of a call: no start with SLC up or SCO
// on unless FORCE, no writes while SCO is on, no restart during a call. A new image confirms itself only after its
// self-test: Bluetooth up and a frame from the S3; otherwise the bootloader brings the old image back.
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "ctl_frame.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lyrat_link.h"
#include "nvs.h"
#include "ota_link.h"
#include "upd_mode.h"

static const char *TAG = "ota";

#define IDLE_MS          30000
#define QUEUE_DEPTH      3          // the window of the S3 is 2 chunks, plus a repeated BEGIN
#define BAUD_PROBE_MS    3000
#define SELFTEST_MS      90000
#define REBOOT_DELAY_MS  300
#define PROJECT          "lyrat_resend"
#define NVS_NS           "ota"
#define NVS_FAILTEST     "failtest"

typedef struct {
    uint16_t len;
    uint8_t data[OTA_REQ_MAX];
} req_msg_t;

static QueueHandle_t s_queue;
static ota_rx_t s_rx;
static const esp_partition_t *s_next;
static esp_ota_handle_t s_handle;
static bool s_open;
static uint32_t s_dropped;          // requests the queue had no room for: the S3 sends them again
static volatile bool s_reboot_pending;
static bool s_app;                  // the headset firmware in ota_0: no receiver, self-test at every start (goal 096)

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

// --- port of the receiver -------------------------------------------------------------------------------------------

static uint32_t port_capacity(void *ctx)
{
    if (s_app) return 0;                             // no_slot: images go to the updater (ota.py restarts into it)
    s_next = esp_ota_get_next_update_partition(NULL);
    return s_next ? (uint32_t)s_next->size : 0;      // 0 with the old table: no_slot
}

static int port_may_begin(void *ctx, uint8_t flags)
{
    if (s_reboot_pending) return OTA_ST_REFUSED;
    // One session at a time in the updater: the UART receiver (ctx NULL) waits for the network one; the network
    // receiver (ctx LYRAT_OTA_NET_CTX, upd_net.c) checks the UART one itself and may replace its own session.
    if (ctx == NULL && lyrat_net_rx_busy()) return OTA_ST_REFUSED;
    if ((lyrat_slc_up() || lyrat_sco_active()) && !(flags & OTA_F_FORCE)) return OTA_ST_REFUSED;
    return OTA_ST_OK;
}

static int port_busy(void *ctx, uint8_t flags)
{
    return lyrat_sco_active();      // with FORCE too: a flash write stops both cores for a moment
}

static int port_check_head(void *ctx, const uint8_t *head, size_t len)
{
    return ota_image_check(head, len, OTA_CHIP_ESP32, PROJECT);
}

static int port_begin(void *ctx, uint32_t size, uint8_t flags)
{
    if (s_open) {
        esp_ota_abort(s_handle);
        s_open = false;
    }
    ESP_LOGI(TAG, "erasing %" PRIu32 " bytes of %s", size, s_next->label);
    esp_err_t err = esp_ota_begin(s_next, size, &s_handle);    // erases now, the writes then only program pages
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "begin: %s", esp_err_to_name(err));
        return 1;
    }
    s_open = true;
    return 0;
}

static int port_write(void *ctx, uint32_t offset, const uint8_t *data, size_t len)
{
    return esp_ota_write(s_handle, data, len) == ESP_OK ? 0 : 1;
}

// The marks of upd_mode.h in NVS; 0 erases the key.
static void upd_mark(const char *key, uint8_t v)
{
    nvs_handle_t h;
    if (nvs_open(UPD_NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    if (v) nvs_set_u8(h, key, v);
    else nvs_erase_key(h, key);
    nvs_commit(h);
    nvs_close(h);
}

static uint8_t upd_mark_get(const char *key)
{
    nvs_handle_t h;
    uint8_t v = 0;
    if (nvs_open(UPD_NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, key, &v);
        nvs_close(h);
    }
    return v;
}

static void failtest_set(uint8_t v)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    if (v) nvs_set_u8(h, NVS_FAILTEST, v);
    else nvs_erase_key(h, NVS_FAILTEST);
    nvs_commit(h);
    nvs_close(h);
}

static int port_finish(void *ctx, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags)
{
    s_open = false;
    esp_err_t err = esp_ota_end(s_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "image check: %s", esp_err_to_name(err));
        return OTA_ST_VERIFY;
    }
    if (flags & OTA_F_DRYRUN) {
        ESP_LOGI(TAG, "dry run: image good, not activated");
        return OTA_ST_OK;
    }
    if (esp_ota_set_boot_partition(s_next) != ESP_OK) return OTA_ST_FLASH;
    if (flags & OTA_F_FAILTEST) failtest_set(1);
    // Its first start must pass the self-test (upd_mode.h), and that start is counted now: the restart may come from
    // this receiver's task before the updater's loop sees the image, and a fresh image that fails must find tries=1.
    upd_mark(UPD_NVS_FRESH, 1);
    upd_mark(UPD_NVS_TRIES, 1);
    s_reboot_pending = true;
    ESP_LOGW(TAG, "new image in %s, restart %s", s_next->label, lyrat_sco_active() ? "after the call" : "now");
    return OTA_ST_OK;
}

static void port_abort(void *ctx)
{
    if (s_open) esp_ota_abort(s_handle);
    s_open = false;
}

typedef enum { ST_OFF, ST_WAIT, ST_PASS, ST_FAIL } selftest_t;
static volatile selftest_t s_selftest = ST_OFF;
static const char *const s_selftest_names[] = {"off", "wait", "pass", "fail"};

static const char *img_state(const esp_partition_t *p)
{
    // The headset firmware erased otadata at its start (lyrat_ota_boot_to_updater): its self-test is the state. The
    // updater in factory is always valid. The S3 and ota.py wait for valid.
    if (s_app) return s_selftest == ST_PASS ? "valid" : s_selftest == ST_FAIL ? "invalid" : "pending";
    if (p && p->subtype == ESP_PARTITION_SUBTYPE_APP_FACTORY) return "valid";
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

static void hex8(const uint8_t *sha, char out[17])
{
    for (int i = 0; i < 8; i++) sprintf(out + 2 * i, "%02x", sha[i]);
}

// "run= ver= elf= img= inv= selftest= baud=": the S3 reads elf, img and inv to follow an update.
static size_t port_status_text(void *ctx, char *out, size_t cap)
{
    const esp_partition_t *run = esp_ota_get_running_partition();
    const esp_app_desc_t *desc = esp_app_get_description();
    char elf[17], inv[17] = "-";
    hex8(desc->app_elf_sha256, elf);
    const esp_partition_t *bad = esp_ota_get_last_invalid_partition();
    uint8_t bad_elf[8];      // only these bytes of its app description: the callers' stacks are small
    if (lyrat_inv_elf(bad_elf)) hex8(bad_elf, inv);     // the updater: a firmware it keeps out (the S3: rolled back)
    else if (bad && esp_partition_read(bad, OTA_APP_ELF_SHA_AT, bad_elf, sizeof(bad_elf)) == ESP_OK) hex8(bad_elf, inv);
    char ver[24];
    snprintf(ver, sizeof(ver), "%.23s", desc->version);
    for (char *c = ver; *c; c++) {
        if (*c == ' ') *c = '_';
    }
    // The keys of the S3 first (elf, img, inv): a STATUS reply is cut at OTA_REPLY_MAX. The mode follows (goal 096).
    int n = snprintf(out, cap, "run=%s ver=%s elf=%s img=%s inv=%s selftest=%s baud=%d ", run ? run->label : "?", ver,
                     elf, img_state(run), inv, s_selftest_names[s_selftest], lyrat_ctl_get_baud());
    if (n < 0) return 0;
    if ((size_t)n >= cap) return cap - 1;
    return (size_t)n + lyrat_mode_text(out + n, cap - (size_t)n);
}

static const ota_rx_port_t s_port = {
    .capacity = port_capacity, .may_begin = port_may_begin, .busy = port_busy, .check_head = port_check_head,
    .auth = NULL, .begin = port_begin, .write = port_write, .finish = port_finish, .abort = port_abort,
    .status_text = port_status_text,
};

// --- receiver task --------------------------------------------------------------------------------------------------

void lyrat_ota_request(const uint8_t *payload, size_t len)
{
    if (!s_queue || len == 0 || len > OTA_REQ_MAX) return;
    static req_msg_t m;             // only the control UART task calls this
    m.len = (uint16_t)len;
    memcpy(m.data, payload, len);
    if (xQueueSend(s_queue, &m, 0) != pdTRUE) s_dropped++;
}

static void ota_task(void *arg)
{
    static req_msg_t m;
    static uint8_t reply[OTA_REPLY_MAX];
    uint32_t baud_since = 0, frames_at_switch = 0;
    for (;;) {
        bool got = xQueueReceive(s_queue, &m, pdMS_TO_TICKS(100)) == pdTRUE;
        uint32_t now = now_ms();
        if (got) {
            size_t n = ota_rx_handle(&s_rx, m.data, m.len, now, reply, sizeof(reply));
            if (n) lyrat_ctl_send(CTL_T_OTA_ACK, reply, n);
        }
        ota_rx_tick(&s_rx, now);
        uint8_t ev = ota_rx_take_events(&s_rx);
        if (ev & OTA_EV_CLOSED && lyrat_ctl_get_baud() != CTL_BAUD_DEFAULT) {
            lyrat_ctl_wait_tx();
            lyrat_ctl_set_baud(CTL_BAUD_DEFAULT);
            baud_since = 0;
        }
        if (ev & OTA_EV_BEGUN && s_rx.baud >= 9600 && s_rx.baud <= 921600 && (int)s_rx.baud != lyrat_ctl_get_baud()) {
            // The reply left at the old speed; the S3 switches when it reads it.
            lyrat_ctl_wait_tx();
            lyrat_ctl_set_baud((int)s_rx.baud);
            baud_since = now;
            frames_at_switch = lyrat_ctl_frames();
            ESP_LOGI(TAG, "uart at %" PRIu32 " baud for the image", s_rx.baud);
        }
        if (baud_since) {
            if (lyrat_ctl_frames() != frames_at_switch) {
                baud_since = 0;             // the S3 speaks at the new speed
            } else if ((uint32_t)(now - baud_since) > BAUD_PROBE_MS) {
                ESP_LOGW(TAG, "nothing at %d baud, back to %d", lyrat_ctl_get_baud(), CTL_BAUD_DEFAULT);
                uint8_t abort_req[5];
                ota_rx_handle(&s_rx, abort_req, ota_build_short(abort_req, OTA_OP_ABORT, s_rx.session), now, reply,
                              sizeof(reply));
                ota_rx_take_events(&s_rx);
                lyrat_ctl_set_baud(CTL_BAUD_DEFAULT);
                baud_since = 0;
            }
        }
        if (ev & OTA_EV_DONE && lyrat_ctl_get_baud() != CTL_BAUD_DEFAULT) {
            lyrat_ctl_wait_tx();
            lyrat_ctl_set_baud(CTL_BAUD_DEFAULT);
        }
        if (s_reboot_pending && !lyrat_sco_active()) {
            vTaskDelay(pdMS_TO_TICKS(REBOOT_DELAY_MS));     // the reply to END is on its way
            if (!lyrat_sco_active()) {
                ESP_LOGW(TAG, "restarting into the new image");
                esp_restart();
            }
        }
    }
}

// --- self-test of a new image ---------------------------------------------------------------------------------------

static bool failtest_take(void)
{
    nvs_handle_t h;
    uint8_t v = 0;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, NVS_FAILTEST, &v);
        nvs_close(h);
    }
    if (v) failtest_set(0);
    return v == 1;
}

static void selftest_task(void *arg)
{
    bool fail_on_purpose = failtest_take();
    // Headset firmware (goal 096): a fresh image must show Bluetooth and the S3 as before and goes back to the updater
    // when it cannot; any other start only needs Bluetooth and never restarts for the S3 (R13: a dead S3 must not
    // take the headset). Passing clears the marks: the updater starts it again next time.
    bool strict = !s_app || upd_mark_get(UPD_NVS_FRESH) != 0;
    uint32_t start = now_ms();
    s_selftest = ST_WAIT;
    if (s_app) ESP_LOGI(TAG, "self-test: %s", strict ? "new image, Bluetooth and the S3 within 90 s" : "Bluetooth");
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        bool ok = lyrat_bt_ready() && (!strict || lyrat_s3_acks() > 0);
        if (fail_on_purpose) {
            // Never a pass: Bluetooth and the S3's frame (or the S3's network) can be there within a second, before the
            // 5 s, and the image would confirm itself (goal 096, incident 002).
            ok = false;
            if ((uint32_t)(now_ms() - start) > 5000) {
                ESP_LOGW(TAG, "self-test fails on purpose (failtest), back to the old image");
                start -= SELFTEST_MS;
            }
        }
        if (ok) {
            if (s_app) {
                upd_mark(UPD_NVS_TRIES, 0);
                upd_mark(UPD_NVS_FRESH, 0);
            } else {
                esp_ota_mark_app_valid_cancel_rollback();
            }
            s_selftest = ST_PASS;
            ESP_LOGI(TAG, "self-test passed: image confirmed");
            vTaskDelete(NULL);
        }
        if ((uint32_t)(now_ms() - start) > SELFTEST_MS) {
            s_selftest = ST_FAIL;
            if (lyrat_sco_active()) continue;           // never in the middle of a call
            if (s_app && !strict) {
                ESP_LOGE(TAG, "self-test failed: bt=%d, no restart (not a new image)", lyrat_bt_ready());
                vTaskDelete(NULL);
            }
            ESP_LOGE(TAG, "self-test failed: bt=%d s3_acks=%" PRIu32 ", %s", lyrat_bt_ready(), lyrat_s3_acks(),
                     s_app ? "back to the updater, it keeps this image out" : "rolling back");
            vTaskDelay(pdMS_TO_TICKS(500));
            if (s_app) esp_restart();
            esp_ota_mark_app_invalid_rollback_and_reboot();
            vTaskDelete(NULL);
        }
    }
}

size_t lyrat_ota_status(char *out, size_t cap)
{
    int n = snprintf(out, cap, "ota state=%s size=%" PRIu32 " next=%" PRIu32 " err=%s done=%" PRIu32 " dropped=%" PRIu32 " ",
                     ota_rx_state_name(s_rx.state), s_rx.size, s_rx.next, ota_status_name(s_rx.last_err), s_rx.done,
                     s_dropped);
    if (n < 0 || (size_t)n >= cap) return n < 0 ? 0 : cap - 1;
    return (size_t)n + port_status_text(NULL, out + n, cap - (size_t)n);
}

void lyrat_ota_set_app(void) { s_app = true; }
void lyrat_ota_port_copy(ota_rx_port_t *out) { *out = s_port; }
int lyrat_ota_uart_busy(void) { return s_rx.state == OTA_RX_RECV; }
const ota_rx_t *lyrat_ota_rx(void) { return &s_rx; }

int lyrat_ota_boot_to_updater(void)
{
    const esp_partition_t *f = esp_partition_find_first(ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_APP_FACTORY, NULL);
    if (!f) {
        ESP_LOGW(TAG, "no factory partition: the old table, no updater");
        return 0;
    }
    esp_err_t err = esp_ota_set_boot_partition(f);      // erases otadata: the bootloader then takes factory
    if (err != ESP_OK) ESP_LOGE(TAG, "boot back to the updater: %s", esp_err_to_name(err));
    else ESP_LOGI(TAG, "next start: the updater (factory)");
    return err == ESP_OK;
}

void lyrat_ota_init(void)
{
    ota_rx_init(&s_rx, &s_port, IDLE_MS);
    // The queue's storage in PSRAM: only tasks use it, and they are stopped while the flash is busy.
    s_queue = xQueueCreateWithCaps(QUEUE_DEPTH, sizeof(req_msg_t), MALLOC_CAP_SPIRAM);
    if (!s_queue) s_queue = xQueueCreate(QUEUE_DEPTH, sizeof(req_msg_t));
    // The stack stays in internal RAM: this task writes the flash.
    if (!s_queue || xTaskCreatePinnedToCore(ota_task, "ota", 4096, NULL, 3, NULL, 1) != pdPASS) {
        ESP_LOGE(TAG, "no OTA task");
    }
    const esp_partition_t *run = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    if (s_app || (run && esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY)) {
        xTaskCreatePinnedToCore(selftest_task, "selftest", 3072, NULL, 2, NULL, 1);
    }
    ESP_LOGI(TAG, "running %s, %s", run ? run->label : "?", img_state(run));
}
