// The updater of the LyraT and the way into it (goal 096, docs/OTA.md "Updater"): the rules, without platform calls.
//
// factory holds the updater (Wi-Fi, image over UDP 5007 or over the UART from the S3, no Bluetooth, no sound), ota_0 the
// headset firmware (no Wi-Fi at all, R13). Every start goes through the updater: it waits a short window for an image,
// then boots the headset firmware, which at once points the boot back to factory. This file holds what can be wrong in
// that, so the PC tests build it too (otahost.dll):
//   - the state of the installed firmware from the marks in NVS (tries, fresh) and whether its image is there;
//   - the window of the updater and when it ends: boot the firmware, the new image, or a later retry of a bad one;
//   - the console command "upd" of the headset firmware: the way into the updater for the PC.
#pragma once

#include <stddef.h>
#include <stdint.h>

#define UPD_WINDOW_MS        10000      // a normal start: the owner's 10 s
#define UPD_WINDOW_STAY_MS   120000     // after "upd": time for the PC to find the address and start
#define UPD_AFTER_SESSION_MS 30000      // the window lasts at least this long after the last request of a session
#define UPD_BAD_RETRY_MS     600000     // a bad firmware is tried again after this, so a passing cause cannot kill the headset
#define UPD_TRIES_MAX        3          // starts of an old firmware that never brought Bluetooth up

// NVS namespace "upd" of the shared nvs partition: both firmwares read and write it.
#define UPD_NVS_NS           "upd"
#define UPD_NVS_TRIES        "tries"    // u8: +1 by the updater before each start of the firmware (1 at the install of a
                                        // new image), 0 once the firmware is up
#define UPD_NVS_FRESH        "fresh"    // u8: 1 from the install of a new image until it passed its self-test
#define UPD_NVS_STAY         "stay"     // u8: written by "upd": 1 stay in the updater, 2 the same with a wrong Wi-Fi password

#define UPD_STAY_NORMAL      1
#define UPD_STAY_NOWIFI_TEST 2

typedef enum {
    UPD_APP_NONE = 0,       // no valid image in ota_0
    UPD_APP_OK,             // a firmware that came up before
    UPD_APP_FRESH,          // just installed, its first start is still ahead
    UPD_APP_BAD,            // a new image that failed, or an old one that keeps failing: not started
} upd_app_t;

upd_app_t upd_app_state(int image_ok, int fresh, uint32_t tries);
const char *upd_app_name(upd_app_t a);

typedef enum {
    UPD_STAY = 0,
    UPD_BOOT_APP,           // the window is over: start the firmware
    UPD_BOOT_RETRY,         // the same for a bad firmware after UPD_BAD_RETRY_MS: clear its marks first
    UPD_BOOT_NEW,           // a new image is in: start it now
} upd_step_t;

typedef struct {
    uint32_t start_ms, window_ms, last_active_ms;
    upd_app_t app;
    uint32_t done_seen;
    int have_done, have_active;
} upd_watch_t;

// stay: the value of UPD_NVS_STAY at this start (0 none).
void upd_watch_init(upd_watch_t *w, uint32_t now_ms, upd_app_t app, uint8_t stay);
// active: a session is open on either receiver now; done: images activated so far (the counters of both receivers).
upd_step_t upd_watch_step(upd_watch_t *w, uint32_t now_ms, int active, uint32_t done);
// The image check came back (ota_0 was empty, now an image is there, or the reverse).
void upd_watch_set_app(upd_watch_t *w, upd_app_t app);
const char *upd_step_name(upd_step_t s);
// Seconds until the window ends; -1 when it never ends by itself (no firmware).
int32_t upd_watch_left_s(const upd_watch_t *w, uint32_t now_ms);

// The command "upd [8]" of the headset firmware. Returns the value for UPD_NVS_STAY (1 or 2) and writes "upd ok ...",
// or returns 0 and writes the refusal. Refused only during a call: the updater has no Bluetooth, the call would drop.
uint8_t upd_command(const char *args, int sco_on, char *out, size_t cap);
