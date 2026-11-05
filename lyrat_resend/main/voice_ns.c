#include "voice_ns.h"

#if __has_include("bench_variant.h")
#include "bench_variant.h"
#endif

#include <math.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_ns.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "spectral_ns.h"

#ifdef BENCH_NS_AFE
#include "esp_afe_config.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#endif

#define TAG "VNS"

#define DEFAULT_MODE    VNS_WEB2  // goal 057 DECISIONS D3: known signal, loss and suppression
#define WEB_BLOCK       160     // the WebRTC block, 10 ms; the frame of 240 does not divide by it
#define WEB_FIFO        (2 * 240 + 2 * WEB_BLOCK)
#define FADE_SAMPLES    80      // 5 ms on each side of a switch
#define SNS_CAL_FRAMES  66      // about 1 s of 15 ms frames

static const char *const s_names[VNS_COUNT] = {
    [VNS_OFF] = "off", [VNS_SNS] = "sns", [VNS_WEB0] = "web0", [VNS_WEB1] = "web1", [VNS_WEB2] = "web2",
#ifdef BENCH_NS_AFE
    [VNS_WEBO] = "webo", [VNS_AFE] = "afe",
#endif
};

static volatile int s_req = DEFAULT_MODE;
static int s_cur = DEFAULT_MODE;
static bool s_fade_in;
static volatile bool s_recal;
static int s_cal_frames;

static ns_handle_t s_web[VNS_COUNT];
static int16_t s_in[WEB_FIFO];
static int16_t s_out[WEB_FIFO];
static int s_in_n, s_out_n;
static int s_fifo_min, s_fifo_max;

static volatile uint32_t s_switches, s_frames, s_max_us, s_underruns;
static volatile uint64_t s_total_us;

// ---------------------------------------------------------------------------
// AFE, test build only: see DECISIONS D1 of goal 057
// ---------------------------------------------------------------------------

#ifdef BENCH_NS_AFE
#define AFE_FIFO 4096
static const esp_afe_sr_iface_t *s_afe;
static esp_afe_sr_data_t *s_afe_data;
static int s_afe_feed, s_afe_fetch;
static int16_t s_afe_in[1024], s_afe_out[AFE_FIFO];
static int s_afe_in_n, s_afe_out_n;
static bool s_afe_primed;
static SemaphoreHandle_t s_afe_lock;

static void afe_fetch_task(void *arg)
{
    for (;;) {
        if (s_cur != VNS_AFE) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        afe_fetch_result_t *r = s_afe->fetch_with_delay(s_afe_data, pdMS_TO_TICKS(200));
        if (!r || r->ret_value != ESP_OK || !r->data || r->data_size <= 0) continue;
        int got = r->data_size / (int)sizeof(int16_t);
        xSemaphoreTake(s_afe_lock, portMAX_DELAY);
        if (s_afe_out_n + got <= AFE_FIFO) {
            memcpy(s_afe_out + s_afe_out_n, r->data, (size_t)got * sizeof(int16_t));
            s_afe_out_n += got;
        }
        xSemaphoreGive(s_afe_lock);
    }
}

static void afe_init(void)
{
    s_afe_lock = xSemaphoreCreateMutex();
    afe_config_t *c = afe_config_init("M", NULL, AFE_TYPE_VC, AFE_MODE_LOW_COST);
    if (!c) return;
    c->aec_init = c->se_init = c->vad_init = c->wakenet_init = c->agc_init = false;
    c->ns_init = true;
    c->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    c->afe_perferred_core = 1;
    afe_config_check(c);
    s_afe = esp_afe_handle_from_config(c);
    s_afe_data = s_afe ? s_afe->create_from_config(c) : NULL;
    if (!s_afe_data) return;
    s_afe_feed = s_afe->get_feed_chunksize(s_afe_data);
    s_afe_fetch = s_afe->get_fetch_chunksize(s_afe_data);
    xTaskCreatePinnedToCore(afe_fetch_task, "afe_fetch", 6144, NULL, 13, NULL, 1);
}

