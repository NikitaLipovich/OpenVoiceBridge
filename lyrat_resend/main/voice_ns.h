#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#if __has_include("bench_variant.h")
#include "bench_variant.h"
#endif

/*
 * Noise suppression of the microphone, one mode at a time, chosen with the command "ns".
 *
 *   off    nothing
 *   sns    spectral gating of spectral_ns.c, its noise profile comes from the first second after boot
 *   web0, web1, web2   WebRTC noise suppression of ESP-SR, esp_ns.h: mild, medium, aggressive
 *
 * The test build "afe" adds the same WebRTC through the whole ESP-SR AFE, and "webo", the older esp_ns call,
 * only to compare them. The default is in voice_ns.c.
 *
 * Everything runs on the microphone task. A new mode starts on a frame boundary with a short fade on both sides,
 * so a switch with the channel open gives no step on the line.
 */

typedef enum {
    VNS_OFF = 0,
    VNS_SNS,
    VNS_WEB0,
    VNS_WEB1,
    VNS_WEB2,
#ifdef BENCH_NS_AFE
    VNS_WEBO,
    VNS_AFE,
#endif
    VNS_COUNT,
} vns_mode_t;

typedef struct {
    uint32_t switches;      // modes actually changed on the microphone task
    uint32_t frames;        // since the last switch
    uint32_t avg_us;
    uint32_t max_us;
    uint32_t underruns;     // frames of the current mode with no output ready, after it started
    int      fifo_min;      // output queue in samples, -1 for a mode without one
    int      fifo_max;
} vns_stats_t;

void voice_ns_init(void);
int  voice_ns_parse(const char *name);              // -1 when there is no such mode
const char *voice_ns_name(int mode);
const char *voice_ns_names(void);                   // all of them, for the help text
void voice_ns_request(int mode);                    // takes effect on the next frame
int  voice_ns_requested(void);
int  voice_ns_current(void);
void voice_ns_recalibrate(void);                    // sns takes a new noise profile from the next second
void voice_ns_get_stats(vns_stats_t *out);
void voice_ns_frame(int16_t *samples, size_t n);    // in place, on the microphone task

#ifdef BENCH_NS_INJECT
// Test build: a known signal replaces the microphone in front of the whole chain. The bus then carries what went in
// (left) and what came out (right), so the S3 records both to the sample, without Bluetooth in between.
// 0 off, 1 voice-like signal plus noise, 2 noise only, 3 signal only. The noise level is in percent of the default.
void voice_ns_inject_set(int what, int noise_percent);
int  voice_ns_inject_get(void);
void voice_ns_inject_fill(int16_t *samples, size_t n);
#endif
