// Microphone mute (goal 111): key debounce, the command and the ramp. See mic_mute.h.
#include "mic_mute.h"

#include <stdio.h>
#include <string.h>

#define MM_STEP ((MM_GAIN_ONE + MM_RAMP_SAMPLES - 1) / MM_RAMP_SAMPLES)

void mm_init(mm_t *m)
{
    memset(m, 0, sizeof(*m));
    m->gain = MM_GAIN_ONE;
}

int mm_key_poll(mm_t *m, int pressed, uint32_t now_ms)
{
    pressed = pressed ? 1 : 0;
    if (pressed == m->key_down) {
        m->key_count = 0;
        return MM_KEY_NONE;
    }
    if (++m->key_count < MM_DEBOUNCE) return MM_KEY_NONE;
    m->key_count = 0;
    m->key_down = (uint8_t)pressed;
    if (pressed) {
        m->down_ms = now_ms;
        return MM_KEY_NONE;
    }
    return (uint32_t)(now_ms - m->down_ms) < MM_SHORT_MAX_MS ? MM_KEY_TOGGLE : MM_KEY_LONG;
}

int mm_set(mm_t *m, int how)
{
    uint8_t want = how == MM_TOGGLE ? (uint8_t)!m->muted : (uint8_t)(how == MM_ON);
    if (want == m->muted) return 0;
    m->muted = want;
    return 1;
}

const char *mm_state_name(int muted) { return muted ? "on" : "off"; }

int mm_command(mm_t *m, const char *args, char *out, size_t out_len)
{
    while (args && *args == ' ') args++;
    int was = m->muted;
    int how;
    if (!args || !*args || strcmp(args, "?") == 0) {
        snprintf(out, out_len, "mute=%s", mm_state_name(was));
        return 0;
    } else if (strcmp(args, "on") == 0) {
        how = MM_ON;
    } else if (strcmp(args, "off") == 0) {
        how = MM_OFF;
    } else if (strcmp(args, "toggle") == 0) {
        how = MM_TOGGLE;
    } else {
        snprintf(out, out_len, "usage: mute on|off|toggle|?");
        return -1;
    }
    int changed = mm_set(m, how);
    snprintf(out, out_len, "mute=%s (was %s)", mm_state_name(m->muted), mm_state_name(was));
    return changed;
}

void mm_apply(mm_t *m, int16_t *buf, size_t n)
{
    int32_t target = m->muted ? 0 : MM_GAIN_ONE;
    int32_t g = m->gain;
    if (g == target) {
        if (g == 0) memset(buf, 0, n * sizeof(int16_t));
        return;
    }
    for (size_t i = 0; i < n; i++) {
        if (g < target) {
            g += MM_STEP;
            if (g > target) g = target;
        } else if (g > target) {
            g -= MM_STEP;
            if (g < target) g = target;
        }
        buf[i] = (int16_t)(((int32_t)buf[i] * g) / MM_GAIN_ONE);
    }
    m->gain = g;
}
