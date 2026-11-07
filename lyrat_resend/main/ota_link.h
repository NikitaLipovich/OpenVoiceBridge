// Image transfer for OTA, the same on every hop: PC -> S3 over UDP, S3 -> LyraT over the control UART (docs/OTA.md).
//
// Plain C without platform calls. The transport and the flash live behind the port structs, so the PC tests compile the
// same file and the S3 can later reach a third board over Wi-Fi with the same code.
//
// Request payload, little endian:
//   0 u8 op   1 u32 session   then by op:
//   BEGIN  5 u32 size   9 sha256[32]   41 u8 flags   42 u32 baud   46 auth[32]       (78 bytes, auth over bytes 0..45)
//   DATA   5 u32 offset 9 data[1..OTA_CHUNK_MAX]
//   END, ABORT, STATUS: nothing more
// Reply payload:
//   0 u8 op   1 u8 status   2 u32 session   6 u32 next   10 text (STATUS only)
//
// The receiver takes chunks in order only. "next" is the count of bytes taken, so every reply is a cumulative ack: a lost,
// repeated or corrupted chunk is answered with the same next, and the sender goes back to it. The image counts only after
// END, when the SHA-256 of all bytes matches the one announced in BEGIN.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "ota_sha256.h"

#define OTA_CHUNK_MAX       1008
#define OTA_REQ_HDR         9
#define OTA_REQ_MAX         (OTA_REQ_HDR + OTA_CHUNK_MAX)
#define OTA_BEGIN_BYTES     78
#define OTA_BEGIN_SIGNED    46
#define OTA_REPLY_HDR       10
#define OTA_REPLY_MAX       256
#define OTA_NONCE_BYTES     16
#define OTA_HEAD_MIN        208     // image header, first segment header and the app description up to its ELF SHA-256
#define OTA_APP_ELF_SHA_AT  176     // offset of the ELF SHA-256 in an app image (header 24 + segment 8 + 144)

// UDP transport of the S3 (and of any later board with a network): datagram = "OT", version, target, payload.
#define OTA_UDP_PORT        5007
#define OTA_UDP_VERSION     1
#define OTA_UDP_HDR         4
#define OTA_TARGET_SELF     0       // the board that receives the datagram
#define OTA_TARGET_LYRAT    1       // staged on the S3, then sent over the UART
#define OTA_OP_HELLO        0x10    // reply: status OK, payload text = 32 hex chars of a fresh nonce
#define OTA_OP_QUERY        0x11    // reply: status OK, payload text = JSON state

// Image check for one chip: the IDs of esp_image_header_t.chip_id
#define OTA_CHIP_ESP32      0x0000
#define OTA_CHIP_ESP32S3    0x0009

typedef enum {
    OTA_OP_BEGIN = 1,
    OTA_OP_DATA = 2,
    OTA_OP_END = 3,
    OTA_OP_ABORT = 4,
    OTA_OP_STATUS = 5,
} ota_op_t;

typedef enum {
    OTA_ST_OK = 0,
    OTA_ST_BUSY = 1,          // a call is on: writes pause, the sender waits and repeats
    OTA_ST_REFUSED = 2,       // not now: SLC or SCO without FORCE, a reboot pending, a relay running
    OTA_ST_NO_SESSION = 3,    // the session is not the open one
    OTA_ST_TOO_BIG = 4,
    OTA_ST_BAD_IMAGE = 5,     // magic, chip or project of the image is not for this board
    OTA_ST_FLASH = 6,
    OTA_ST_SHA = 7,           // all bytes came, their SHA-256 is not the announced one: the image is dropped
    OTA_ST_INCOMPLETE = 8,    // END before all bytes: next says where to go on
    OTA_ST_AUTH = 9,          // the token proof does not match
    OTA_ST_BAD_REQ = 10,
    OTA_ST_VERIFY = 11,       // the platform's own check of the image failed
    OTA_ST_NO_SLOT = 12,      // no second app partition: still the old layout
    OTA_ST_TIMEOUT = 13,      // sender side only
    OTA_ST_COUNT
} ota_status_t;

#define OTA_F_FORCE     0x01  // also with SLC up or SCO on: the operator takes the risk for the call
#define OTA_F_FAILTEST  0x02  // the new image fails its self-test once on purpose, to see the rollback
#define OTA_F_DRYRUN    0x04  // everything is received and checked, nothing is activated

const char *ota_status_name(int status);

// Checks the start of an image: magic 0xE9, chip, app description magic, project name (NULL skips the name).
int ota_image_check(const uint8_t *head, size_t len, uint16_t chip_id, const char *project);
// The ELF SHA-256 the app description carries, the value esp_app_get_elf_sha256() prints at run time.
const uint8_t *ota_image_elf_sha(const uint8_t *head, size_t len);

// Token proof of BEGIN: HMAC-SHA256(token, nonce || BEGIN bytes 0..45).
void ota_auth_make(const uint8_t *token, size_t token_len, const uint8_t nonce[OTA_NONCE_BYTES], const uint8_t *begin,
                   uint8_t out[OTA_SHA256_BYTES]);
