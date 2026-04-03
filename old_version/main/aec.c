#include "aec.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#if AEC_ENABLED

// Reference ring buffer holds enough samples for several frames + delay headroom
#define AEC_BUF_SAMPLES  (160 * 4)   // 4 × 20 ms frames at 8 kHz

static int16_t      s_buf[AEC_BUF_SAMPLES];
static uint32_t     s_write_pos = 0;
static portMUX_TYPE s_mux       = portMUX_INITIALIZER_UNLOCKED;

void aec_init(void)
{
    for (int i = 0; i < AEC_BUF_SAMPLES; i++) s_buf[i] = 0;
    s_write_pos = 0;
}

void aec_write_reference(const int16_t *samples, size_t count)
{
    taskENTER_CRITICAL(&s_mux);
    for (size_t i = 0; i < count; i++) {
        s_buf[s_write_pos] = samples[i];
        s_write_pos = (s_write_pos + 1) % AEC_BUF_SAMPLES;
    }
    taskEXIT_CRITICAL(&s_mux);
}

void aec_process(int16_t *samples, size_t count)
{
    taskENTER_CRITICAL(&s_mux);
    // read_pos points to the reference sample that arrived `delay` samples ago
    uint32_t read_pos = (s_write_pos + AEC_BUF_SAMPLES
                         - (uint32_t)AEC_DELAY_SAMPLES
                         - (uint32_t)count)
                        % AEC_BUF_SAMPLES;
    for (size_t i = 0; i < count; i++) {
        int32_t ref = (int32_t)s_buf[(read_pos + i) % AEC_BUF_SAMPLES];
        int32_t v   = (int32_t)samples[i] - ((ref * AEC_ALPHA_Q8) >> 8);
        if (v >  32767) v =  32767;
        if (v < -32768) v = -32768;
        samples[i] = (int16_t)v;
    }
    taskEXIT_CRITICAL(&s_mux);
}

#else  // AEC_ENABLED == 0 — compile to no-ops

void aec_init(void)                                          {}
void aec_write_reference(const int16_t *s, size_t n)        { (void)s; (void)n; }
void aec_process(int16_t *s, size_t n)                      { (void)s; (void)n; }

#endif
