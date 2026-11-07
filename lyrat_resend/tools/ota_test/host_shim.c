// PC build of the shared firmware code (build_host.cmd): exports for the Python tests what the headers keep inline,
// and opaque constructors for the sender, the receiver and the log ring. Made by goal 090.
#define __attribute__(x)
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ctl_frame.h"
#include "ota_link.h"
#include "dbg_log.h"

size_t host_ctl_build(uint8_t *out, uint8_t type, uint16_t seq, const uint8_t *payload, size_t len)
{
    return ctl_frame_build(out, type, seq, payload, len);
}

ctl_parser_t *host_ctl_parser_new(void) { return (ctl_parser_t *)calloc(1, sizeof(ctl_parser_t)); }
void host_ctl_parser_free(ctl_parser_t *p) { free(p); }
int host_ctl_feed(ctl_parser_t *p, uint8_t byte, uint32_t now_ms) { return ctl_parser_feed(p, byte, now_ms) ? 1 : 0; }
uint8_t host_ctl_type(ctl_parser_t *p) { return ctl_frame_type(p); }
uint16_t host_ctl_seq(ctl_parser_t *p) { return ctl_frame_seq(p); }
size_t host_ctl_len(ctl_parser_t *p) { return ctl_frame_len(p); }
const uint8_t *host_ctl_payload(ctl_parser_t *p) { return ctl_frame_payload(p); }
void host_ctl_counts(ctl_parser_t *p, uint32_t *out5)
{
    out5[0] = p->frames; out5[1] = p->bad_header; out5[2] = p->bad_crc; out5[3] = p->truncated; out5[4] = p->noise;
}
size_t host_sizeof_rx(void) { return sizeof(ota_rx_t); }

// Sender and receiver as opaque objects for ctypes.
ota_tx_t *host_tx_new(const ota_tx_port_t *port)
{
    ota_tx_t *tx = (ota_tx_t *)calloc(1, sizeof(ota_tx_t));
    ota_tx_init(tx, port);
    return tx;
}

void host_tx_config(ota_tx_t *tx, const uint32_t *v)
{
    tx->chunk = v[0]; tx->window = v[1]; tx->rto_ms = v[2]; tx->begin_rto_ms = v[3]; tx->begin_max_ms = v[4];
    tx->busy_wait_ms = v[5]; tx->busy_max_ms = v[6]; tx->max_stalls = v[7]; tx->max_restarts = v[8];
}

void host_tx_set_auth(ota_tx_t *tx, const uint8_t *auth) { memcpy(tx->auth, auth, OTA_SHA256_BYTES); }

// state, fail_status, acked, sent_to, session, stalls, restarts, sent, resent, busy_replies, stale
void host_tx_info(const ota_tx_t *tx, uint32_t *v)
{
    v[0] = tx->state; v[1] = tx->fail_status; v[2] = tx->acked; v[3] = tx->sent_to; v[4] = tx->session; v[5] = tx->stalls;
    v[6] = tx->restarts; v[7] = tx->sent; v[8] = tx->resent; v[9] = tx->busy_replies; v[10] = tx->stale;
}

void host_free(void *p) { free(p); }

ota_rx_t *host_rx_new(const ota_rx_port_t *port, uint32_t idle_ms)
{
    ota_rx_t *rx = (ota_rx_t *)calloc(1, sizeof(ota_rx_t));
    ota_rx_init(rx, port, idle_ms);
    return rx;
}

// state, session, size, next, flags, baud, last_err, begins, chunks, dups, gaps, busy, aborts, timeouts, done
void host_rx_info(const ota_rx_t *rx, uint32_t *v)
{
    v[0] = rx->state; v[1] = rx->session; v[2] = rx->size; v[3] = rx->next; v[4] = rx->flags; v[5] = rx->baud;
    v[6] = rx->last_err; v[7] = rx->begins; v[8] = rx->chunks; v[9] = rx->dups; v[10] = rx->gaps; v[11] = rx->busy;
    v[12] = rx->aborts; v[13] = rx->timeouts; v[14] = rx->done;
}

dbg_ring_t *host_ring_new(uint32_t n, uint8_t board, uint16_t boot)
{
    dbg_ring_t *r = (dbg_ring_t *)calloc(1, sizeof(dbg_ring_t));
    dbg_slot_t *slots = (dbg_slot_t *)calloc(n, sizeof(dbg_slot_t));
    dbg_ring_init(r, slots, n, board, boot);
    return r;
}

// Formats like the vprintf hook does: reserve, vsnprintf into the slot, commit.
static int ring_printf(dbg_ring_t *r, uint32_t t_ms, const char *fmt, ...)
{
    dbg_slot_t *s = dbg_ring_reserve(r, t_ms);
    if (!s) return 0;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(s->text, DBG_TEXT_MAX, fmt, ap);
    va_end(ap);
    dbg_ring_commit(r, s, n);
    return 1;
}

int host_ring_log(dbg_ring_t *r, uint32_t t_ms, const char *line) { return ring_printf(r, t_ms, "%s", line); }

// count, lost, seq, taken
void host_ring_info(dbg_ring_t *r, uint32_t *v) { v[0] = r->count; v[1] = r->lost; v[2] = r->seq; v[3] = r->taken; }
