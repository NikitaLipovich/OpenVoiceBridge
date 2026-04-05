/*
 * Spectral Noise Suppression — stationary spectral gating.
 *
 * Debug modes (set via CLI "sns 0/1/2"):
 *   0 = OFF (passthrough)
 *   1 = FFT→IFFT only (no mask — tests reconstruction quality)
 *   2 = Full pipeline (mask active — actual denoising)
 */

#include "spectral_ns.h"
#include <string.h>
#include <math.h>
#include "esp_log.h"
#include "esp_attr.h"
#include "dsps_fft2r.h"

#define TAG "SNS"

#define EPS               1e-10f
#define OVERLAP_BUF_SIZE  (SNS_FFT_SIZE * 2)
#define OLA_NORM          1.5f

// ── Config ──────────────────────────────────────────────────────────────────

static int   s_mode = 2;               // 0=off, 1=fft-only, 2=full
static float s_floor_gain = 0.02f;     // noise bin attenuation (0.02 = -34dB)

// ── Buffers (large in PSRAM) ────────────────────────────────────────────────

static float s_hann[SNS_FFT_SIZE];
static float s_noise_thresh[SNS_N_BINS];
static float s_prev_gain[SNS_N_BINS];         // temporal gain smoothing (anti-musical-noise)
static int   s_calibrated = 0;

EXT_RAM_BSS_ATTR static float s_noise_sum_db[SNS_N_BINS];
EXT_RAM_BSS_ATTR static float s_noise_sum_sq_db[SNS_N_BINS];
static int   s_noise_frames;

EXT_RAM_BSS_ATTR static float s_in_ring[SNS_FFT_SIZE];
static int   s_in_pos = 0;

EXT_RAM_BSS_ATTR static float s_ola_buf[OVERLAP_BUF_SIZE];
static int   s_ola_write = 0;
static int   s_ola_read  = 0;

static int   s_hop_count = 0;

EXT_RAM_BSS_ATTR static float s_out_buf[SNS_FRAME_SAMPLES * 2];
static int   s_out_count = 0;

EXT_RAM_BSS_ATTR static float s_fft_buf[SNS_FFT_SIZE * 2];

// Debug: count how many bins are masked per hop
static int s_dbg_masked = 0;
static int s_dbg_passed = 0;
static int s_dbg_hops   = 0;

// ── Helpers ─────────────────────────────────────────────────────────────────

static void make_hann(void)
{
    for (int i = 0; i < SNS_FFT_SIZE; i++)
        s_hann[i] = 0.5f * (1.0f - cosf(2.0f * M_PI * i / SNS_FFT_SIZE));
}

static void real_to_complex(const float *real, float *cpx, int n)
{
    for (int i = 0; i < n; i++) {
        cpx[2 * i]     = real[i];
        cpx[2 * i + 1] = 0.0f;
    }
}

static void do_ifft(void)
{
    // Conjugate → FFT → conjugate → /N
    for (int i = 0; i < SNS_FFT_SIZE; i++)
        s_fft_buf[2 * i + 1] = -s_fft_buf[2 * i + 1];

    dsps_fft2r_fc32(s_fft_buf, SNS_FFT_SIZE);
    dsps_bit_rev_fc32(s_fft_buf, SNS_FFT_SIZE);

    for (int i = 0; i < SNS_FFT_SIZE; i++) {
        s_fft_buf[2 * i]     /= (float)SNS_FFT_SIZE;
        s_fft_buf[2 * i + 1] = 0.0f;
    }
}

// ── Process one hop ─────────────────────────────────────────────────────────

