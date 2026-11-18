// The updater of the LyraT, in the factory partition (goal 096, docs/OTA.md "Updater").
//
// Every start of the board comes here first. The updater tells the version of the headset firmware in ota_0 (git hash
// and ELF SHA-256 of its app description) in its log, joins the bench Wi-Fi, takes a new image over UDP 5007 (upd_net.c)
// or over the control UART from the S3 (lyrat_ota.c, the way of before), and after a short window starts the headset
// firmware, which points the next start back here. No Bluetooth, no sound: Wi-Fi never runs next to a call (R13).
// The rules of the window and of a bad firmware are in ../../main/upd_mode.c, which the PC tests build.
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "ctl_frame.h"
#include "driver/uart.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lyrat_link.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "upd_mode.h"
#include "upd_net.h"

static const char *TAG = "updater";

// The control UART to the S3, as in the headset firmware (docs/S3_UART.md).
#define CTL_UART        UART_NUM_1
#define CTL_PIN_TX      14
#define CTL_PIN_RX      13
#define CTL_RX_RING     4096
#define CTL_EVENTS      16
#define CTL_POLL_MS     100
#define CTL_HB_MS       1000
#define CON_UART        UART_NUM_0
#define CMD_LINE_MAX    128
#define REPLY_MAX       640
#define REBOOT_DELAY_MS 1000
#define RECHECK_MS      60000       // no firmware in ota_0: look again this often (an image may have come by USB)

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

// The names of main.c: the bench tools read "BOOT reset_reason=<NAME> code=<n>" of both firmwares (console.py).
static const char *reset_reason_name(esp_reset_reason_t r)
{
    switch (r) {
    case ESP_RST_POWERON:   return "POWERON";
    case ESP_RST_EXT:       return "EXT";
    case ESP_RST_SW:        return "SW";
    case ESP_RST_PANIC:     return "PANIC";
    case ESP_RST_INT_WDT:   return "INT_WDT";
    case ESP_RST_TASK_WDT:  return "TASK_WDT";
    case ESP_RST_WDT:       return "WDT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    case ESP_RST_BROWNOUT:  return "BROWNOUT";
    case ESP_RST_SDIO:      return "SDIO";
    default:                return "UNKNOWN";
    }
}

// --- the firmware in ota_0 ------------------------------------------------------------------------------------------

static const esp_partition_t *s_app_part;
static esp_app_desc_t s_app_desc;
static bool s_app_image;
static char s_app_elf[17] = "-";
static upd_app_t s_app = UPD_APP_NONE;
static uint8_t s_tries, s_fresh, s_stay;
static upd_watch_t s_watch;
static SemaphoreHandle_t s_lock;    // s_watch and s_app: the main loop and the commands

static uint8_t mark_get(const char *key)
{
    nvs_handle_t h;
    uint8_t v = 0;
    if (nvs_open(UPD_NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, key, &v);
        nvs_close(h);
    }
    return v;
}

