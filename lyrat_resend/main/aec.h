#pragma once
#include <stddef.h>
#include <stdint.h>

// -------------------------------------------------------------------
// Acoustic Echo Cancellation (simple delay-and-subtract)
//
// Tuning:
//   AEC_DELAY_SAMPLES  — acoustic propagation delay earphone→mic
//                        at 16 kHz: 1 sample = 0.0625 ms
//   AEC_ALPHA_Q8       — echo coupling factor, Q8 fixed-point
//                        256 = 1.0 (full cancellation), 80 ≈ 0.31
//                        too high → voice sounds hollow
//                        too low  → echo remains
// -------------------------------------------------------------------

// Delay from reference write to echo arrival in mic.
// Pipeline: rb_call_rx → i2s_tx_task → ES8388 DAC → acoustic → ES8388 ADC.
// At 16 kHz, 1 sample = 0.0625 ms. Start at 0 and tune if echo remains.
#define AEC_DELAY_SAMPLES  0

// Echo coupling factor, Q8 fixed-point (256 = 1.0).
#define AEC_ALPHA_Q8       128   // 0.5 — moderate-strong cancellation

// Call once at startup
void aec_init(void);

// Feed reference samples (call audio going to headphones).
void aec_write_reference(const int16_t *samples, size_t count);

// Apply echo cancellation to mic samples in-place.
void aec_process(int16_t *samples, size_t count);
