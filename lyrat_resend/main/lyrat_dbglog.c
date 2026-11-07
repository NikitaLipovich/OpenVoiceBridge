// Log of the LyraT for the PC, through the S3 (docs/OTA.md, "Debug log").
//
// Every ESP_LOG line also goes into a ring in PSRAM (the slots; the ring and its lock stay in internal RAM). The S3 says
// in each heartbeat ack whether a PC listens and at which level; only then the control UART task drains the ring into
// frames CTL_T_LOG, at most LOG_RATE bytes a second, a sixth of the line at 115200. Without a listener the lines wait,
// so those of the start reach the PC later.
#include <stdio.h>
#include <string.h>

#include "ctl_frame.h"
#include "dbg_log.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "lyrat_link.h"

#define LOG_LINES     96
#define LOG_RATE      2000        // bytes per second onto the UART
#define LOG_BURST     1024
#define LOG_FRAME     600         // payload of one frame at most

static EXT_RAM_BSS_ATTR dbg_slot_t s_slots[LOG_LINES];
static dbg_ring_t s_ring;
static volatile bool s_on;
static volatile uint8_t s_level = 0xFF;
static uint8_t s_applied = 0xFF;
static int64_t s_fill_us;
static int32_t s_tokens = LOG_BURST;
static uint32_t s_frames, s_bytes;

void lyrat_log_init(uint16_t boot_id)
{
    dbg_ring_init(&s_ring, s_slots, LOG_LINES, DBG_BOARD_LYRAT, boot_id);
    dbg_log_install(&s_ring);
}

void lyrat_log_on_ack(const uint8_t *payload, size_t len)
{
    // An S3 older than goal 090 sends an empty ack: no listener.
    s_on = len >= 2 && payload[0];
    if (len >= 2 && payload[1] <= DBG_LVL_V) s_level = payload[1];
}

void lyrat_log_drain(void)
{
    if (s_level != 0xFF && s_level != s_applied && s_on) {
        s_applied = s_level;
        esp_log_level_set("*", (esp_log_level_t)s_applied);
    }
    int64_t now = esp_timer_get_time();
    if (s_fill_us) {
        s_tokens += (int32_t)((now - s_fill_us) * LOG_RATE / 1000000);
        if (s_tokens > LOG_BURST) s_tokens = LOG_BURST;
    }
    s_fill_us = now;
    if (!s_on || s_tokens < DBG_REC_HDR + 40) return;
    static uint8_t payload[LOG_FRAME];      // only the control UART task drains
    size_t cap = (size_t)s_tokens < sizeof(payload) ? (size_t)s_tokens : sizeof(payload);
    uint32_t count = 0;
    size_t n = dbg_ring_pack(&s_ring, payload, cap, &count);
    if (!n) return;
    lyrat_ctl_send(CTL_T_LOG, payload, n);
    s_tokens -= (int32_t)n;
    s_frames++;
    s_bytes += (uint32_t)n;
}

size_t lyrat_log_status(char *out, size_t cap)
{
    int n = snprintf(out, cap, "log on=%d level=%d pending=%lu lost=%lu sent=%lu frames=%lu bytes=%lu", s_on,
                     s_level == 0xFF ? -1 : s_level, (unsigned long)dbg_ring_pending(&s_ring),
                     (unsigned long)s_ring.lost, (unsigned long)s_ring.taken, (unsigned long)s_frames,
                     (unsigned long)s_bytes);
    return n > 0 ? (size_t)n : 0;
}