static void afe_frame(int16_t *io, size_t n)
{
    memcpy(s_afe_in + s_afe_in_n, io, n * sizeof(int16_t));
    s_afe_in_n += (int)n;
    int off = 0;
    while (s_afe_in_n - off >= s_afe_feed) {
        s_afe->feed(s_afe_data, s_afe_in + off);
        off += s_afe_feed;
    }
    memmove(s_afe_in, s_afe_in + off, (size_t)(s_afe_in_n - off) * sizeof(int16_t));
    s_afe_in_n -= off;
    xSemaphoreTake(s_afe_lock, portMAX_DELAY);
    if (!s_afe_primed && s_afe_out_n >= s_afe_fetch + (int)n) {
        s_afe_primed = true;
        s_fifo_min = AFE_FIFO;
        s_fifo_max = 0;
    }
    if (s_afe_primed && s_afe_out_n >= (int)n) {
        if (s_afe_out_n > s_fifo_max) s_fifo_max = s_afe_out_n;
        memcpy(io, s_afe_out, n * sizeof(int16_t));
        s_afe_out_n -= (int)n;
        memmove(s_afe_out, s_afe_out + n, (size_t)s_afe_out_n * sizeof(int16_t));
        if (s_afe_out_n < s_fifo_min) s_fifo_min = s_afe_out_n;
    } else {
        memset(io, 0, n * sizeof(int16_t));
        if (s_afe_primed) s_underruns++;
    }
    xSemaphoreGive(s_afe_lock);
}
#endif

// ---------------------------------------------------------------------------
// The modes
// ---------------------------------------------------------------------------

static void web_frame(ns_handle_t ns, int16_t *io, size_t n)
{
    memcpy(s_in + s_in_n, io, n * sizeof(int16_t));
    s_in_n += (int)n;
    int off = 0;
    while (s_in_n - off >= WEB_BLOCK) {
        ns_process(ns, s_in + off, s_out + s_out_n);
        s_out_n += WEB_BLOCK;
        off += WEB_BLOCK;
    }
    memmove(s_in, s_in + off, (size_t)(s_in_n - off) * sizeof(int16_t));
    s_in_n -= off;
    if (s_out_n > s_fifo_max) s_fifo_max = s_out_n;
    if (s_out_n >= (int)n) {
        memcpy(io, s_out, n * sizeof(int16_t));
        s_out_n -= (int)n;
        memmove(s_out, s_out + n, (size_t)s_out_n * sizeof(int16_t));
    } else {
        memset(io, 0, n * sizeof(int16_t));
        s_underruns++;
    }
    if (s_out_n < s_fifo_min) s_fifo_min = s_out_n;
}

static void sns_frame(int16_t *io, size_t n)
{
    static int16_t out[SNS_FRAME_SAMPLES];
    if (!spectral_ns_is_calibrated()) return;   // nothing to compare with yet: the sound goes through
    int produced = spectral_ns_process(io, out, n);
    if (produced >= (int)n) {
        memcpy(io, out, n * sizeof(int16_t));
    } else {
        // Only right after a reset. The silence goes in front: the next frame must continue where this one stops.
        memset(io, 0, (n - (size_t)produced) * sizeof(int16_t));
        memcpy(io + (n - (size_t)produced), out, (size_t)produced * sizeof(int16_t));
    }
}

static void mode_start(int mode)
{
    s_in_n = 0;
    // The output starts with the part of a block that the first frame cannot fill, so no frame comes up short.
    s_out_n = (240 % WEB_BLOCK) ? WEB_BLOCK - (240 % WEB_BLOCK) : 0;
    memset(s_out, 0, (size_t)s_out_n * sizeof(int16_t));
    s_fifo_min = WEB_FIFO;
    s_fifo_max = 0;
    if (mode == VNS_SNS) {
        spectral_ns_reset();
        if (spectral_ns_get_mode() == 0) spectral_ns_set_mode(4);
    }
#ifdef BENCH_NS_AFE
    if (mode == VNS_AFE) {
        s_afe_in_n = 0;
        xSemaphoreTake(s_afe_lock, portMAX_DELAY);
        s_afe_out_n = 0;
        s_afe_primed = false;
        xSemaphoreGive(s_afe_lock);
    }
#endif
    s_frames = 0;
    s_total_us = 0;
    s_max_us = 0;
    s_underruns = 0;
}

static void fade(int16_t *x, size_t count, bool rising)
{
    for (size_t i = 0; i < count; i++) {
        int32_t w = (int32_t)(rising ? i + 1 : count - i);
        x[i] = (int16_t)(((int32_t)x[i] * w) / (int32_t)(count + 1));
    }
}

