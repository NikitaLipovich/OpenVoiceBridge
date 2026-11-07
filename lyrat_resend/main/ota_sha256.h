// SHA-256 and HMAC-SHA256 in plain C, no platform calls: the OTA code runs the same on both boards and in the PC tests.
#pragma once

#include <stddef.h>
#include <stdint.h>

#define OTA_SHA256_BYTES 32

typedef struct {
    uint32_t h[8];
    uint64_t bytes;
    uint8_t block[64];
    size_t fill;
} ota_sha256_t;

void ota_sha256_start(ota_sha256_t *s);
void ota_sha256_update(ota_sha256_t *s, const void *data, size_t len);
void ota_sha256_finish(ota_sha256_t *s, uint8_t out[OTA_SHA256_BYTES]);

void ota_hmac_sha256(const uint8_t *key, size_t key_len, const void *msg, size_t msg_len, uint8_t out[OTA_SHA256_BYTES]);

// Compares without an early exit, so the time does not tell how many bytes matched.
int ota_equal_ct(const uint8_t *a, const uint8_t *b, size_t len);
