#pragma once
#include <stdint.h>
#include <stddef.h>

/*
 * Spectral Noise Suppression — port of noisereduce stationary spectral gating.
 *
 * Internal pipeline (each step toggleable for debugging):
 *   Step A: FFT (windowed 512-point)
 *   Step B: Compute magnitude → dB per bin
 *   Step C: Compare to noise threshold → binary mask
 *   Step D: Apply mask to complex FFT bins (gain=1.0 or 0.02)
 *   Step E: IFFT + overlap-add → output
 *
 * Debug modes (set via spectral_ns_set_mode):
 *   0 = OFF (passthrough, no FFT)
 *   1 = FFT→IFFT only (no mask — tests FFT reconstruction)
 *   2 = Mask but log only (shows % masked bins, doesn't apply)
 *   3 = Mask with soft gain (floor_gain applied, adjustable via snsg)
 *   4 = Full pipeline (hard mask: 1.0 or floor_gain)
 */

#define SNS_FFT_SIZE      512
#define SNS_N_BINS        (SNS_FFT_SIZE / 2 + 1)   // 257
#define SNS_HOP           128
#define SNS_FRAME_SAMPLES 240

// Initialize (call once)
void spectral_ns_init(void);

// Feed noise-only audio for profile estimation
void spectral_ns_feed_noise(const int16_t *samples, size_t count);

// Finalize noise profile
void spectral_ns_finalize_noise(float n_std_thresh);

// Set noise threshold manually
void spectral_ns_set_threshold(const float *thresh_db, size_t n_bins);

// Process audio: input → output
int spectral_ns_process(const int16_t *in, int16_t *out, size_t n_samples);

// Is noise profile ready?
int spectral_ns_is_calibrated(void);

// Set processing mode: 0=off, 1=FFT-IFFT, 2=mask-log, 3=soft-mask, 4=hard-mask
void spectral_ns_set_mode(int mode);
int  spectral_ns_get_mode(void);

// Set noise floor gain (0.0 - 1.0, default 0.02 = -34dB suppression)
void spectral_ns_set_floor_gain(float gain);