void voice_ns_frame(int16_t *samples, size_t n)
{
    int64_t t0 = esp_timer_get_time();
    if (s_recal) {
        s_recal = false;
        spectral_ns_reset_profile();
        s_cal_frames = 0;
    }
    // The profile is taken in any mode, so that sns is ready whenever it is asked for.
    if (!spectral_ns_is_calibrated()) {
        spectral_ns_feed_noise(samples, n);
        if (++s_cal_frames >= SNS_CAL_FRAMES) {
            spectral_ns_finalize_noise(1.0f);
            ESP_LOGI(TAG, "sns profile taken, %d frames", s_cal_frames);
        }
    }

    switch (s_cur) {
    case VNS_OFF:
        break;
    case VNS_SNS:
        sns_frame(samples, n);
        break;
#ifdef BENCH_NS_AFE
    case VNS_AFE:
        afe_frame(samples, n);
        break;
#endif
    default:
        web_frame(s_web[s_cur], samples, n);
        break;
    }

    if (s_fade_in) {
        fade(samples, FADE_SAMPLES, true);
        s_fade_in = false;
    }
    int req = s_req;
    bool switching = req != s_cur;
    if (switching) {
        fade(samples + (n - FADE_SAMPLES), FADE_SAMPLES, false);
        // The rest of the time this frame stays silent on the line, the first of the new mode comes up from zero.
        s_cur = req;
        mode_start(req);
        s_fade_in = true;
        s_switches++;
        return;
    }

    uint32_t took = (uint32_t)(esp_timer_get_time() - t0);
    s_frames++;
    s_total_us += took;
    if (took > s_max_us) s_max_us = took;
}

#ifdef BENCH_NS_INJECT
static void inject_make_task(void *arg);
#endif

