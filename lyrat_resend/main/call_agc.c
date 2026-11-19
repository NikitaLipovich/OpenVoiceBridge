// Automatic gain of the call audio toward the headphones (goal 113). See call_agc.h.
#include "call_agc.h"

#include <math.h>
#include <string.h>

#define RATE            16000.0f
#define FS              32768.0f
#define SPEECH_SNR      10.0f       // dB over the noise floor: a speech frame
#define SPEECH_MIN_DB   (-60.0f)    // and above this level at the input
#define RAISE_SNR       10.0f       // talker level over the floor needed to raise the gain
#define FLOOR_MIN_DB    (-75.0f)    // digital zeros do not pull the floor below this
#define LEVEL_TAU_S     3.0f        // talker level: power mean over this much speech
#define LEVEL_SEED_S    0.5f        // the starting level counts as this much speech
#define UP_DB_S         2.0f
#define DOWN_DB_S       4.0f
#define LEARN_UP_DB_S   6.0f        // the first LEVEL_TAU_S of speech: fitting to the line and the talker
#define LEARN_DOWN_DB_S 12.0f
#define LIM_RELEASE_DB_S 6.0f
#define ENERGY_TAU_S    0.03f
#define SUBWIN_S        0.25f
#define FLUX_TAU_S      0.3f        // frame-to-frame level change, averaged: speech moves, steady noise does not
#define FLUX_CLIP_DB    6.0f        // one step (a sound starting) counts at most this much
#define FLUX_MIN_DB     1.0f        // real calls: speech median 1.4-2.5 dB, steady noise <= 0.81 dB (goal 113 sim)
#define BIG             1e9f

static float db_to_lin(float db) { return powf(10.0f, db / 20.0f); }
static float lin_to_db(float g) { return 20.0f * log10f(g > 1e-9f ? g : 1e-9f); }
static float pow_to_db(float p) { return 10.0f * log10f(p > 1e-12f ? p : 1e-12f); }

void call_agc_init(call_agc_t *a, float init_gain)
{
    memset(a, 0, sizeof(*a));
    a->target_db = -18.0f;
    a->gain_min = 0.25f;
    a->gain_max = 8.0f;
    a->init_gain = init_gain;
    call_agc_reset(a);
}

void call_agc_reset(call_agc_t *a)
{
    float g0 = a->init_gain;
    if (g0 < a->gain_min) g0 = a->gain_min;
    if (g0 > a->gain_max) g0 = a->gain_max;
    memset(a->delay, 0, sizeof(a->delay));
    a->n = 0;
    a->have_delay = 0;
    a->delay_peak = 0;
    a->g_prev = g0;
    a->g_agc = g0;
    a->g_lim = 16.0f;
    // The talker is assumed at the level that the starting gain brings to the target: the call starts as before.
    float l0 = a->target_db - lin_to_db(g0);
    a->level_p = powf(10.0f, l0 / 10.0f);
    a->speech_s = LEVEL_SEED_S;
    a->e_smooth = 0;
    for (int i = 0; i < CALL_AGC_SUBWIN; i++) a->sub_min[i] = BIG;
    a->cur_min = BIG;
    a->sub_idx = 0;
    a->sub_frames = 0;
    a->floor_p = powf(10.0f, FLOOR_MIN_DB / 10.0f);
    a->frames = a->speech_frames = a->lim_frames = a->clips = 0;
    a->last_rms_db = -120.0f;
    a->flux_db = 0;
    a->last_speech = 0;
}

void call_agc_drop_delay(call_agc_t *a)
{
    a->have_delay = 0;
    a->delay_peak = 0;
}