static void process_hop(void)
{
    // Step A: Window + FFT
    float windowed[SNS_FFT_SIZE];
    for (int i = 0; i < SNS_FFT_SIZE; i++) {
        int idx = (s_in_pos + i) % SNS_FFT_SIZE;
        windowed[i] = s_in_ring[idx] * s_hann[i];
    }

    real_to_complex(windowed, s_fft_buf, SNS_FFT_SIZE);
    dsps_fft2r_fc32(s_fft_buf, SNS_FFT_SIZE);
    dsps_bit_rev_fc32(s_fft_buf, SNS_FFT_SIZE);

    // Step B+C+D: Spectral mask (modes 2-4)
    // Soft gain curve + temporal smoothing to prevent musical noise
    if (s_mode >= 2 && s_calibrated) {
        int masked = 0, passed = 0;

        for (int k = 0; k < SNS_N_BINS; k++) {
            float re = s_fft_buf[2 * k];
            float im = s_fft_buf[2 * k + 1];
            float mag = sqrtf(re * re + im * im);
            float mag_db = 20.0f * log10f(mag + EPS);

            // Soft gain: gradual transition over 12dB range (wide ramp)
            float gain;
            float diff = mag_db - s_noise_thresh[k];
            if (diff > 12.0f) {
                gain = 1.0f;
                passed++;
            } else if (diff < -3.0f) {
                gain = s_floor_gain;
                masked++;
            } else {
                // Smooth ramp from floor_gain to 1.0 over 15dB (-3 to +12)
                float t = (diff + 3.0f) / 15.0f;  // 0..1
                gain = s_floor_gain + (1.0f - s_floor_gain) * t * t;  // quadratic ease-in
                passed++;
            }

            // Very heavy temporal smoothing: 95% previous + 5% current
            // Takes ~20 hops (160ms) for full gain change — eliminates 125Hz buzz
            gain = 0.95f * s_prev_gain[k] + 0.05f * gain;
            s_prev_gain[k] = gain;

            if (s_mode >= 3) {
                s_fft_buf[2 * k]     = re * gain;
                s_fft_buf[2 * k + 1] = im * gain;

                if (k > 0 && k < SNS_FFT_SIZE / 2) {
                    int mk = SNS_FFT_SIZE - k;
                    s_fft_buf[2 * mk]     = s_fft_buf[2 * k];
                    s_fft_buf[2 * mk + 1] = -s_fft_buf[2 * k + 1];
                }
            }
        }

        s_dbg_masked += masked;
        s_dbg_passed += passed;
    }
    // mode 1: skip mask entirely — FFT data passes through unchanged

    // Step E: IFFT
    do_ifft();

    // Overlap-add with synthesis window
    for (int i = 0; i < SNS_FFT_SIZE; i++) {
        int idx = (s_ola_write + i) % OVERLAP_BUF_SIZE;
        s_ola_buf[idx] += s_fft_buf[2 * i] * s_hann[i];
    }

    // Read output
    for (int i = 0; i < SNS_HOP; i++) {
        int idx = (s_ola_read + i) % OVERLAP_BUF_SIZE;
        float v = s_ola_buf[idx] / OLA_NORM;
        s_ola_buf[idx] = 0.0f;
        if (s_out_count < SNS_FRAME_SAMPLES * 2)
            s_out_buf[s_out_count++] = v;
    }

    s_ola_write = (s_ola_write + SNS_HOP) % OVERLAP_BUF_SIZE;
    s_ola_read  = (s_ola_read + SNS_HOP) % OVERLAP_BUF_SIZE;
    s_dbg_hops++;
}

// ── Public API ──────────────────────────────────────────────────────────────

void spectral_ns_init(void)
{
    make_hann();
    dsps_fft2r_init_fc32(NULL, SNS_FFT_SIZE);

    memset(s_noise_thresh, 0, sizeof(s_noise_thresh));
    for (int i = 0; i < SNS_N_BINS; i++) s_prev_gain[i] = 1.0f;
    memset(s_noise_sum_db, 0, sizeof(s_noise_sum_db));
    memset(s_noise_sum_sq_db, 0, sizeof(s_noise_sum_sq_db));
    s_noise_frames = 0;
    s_calibrated   = 0;
    s_mode         = 4;  // hard-mask by default
    s_floor_gain   = 0.02f;

    memset(s_in_ring, 0, sizeof(s_in_ring));
    s_in_pos = 0;
    memset(s_ola_buf, 0, sizeof(s_ola_buf));
    s_ola_write = 0;
    s_ola_read  = 0;
    s_hop_count = 0;
    s_out_count = 0;

    s_dbg_masked = 0;
    s_dbg_passed = 0;
    s_dbg_hops   = 0;

    ESP_LOGI(TAG, "init: FFT=%d hop=%d bins=%d", SNS_FFT_SIZE, SNS_HOP, SNS_N_BINS);
}

void spectral_ns_feed_noise(const int16_t *samples, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        s_in_ring[s_in_pos] = (float)samples[i] / 32768.0f;
        s_in_pos = (s_in_pos + 1) % SNS_FFT_SIZE;
        s_hop_count++;

        if (s_hop_count >= SNS_HOP) {
            s_hop_count = 0;

            float windowed[SNS_FFT_SIZE];
            for (int j = 0; j < SNS_FFT_SIZE; j++) {
                int idx = (s_in_pos + j) % SNS_FFT_SIZE;
                windowed[j] = s_in_ring[idx] * s_hann[j];
            }

            real_to_complex(windowed, s_fft_buf, SNS_FFT_SIZE);
            dsps_fft2r_fc32(s_fft_buf, SNS_FFT_SIZE);
            dsps_bit_rev_fc32(s_fft_buf, SNS_FFT_SIZE);

            for (int k = 0; k < SNS_N_BINS; k++) {
                float re = s_fft_buf[2 * k];
                float im = s_fft_buf[2 * k + 1];
                float mag = sqrtf(re * re + im * im);
                float db = 20.0f * log10f(mag + EPS);
                s_noise_sum_db[k]    += db;
                s_noise_sum_sq_db[k] += db * db;
            }
            s_noise_frames++;
        }
    }
}

