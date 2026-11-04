// Packet loss concealment by pitch search, for both HFP codecs. The method is that of the stack's sbc_plc.c
// (Goodman pattern matching), which the external codec path leaves out.
#pragma once

#include <stdint.h>

typedef enum {
    PLC_MSBC,   // 16 kHz, 120 samples a frame
    PLC_CVSD,   // 8 kHz, 60 samples a frame
} plc_mode_t;

#define PLC_MAX_FRAME   120

void plc_reset(plc_mode_t mode);

// A good frame: passes it to out, fading from the concealed signal when a loss has just ended.
void plc_good(const int16_t *in, int16_t *out);

// A lost frame. zir: the codec's ringing for this frame, or NULL if the codec has no memory (CVSD).
void plc_bad(const int16_t *zir, int16_t *out);

// Samples per frame of the current mode.
int plc_frame(void);
