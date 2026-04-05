#pragma once
#include <stdint.h>
#include <stddef.h>

/*
 * G.711 µ-law codec — 2:1 compression (16-bit PCM → 8-bit µ-law)
 * No state between samples — each sample encoded independently.
 * No oscillation artifacts like ADPCM.
 */

// Encode PCM to µ-law: n_samples int16 → n_samples uint8
void ulaw_encode(const int16_t *pcm, uint8_t *ulaw, size_t n_samples);

// Decode µ-law to PCM: n_bytes uint8 → n_bytes int16
void ulaw_decode(const uint8_t *ulaw, int16_t *pcm, size_t n_bytes);