void spectral_ns_finalize_noise(float n_std_thresh)
{
    if (s_noise_frames < 2) {
        ESP_LOGW(TAG, "Not enough noise frames (%d)", s_noise_frames);
        return;
    }

    float n = (float)s_noise_frames;
    for (int k = 0; k < SNS_N_BINS; k++) {
        float mean = s_noise_sum_db[k] / n;
        float var  = s_noise_sum_sq_db[k] / n - mean * mean;
        float std  = (var > 0) ? sqrtf(var) : 0.0f;
        s_noise_thresh[k] = mean + n_std_thresh * std;
    }
    s_calibrated = 1;

    // Reset for clean processing
    memset(s_in_ring, 0, sizeof(s_in_ring));
    s_in_pos    = 0;
    s_hop_count = 0;
    s_out_count = 0;
    memset(s_ola_buf, 0, sizeof(s_ola_buf));
    s_ola_write = 0;
    s_ola_read  = 0;

    ESP_LOGI(TAG, "Calibrated: %d frames, thresh[0]=%.1f thresh[64]=%.1f thresh[128]=%.1f thresh[200]=%.1f dB",
             s_noise_frames,
             s_noise_thresh[0], s_noise_thresh[64],
             s_noise_thresh[128], s_noise_thresh[200]);
}

void spectral_ns_set_threshold(const float *thresh_db, size_t n_bins)
{
    size_t copy = (n_bins < SNS_N_BINS) ? n_bins : SNS_N_BINS;
    memcpy(s_noise_thresh, thresh_db, copy * sizeof(float));
    s_calibrated = 1;
}

int spectral_ns_is_calibrated(void) { return s_calibrated; }

void spectral_ns_set_mode(int mode)
{
    s_mode = mode;
    s_dbg_masked = 0;
    s_dbg_passed = 0;
    s_dbg_hops   = 0;
    static const char *mode_names[] = {"OFF", "FFT-IFFT", "mask-LOG", "soft-MASK", "hard-MASK"};
    ESP_LOGI(TAG, "mode=%d (%s) floor=%.3f", mode,
             (mode >= 0 && mode <= 4) ? mode_names[mode] : "?", s_floor_gain);
}

int spectral_ns_get_mode(void) { return s_mode; }

void spectral_ns_set_floor_gain(float gain)
{
    s_floor_gain = gain;
    ESP_LOGI(TAG, "floor_gain=%.3f (%.1f dB)", gain, 20.0f * log10f(gain + EPS));
}

int spectral_ns_process(const int16_t *in, int16_t *out, size_t n_samples)
{
    // Mode 0: passthrough
    if (s_mode == 0) {
        memcpy(out, in, n_samples * sizeof(int16_t));
        return (int)n_samples;
    }

    // DO NOT reset s_out_count — preserve buffered samples from previous calls!
    // 240 input samples produce ~1.875 hops × 128 = 240 output on average,
    // but the output comes in bursts of 128. Buffer smooths this out.

    for (size_t i = 0; i < n_samples; i++) {
        s_in_ring[s_in_pos] = (float)in[i] / 32768.0f;
        s_in_pos = (s_in_pos + 1) % SNS_FFT_SIZE;
        s_hop_count++;

        if (s_hop_count >= SNS_HOP) {
            s_hop_count = 0;
            process_hop();
        }
    }

    // Return up to n_samples from the output buffer
    int produced = (s_out_count > (int)n_samples) ? (int)n_samples : s_out_count;
    for (int i = 0; i < produced; i++) {
        float v = s_out_buf[i] * 32768.0f;
        if (v >  32767.0f) v =  32767.0f;
        if (v < -32768.0f) v = -32768.0f;
        out[i] = (int16_t)v;
    }

    // Shift remaining buffered samples to front
    if (s_out_count > produced)
        memmove(s_out_buf, s_out_buf + produced,
                (s_out_count - produced) * sizeof(float));
    s_out_count -= produced;

    // Debug log
    if (s_dbg_hops > 0 && s_dbg_hops % 100 == 0) {
        int total = s_dbg_masked + s_dbg_passed;
        ESP_LOGI(TAG, "mode=%d hops=%d buf=%d bins: %d%% masked, floor=%.3f",
                 s_mode, s_dbg_hops, s_out_count,
                 total > 0 ? (s_dbg_masked * 100 / total) : 0,
                 s_floor_gain);
    }

    return produced;
}