int ota_auth_check(const uint8_t *token, size_t token_len, const uint8_t nonce[OTA_NONCE_BYTES], const uint8_t *begin);

size_t ota_build_begin(uint8_t *out, uint32_t session, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags,
                       uint32_t baud, const uint8_t *auth);
size_t ota_build_short(uint8_t *out, uint8_t op, uint32_t session);

static inline uint32_t ota_get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline void ota_put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

// ---------------------------------------------------------------------------------------------------------------------
// Receiver

typedef struct {
    void *ctx;
    uint32_t (*capacity)(void *ctx);                                   // bytes of the target slot, 0: no slot
    int (*may_begin)(void *ctx, uint8_t flags);                        // OTA_ST_OK or a refusal
    int (*busy)(void *ctx, uint8_t flags);                             // nonzero: hold the writes now
    int (*check_head)(void *ctx, const uint8_t *head, size_t len);     // OTA_ST_OK or OTA_ST_BAD_IMAGE
    int (*auth)(void *ctx, const uint8_t *begin);                      // NULL: no token on this hop; 1 accepted
    int (*begin)(void *ctx, uint32_t size, uint8_t flags);             // 0 ok; may erase for seconds
    int (*write)(void *ctx, uint32_t offset, const uint8_t *data, size_t len);
    int (*finish)(void *ctx, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags);  // OK / VERIFY / FLASH
    void (*abort)(void *ctx);
    size_t (*status_text)(void *ctx, char *out, size_t cap);           // NULL allowed
} ota_rx_port_t;

typedef enum { OTA_RX_IDLE = 0, OTA_RX_RECV = 1, OTA_RX_DONE = 2 } ota_rx_state_t;

#define OTA_EV_BEGUN   0x01   // a session was opened: the UART owner switches to rx.baud after the reply
#define OTA_EV_CLOSED  0x02   // a session ended without activation: back to the default baud
#define OTA_EV_DONE    0x04   // a new image is active: restart after the reply

typedef struct {
    const ota_rx_port_t *port;
    uint32_t idle_ms;
    ota_rx_state_t state;
    uint32_t session, size, next, baud, last_ms;
    uint8_t flags;
    uint8_t sha[OTA_SHA256_BYTES];
    ota_sha256_t hash;
    uint8_t events;
    uint8_t last_err;
    // The answer to the last END, for a repeat whose reply got lost.
    uint32_t memo_session, memo_next;
    uint8_t memo_status, have_memo;
    uint32_t begins, chunks, dups, gaps, busy, aborts, timeouts, done;
} ota_rx_t;

void ota_rx_init(ota_rx_t *rx, const ota_rx_port_t *port, uint32_t idle_ms);
size_t ota_rx_handle(ota_rx_t *rx, const uint8_t *req, size_t len, uint32_t now_ms, uint8_t *reply, size_t cap);
void ota_rx_tick(ota_rx_t *rx, uint32_t now_ms);
uint8_t ota_rx_take_events(ota_rx_t *rx);
const char *ota_rx_state_name(ota_rx_state_t s);

// ---------------------------------------------------------------------------------------------------------------------
// Sender, go-back-N with a window of chunks

typedef struct {
    void *ctx;
    int (*read)(void *ctx, uint32_t offset, uint8_t *buf, size_t len);    // 0 ok
} ota_tx_port_t;

typedef enum { OTA_TX_BEGIN = 0, OTA_TX_SEND = 1, OTA_TX_END = 2, OTA_TX_DONE = 3, OTA_TX_FAILED = 4 } ota_tx_state_t;

typedef struct {
    // Set before ota_tx_start(), the defaults come from ota_tx_init().
    uint32_t chunk, window;
    uint32_t rto_ms, begin_rto_ms, begin_max_ms, busy_wait_ms, busy_max_ms;
    uint32_t max_stalls, max_restarts;
    const ota_tx_port_t *port;
    uint32_t size, baud, session;
    uint8_t sha[OTA_SHA256_BYTES];
    uint8_t flags;
    uint8_t auth[OTA_SHA256_BYTES];
    // State
    ota_tx_state_t state;
    uint32_t acked, sent_to, deadline_ms, started_ms, phase_ms, busy_since_ms, fast_at;
    uint8_t busy_now, have_fast;
    uint8_t fail_status;
    uint32_t stalls, restarts, sent, resent, busy_replies, stale;
} ota_tx_t;

void ota_tx_init(ota_tx_t *tx, const ota_tx_port_t *port);
void ota_tx_start(ota_tx_t *tx, uint32_t session, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags,
                  uint32_t baud, uint32_t now_ms);
size_t ota_tx_poll(ota_tx_t *tx, uint32_t now_ms, uint8_t *out, size_t cap);
void ota_tx_on_reply(ota_tx_t *tx, const uint8_t *rep, size_t len, uint32_t now_ms);
const char *ota_tx_state_name(ota_tx_state_t s);
