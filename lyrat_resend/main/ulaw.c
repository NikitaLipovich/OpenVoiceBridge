/*
 * G.711 µ-law encoder/decoder — ITU-T standard table-based implementation.
 */

#include "ulaw.h"

#define BIAS 0x84
#define CLIP 32635

static const int16_t exp_lut[256] = {
    0,0,1,1,2,2,2,2,3,3,3,3,3,3,3,3,
    4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,
    5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,
    5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,
    6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
    6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
    6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
    6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
    7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7
};

static uint8_t pcm_to_ulaw(int16_t pcm_val)
{
    int sign = (pcm_val >> 8) & 0x80;
    if (sign) pcm_val = -pcm_val;
    if (pcm_val > CLIP) pcm_val = CLIP;
    pcm_val += BIAS;

    int exponent = exp_lut[(pcm_val >> 7) & 0xFF];
    int mantissa = (pcm_val >> (exponent + 3)) & 0x0F;

    return ~(sign | (exponent << 4) | mantissa);
}

static int16_t ulaw_to_pcm(uint8_t u_val)
{
    u_val = ~u_val;
    int sign = u_val & 0x80;
    int exponent = (u_val >> 4) & 0x07;
    int mantissa = u_val & 0x0F;

    int16_t sample = (int16_t)((mantissa << (exponent + 3)) + BIAS * (1 << exponent) - BIAS);

    return sign ? -sample : sample;
}

void ulaw_encode(const int16_t *pcm, uint8_t *ulaw, size_t n_samples)
{
    for (size_t i = 0; i < n_samples; i++)
        ulaw[i] = pcm_to_ulaw(pcm[i]);
}

void ulaw_decode(const uint8_t *ulaw, int16_t *pcm, size_t n_bytes)
{
    for (size_t i = 0; i < n_bytes; i++)
        pcm[i] = ulaw_to_pcm(ulaw[i]);
}
