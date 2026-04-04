/*
 * IMA ADPCM encoder/decoder
 * Standard IMA step table and index table.
 */

#include "adpcm.h"

static const int16_t step_table[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767
};

static const int8_t index_table[16] = {
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8
};

static inline int clamp_index(int idx) {
    if (idx < 0) return 0;
    if (idx > 88) return 88;
    return idx;
}

static inline int16_t clamp_sample(int val) {
    if (val > 32767) return 32767;
    if (val < -32768) return -32768;
    return (int16_t)val;
}

static uint8_t encode_sample(adpcm_state_t *state, int16_t sample)
{
    int step = step_table[state->index];
    int diff = sample - state->predicted;
    uint8_t nibble = 0;

    if (diff < 0) {
        nibble = 8;
        diff = -diff;
    }

    if (diff >= step) { nibble |= 4; diff -= step; }
    step >>= 1;
    if (diff >= step) { nibble |= 2; diff -= step; }
    step >>= 1;
    if (diff >= step) { nibble |= 1; }

    // Decode to update predictor (same as decoder would)
    step = step_table[state->index];
    int delta = step >> 3;
    if (nibble & 4) delta += step;
    if (nibble & 2) delta += step >> 1;
    if (nibble & 1) delta += step >> 2;
    if (nibble & 8) delta = -delta;

    state->predicted = clamp_sample(state->predicted + delta);
    state->index = (int8_t)clamp_index(state->index + index_table[nibble]);

    return nibble;
}

size_t adpcm_encode(adpcm_state_t *state, const int16_t *in, uint8_t *out, size_t n_samples)
{
    size_t bytes = 0;
    for (size_t i = 0; i < n_samples; i += 2) {
        uint8_t lo = encode_sample(state, in[i]);
        uint8_t hi = (i + 1 < n_samples) ? encode_sample(state, in[i + 1]) : 0;
        out[bytes++] = (hi << 4) | (lo & 0x0F);
    }
    return bytes;
}

static int16_t decode_sample(adpcm_state_t *state, uint8_t nibble)
{
    int step = step_table[state->index];
    int delta = step >> 3;
    if (nibble & 4) delta += step;
    if (nibble & 2) delta += step >> 1;
    if (nibble & 1) delta += step >> 2;
    if (nibble & 8) delta = -delta;

    state->predicted = clamp_sample(state->predicted + delta);
    state->index = (int8_t)clamp_index(state->index + index_table[nibble & 0x0F]);

    return state->predicted;
}

size_t adpcm_decode(adpcm_state_t *state, const uint8_t *in, int16_t *out, size_t n_bytes)
{
    size_t samples = 0;
    for (size_t i = 0; i < n_bytes; i++) {
        out[samples++] = decode_sample(state, in[i] & 0x0F);
        out[samples++] = decode_sample(state, (in[i] >> 4) & 0x0F);
    }
    return samples;
}
