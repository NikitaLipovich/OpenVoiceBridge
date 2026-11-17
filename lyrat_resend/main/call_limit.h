// Soft limiter of the call audio toward the headphones (console "lim"), plain C so the PC tests build it too.
//
// Linear up to SW_LIMIT_THRESHOLD, then a curve that approaches SW_LIMIT_MAX and never reaches it. The input is a sample
// times the call gain (1..8), so up to 8 * 32768. The product of the excess and the headroom needs 64 bits: in 32 bits it
// overflowed from about 148000 on (call gain 5 and more with a loud phone) and turned a peak into the opposite sign, a
// click in the ear (goal 094 #004).
#pragma once

#include <stdint.h>

#define SW_LIMIT_THRESHOLD  16384   // ~0.5 FS: linear zone
#define SW_LIMIT_MAX        32700   // ceiling

static inline int16_t call_soft_limit(int32_t s)
{
    int32_t sign = (s < 0) ? -1 : 1;
    int64_t a = (int64_t)s * sign;
    if (a > SW_LIMIT_THRESHOLD) {
        int64_t excess = a - SW_LIMIT_THRESHOLD;
        int64_t headroom = SW_LIMIT_MAX - SW_LIMIT_THRESHOLD;
        a = SW_LIMIT_THRESHOLD + (excess * headroom) / (headroom + excess);
    }
    return (int16_t)(sign * (int32_t)a);
}
