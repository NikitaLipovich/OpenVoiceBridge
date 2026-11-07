// Framing of the control UART between the LyraT and the ESP32-S3-ETH. Used by both firmwares, see docs/S3_UART.md.
//
//   0 0xA5  1 0x5A  2 type  3 seq lo  4 seq hi  5 len lo  6 len hi  7 xor of bytes 2..6
//   8 payload of len bytes
//   then CRC16-CCITT (poly 0x1021, init 0xFFFF) of bytes 2..end of payload, low byte first
//
// The header has its own check so that noise cannot announce a long frame and swallow the real ones after it.
// A frame that stops arriving is dropped after CTL_GAP_MS, so a sender that dies in the middle costs one frame only.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define CTL_MAGIC0        0xA5
#define CTL_MAGIC1        0x5A
#define CTL_HEADER_BYTES  8
#define CTL_CRC_BYTES     2
#define CTL_MAX_PAYLOAD   1024
#define CTL_MAX_FRAME     (CTL_HEADER_BYTES + CTL_MAX_PAYLOAD + CTL_CRC_BYTES)
#define CTL_GAP_MS        200     // the driver hands bytes over per 120: 125 ms apart at 9600
#define CTL_BAUD_DEFAULT  115200

typedef enum {
    CTL_T_CMD = 1,      // S3 to LyraT: payload is a console command
    CTL_T_REPLY = 2,    // LyraT to S3: same seq, payload is a status byte and the text of the command
    CTL_T_HB = 3,       // LyraT to S3 every second, see ctl_hb_t
    CTL_T_HB_ACK = 4,   // S3 to LyraT: same seq; payload u8 log on, u8 log level (none from an S3 before goal 090)
    CTL_T_LOG = 5,      // LyraT to S3: log records of dbg_log.h, docs/OTA.md
    CTL_T_OTA = 6,      // S3 to LyraT: an ota_link request, docs/OTA.md
    CTL_T_OTA_ACK = 7,  // LyraT to S3: an ota_link reply
    CTL_T_MODE = 8,     // reserved for R21 (goal 095): relay to node, u8 mode, u8 flags, ...; not implemented yet
    CTL_T_MODE_ACK = 9, // reserved for R21: node to relay, u8 status (0 ok, 2 unsupported), u8 mode
} ctl_type_t;

typedef enum {
    CTL_ST_OK = 0,
    CTL_ST_UNKNOWN = 1,   // the command is not known to the console
    CTL_ST_BAD = 2,       // not text, or too long for the console
} ctl_status_t;

#ifdef _MSC_VER                // the PC tests build this header with MSVC
#pragma pack(push, 1)
#define CTL_PACKED
#else
#define CTL_PACKED __attribute__((packed))
#endif
typedef struct CTL_PACKED {
    uint32_t up_s;
    uint8_t flags;        // bit 0 SLC up, bit 1 SCO active
    uint8_t tone;
} ctl_hb_t;
#ifdef _MSC_VER
#pragma pack(pop)
#endif

typedef struct {
    uint8_t buf[CTL_MAX_FRAME];
    size_t pos, need;
    uint32_t last_ms;
    uint32_t frames, bad_header, bad_crc, truncated, noise;
} ctl_parser_t;

static inline uint16_t ctl_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    while (len--) {
        crc ^= (uint16_t)(*data++) << 8;
        for (int i = 0; i < 8; i++) crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}

static inline uint8_t ctl_header_check(const uint8_t *frame)
{
    return (uint8_t)(frame[2] ^ frame[3] ^ frame[4] ^ frame[5] ^ frame[6]);
}

// Writes a whole frame into out, which must hold CTL_HEADER_BYTES + len + CTL_CRC_BYTES; returns its size.
static inline size_t ctl_frame_build(uint8_t *out, uint8_t type, uint16_t seq, const void *payload, size_t len)
{
    out[0] = CTL_MAGIC0;
    out[1] = CTL_MAGIC1;
    out[2] = type;
    out[3] = (uint8_t)seq;
    out[4] = (uint8_t)(seq >> 8);
    out[5] = (uint8_t)len;
    out[6] = (uint8_t)(len >> 8);
    out[7] = ctl_header_check(out);
    if (len) memcpy(out + CTL_HEADER_BYTES, payload, len);
    uint16_t crc = ctl_crc16(out + 2, CTL_HEADER_BYTES - 2 + len);
    out[CTL_HEADER_BYTES + len] = (uint8_t)crc;
    out[CTL_HEADER_BYTES + len + 1] = (uint8_t)(crc >> 8);
    return CTL_HEADER_BYTES + len + CTL_CRC_BYTES;
}

// Feeds one byte; true when p->buf holds a whole valid frame.
static inline bool ctl_parser_feed(ctl_parser_t *p, uint8_t byte, uint32_t now_ms)
{
    if (p->pos && (uint32_t)(now_ms - p->last_ms) > CTL_GAP_MS) {
        p->truncated++;
        p->pos = 0;
    }
    p->last_ms = now_ms;
    if (p->pos == 0) {
        if (byte == CTL_MAGIC0) p->buf[p->pos++] = byte;
        else p->noise++;
        return false;
    }
    if (p->pos == 1) {
        if (byte == CTL_MAGIC1) {
            p->buf[p->pos++] = byte;
        } else if (byte != CTL_MAGIC0) {    // a repeated first magic byte may still start a frame
            p->noise += 2;
            p->pos = 0;
        } else {
            p->noise++;
        }
        return false;
    }
    p->buf[p->pos++] = byte;
    if (p->pos == CTL_HEADER_BYTES) {
        size_t len = (size_t)p->buf[5] | ((size_t)p->buf[6] << 8);
        if (ctl_header_check(p->buf) != byte || len > CTL_MAX_PAYLOAD) {
            p->bad_header++;
            p->pos = 0;
            return false;
        }
        p->need = CTL_HEADER_BYTES + len + CTL_CRC_BYTES;
        return false;
    }
    if (p->pos < CTL_HEADER_BYTES || p->pos < p->need) return false;
    size_t len = p->need - CTL_HEADER_BYTES - CTL_CRC_BYTES;
    uint16_t got = (uint16_t)p->buf[CTL_HEADER_BYTES + len] | ((uint16_t)p->buf[CTL_HEADER_BYTES + len + 1] << 8);
    p->pos = 0;
    if (ctl_crc16(p->buf + 2, CTL_HEADER_BYTES - 2 + len) != got) {
        p->bad_crc++;
        return false;
    }
    p->frames++;
    return true;
}

static inline uint8_t ctl_frame_type(const ctl_parser_t *p) { return p->buf[2]; }
static inline uint16_t ctl_frame_seq(const ctl_parser_t *p) { return (uint16_t)p->buf[3] | ((uint16_t)p->buf[4] << 8); }
static inline size_t ctl_frame_len(const ctl_parser_t *p) { return (size_t)p->buf[5] | ((size_t)p->buf[6] << 8); }
static inline const uint8_t *ctl_frame_payload(const ctl_parser_t *p) { return p->buf + CTL_HEADER_BYTES; }
