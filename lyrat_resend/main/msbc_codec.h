// mSBC frame codec on top of the SBC code the Bluetooth component already builds (see goal 045 DECISIONS D1).
#pragma once

#include <stdint.h>
#include <stdbool.h>

#define MSBC_PCM_SAMPLES   120      // one frame, 7.5 ms at 16 kHz
#define MSBC_FRAME_BYTES   57       // encoded frame without the H2 header and the padding byte

void msbc_codec_reset(void);

// Decodes one frame. Returns false when the frame is not usable (no syncword, CRC mismatch); then pcm is untouched.
bool msbc_decode(const uint8_t *frame, uint32_t len, int16_t *pcm);

// The decoder output for a frame of zero signal: the ringing of its filters, which the concealment fades into.
void msbc_decode_zero(int16_t *pcm);

// Encodes MSBC_PCM_SAMPLES samples into exactly MSBC_FRAME_BYTES bytes.
void msbc_encode(const int16_t *pcm, uint8_t *frame);
