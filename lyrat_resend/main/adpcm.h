#pragma once
#include <stdint.h>
#include <stddef.h>

/*
 * IMA ADPCM codec — 4:1 compression (16-bit PCM → 4-bit ADPCM)
 *
 * Encoder state must persist between calls (streaming).
 * Decoder on Python side uses the same algorithm.
 */

typedef struct {
    int16_t predicted;   // predicted sample value
    int8_t  index;       // step table index (0-88)
} adpcm_state_t;

// Initialize state
static inline void adpcm_init(adpcm_state_t *state) {
    state->predicted = 0;
    state->index = 0;
}

// Encode PCM samples to ADPCM
// in:  n_samples of int16_t PCM
// out: n_samples/2 bytes of ADPCM (2 nibbles per byte)
// Returns number of bytes written to out
size_t adpcm_encode(adpcm_state_t *state, const int16_t *in, uint8_t *out, size_t n_samples);

// Decode ADPCM to PCM (for testing on ESP32 side)
size_t adpcm_decode(adpcm_state_t *state, const uint8_t *in, int16_t *out, size_t n_bytes);