void voice_ns_init(void)
{
    static const int web_level[] = {0, 1, 2};
    for (int i = 0; i < 3; i++) {
        s_web[VNS_WEB0 + i] = ns_pro_create(10, web_level[i], 16000);
        if (!s_web[VNS_WEB0 + i]) {
            ESP_LOGE(TAG, "ns_pro_create %d failed", web_level[i]);
        }
    }
#ifdef BENCH_NS_AFE
    s_web[VNS_WEBO] = ns_create(10);
    afe_init();
#endif
    mode_start(s_cur);
#ifdef BENCH_NS_INJECT
    xTaskCreatePinnedToCore(inject_make_task, "inj_make", 4096, NULL, 1, NULL, 1);
#endif
    ESP_LOGI(TAG, "ns=%s, psram free %u", s_names[s_cur], (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

int voice_ns_parse(const char *name)
{
    for (int i = 0; i < VNS_COUNT; i++) {
        if (strcmp(name, s_names[i]) == 0) return i;
    }
    return -1;
}

const char *voice_ns_name(int mode) { return (mode >= 0 && mode < VNS_COUNT) ? s_names[mode] : "?"; }

const char *voice_ns_names(void)
{
#ifdef BENCH_NS_AFE
    return "off|sns|web0|web1|web2|webo|afe";
#else
    return "off|sns|web0|web1|web2";
#endif
}

void voice_ns_request(int mode)
{
    if (mode >= 0 && mode < VNS_COUNT && (mode != VNS_OFF ? s_web[mode] || mode == VNS_SNS
#ifdef BENCH_NS_AFE
        || (mode == VNS_AFE && s_afe_data)
#endif
        : true)) {
        s_req = mode;
    }
}

int voice_ns_requested(void) { return s_req; }
int voice_ns_current(void) { return s_cur; }
void voice_ns_recalibrate(void) { s_recal = true; }

void voice_ns_get_stats(vns_stats_t *out)
{
    uint32_t frames = s_frames;
    out->switches = s_switches;
    out->frames = frames;
    out->avg_us = frames ? (uint32_t)(s_total_us / frames) : 0;
    out->max_us = s_max_us;
    out->underruns = s_underruns;
    bool queued = s_cur != VNS_OFF && s_cur != VNS_SNS;
    out->fifo_min = queued ? s_fifo_min : -1;
    out->fifo_max = queued ? s_fifo_max : -1;
}

// ---------------------------------------------------------------------------
// Known signal in front of the chain, test build only
// ---------------------------------------------------------------------------

#ifdef BENCH_NS_INJECT
#define INJ_SECONDS 6
#define INJ_SAMPLES (INJ_SECONDS * 16000)
#define INJ_SIGNAL_PEAK 5000.0f     // about what speech gives before the gain, the gain then brings it near half of full scale
#define INJ_NOISE_RMS   150.0f      // the floor of the real microphone line, goal 042 and 050

static int16_t *s_inj_sig, *s_inj_noise;
static volatile int s_inj_what, s_inj_noise_q8 = 256;
static volatile bool s_inj_ready;
static uint32_t s_inj_pos;

// Syllables of 150-300 ms with pauses, one long pause per phrase, as in speech. The pitch glides, the harmonics fall.
static float syllable_env(float t)
{
    static const float pattern[][2] = {
        {0.10f, 0.20f}, {0.40f, 0.18f}, {0.68f, 0.28f}, {1.10f, 0.15f}, {1.35f, 0.25f}, {2.20f, 0.20f}, {2.52f, 0.30f},
        {2.95f, 0.17f}, {3.25f, 0.22f}, {4.10f, 0.26f}, {4.48f, 0.15f}, {4.75f, 0.30f},
    };
    const float rise = 0.015f, decay = 0.040f;
    for (size_t i = 0; i < sizeof(pattern) / sizeof(pattern[0]); i++) {
        float x = t - pattern[i][0];
        if (x < 0 || x > pattern[i][1]) continue;
        float e = 1.0f;
        if (x < rise) e = x / rise;
        else if (x > pattern[i][1] - decay) e = (pattern[i][1] - x) / decay;
        return e;
    }
    return 0.0f;
}

// Takes seconds: on its own low task, so the console and the control UART keep answering.
static void inject_make_task(void *arg)
{
    s_inj_sig = heap_caps_malloc(INJ_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    s_inj_noise = heap_caps_malloc(INJ_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!s_inj_sig || !s_inj_noise) {
        ESP_LOGE(TAG, "no memory for the test signal");
        vTaskDelete(NULL);
    }
    float phase = 0;
    float peak = 1.0f;
    float *tmp = heap_caps_malloc(INJ_SAMPLES * sizeof(float), MALLOC_CAP_SPIRAM);
    for (int i = 0; i < INJ_SAMPLES; i++) {
        float t = (float)i / 16000.0f;
        float f0 = 155.0f + 45.0f * sinf(2.0f * (float)M_PI * 0.7f * t) * cosf(2.0f * (float)M_PI * 0.23f * t);
        phase += 2.0f * (float)M_PI * f0 / 16000.0f;
        if (phase > 2.0f * (float)M_PI) phase -= 2.0f * (float)M_PI;
        float e = syllable_env(t);
        float v = 0;
        if (e > 0) {
            int harmonics = (int)(3400.0f / f0);
            for (int k = 1; k <= harmonics; k += 1) {
                v += sinf((float)k * phase) / (float)k;
            }
        }
        tmp[i] = e * v;
        float a = tmp[i] < 0 ? -tmp[i] : tmp[i];
        if (a > peak) peak = a;
    }
    for (int i = 0; i < INJ_SAMPLES; i++) {
        s_inj_sig[i] = (int16_t)(tmp[i] * INJ_SIGNAL_PEAK / peak);
    }
    // A fan: low-passed noise and a few lines of the blades, the same every time.
    uint32_t lfsr = 0xACE1u;
    float lp = 0, sum = 0;
    for (int i = 0; i < INJ_SAMPLES; i++) {
        lfsr ^= lfsr << 13;
        lfsr ^= lfsr >> 17;
        lfsr ^= lfsr << 5;
        float white = (float)(int32_t)lfsr / 2147483648.0f;
        lp += 0.25f * (white - lp);
        float t = (float)i / 16000.0f;
        float lines = 0.20f * sinf(2.0f * (float)M_PI * 120.0f * t) + 0.12f * sinf(2.0f * (float)M_PI * 240.0f * t)
                      + 0.06f * sinf(2.0f * (float)M_PI * 480.0f * t);
        tmp[i] = lp + 0.15f * white + lines;
        sum += tmp[i] * tmp[i];
    }
    float scale = INJ_NOISE_RMS / sqrtf(sum / INJ_SAMPLES);
    for (int i = 0; i < INJ_SAMPLES; i++) {
        s_inj_noise[i] = (int16_t)(tmp[i] * scale);
    }
    heap_caps_free(tmp);
    s_inj_ready = true;
    ESP_LOGI(TAG, "test signal ready");
    vTaskDelete(NULL);
}

void voice_ns_inject_set(int what, int noise_percent)
{
    if (noise_percent > 0) s_inj_noise_q8 = noise_percent * 256 / 100;
    s_inj_pos = 0;
    s_inj_what = s_inj_ready ? what : 0;
}

int voice_ns_inject_get(void) { return s_inj_what; }

void voice_ns_inject_fill(int16_t *samples, size_t n)
{
    int what = s_inj_what;
    uint32_t pos = s_inj_pos;
    for (size_t i = 0; i < n; i++) {
        int32_t v = 0;
        if (what == 1 || what == 3) v += s_inj_sig[pos];
        if (what == 1 || what == 2) v += ((int32_t)s_inj_noise[pos] * s_inj_noise_q8) >> 8;
        samples[i] = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
        if (++pos >= INJ_SAMPLES) pos = 0;
    }
    s_inj_pos = pos;
}
#endif