static void mark_set(const char *key, uint8_t v)
{
    nvs_handle_t h;
    if (nvs_open(UPD_NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    if (v) nvs_set_u8(h, key, v);
    else nvs_erase_key(h, key);
    nvs_commit(h);
    nvs_close(h);
}

static void app_check(void)
{
    s_app_part = esp_partition_find_first(ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_APP_OTA_0, NULL);
    s_app_image = s_app_part && esp_ota_get_partition_description(s_app_part, &s_app_desc) == ESP_OK &&
                  strcmp(s_app_desc.project_name, "lyrat_resend") == 0;
    if (s_app_image) {
        for (int i = 0; i < 8; i++) sprintf(s_app_elf + 2 * i, "%02x", s_app_desc.app_elf_sha256[i]);
    } else {
        snprintf(s_app_elf, sizeof(s_app_elf), "-");
    }
    s_tries = mark_get(UPD_NVS_TRIES);
    s_fresh = mark_get(UPD_NVS_FRESH);
    s_app = upd_app_state(s_app_image, s_fresh, s_tries);
}

int lyrat_inv_elf(uint8_t out[8])
{
    // Not after an image came in this start: it may be the same build again, and the S3 and ota.py would read the old
    // verdict as a rollback of the new image (goal 096, incident 005).
    if (s_app != UPD_APP_BAD || upd_net_rx()->done + lyrat_ota_rx()->done > 0) return 0;
    memcpy(out, s_app_desc.app_elf_sha256, 8);
    return 1;
}

size_t lyrat_mode_text(char *out, size_t cap)
{
    char net[60];
    upd_net_text(net, sizeof(net));
    int32_t left = upd_watch_left_s(&s_watch, now_ms());
    int n = snprintf(out, cap, "mode=updater app=%.16s app_elf=%s app_st=%s tries=%u %s left=%" PRId32,
                     s_app_image ? s_app_desc.version : "-", s_app_elf, upd_app_name(s_app), s_tries, net, left);
    if (n < 0) return 0;
    return (size_t)n < cap ? (size_t)n : cap - 1;
}

// Starts the firmware in ota_0: the full image check of the IDF, then the restart. Returns only when it cannot.
static void boot_app(bool retry)
{
    if (retry) {
        ESP_LOGW(TAG, "trying the firmware again after %d min: %s", UPD_BAD_RETRY_MS / 60000, s_app_elf);
        mark_set(UPD_NVS_FRESH, 0);
        mark_set(UPD_NVS_TRIES, 0);
    }
    esp_err_t err = s_app_part ? esp_ota_set_boot_partition(s_app_part) : ESP_ERR_NOT_FOUND;
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ota_0 cannot start (%s): staying here", esp_err_to_name(err));
        s_app_image = false;
        s_app = UPD_APP_NONE;
        upd_watch_set_app(&s_watch, s_app);
        return;
    }
    mark_set(UPD_NVS_TRIES, (uint8_t)((retry ? 0 : s_tries) + 1));
    ESP_LOGI(TAG, "starting the firmware %s elf %s", s_app_desc.version, s_app_elf);
    lyrat_ctl_wait_tx();
    vTaskDelay(pdMS_TO_TICKS(100));
    esp_restart();
}

// --- lyrat_link.h, the "main" part ----------------------------------------------------------------------------------

static ctl_parser_t s_ctl_parser;
static uint8_t s_ctl_tx[CTL_MAX_FRAME];     // the last reply, for a repeated seq
static size_t s_ctl_tx_reply_len;
static uint16_t s_ctl_last_seq;
static bool s_ctl_have_seq;
static volatile int s_ctl_baud = CTL_BAUD_DEFAULT;
static volatile uint32_t s_ctl_rx_bytes, s_ctl_cmds, s_ctl_dups, s_ctl_hb, s_ctl_hb_acks, s_ctl_bad_type, s_ctl_tx_fail,
    s_ctl_ovf;

bool lyrat_sco_active(void) { return false; }
bool lyrat_slc_up(void) { return false; }
bool lyrat_bt_ready(void) { return false; }
uint32_t lyrat_s3_acks(void) { return s_ctl_hb_acks; }
uint32_t lyrat_ctl_frames(void) { return s_ctl_parser.frames; }
int lyrat_ctl_get_baud(void) { return s_ctl_baud; }
void lyrat_ctl_wait_tx(void) { uart_wait_tx_done(CTL_UART, pdMS_TO_TICKS(200)); }

int lyrat_ctl_set_baud(int baud)
{
    if (baud >= 1200 && baud <= 921600 && uart_set_baudrate(CTL_UART, (uint32_t)baud) == ESP_OK) s_ctl_baud = baud;
    return s_ctl_baud;
}

static void ctl_send(const uint8_t *frame, size_t bytes)
{
    if (uart_write_bytes(CTL_UART, frame, bytes) != (int)bytes) s_ctl_tx_fail++;
}

void lyrat_ctl_send(uint8_t type, const void *payload, size_t len)
{
    uint8_t frame[CTL_HEADER_BYTES + REPLY_MAX + CTL_CRC_BYTES];
    if (len > REPLY_MAX) {
        s_ctl_tx_fail++;
        return;
    }
    ctl_send(frame, ctl_frame_build(frame, type, 0, payload, len));
}

// --- commands -------------------------------------------------------------------------------------------------------

static volatile bool s_reboot_now;

// Writes the reply text; returns 0 when the command is unknown.
static int run_cmd(const char *line, char *out, size_t cap)
{
    char cmd[16] = {0};
    sscanf(line, "%15s", cmd);
    if (strcmp(cmd, "s") == 0 || strcmp(cmd, "status") == 0) {
        char m[200];
        lyrat_mode_text(m, sizeof(m));
        snprintf(out, cap, "=== Updater ===\n  %s\n  boot_reason=%s\n  heap    = %" PRIu32, m,
                 reset_reason_name(esp_reset_reason()),
                 esp_get_free_heap_size());
    } else if (strcmp(cmd, "ota") == 0) {
        lyrat_ota_status(out, cap);
    } else if (strcmp(cmd, "ulog") == 0) {
        lyrat_log_status(out, cap);
    } else if (strcmp(cmd, "ustat") == 0) {
        snprintf(out, cap, "ustat baud=%d rx_bytes=%" PRIu32 " frames=%" PRIu32 " bad_hdr=%" PRIu32 " bad_crc=%" PRIu32
                 " cmds=%" PRIu32 " dups=%" PRIu32 " hb=%" PRIu32 " acks=%" PRIu32 " bad_type=%" PRIu32 " ovf=%" PRIu32
                 " tx_fail=%" PRIu32, s_ctl_baud, s_ctl_rx_bytes, s_ctl_parser.frames, s_ctl_parser.bad_header,
                 s_ctl_parser.bad_crc, s_ctl_cmds, s_ctl_dups, s_ctl_hb, s_ctl_hb_acks, s_ctl_bad_type, s_ctl_ovf,
                 s_ctl_tx_fail);
    } else if (strcmp(cmd, "reboot") == 0) {
        snprintf(out, cap, "reboot in %d ms (the updater again)", REBOOT_DELAY_MS);
        s_reboot_now = true;
    } else if (strcmp(cmd, "app") == 0) {
        // The operator takes a bad firmware back: start it now with clean marks.
        mark_set(UPD_NVS_FRESH, 0);
        mark_set(UPD_NVS_TRIES, 0);
        xSemaphoreTake(s_lock, portMAX_DELAY);
        s_tries = s_fresh = 0;
        s_app = upd_app_state(s_app_image, 0, 0);
        s_watch.start_ms = now_ms() - s_watch.window_ms;      // the window is over
        upd_watch_set_app(&s_watch, s_app);
        xSemaphoreGive(s_lock);
        snprintf(out, cap, "app: %s", s_app_image ? "starting the firmware" : "no firmware in ota_0");
    } else if (strcmp(cmd, "upd") == 0) {
        snprintf(out, cap, "upd: already in the updater");
    } else if (strcmp(cmd, "help") == 0 || strcmp(cmd, "h") == 0 || strcmp(cmd, "?") == 0) {
        snprintf(out, cap, "updater: s, ota, ulog, ustat, reboot, app (start the firmware now, also a bad one), help");
    } else {
        snprintf(out, cap, "%s: not in the updater (no Bluetooth, no sound here)", cmd);
        return 0;
    }
    return 1;
}

static void ctl_on_command(const ctl_parser_t *p)
{
    uint16_t seq = ctl_frame_seq(p);
    if (s_ctl_have_seq && seq == s_ctl_last_seq) {      // the reply got lost: never run a command twice
        s_ctl_dups++;
        ctl_send(s_ctl_tx, s_ctl_tx_reply_len);
        return;
    }
    size_t len = ctl_frame_len(p);
    const uint8_t *text = ctl_frame_payload(p);
    static char reply[REPLY_MAX];
    char line[CMD_LINE_MAX];
    bool valid = len > 0 && len < sizeof(line);
    for (size_t i = 0; valid && i < len; i++) valid = text[i] >= 0x20 && text[i] < 0x7F;
    size_t n = 1;
    reply[0] = CTL_ST_BAD;
    if (valid) {
        memcpy(line, text, len);
        line[len] = 0;
        s_ctl_cmds++;
        int known = run_cmd(line, reply + 1, sizeof(reply) - 1);
        reply[0] = known ? CTL_ST_OK : CTL_ST_UNKNOWN;
        n += strlen(reply + 1);
    }
    s_ctl_tx_reply_len = ctl_frame_build(s_ctl_tx, CTL_T_REPLY, seq, reply, n);
    s_ctl_last_seq = seq;
    s_ctl_have_seq = true;
    ctl_send(s_ctl_tx, s_ctl_tx_reply_len);
}

static void ctl_rx_bytes(size_t available)
{
    uint8_t chunk[64];
    while (available) {
        int got = uart_read_bytes(CTL_UART, chunk, available < sizeof(chunk) ? available : sizeof(chunk), 0);
        if (got <= 0) return;
        available -= (size_t)got;
        s_ctl_rx_bytes += (uint32_t)got;
        uint32_t now = now_ms();
        for (int i = 0; i < got; i++) {
            if (!ctl_parser_feed(&s_ctl_parser, chunk[i], now)) continue;
            uint8_t type = ctl_frame_type(&s_ctl_parser);
            if (type == CTL_T_CMD) {
                ctl_on_command(&s_ctl_parser);
            } else if (type == CTL_T_HB_ACK) {
                s_ctl_hb_acks++;
                lyrat_log_on_ack(ctl_frame_payload(&s_ctl_parser), ctl_frame_len(&s_ctl_parser));
            } else if (type == CTL_T_OTA) {
                lyrat_ota_request(ctl_frame_payload(&s_ctl_parser), ctl_frame_len(&s_ctl_parser));
            } else {
                s_ctl_bad_type++;
            }
        }
    }
}

static void ctl_uart_task(void *arg)
{
    QueueHandle_t events = NULL;
    uart_config_t cfg = {
        .baud_rate = CTL_BAUD_DEFAULT, .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1, .flow_ctrl = UART_HW_FLOWCTRL_DISABLE, .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t err = uart_driver_install(CTL_UART, CTL_RX_RING, 0, CTL_EVENTS, &events, 0);
    if (err == ESP_OK) err = uart_param_config(CTL_UART, &cfg);
    if (err == ESP_OK) err = uart_set_pin(CTL_UART, CTL_PIN_TX, CTL_PIN_RX, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "control UART failed: %s", esp_err_to_name(err));
        vTaskDelete(NULL);
    }
    int64_t next_hb = esp_timer_get_time();
    uint16_t hb_seq = 0;
    for (;;) {
        uart_event_t ev;
        if (xQueueReceive(events, &ev, pdMS_TO_TICKS(CTL_POLL_MS)) == pdTRUE &&
            (ev.type == UART_FIFO_OVF || ev.type == UART_BUFFER_FULL)) {
            s_ctl_ovf++;
            uart_flush_input(CTL_UART);
            xQueueReset(events);
        }
        size_t available = 0;
        if (uart_get_buffered_data_len(CTL_UART, &available) == ESP_OK && available) ctl_rx_bytes(available);
        lyrat_log_drain();
        int64_t now = esp_timer_get_time();
        if (now >= next_hb) {
            next_hb = now + CTL_HB_MS * 1000LL;
            uint8_t frame[CTL_HEADER_BYTES + sizeof(ctl_hb_t) + CTL_CRC_BYTES];
            ctl_hb_t hb = {.up_s = (uint32_t)(now / 1000000), .flags = 0, .tone = 0};    // no SLC, no SCO here
            ctl_send(frame, ctl_frame_build(frame, CTL_T_HB, hb_seq++, &hb, sizeof(hb)));
            s_ctl_hb++;
        }
    }
}

static void console_task(void *arg)
{
    uart_config_t cfg = {
        .baud_rate = 115200, .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1, .flow_ctrl = UART_HW_FLOWCTRL_DISABLE, .source_clk = UART_SCLK_DEFAULT,
    };
    if (uart_driver_install(CON_UART, 256, 0, 0, NULL, 0) != ESP_OK || uart_param_config(CON_UART, &cfg) != ESP_OK) {
        vTaskDelete(NULL);
    }
    char line[CMD_LINE_MAX];
    static char reply[REPLY_MAX];
    int pos = 0;
    for (;;) {
        uint8_t ch;
        if (uart_read_bytes(CON_UART, &ch, 1, portMAX_DELAY) <= 0) continue;
        if (ch == '\n' || ch == '\r') {
            if (pos > 0) {
                line[pos] = 0;
                run_cmd(line, reply, sizeof(reply));
                printf("%s\n", reply);
                pos = 0;
            }
        } else if (pos < CMD_LINE_MAX - 1) {
            line[pos++] = (char)ch;
        }
    }
}

// --- start ----------------------------------------------------------------------------------------------------------

void app_main(void)
{
    lyrat_log_init((uint16_t)esp_random());
    esp_reset_reason_t reason = esp_reset_reason();
    ESP_LOGI(TAG, "BOOT reset_reason=%s code=%d mode=updater", reset_reason_name(reason), (int)reason);
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        // Never erased here: the pairings of the headset live in this partition. Without NVS the marks read 0.
        ESP_LOGE(TAG, "nvs: %s, going on without the marks", esp_err_to_name(ret));
    }
    s_lock = xSemaphoreCreateMutex();
    configASSERT(s_lock);
    s_stay = mark_get(UPD_NVS_STAY);
    if (s_stay) mark_set(UPD_NVS_STAY, 0);
    app_check();
    upd_watch_init(&s_watch, now_ms(), s_app, s_stay);
    const esp_app_desc_t *me = esp_app_get_description();
    ESP_LOGI(TAG, "updater ver=%s", me->version);
    // The owner's line: which headset firmware is installed, by its git hash and ELF SHA-256.
    ESP_LOGI(TAG, "updater: app ver=%s elf=%s state=%s tries=%u fresh=%u stay=%u, window %" PRIu32 " s",
             s_app_image ? s_app_desc.version : "-", s_app_elf, upd_app_name(s_app), s_tries, s_fresh, s_stay,
             s_app == UPD_APP_NONE ? 0 : s_watch.window_ms / 1000);

    lyrat_ota_init();       // the UART receiver: images from the S3 go into ota_0
    xTaskCreatePinnedToCore(ctl_uart_task, "ctl_uart", 5120, NULL, 4, NULL, 1);
    xTaskCreatePinnedToCore(console_task, "console", 4096, NULL, 3, NULL, 1);
    upd_net_start(s_stay == UPD_STAY_NOWIFI_TEST);

    uint32_t last_check = now_ms(), last_line = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(100));
        uint32_t now = now_ms();
        if (s_reboot_now) {
            vTaskDelay(pdMS_TO_TICKS(REBOOT_DELAY_MS));
            esp_restart();
        }
        const ota_rx_t *net = upd_net_rx(), *uart = lyrat_ota_rx();
        int active = net->state == OTA_RX_RECV || uart->state == OTA_RX_RECV;
        xSemaphoreTake(s_lock, portMAX_DELAY);
        if (s_app == UPD_APP_NONE && (uint32_t)(now - last_check) > RECHECK_MS) {
            last_check = now;
            app_check();
            upd_watch_set_app(&s_watch, s_app);
        }
        upd_step_t step = upd_watch_step(&s_watch, now, active, net->done + uart->done);
        xSemaphoreGive(s_lock);
        if (step == UPD_BOOT_NEW) {
            // The receiver pointed the boot at the new image and marked it fresh; its reply leaves first.
            ESP_LOGW(TAG, "new image in ota_0, starting it");
            vTaskDelay(pdMS_TO_TICKS(300));
            lyrat_ctl_wait_tx();
            esp_restart();
        } else if (step == UPD_BOOT_APP || step == UPD_BOOT_RETRY) {
            boot_app(step == UPD_BOOT_RETRY);
        }
        if ((uint32_t)(now - last_line) >= 5000) {
            last_line = now;
            char m[200];
            lyrat_mode_text(m, sizeof(m));
            ESP_LOGI(TAG, "%s heap=%" PRIu32, m, esp_get_free_heap_size());
        }
    }
}
