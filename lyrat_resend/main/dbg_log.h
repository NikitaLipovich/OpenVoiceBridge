// Debug log over the network (docs/OTA.md, "Debug log"): every log line also goes into a ring of fixed slots, which a
// sender drains to the S3 (LyraT, UART frame CTL_T_LOG) or to the PC (S3, UDP). Writing never waits: a full ring drops the
// new line and counts it, and the line numbers keep counting, so the PC sees where lines are missing and why.
//
// Record, little endian, the same in a UART frame and in a UDP datagram:
//   0 u8 board   1 u8 level   2 u16 boot id   4 u32 line number since boot   8 u32 ms since boot
//   12 u32 lines dropped on the board so far   16 u16 text length   18 text, no newline
// UDP datagram: "UL", version 1, count of records, then the records.
//
// The ring code is plain C: the PC tests build it too. Only dbg_log_install() and the lock use the platform.
#pragma once

#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
#endif

#define DBG_BOARD_S3     0
#define DBG_BOARD_LYRAT  1
#define DBG_REC_HDR      18
#define DBG_TEXT_MAX     160
#define DBG_UDP_PORT     5008
#define DBG_UDP_VERSION  1
#define DBG_UDP_HDR      4
#define DBG_UDP_MAX      1400

enum { DBG_LVL_NONE = 0, DBG_LVL_E = 1, DBG_LVL_W = 2, DBG_LVL_I = 3, DBG_LVL_D = 4, DBG_LVL_V = 5 };

enum { DBG_SLOT_FREE = 0, DBG_SLOT_FILLING = 1, DBG_SLOT_READY = 2 };

typedef struct {
    volatile uint8_t state;
    uint8_t board, level;
    uint16_t boot, len;
    uint32_t seq, t_ms, lost;
    char text[DBG_TEXT_MAX];
} dbg_slot_t;

typedef struct {
    dbg_slot_t *slots;
    uint32_t n, head, tail, count;
    uint32_t seq;       // next line number
    uint32_t lost;      // lines this ring dropped
    uint32_t taken;     // lines handed to the sender
    uint16_t boot;
    uint8_t board;
#ifdef ESP_PLATFORM
    portMUX_TYPE mux;
#endif
} dbg_ring_t;

void dbg_ring_init(dbg_ring_t *r, dbg_slot_t *slots, uint32_t n, uint8_t board, uint16_t boot);

// Producer: reserve a slot (NULL when full, the line is counted as lost), write text into it, commit.
dbg_slot_t *dbg_ring_reserve(dbg_ring_t *r, uint32_t t_ms);
void dbg_ring_commit(dbg_ring_t *r, dbg_slot_t *s, int formatted_len);
int dbg_ring_push(dbg_ring_t *r, uint32_t t_ms, const char *text, size_t len);

// A record made elsewhere (the LyraT's, relayed by the S3) keeps its own board, number and counters. 0 when it does not
// parse, -1 when the ring was full.
int dbg_ring_push_record(dbg_ring_t *r, const uint8_t *rec, size_t len, size_t *used);

// Consumer: whole records into out, oldest first; returns the bytes written, *count the records.
size_t dbg_ring_pack(dbg_ring_t *r, uint8_t *out, size_t cap, uint32_t *count);
uint32_t dbg_ring_pending(dbg_ring_t *r);

size_t dbg_rec_encode(uint8_t *out, size_t cap, const dbg_slot_t *s);
// Level of an ESP_LOG line ("E (", "W (", ...) after an optional colour code; 0 when it is not one.
int dbg_line_level(const char *text, size_t len);

#ifdef ESP_PLATFORM
// Every ESP_LOG line from now on also goes into the ring; the console output stays as it was.
void dbg_log_install(dbg_ring_t *r);
#endif
