#include <string.h>

#include "esp_log.h"

#include "oi_codec_sbc.h"
#include "oi_status.h"
#include "sbc_encoder.h"

#include "msbc_codec.h"

#define TAG "MSBC"

// The same parameters the stack uses for its internal codec.
#define DEC_DATA_WORDS   (CODEC_DATA_WORDS(1, SBC_CODEC_FAST_FILTER_BUFFERS))

static OI_CODEC_SBC_DECODER_CONTEXT s_dec;
static OI_UINT32                    s_dec_data[DEC_DATA_WORDS];
static SBC_ENC_PARAMS               s_enc;

// A frame of zero signal, from the stack's sbc_plc.c: decoding it gives the ringing of the synthesis filter.
static const uint8_t s_zero_frame[MSBC_FRAME_BYTES] = {
    0xad, 0x00, 0x00, 0xc5, 0x00, 0x00, 0x00, 0x00, 0x77, 0x6d,
    0xb6, 0xdd, 0xdb, 0x6d, 0xb7, 0x76, 0xdb, 0x6d, 0xdd, 0xb6, 0xdb, 0x77, 0x6d,
    0xb6, 0xdd, 0xdb, 0x6d, 0xb7, 0x76, 0xdb, 0x6d, 0xdd, 0xb6, 0xdb, 0x77, 0x6d,
    0xb6, 0xdd, 0xdb, 0x6d, 0xb7, 0x76, 0xdb, 0x6d, 0xdd, 0xb6, 0xdb, 0x77, 0x6d,
    0xb6, 0xdd, 0xdb, 0x6d, 0xb7, 0x76, 0xdb, 0x6c
};

static void decoder_reset(void)
{
    OI_STATUS st = OI_CODEC_SBC_DecoderReset(&s_dec, s_dec_data, sizeof(s_dec_data), 1, 1, FALSE, TRUE);
    if (!OI_SUCCESS(st)) {
        ESP_LOGE(TAG, "decoder reset failed: %d", st);
    }
}

void msbc_codec_reset(void)
{
    decoder_reset();

    memset(&s_enc, 0, sizeof(s_enc));
    s_enc.sbc_mode            = SBC_MODE_MSBC;
    s_enc.s16NumOfBlocks      = 15;
    s_enc.s16NumOfSubBands    = 8;
    s_enc.s16AllocationMethod = SBC_LOUDNESS;
    s_enc.s16BitPool          = 26;
    s_enc.s16ChannelMode      = SBC_MONO;
    s_enc.s16NumOfChannels    = 1;
    s_enc.s16SamplingFreq     = SBC_sf16000;
    SBC_Encoder_Init(&s_enc);
}

static OI_STATUS decode(const uint8_t *frame, uint32_t len, int16_t *pcm)
{
    const OI_BYTE *data = frame;
    OI_UINT32 bytes = len;
    OI_UINT32 pcm_bytes = MSBC_PCM_SAMPLES * sizeof(int16_t);
    return OI_CODEC_SBC_DecodeFrame(&s_dec, &data, &bytes, pcm, &pcm_bytes);
}

bool msbc_decode(const uint8_t *frame, uint32_t len, int16_t *pcm)
{
    OI_STATUS st = decode(frame, len, pcm);
    if (OI_SUCCESS(st)) {
        return true;
    }
    if (st == OI_STATUS_INVALID_PARAMETERS) {
        // A corrupt frame can leave the decoder unusable: the stack resets it too.
        decoder_reset();
    }
    return false;
}

void msbc_decode_zero(int16_t *pcm)
{
    if (!OI_SUCCESS(decode(s_zero_frame, MSBC_FRAME_BYTES, pcm))) {
        memset(pcm, 0, MSBC_PCM_SAMPLES * sizeof(int16_t));
    }
}

void msbc_encode(const int16_t *pcm, uint8_t *frame)
{
    memcpy(s_enc.as16PcmBuffer, pcm, MSBC_PCM_SAMPLES * sizeof(int16_t));
    s_enc.pu8Packet = frame;
    SBC_Encoder(&s_enc);
}
