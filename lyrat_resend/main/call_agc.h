// Automatic gain of the call audio toward the headphones (goal 113), plain C so the PC builds it too (agchost.dll).
//
// Two gains, the smaller one wins at the end of each frame, and the gain moves linearly over the frame's samples:
//  - a slow level control: the talker's level over speech frames (power mean, about 3 s), the gain toward the target
//    level by at most +2 dB/s and -4 dB/s (+6 / -12 dB/s over the first 3 s of speech of a call, to fit the line),
//    changed only on speech frames (held in pauses, silence and noise). A speech frame: 10 dB over the noise floor
//    (minimum over 2 s) and a level that keeps moving (steady noise does not);
//  - a peak guard: the output is one frame late, so the peaks of the frame being sent and of the next one are known;
//    the gain at both ends of every frame keeps peak * gain <= CALL_AGC_LIMIT, so no sample clips, with no curve on
//    the samples. Released by 6 dB/s.
// See goals_harness/phone-then-pc/113-downlink-agc/DECISIONS.md D1.
#pragma once

#include <stdint.h>

#define CALL_AGC_MAX_FRAME  256
#define CALL_AGC_LIMIT      29491       // 0.9 FS: the highest sample the guard lets out
#define CALL_AGC_SUBWIN     8           // noise floor: minimum over 8 sub-windows

typedef struct {
    // settings
    float target_db;        // active speech level at the output, dBFS (-18)
    float gain_min;         // linear (0.25)
    float gain_max;         // linear (8)
    float init_gain;        // linear: the gain at start and reset (call_gain)
    // state
    int16_t delay[CALL_AGC_MAX_FRAME];   // the frame waiting to be sent
    int     n;              // frame size, fixed by the first call
    int     have_delay;
    float   delay_peak;
    float   g_prev;         // gain at the end of the last sent frame
    float   g_agc;
    float   g_lim;
    float   level_p;        // speech power mean (linear, FS^2 = 1)
    float   speech_s;       // seconds of speech so far (for the running mean at start)
    float   e_smooth;       // smoothed frame energy (noise floor input)
    float   sub_min[CALL_AGC_SUBWIN];
    float   cur_min;
    int     sub_idx, sub_frames;
    float   floor_p;        // noise floor power
    // stats (read by the console, reset with the state)
    uint32_t frames, speech_frames, lim_frames, clips;
    float   last_rms_db;
    float   flux_db;        // mean frame-to-frame level change: speech needs >= 1 dB
    int     last_speech;
} call_agc_t;

void call_agc_init(call_agc_t *a, float init_gain);     // defaults, then reset
void call_agc_reset(call_agc_t *a);                     // state only, keeps the settings
void call_agc_drop_delay(call_agc_t *a);                // a new stream: the frame left from the old one is not sent

// One frame in, the previous frame out (zeros on the first call). n <= CALL_AGC_MAX_FRAME and the same every call.
// in and out may be the same buffer.
void call_agc_process(call_agc_t *a, const int16_t *in, int16_t *out, int n);

float call_agc_gain_db(const call_agc_t *a);
float call_agc_level_db(const call_agc_t *a);   // talker level at the input, dBFS
float call_agc_floor_db(const call_agc_t *a);   // noise floor at the input, dBFS
