// Asks for sniff mode before the phone does (found by bench goal 055).
//
// Around 7 s after the last AT command the phone asks for sniff by itself. On the Lenovo L71091 about one such request
// in ten fails with status 0x23 (LMP transaction collision) and the link then stalls until the 30 s LMP limit. If the
// first channel of this connection opens in those seconds, the phone's +BCS gets no reply in its 5 s, and the phone
// disables codec negotiation for the first three bytes of this address for good: mSBC is lost until its Bluetooth
// settings are reset. A link already in sniff gives the phone nothing to ask for, and the board's own request 3 s after
// the link went quiet was never seen to stall. Bluedroid's idle timer cannot be shortened: its table is constant.
#include <stdbool.h>
#include <stdint.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "link_power.h"
#if __has_include("bench_variant.h")
#include "bench_variant.h"
#endif

#define QUIET_BEFORE_REQUEST_US (3 * 1000000LL)  // well before the phone's 7 s
#define REQUEST_RETRY_US        (5 * 1000000LL)
#define CHECK_PERIOD_US         (500 * 1000LL)
// 3 s, against the stack's 5 s. If the board dies while the phone waits for its reply to +BCS, the phone adds the same
// entry when its 5 s timer ends, unless the link has dropped before that (goal 055 #005). Slots are 0.625 ms.
#define LINK_SUPERVISION_SLOTS  4800

// Not in a public header. Parameters are the stack's own for an HFP link (BTA_DM_PM_SNIFF).
extern uint8_t btsnd_hcic_sniff_mode(uint16_t handle, uint16_t max_sniff_period, uint16_t min_sniff_period,
                                     uint16_t sniff_attempt, uint16_t sniff_timeout);
extern void BTM_SetDefaultLinkSuperTout(uint16_t timeout);

static volatile bool s_acl_up, s_slc_up, s_sco_open, s_mode_active;
static volatile uint16_t s_handle;
static volatile int64_t s_quiet_since_us, s_last_request_us;

static void quiet_restarts(void)
{
    s_quiet_since_us = esp_timer_get_time();
}

static void check_cb(void *arg)
{
    (void)arg;
    int64_t now = esp_timer_get_time();
    if (!s_acl_up || !s_slc_up || s_sco_open || !s_mode_active) return;
    if (now - s_quiet_since_us < QUIET_BEFORE_REQUEST_US || now - s_last_request_us < REQUEST_RETRY_US) return;
    s_last_request_us = now;
    btsnd_hcic_sniff_mode(s_handle, 800, 400, 4, 1);
}

void link_power_init(void)
{
    // The board is the central on links it pages, so this value is the link's. See LINK_SUPERVISION_SLOTS.
    BTM_SetDefaultLinkSuperTout(LINK_SUPERVISION_SLOTS);
#ifndef BENCH_NO_FIRST_SNIFF
    const esp_timer_create_args_t args = {.callback = check_cb, .name = "link_power"};
    esp_timer_handle_t timer;
    ESP_ERROR_CHECK(esp_timer_create(&args, &timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(timer, CHECK_PERIOD_US));
#endif
}

void link_power_acl(bool up, uint16_t handle)
{
    s_acl_up = up;
    s_handle = handle;
    s_mode_active = true;
    quiet_restarts();
}

void link_power_slc(bool up)
{
    s_slc_up = up;
    quiet_restarts();
}

void link_power_sco(bool open)
{
    s_sco_open = open;
    quiet_restarts();
}

void link_power_mode(bool active)
{
    s_mode_active = active;
    if (active) quiet_restarts();
}
