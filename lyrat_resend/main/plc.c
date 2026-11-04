/*
 * SPDX-FileCopyrightText: 2015-2021 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Based on components/bt/host/bluedroid/external/sbc/plc/sbc_plc.c of ESP-IDF v5.5.2. That file is compiled out when
 * CONFIG_BT_HFP_USE_EXTERNAL_CODEC is on. Changed here: the parameters depend on the mode, long losses fade to silence,
 * and a codec without memory starts the replica at full level.
 */

#include <string.h>

#include "plc.h"

// Largest case is mSBC: a frame of 120, a search window of 256, a template of 64.
#define MAX_FS    PLC_MAX_FRAME
#define MAX_N     256
#define MAX_RT    36
#define MAX_OLAL  16
#define MAX_HIST  (MAX_N + MAX_FS - 1)

typedef struct {
    int fs, n, m, lhist, rt, olal;
    const float *rcos;
} plc_cfg_t;

// Raised cosine for the overlap-add, from sbc_plc.c.
static const float s_rcos16[16] = {
    0.99148655f, 0.96623611f, 0.92510857f, 0.86950446f,
    0.80131732f, 0.72286918f, 0.63683150f, 0.54613418f,
    0.45386582f, 0.36316850f, 0.27713082f, 0.19868268f,
    0.13049554f, 0.07489143f, 0.03376389f, 0.00851345f,
};

static const float s_rcos8[8] = {
    0.96984631f, 0.88302222f, 0.75000000f, 0.58682409f,
    0.41317591f, 0.25000000f, 0.11697778f, 0.03015369f,
};

// The same times in both modes: search window 16 ms, template 4 ms, overlap 1 ms. Reconvergence 2.25 ms is the time the
// SBC filters need after a loss. CVSD needs none: on the recordings of goal 044 the signal is back as soon as the
// half-band filter lets it through (045 sandbox/scripts/cvsd_after_loss.py).
static const plc_cfg_t s_cfg[] = {
    [PLC_MSBC] = { .fs = 120, .n = 256, .m = 64, .lhist = 256 + 120 - 1, .rt = 36, .olal = 16, .rcos = s_rcos16 },
    [PLC_CVSD] = { .fs = 60,  .n = 128, .m = 32, .lhist = 128 + 60 - 1,  .rt = 0,  .olal = 8,  .rcos = s_rcos8 },
};

// A repeated period is fine for a moment and a buzz after that: 22.5 ms at full level, then 45 ms down to silence.
#define HOLD_FRAMES  3
#define FADE_FRAMES  6

static const plc_cfg_t *c = &s_cfg[PLC_MSBC];
static int16_t s_hist[MAX_HIST + MAX_FS + MAX_RT + MAX_OLAL];
static int     s_bestlag;
static int     s_nbf;       // bad frames in a row
static float   s_gain;      // of the concealed signal, on the way to silence

static float sqrt_fast(float x)
{
    union {
        int i;
        float y;
    } u;
    float x2 = x * 0.5f;
    u.y = x;
    u.i = 0x5f375a86 - (u.i >> 1);
    u.y = u.y * (1.5f - (x2 * u.y * u.y));
    return x * u.y;
}

// The lag of the best normalized correlation with the last template in the history. The energies are kept as integers
// and slide with the lag, which leaves one multiplication per sample and lag out of three.
static int pattern_match(const int16_t *y)
{
    const int16_t *x = &y[c->lhist - c->m];
    int64_t x2 = 0, y2 = 0;
    for (int i = 0; i < c->m; i++) {
        x2 += (int32_t)x[i] * x[i];
        y2 += (int32_t)y[i] * y[i];
    }
    float best = -2.0f;
    int   lag  = 0;
    for (int n = 0; n < c->n; n++) {
        float num = 0;
        for (int i = 0; i < c->m; i++) {
            num += (float)x[i] * y[n + i];
        }
        float den = sqrt_fast((float)x2 * (float)y2);
        float cn  = den > 0 ? num / den : 0;
        if (cn > best) {
            lag  = n;
            best = cn;
        }
        y2 += (int32_t)y[n + c->m] * y[n + c->m] - (int32_t)y[n] * y[n];
    }
    return lag;
}

