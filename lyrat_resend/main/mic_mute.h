// Microphone mute of the headset (goal 111): the rules, without platform calls.
//
// The key: "Rec" of the LyraT v4.3 (GPIO36, pressed = 0). GPIO36/39 of the ESP32 get ~80 ns pulls to 0 while the SAR ADC
// or the radio powers up (erratum), so the key is polled every MM_POLL_MS, never on an interrupt, and a change counts
// only after MM_DEBOUNCE equal samples in a row. The mute toggles when the key is let go after a press shorter than
// MM_SHORT_MAX_MS: a stuck-low input never toggles, and a long hold does nothing.
// The sound: while muted the frames to the phone keep coming (the SCO channel is not touched), their samples go to zero
// through a linear ramp of MM_RAMP_SAMPLES (10 ms at 16 kHz) and back, so neither edge clicks.
// The console command "mute on|off|toggle|?" (mm_command). The state is not stored: a boot starts with the mic on.
// main.c glues this to the key, the LED (GPIO22, lit while muted), the mic task, the log and the heartbeat (flags bit 2);
// the PC tests build it too (otahost.dll, tests/test_mic_mute.py).
#pragma once

#include <stddef.h>
#include <stdint.h>

#define MM_POLL_MS       10
#define MM_DEBOUNCE      3          // equal samples in a row for a change of the key
#define MM_SHORT_MAX_MS  2000       // a press held this long or longer does not toggle
#define MM_RAMP_SAMPLES  160        // 10 ms at 16 kHz
#define MM_GAIN_ONE      32768      // gain Q15

// mm_set() how
#define MM_OFF     0
#define MM_ON      1
#define MM_TOGGLE  2

// mm_key_poll() result
#define MM_KEY_NONE    0
#define MM_KEY_TOGGLE  1            // a short press ended: toggle the mute
#define MM_KEY_LONG    2            // a long press ended: nothing (logged)

typedef struct {
    uint8_t  muted;                 // the wanted state; written by the key and the console, read by the mic task
    uint8_t  key_down;              // the debounced key
    uint8_t  key_count;             // samples in a row that differ from key_down
    uint8_t  pad;
    uint32_t down_ms;               // when the press was taken
    int32_t  gain;                  // Q15, MM_GAIN_ONE = mic on; touched only by mm_apply (the mic task)
} mm_t;

void mm_init(mm_t *m);

// One sample of the key every MM_POLL_MS. pressed: 1 when the key reads pressed. now_ms: a millisecond clock.
int mm_key_poll(mm_t *m, int pressed, uint32_t now_ms);

// Returns 1 when the state changed.
int mm_set(mm_t *m, int how);

// "mute <args>": "" or "?" shows, on|off|toggle sets. out: the reply line. Returns 1 changed, 0 not, -1 bad args.
int mm_command(mm_t *m, const char *args, char *out, size_t out_len);

// The samples to the phone, in place, after everything else of the mic path.
void mm_apply(mm_t *m, int16_t *buf, size_t n);

const char *mm_state_name(int muted);
