// The updater of the LyraT and the way into it: the rules, without platform calls (upd_mode.h). Goal 096.
#include "upd_mode.h"

#include <stdio.h>
#include <string.h>

upd_app_t upd_app_state(int image_ok, int fresh, uint32_t tries)
{
    if (!image_ok) return UPD_APP_NONE;
    if (fresh && tries >= 1) return UPD_APP_BAD;        // the new image started once and never passed its self-test
    if (tries >= UPD_TRIES_MAX) return UPD_APP_BAD;     // an old one that keeps failing before Bluetooth is up
    return fresh ? UPD_APP_FRESH : UPD_APP_OK;
}

const char *upd_app_name(upd_app_t a)
{
    static const char *const names[] = {"none", "ok", "fresh", "bad"};
    return (unsigned)a < sizeof(names) / sizeof(names[0]) ? names[a] : "?";
}

static uint32_t window_of(upd_app_t app, uint8_t stay)
{
    if (app == UPD_APP_BAD) return UPD_BAD_RETRY_MS;
    return stay ? UPD_WINDOW_STAY_MS : UPD_WINDOW_MS;
}

void upd_watch_init(upd_watch_t *w, uint32_t now_ms, upd_app_t app, uint8_t stay)
{
    memset(w, 0, sizeof(*w));
    w->start_ms = now_ms;
    w->app = app;
    w->window_ms = window_of(app, stay);
}

void upd_watch_set_app(upd_watch_t *w, upd_app_t app)
{
    w->app = app;
}

upd_step_t upd_watch_step(upd_watch_t *w, uint32_t now_ms, int active, uint32_t done)
{
    if (!w->have_done) {
        w->done_seen = done;
        w->have_done = 1;
    }
    if (done != w->done_seen) {
        w->done_seen = done;
        return UPD_BOOT_NEW;            // the receiver pointed the boot at the new image already
    }
    if (active) {
        w->last_active_ms = now_ms;
        w->have_active = 1;
        return UPD_STAY;
    }
    if (w->app == UPD_APP_NONE) return UPD_STAY;        // nothing to start: wait for an image as long as it takes
    if ((uint32_t)(now_ms - w->start_ms) < w->window_ms) return UPD_STAY;
    if (w->have_active && (uint32_t)(now_ms - w->last_active_ms) < UPD_AFTER_SESSION_MS) return UPD_STAY;
    return w->app == UPD_APP_BAD ? UPD_BOOT_RETRY : UPD_BOOT_APP;
}

const char *upd_step_name(upd_step_t s)
{
    static const char *const names[] = {"stay", "boot_app", "boot_retry", "boot_new"};
    return (unsigned)s < sizeof(names) / sizeof(names[0]) ? names[s] : "?";
}

int32_t upd_watch_left_s(const upd_watch_t *w, uint32_t now_ms)
{
    if (w->app == UPD_APP_NONE) return -1;
    uint32_t end = w->start_ms + w->window_ms;
    if (w->have_active && (int32_t)(w->last_active_ms + UPD_AFTER_SESSION_MS - end) > 0) {
        end = w->last_active_ms + UPD_AFTER_SESSION_MS;
    }
    int32_t left = (int32_t)(end - now_ms);
    return left <= 0 ? 0 : (left + 999) / 1000;
}

uint8_t upd_command(const char *args, int sco_on, char *out, size_t cap)
{
    const char *a = args ? args : "";
    while (*a == ' ') a++;
    uint8_t stay;
    if (*a == '\0') {
        stay = UPD_STAY_NORMAL;
    } else if (strcmp(a, "8") == 0) {
        stay = UPD_STAY_NOWIFI_TEST;
    } else {
        snprintf(out, cap, "upd refused: bad request ('upd', or 'upd 8' for the test without network)");
        return 0;
    }
    if (sco_on) {
        snprintf(out, cap, "upd refused: call (the updater has no Bluetooth)");
        return 0;
    }
    snprintf(out, cap, "upd ok stay=%u, restarting into the updater", stay);
    return stay;
}