static float amplitude_match(const int16_t *y, int lag)
{
    float sumx = 0, sumy = 0.000001f;
    for (int i = 0; i < c->fs; i++) {
        int a = y[c->lhist - c->fs + i];
        int b = y[lag + i];
        sumx += (float)(a < 0 ? -a : a);
        sumy += (float)(b < 0 ? -b : b);
    }
    float sf = sumx / sumy;
    if (sf < 0.75f) sf = 0.75f;
    if (sf > 1.2f)  sf = 1.2f;
    return sf;
}

static int16_t crop(float v)
{
    if (v > 32767.0f)  v = 32767.0f;
    if (v < -32768.0f) v = -32768.0f;
    return (int16_t)v;
}

void plc_reset(plc_mode_t mode)
{
    c = &s_cfg[mode];
    s_nbf = 0;
    s_bestlag = 0;
    s_gain = 1.0f;
    memset(s_hist, 0, sizeof(s_hist));
}

int plc_frame(void)
{
    return c->fs;
}

void plc_bad(const int16_t *zir, int16_t *out)
{
    int i = 0;
    int fs = c->fs, ol = c->olal, rt = c->rt, lh = c->lhist;
    const float *rc = c->rcos;
    int16_t *h = s_hist;

    s_nbf++;
    if (s_nbf == 1) {
        s_bestlag = pattern_match(h) + c->m;
        float sf = amplitude_match(h, s_bestlag);

        // The ringing of the codec's filters fades into the replica. Without a codec memory there is nothing to fade
        // from, and the search has already chosen a place that continues the signal.
        if (zir) {
            for (i = 0; i < ol; i++) {
                h[lh + i] = crop(zir[i] * rc[i] + sf * h[s_bestlag + i] * rc[ol - i - 1]);
            }
        }
        for (; i < fs; i++) {
            h[lh + i] = crop(sf * h[s_bestlag + i]);
        }
        for (; i < fs + ol; i++) {
            h[lh + i] = crop(sf * h[s_bestlag + i] * rc[i - fs] + h[s_bestlag + i] * rc[ol - 1 - i + fs]);
        }
        for (; i < fs + rt + ol; i++) {
            h[lh + i] = h[s_bestlag + i];
        }
    } else {
        for (; i < fs + rt + ol; i++) {
            h[lh + i] = h[s_bestlag + i];
        }
    }

    if (s_nbf > HOLD_FRAMES) {
        float step = 1.0f / (float)(FADE_FRAMES * fs);
        for (i = 0; i < fs; i++) {
            out[i] = (int16_t)(h[lh + i] * s_gain);
            s_gain = s_gain > step ? s_gain - step : 0;
        }
    } else {
        memcpy(out, &h[lh], (size_t)fs * sizeof(int16_t));
    }
    memmove(h, &h[fs], (size_t)(lh + rt + ol) * sizeof(int16_t));
}

void plc_good(const int16_t *in, int16_t *out)
{
    int i = 0;
    int fs = c->fs, ol = c->olal, rt = c->rt, lh = c->lhist;
    const float *rc = c->rcos;
    int16_t *h = s_hist;

    if (s_nbf > 0) {
        for (i = 0; i < rt; i++) {
            out[i] = (int16_t)(h[lh + i] * s_gain);
        }
        for (; i < rt + ol; i++) {
            out[i] = (int16_t)(h[lh + i] * s_gain * rc[i - rt] + in[i] * rc[ol - 1 - i + rt]);
        }
    }
    for (; i < fs; i++) {
        out[i] = in[i];
    }
    memcpy(&h[lh], out, (size_t)fs * sizeof(int16_t));
    memmove(h, &h[fs], (size_t)lh * sizeof(int16_t));
    s_nbf = 0;
    s_gain = 1.0f;
}
