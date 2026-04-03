#pragma once
#include <stddef.h>
#include <stdint.h>

// -------------------------------------------------------------------
// Acoustic Echo Cancellation (simple delay-and-subtract)
//
// Enable/disable with a single define:
//   #define AEC_ENABLED 1   → AEC active
//   #define AEC_ENABLED 0   → all functions compile to no-ops
//
// Tuning (if AEC_ENABLED):
//   AEC_DELAY_SAMPLES  — acoustic propagation delay earphone→mic
//                        at 8 kHz: 1 sample = 0.125 ms
//   AEC_ALPHA_Q8       — echo coupling factor, Q8 fixed-point
//                        256 = 1.0 (full cancellation), 80 ≈ 0.31
//                        too high → voice sounds hollow
//                        too low  → echo remains
// -------------------------------------------------------------------

#define AEC_ENABLED        1     // 1 = on, 0 = off

// Delay from reference write to echo arrival in mic.
// Pipeline: s_rb_tx → i2s_tx_task → WM8960 DAC → acoustic → WM8960 ADC.
// At 8 kHz, 1 sample = 0.125 ms. Start at 0 and tune if echo remains.
#define AEC_DELAY_SAMPLES  0

// Echo coupling factor, Q8 fixed-point (256 = 1.0).
// Headset mic is right next to speaker → strong coupling. Tune up if echo remains,
// tune down if voice sounds hollow.
#define AEC_ALPHA_Q8       128   // 0.5 — moderate-strong cancellation

// Call once at startup (zero-initialises the reference buffer)
void aec_init(void);

// Feed reference samples (amplified call audio going to headphones).
// Call from i2s_rx_task after SW_GAIN is applied.
void aec_write_reference(const int16_t *samples, size_t count);

// Apply echo cancellation to mic samples in-place (mono, count samples).
// Call from mic_rx_task before sending to phone / UDP.
void aec_process(int16_t *samples, size_t count);