// Level, noise floor and the slow gain from one frame of input.
static void analyse(call_agc_t *a, float e, float dt)
{
    a->e_smooth += (1.0f - expf(-dt / ENERGY_TAU_S)) * (e - a->e_smooth);
    if (a->e_smooth < a->cur_min) a->cur_min = a->e_smooth;
    if ((float)(++a->sub_frames) * dt >= SUBWIN_S) {
        a->sub_min[a->sub_idx] = a->cur_min;
        a->sub_idx = (a->sub_idx + 1) % CALL_AGC_SUBWIN;
        a->cur_min = BIG;
        a->sub_frames = 0;
    }
    float fl = a->cur_min;
    for (int i = 0; i < CALL_AGC_SUBWIN; i++) if (a->sub_min[i] < fl) fl = a->sub_min[i];
    float fl_min = powf(10.0f, FLOOR_MIN_DB / 10.0f);
    a->floor_p = fl < fl_min ? fl_min : fl;

    float e_db = pow_to_db(e);
    float step = fabsf(e_db - a->last_rms_db);
    if (step > FLUX_CLIP_DB) step = FLUX_CLIP_DB;
    a->flux_db += (dt / FLUX_TAU_S) * (step - a->flux_db);
    a->last_rms_db = e_db;
    int speech = e_db > pow_to_db(a->floor_p) + SPEECH_SNR && e_db > SPEECH_MIN_DB && a->flux_db >= FLUX_MIN_DB;
    a->last_speech = speech;
    if (!speech) return;        // pauses, silence and noise: the gain holds
    a->speech_frames++;

    a->speech_s += dt;
    float tau = a->speech_s < LEVEL_TAU_S ? a->speech_s : LEVEL_TAU_S;
    a->level_p += (dt / tau) * (e - a->level_p);

    float l_db = pow_to_db(a->level_p);
    float want = a->target_db - l_db;
    float lo = lin_to_db(a->gain_min), hi = lin_to_db(a->gain_max);
    if (want < lo) want = lo;
    if (want > hi) want = hi;
    float cur = lin_to_db(a->g_agc);
    int learning = a->speech_s < LEVEL_TAU_S + LEVEL_SEED_S;
    if (want > cur) {
        if (l_db - pow_to_db(a->floor_p) >= RAISE_SNR) {
            float up = (learning ? LEARN_UP_DB_S : UP_DB_S) * dt;
            cur += (want - cur < up) ? want - cur : up;
        }
    } else {
        float down = (learning ? LEARN_DOWN_DB_S : DOWN_DB_S) * dt;
        cur -= (cur - want < down) ? cur - want : down;
    }
    a->g_agc = db_to_lin(cur);
}

static float frame_peak(const int16_t *x, int n, float *energy)
{
    int32_t pk = 0;
    float e = 0;
    for (int i = 0; i < n; i++) {
        int32_t v = x[i] < 0 ? -(int32_t)x[i] : x[i];
        if (v > pk) pk = v;
        float f = (float)x[i] / FS;
        e += f * f;
    }
    *energy = e / (float)n;
    return (float)pk;
}

void call_agc_process(call_agc_t *a, const int16_t *in, int16_t *out, int n)
{
    if (n <= 0) return;
    if (n > CALL_AGC_MAX_FRAME) n = CALL_AGC_MAX_FRAME;
    a->n = n;
    float dt = (float)n / RATE;
    int16_t next[CALL_AGC_MAX_FRAME];
    memcpy(next, in, (size_t)n * sizeof(int16_t));     // in and out may be one buffer

    float e;
    float p_next = frame_peak(next, n, &e);
    analyse(a, e, dt);
    a->frames++;

    if (!a->have_delay) {
        memset(out, 0, (size_t)n * sizeof(int16_t));
    } else {
        float p_cur = a->delay_peak;
        float p = p_cur > p_next ? p_cur : p_next;
        float cap = (float)CALL_AGC_LIMIT / (p > 1.0f ? p : 1.0f);
        // The end of the last frame was chosen knowing this frame's peak; this only acts after a reset or a new
        // stream, where that was not known.
        float cap_cur = (float)CALL_AGC_LIMIT / (p_cur > 1.0f ? p_cur : 1.0f);
        if (a->g_prev > cap_cur) a->g_prev = cap_cur;

        float g_lim = a->g_lim * db_to_lin(LIM_RELEASE_DB_S * dt);
        if (g_lim > 16.0f) g_lim = 16.0f;
        if (g_lim > cap) g_lim = cap;
        a->g_lim = g_lim;
        float g_end = a->g_agc;
        if (g_lim < g_end) {
            g_end = g_lim;
            a->lim_frames++;
        }

        float g0 = a->g_prev, dg = (g_end - g0) / (float)n;
        for (int i = 0; i < n; i++) {
            float g = g0 + dg * (float)(i + 1);
            float y = g * (float)a->delay[i];
            int32_t s = (int32_t)lrintf(y);
            if (s > 32767) { s = 32767; a->clips++; }
            if (s < -32767) { s = -32767; a->clips++; }
            out[i] = (int16_t)s;
        }
        a->g_prev = g_end;
    }
    memcpy(a->delay, next, (size_t)n * sizeof(int16_t));
    a->delay_peak = p_next;
    a->have_delay = 1;
}

float call_agc_gain_db(const call_agc_t *a) { return lin_to_db(a->g_prev); }
float call_agc_level_db(const call_agc_t *a) { return pow_to_db(a->level_p); }
float call_agc_floor_db(const call_agc_t *a) { return pow_to_db(a->floor_p); }
