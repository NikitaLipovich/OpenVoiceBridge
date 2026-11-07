// ROM bundle: what U3 writes into a bricked U1/U2 through the ESP32 ROM loader (goal 095, docs/PLATFORM_OTA.md).
//
// Little endian. Header of ROM_BUNDLE_HEAD bytes, then the data of the segments one after the other:
//   0 "RBND"   4 u8 version 1   5 u8 chip (esp_image chip id, 0 = ESP32)   6 u8 count (1..8)   7 u8 flags 0
//   8 u32 flash size of the target   12 u32 total size of the bundle, header included
//   16 + 24 * i: u32 offset, u32 size, md5[16] of the segment's bytes
// Made by platform_ota.py bundle from an idf.py build (flasher_args.json). The ota_link transfer covers the whole bundle
// with its SHA-256; the MD5 of each segment is what the ROM loader reports after writing it.
//
// Plain C: the PC tests build it too.
#pragma once

#include <stddef.h>
#include <stdint.h>

#define ROM_BUNDLE_MAGIC     "RBND"
#define ROM_BUNDLE_VERSION   1
#define ROM_BUNDLE_HEAD      256     // also >= OTA_HEAD_MIN: the first chunk of a transfer holds the whole header
#define ROM_BUNDLE_MAX_SEG   8
#define ROM_SECTOR           4096u

typedef struct {
    uint32_t offset, size;      // where in the target's flash
    uint32_t data_at;           // where in the bundle
    uint8_t md5[16];
} rom_segment_t;

typedef struct {
    uint8_t chip, count;
    uint32_t flash_size, total;
    rom_segment_t seg[ROM_BUNDLE_MAX_SEG];
} rom_bundle_t;

typedef enum {
    ROM_BUNDLE_OK = 0,
    ROM_BUNDLE_SHORT,       // fewer bytes than the header
    ROM_BUNDLE_MAGIC_BAD,
    ROM_BUNDLE_VERSION_BAD,
    ROM_BUNDLE_CHIP_BAD,
    ROM_BUNDLE_COUNT_BAD,
    ROM_BUNDLE_FLASH_BAD,   // flash size not a power of two from 1 to 16 MB
    ROM_BUNDLE_ALIGN,       // a segment not on a sector boundary, or empty
    ROM_BUNDLE_RANGE,       // a segment past the end of the flash, or below 0x1000
    ROM_BUNDLE_OVERLAP,     // segments overlap or are not in order
    ROM_BUNDLE_PROTECTED,   // a segment touches the protected range (NVS: pairings, calibration)
    ROM_BUNDLE_SIZE,        // total does not match the header plus the segments, or the size of the transfer
} rom_bundle_err_t;

const char *rom_bundle_err_name(int err);

// Parses and checks a header. size: bytes of the whole bundle as announced by the transfer (0 skips that check).
// protect_from..protect_to (exclusive) must not be touched by any segment; 0, 0 for none.
int rom_bundle_parse(const uint8_t *head, size_t len, uint32_t size, uint8_t chip, uint32_t protect_from,
                     uint32_t protect_to, rom_bundle_t *out);
