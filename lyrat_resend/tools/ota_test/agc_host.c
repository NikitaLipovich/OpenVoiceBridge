// PC side of the call AGC (goal 113): the firmware's main/call_agc.c on whole signals, for tools/bench/agc.py.
#include <stdlib.h>
#include "call_agc.h"

#define EXPORT __declspec(dllexport)

EXPORT int host_agc_size(void) { return (int)sizeof(call_agc_t); }

EXPORT void host_agc_init(call_agc_t *a, float init_gain, float target_db, float gain_min, float gain_max)
{
    call_agc_init(a, init_gain);
    a->target_db = target_db;
    a->gain_min = gain_min;
    a->gain_max = gain_max;
    call_agc_reset(a);
}

// count samples in whole frames of n; per frame: gain at its end (dB, of the frame sent), speech flag, guard flag.
EXPORT void host_agc_run(call_agc_t *a, const int16_t *in, int16_t *out, int count, int n, float *gain_db,
                         int *speech, int *lim)
{
    for (int f = 0; f * n + n <= count; f++) {
        uint32_t lf = a->lim_frames;
        call_agc_process(a, in + f * n, out + f * n, n);
        gain_db[f] = call_agc_gain_db(a);
        speech[f] = a->last_speech;
        lim[f] = a->lim_frames != lf;
    }
}

EXPORT void host_agc_stats(const call_agc_t *a, float *level_db, float *floor_db, unsigned *clips, unsigned *lim_frames)
{
    *level_db = call_agc_level_db(a);
    *floor_db = call_agc_floor_db(a);
    *clips = a->clips;
    *lim_frames = a->lim_frames;
}
