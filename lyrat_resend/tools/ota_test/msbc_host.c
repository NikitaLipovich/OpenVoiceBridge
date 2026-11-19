// Exports of the firmware's mSBC codec (main/msbc_codec.c) for the PC: call_check decodes what the LyraT sent.
#include <stdint.h>
#include "msbc_codec.h"

__declspec(dllexport) void host_msbc_reset(void) { msbc_codec_reset(); }

// Decodes n frames of MSBC_FRAME_BYTES each; a bad frame gives the zero frame's ringing. Returns the good ones.
__declspec(dllexport) int host_msbc_decode(const uint8_t *frames, int n, int16_t *pcm)
{
    int good = 0;
    for (int i = 0; i < n; i++) {
        int16_t *out = pcm + i * MSBC_PCM_SAMPLES;
        if (msbc_decode(frames + i * MSBC_FRAME_BYTES, MSBC_FRAME_BYTES, out)) good++;
        else msbc_decode_zero(out);
    }
    return good;
}

__declspec(dllexport) void host_msbc_encode(const int16_t *pcm, int n, uint8_t *frames)
{
    for (int i = 0; i < n; i++) msbc_encode(pcm + i * MSBC_PCM_SAMPLES, frames + i * MSBC_FRAME_BYTES);
}
