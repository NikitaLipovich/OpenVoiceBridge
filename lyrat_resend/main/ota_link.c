// Image transfer for OTA: receiver and sender state machines, see ota_link.h and docs/OTA.md.
#include "ota_link.h"

#include <stdio.h>
#include <string.h>

#define APP_DESC_AT         32          // image header 24 bytes, first segment header 8 bytes
#define APP_DESC_MAGIC      0xABCD5432u
#define APP_PROJECT_AT      (APP_DESC_AT + 48)
#define IMAGE_MAGIC         0xE9
#define IMAGE_CHIP_AT       12

static const char *const s_status_names[OTA_ST_COUNT] = {
    "ok", "busy", "refused", "no_session", "too_big", "bad_image", "flash", "sha", "incomplete", "auth", "bad_req",
    "verify", "no_slot", "timeout",
};

const char *ota_status_name(int status)
{
    return status >= 0 && status < OTA_ST_COUNT ? s_status_names[status] : "?";
}

int ota_image_check(const uint8_t *head, size_t len, uint16_t chip_id, const char *project)
{
    if (len < OTA_HEAD_MIN || head[0] != IMAGE_MAGIC) return OTA_ST_BAD_IMAGE;
    uint16_t chip = (uint16_t)(head[IMAGE_CHIP_AT] | (head[IMAGE_CHIP_AT + 1] << 8));
    if (chip != chip_id || ota_get32(head + APP_DESC_AT) != APP_DESC_MAGIC) return OTA_ST_BAD_IMAGE;
    if (project) {
        const char *name = (const char *)head + APP_PROJECT_AT;
        if (memchr(name, 0, 32) == NULL || strcmp(name, project) != 0) return OTA_ST_BAD_IMAGE;
    }
    return OTA_ST_OK;
}

const uint8_t *ota_image_elf_sha(const uint8_t *head, size_t len)
{
    return len >= OTA_HEAD_MIN ? head + OTA_APP_ELF_SHA_AT : NULL;
}

void ota_auth_make(const uint8_t *token, size_t token_len, const uint8_t nonce[OTA_NONCE_BYTES], const uint8_t *begin,
                   uint8_t out[OTA_SHA256_BYTES])
{
    uint8_t msg[OTA_NONCE_BYTES + OTA_BEGIN_SIGNED];
    memcpy(msg, nonce, OTA_NONCE_BYTES);
    memcpy(msg + OTA_NONCE_BYTES, begin, OTA_BEGIN_SIGNED);
    ota_hmac_sha256(token, token_len, msg, sizeof(msg), out);
}

int ota_auth_check(const uint8_t *token, size_t token_len, const uint8_t nonce[OTA_NONCE_BYTES], const uint8_t *begin)
{
    uint8_t want[OTA_SHA256_BYTES];
    ota_auth_make(token, token_len, nonce, begin, want);
    return ota_equal_ct(want, begin + OTA_BEGIN_SIGNED, OTA_SHA256_BYTES);
}

size_t ota_build_begin(uint8_t *out, uint32_t session, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags,
                       uint32_t baud, const uint8_t *auth)
{
    out[0] = OTA_OP_BEGIN;
    ota_put32(out + 1, session);
    ota_put32(out + 5, size);
    memcpy(out + 9, sha, OTA_SHA256_BYTES);
    out[41] = flags;
    ota_put32(out + 42, baud);
    if (auth) memcpy(out + OTA_BEGIN_SIGNED, auth, OTA_SHA256_BYTES);
    else memset(out + OTA_BEGIN_SIGNED, 0, OTA_SHA256_BYTES);
    return OTA_BEGIN_BYTES;
}

size_t ota_build_short(uint8_t *out, uint8_t op, uint32_t session)
{
    out[0] = op;
    ota_put32(out + 1, session);
    return 5;
}

// ---------------------------------------------------------------------------------------------------------------------
// Receiver

static const char *const s_rx_state_names[] = {"idle", "recv", "done"};

const char *ota_rx_state_name(ota_rx_state_t s)
{
    return (unsigned)s < 3 ? s_rx_state_names[s] : "?";
}

void ota_rx_init(ota_rx_t *rx, const ota_rx_port_t *port, uint32_t idle_ms)
{
    memset(rx, 0, sizeof(*rx));
    rx->port = port;
    rx->idle_ms = idle_ms;
}

static size_t put_reply(uint8_t *out, size_t cap, uint8_t op, uint8_t status, uint32_t session, uint32_t next)
{
    if (cap < OTA_REPLY_HDR) return 0;
    out[0] = op;
    out[1] = status;
    ota_put32(out + 2, session);
    ota_put32(out + 6, next);
    return OTA_REPLY_HDR;
}

// Ends the open session without activating anything.
static void rx_close(ota_rx_t *rx, int abort_flash, uint8_t err)
{
    if (abort_flash && rx->port->abort) rx->port->abort(rx->port->ctx);
    rx->state = OTA_RX_IDLE;
    rx->events |= OTA_EV_CLOSED;
    if (err) rx->last_err = err;
}

static void rx_memo(ota_rx_t *rx, uint8_t status)
{
    rx->memo_session = rx->session;
    rx->memo_status = status;
    rx->memo_next = rx->next;
    rx->have_memo = 1;
}

static size_t rx_begin(ota_rx_t *rx, const uint8_t *req, size_t len, uint32_t now, uint8_t *out, size_t cap)
{
    const ota_rx_port_t *p = rx->port;
    uint32_t session = ota_get32(req + 1);
    if (len != OTA_BEGIN_BYTES) return put_reply(out, cap, OTA_OP_BEGIN, OTA_ST_BAD_REQ, session, 0);
    // A repeat after a lost reply changes nothing; erasing may also hold the first reply back for seconds.
    if (rx->state == OTA_RX_RECV && session == rx->session) {
        rx->last_ms = now;
        return put_reply(out, cap, OTA_OP_BEGIN, OTA_ST_OK, session, rx->next);
    }
    if (rx->state == OTA_RX_DONE) {
        int same = session == rx->session;
        return put_reply(out, cap, OTA_OP_BEGIN, same ? OTA_ST_OK : OTA_ST_REFUSED, session, same ? rx->size : 0);
    }
    if (p->auth && !p->auth(p->ctx, req)) {
        rx->last_err = OTA_ST_AUTH;
        return put_reply(out, cap, OTA_OP_BEGIN, OTA_ST_AUTH, session, 0);
    }
    uint32_t size = ota_get32(req + 5);
    uint8_t flags = req[41];
    uint32_t room = p->capacity(p->ctx);
    int st = room == 0 ? OTA_ST_NO_SLOT : p->may_begin ? p->may_begin(p->ctx, flags) : OTA_ST_OK;
    if (st == OTA_ST_OK && (size == 0 || size > room)) st = OTA_ST_TOO_BIG;
    if (st != OTA_ST_OK) {
        rx->last_err = (uint8_t)st;
        return put_reply(out, cap, OTA_OP_BEGIN, (uint8_t)st, session, 0);
    }
    if (rx->state == OTA_RX_RECV) {     // a new sender takes over from one that went away
        rx_close(rx, 1, 0);
        rx->aborts++;
    }
    if (p->begin(p->ctx, size, flags) != 0) {
        rx->last_err = OTA_ST_FLASH;
        rx->events |= OTA_EV_CLOSED;
        return put_reply(out, cap, OTA_OP_BEGIN, OTA_ST_FLASH, session, 0);
    }
    rx->state = OTA_RX_RECV;
    rx->session = session;
    rx->size = size;
    rx->flags = flags;
    rx->baud = ota_get32(req + 42);
    memcpy(rx->sha, req + 9, OTA_SHA256_BYTES);
    rx->next = 0;
    ota_sha256_start(&rx->hash);
    rx->last_ms = now;
    rx->begins++;
    rx->events |= OTA_EV_BEGUN;
    return put_reply(out, cap, OTA_OP_BEGIN, OTA_ST_OK, session, 0);
}

static size_t rx_data(ota_rx_t *rx, const uint8_t *req, size_t len, uint32_t now, uint8_t *out, size_t cap)
{
    const ota_rx_port_t *p = rx->port;
    uint32_t session = ota_get32(req + 1);
    if (len <= OTA_REQ_HDR || len > OTA_REQ_MAX) return put_reply(out, cap, OTA_OP_DATA, OTA_ST_BAD_REQ, session, 0);
    if (rx->state != OTA_RX_RECV || session != rx->session) {
        return put_reply(out, cap, OTA_OP_DATA, OTA_ST_NO_SESSION, session, 0);
    }
    rx->last_ms = now;
    if (p->busy && p->busy(p->ctx, rx->flags)) {
        rx->busy++;
        return put_reply(out, cap, OTA_OP_DATA, OTA_ST_BUSY, session, rx->next);
    }
    uint32_t offset = ota_get32(req + 5);
    size_t n = len - OTA_REQ_HDR;
    const uint8_t *data = req + OTA_REQ_HDR;
    if (offset != rx->next) {           // a repeat, or one past a lost chunk: the reply points back to next
        if (offset < rx->next) rx->dups++;
        else rx->gaps++;
        return put_reply(out, cap, OTA_OP_DATA, OTA_ST_OK, session, rx->next);
    }
    if (n > rx->size - offset) return put_reply(out, cap, OTA_OP_DATA, OTA_ST_BAD_REQ, session, rx->next);
    if (offset == 0) {
        size_t need = rx->size < OTA_HEAD_MIN ? rx->size : OTA_HEAD_MIN;
        if (n < need) return put_reply(out, cap, OTA_OP_DATA, OTA_ST_BAD_REQ, session, rx->next);
        int st = p->check_head ? p->check_head(p->ctx, data, n) : OTA_ST_OK;
        if (st != OTA_ST_OK) {
            rx_close(rx, 1, (uint8_t)st);
            return put_reply(out, cap, OTA_OP_DATA, (uint8_t)st, session, 0);
        }
    }
    if (p->write(p->ctx, offset, data, n) != 0) {
        rx_close(rx, 1, OTA_ST_FLASH);
        return put_reply(out, cap, OTA_OP_DATA, OTA_ST_FLASH, session, rx->next);
    }
    ota_sha256_update(&rx->hash, data, n);
    rx->next += (uint32_t)n;
    rx->chunks++;
    return put_reply(out, cap, OTA_OP_DATA, OTA_ST_OK, session, rx->next);
}

static size_t rx_end(ota_rx_t *rx, uint32_t session, uint32_t now, uint8_t *out, size_t cap)
{
    const ota_rx_port_t *p = rx->port;
    int open = rx->state == OTA_RX_RECV && session == rx->session;
    if (!open && rx->have_memo && session == rx->memo_session) {
        return put_reply(out, cap, OTA_OP_END, rx->memo_status, session, rx->memo_next);
    }
    if (!open) return put_reply(out, cap, OTA_OP_END, OTA_ST_NO_SESSION, session, 0);
    rx->last_ms = now;
    if (rx->next != rx->size) return put_reply(out, cap, OTA_OP_END, OTA_ST_INCOMPLETE, session, rx->next);
    uint8_t digest[OTA_SHA256_BYTES];
    ota_sha256_finish(&rx->hash, digest);
    if (!ota_equal_ct(digest, rx->sha, OTA_SHA256_BYTES)) {
        rx_memo(rx, OTA_ST_SHA);
        rx_close(rx, 1, OTA_ST_SHA);
        return put_reply(out, cap, OTA_OP_END, OTA_ST_SHA, session, rx->next);
    }
    // The port releases its handle whatever finish returns.
    int st = p->finish(p->ctx, rx->size, digest, rx->flags);
    if (st != OTA_ST_OK) {
        rx_memo(rx, (uint8_t)st);
        rx_close(rx, 0, (uint8_t)st);
        return put_reply(out, cap, OTA_OP_END, (uint8_t)st, session, rx->next);
    }
    rx->done++;
    rx->last_err = OTA_ST_OK;
    rx_memo(rx, OTA_ST_OK);
    if (rx->flags & OTA_F_DRYRUN) {
        rx->state = OTA_RX_IDLE;
        rx->events |= OTA_EV_CLOSED;
    } else {
        rx->state = OTA_RX_DONE;
        rx->events |= OTA_EV_DONE;
    }
    return put_reply(out, cap, OTA_OP_END, OTA_ST_OK, session, rx->next);
}

size_t ota_rx_handle(ota_rx_t *rx, const uint8_t *req, size_t len, uint32_t now_ms, uint8_t *out, size_t cap)
{
    if (len < 5) return put_reply(out, cap, len ? req[0] : 0, OTA_ST_BAD_REQ, 0, 0);
    uint8_t op = req[0];
    uint32_t session = ota_get32(req + 1);
    switch (op) {
    case OTA_OP_BEGIN:
        return rx_begin(rx, req, len, now_ms, out, cap);
    case OTA_OP_DATA:
        return rx_data(rx, req, len, now_ms, out, cap);
    case OTA_OP_END:
        if (len != 5) break;
        return rx_end(rx, session, now_ms, out, cap);
    case OTA_OP_ABORT:
        if (len != 5) break;
        if (rx->state == OTA_RX_RECV && session == rx->session) {
            rx_close(rx, 1, 0);
            rx->aborts++;
            return put_reply(out, cap, op, OTA_ST_OK, session, rx->next);
        }
        return put_reply(out, cap, op, OTA_ST_NO_SESSION, session, 0);
    case OTA_OP_STATUS: {
        size_t n = put_reply(out, cap, op, OTA_ST_OK, rx->session, rx->next);
        if (!n) return 0;
        int t = snprintf((char *)out + n, cap - n,
                         "state=%s size=%lu next=%lu err=%s begins=%lu chunks=%lu dups=%lu gaps=%lu busy=%lu aborts=%lu "
                         "timeouts=%lu done=%lu",
                         ota_rx_state_name(rx->state), (unsigned long)rx->size, (unsigned long)rx->next,
                         ota_status_name(rx->last_err), (unsigned long)rx->begins, (unsigned long)rx->chunks,
                         (unsigned long)rx->dups, (unsigned long)rx->gaps, (unsigned long)rx->busy,
                         (unsigned long)rx->aborts, (unsigned long)rx->timeouts, (unsigned long)rx->done);
        if (t < 0) t = 0;
        if ((size_t)t >= cap - n) t = (int)(cap - n - 1);
        n += (size_t)t;
        if (rx->port->status_text && cap - n > 2) {
            out[n++] = ' ';
            n += rx->port->status_text(rx->port->ctx, (char *)out + n, cap - n);
        }
        return n;
    }
    default:
        break;
    }
    return put_reply(out, cap, op, OTA_ST_BAD_REQ, session, 0);
}

void ota_rx_tick(ota_rx_t *rx, uint32_t now_ms)
{
    if (rx->state == OTA_RX_RECV && (uint32_t)(now_ms - rx->last_ms) > rx->idle_ms) {
        rx->timeouts++;
        rx_close(rx, 1, OTA_ST_TIMEOUT);
    }
}

uint8_t ota_rx_take_events(ota_rx_t *rx)
{
    uint8_t e = rx->events;
    rx->events = 0;
    return e;
}

// ---------------------------------------------------------------------------------------------------------------------
// Sender

static const char *const s_tx_state_names[] = {"begin", "send", "end", "done", "failed"};

const char *ota_tx_state_name(ota_tx_state_t s)
{
    return (unsigned)s < 5 ? s_tx_state_names[s] : "?";
}

void ota_tx_init(ota_tx_t *tx, const ota_tx_port_t *port)
{
    memset(tx, 0, sizeof(*tx));
    tx->port = port;
    tx->chunk = OTA_CHUNK_MAX;
    tx->window = 2;
    tx->rto_ms = 600;
    tx->begin_rto_ms = 2000;
    tx->begin_max_ms = 90000;           // erasing 2 MB on the LyraT takes about 20 s
    tx->busy_wait_ms = 1000;
    tx->busy_max_ms = 20u * 60u * 1000u;
    tx->max_stalls = 20;
    tx->max_restarts = 3;
    tx->state = OTA_TX_FAILED;
}

void ota_tx_start(ota_tx_t *tx, uint32_t session, uint32_t size, const uint8_t sha[OTA_SHA256_BYTES], uint8_t flags,
                  uint32_t baud, uint32_t now_ms)
{
    tx->session = session;
    tx->size = size;
    memcpy(tx->sha, sha, OTA_SHA256_BYTES);
    tx->flags = flags;
    tx->baud = baud;
    tx->state = OTA_TX_BEGIN;
    tx->acked = tx->sent_to = 0;
    tx->deadline_ms = tx->started_ms = tx->phase_ms = now_ms;
    tx->busy_since_ms = 0;
    tx->busy_now = tx->have_fast = 0;
    tx->fail_status = OTA_ST_OK;
    tx->stalls = tx->restarts = tx->sent = tx->resent = tx->busy_replies = tx->stale = 0;
}

static int due(uint32_t now, uint32_t deadline)
{
    return (int32_t)(now - deadline) >= 0;
}

static void tx_fail(ota_tx_t *tx, uint8_t status)
{
    tx->state = OTA_TX_FAILED;
    tx->fail_status = status;
}

static int auth_used(const ota_tx_t *tx)
{
    for (int i = 0; i < OTA_SHA256_BYTES; i++) {
        if (tx->auth[i]) return 1;
    }
    return 0;
}

size_t ota_tx_poll(ota_tx_t *tx, uint32_t now, uint8_t *out, size_t cap)
{
    switch (tx->state) {
    case OTA_TX_BEGIN:
        if (!due(now, tx->deadline_ms) || cap < OTA_BEGIN_BYTES) return 0;
        if ((uint32_t)(now - tx->phase_ms) > tx->begin_max_ms) {
            tx_fail(tx, OTA_ST_TIMEOUT);
            return 0;
        }
        tx->deadline_ms = now + tx->begin_rto_ms;
        tx->sent++;
        return ota_build_begin(out, tx->session, tx->size, tx->sha, tx->flags, tx->baud,
                               auth_used(tx) ? tx->auth : NULL);
    case OTA_TX_SEND: {
        if (tx->busy_now) {
            if (!due(now, tx->deadline_ms)) return 0;
            tx->busy_now = 0;
            tx->sent_to = tx->acked;
        }
        uint32_t limit = tx->acked + tx->window * tx->chunk;
        if (limit > tx->size || limit < tx->acked) limit = tx->size;
        if (tx->sent_to < limit) {
            uint32_t n = tx->size - tx->sent_to;
            if (n > tx->chunk) n = tx->chunk;
            if (cap < OTA_REQ_HDR + n) return 0;
            out[0] = OTA_OP_DATA;
            ota_put32(out + 1, tx->session);
            ota_put32(out + 5, tx->sent_to);
            if (tx->port->read(tx->port->ctx, tx->sent_to, out + OTA_REQ_HDR, n) != 0) {
                tx_fail(tx, OTA_ST_FLASH);
                return 0;
            }
            if (tx->sent_to == tx->acked) tx->deadline_ms = now + tx->rto_ms;
            tx->sent++;
            tx->sent_to += n;
            return OTA_REQ_HDR + n;
        }
        if (due(now, tx->deadline_ms)) {    // nothing moved: go back to the first byte not acked
            if (++tx->stalls > tx->max_stalls) {
                tx_fail(tx, OTA_ST_TIMEOUT);
                return 0;
            }
            tx->resent++;
            tx->sent_to = tx->acked;
            return ota_tx_poll(tx, now, out, cap);
        }
        return 0;
    }
    case OTA_TX_END:
        if (!due(now, tx->deadline_ms)) return 0;
        if ((uint32_t)(now - tx->phase_ms) > tx->begin_max_ms) {
            tx_fail(tx, OTA_ST_TIMEOUT);
            return 0;
        }
        tx->deadline_ms = now + tx->begin_rto_ms;
        tx->sent++;
        return ota_build_short(out, OTA_OP_END, tx->session);
    default:
        return 0;
    }
}

static void tx_restart(ota_tx_t *tx, uint32_t now)
{
    if (auth_used(tx) || ++tx->restarts > tx->max_restarts) {
        tx_fail(tx, OTA_ST_NO_SESSION);
        return;
    }
    tx->session++;
    tx->state = OTA_TX_BEGIN;
    tx->acked = tx->sent_to = 0;
    tx->deadline_ms = tx->phase_ms = now;
    tx->busy_now = tx->have_fast = 0;
}

static void tx_go_end(ota_tx_t *tx, uint32_t now)
{
    tx->state = OTA_TX_END;
    tx->deadline_ms = tx->phase_ms = now;
}

void ota_tx_on_reply(ota_tx_t *tx, const uint8_t *rep, size_t len, uint32_t now)
{
    if (len < OTA_REPLY_HDR || tx->state == OTA_TX_DONE || tx->state == OTA_TX_FAILED) return;
    uint8_t op = rep[0], st = rep[1];
    uint32_t session = ota_get32(rep + 2), next = ota_get32(rep + 6);
    if (session != tx->session) {
        tx->stale++;
        return;
    }
    if (op == OTA_OP_BEGIN) {
        if (tx->state != OTA_TX_BEGIN) return;
        if (st != OTA_ST_OK) {
            tx_fail(tx, st);
            return;
        }
        if (next > tx->size) next = 0;
        tx->state = OTA_TX_SEND;
        tx->acked = tx->sent_to = next;
        tx->deadline_ms = tx->phase_ms = now;
        tx->stalls = 0;
        if (tx->acked == tx->size) tx_go_end(tx, now);
    } else if (op == OTA_OP_DATA) {
        if (tx->state != OTA_TX_SEND) return;
        if (st == OTA_ST_BUSY) {
            tx->busy_replies++;
            if (!tx->busy_since_ms) tx->busy_since_ms = now ? now : 1;
            else if ((uint32_t)(now - tx->busy_since_ms) > tx->busy_max_ms) {
                tx_fail(tx, OTA_ST_BUSY);
                return;
            }
            tx->busy_now = 1;
            tx->deadline_ms = now + tx->busy_wait_ms;
            tx->sent_to = tx->acked;
            return;
        }
        tx->busy_since_ms = 0;
        if (st == OTA_ST_NO_SESSION) {
            tx_restart(tx, now);
            return;
        }
        if (st != OTA_ST_OK) {
            tx_fail(tx, st);
            return;
        }
        if (next > tx->size) {
            tx_fail(tx, OTA_ST_BAD_REQ);
            return;
        }
        if (next > tx->acked) {
            tx->acked = next;
            tx->stalls = 0;
            tx->have_fast = 0;
            tx->deadline_ms = now + tx->rto_ms;
            if (tx->sent_to < tx->acked) tx->sent_to = tx->acked;
            if (tx->acked == tx->size) tx_go_end(tx, now);
        } else if (next == tx->acked && tx->sent_to > tx->acked) {
            // The receiver got a chunk past a gap: send the gap again now, once per position.
            if (!tx->have_fast || tx->fast_at != tx->acked) {
                tx->have_fast = 1;
                tx->fast_at = tx->acked;
                tx->resent++;
                tx->sent_to = tx->acked;
                tx->deadline_ms = now + tx->rto_ms;
            }
        } else if (next < tx->acked) {      // the receiver lost what it had taken
            tx->acked = tx->sent_to = next;
            tx->deadline_ms = now + tx->rto_ms;
        }
    } else if (op == OTA_OP_END) {
        if (tx->state != OTA_TX_END) return;
        if (st == OTA_ST_OK) {
            tx->state = OTA_TX_DONE;
        } else if (st == OTA_ST_INCOMPLETE && next < tx->size) {
            tx->state = OTA_TX_SEND;
            tx->acked = tx->sent_to = next;
            tx->deadline_ms = now;
        } else {
            tx_fail(tx, st);
        }
    } else {
        tx->stale++;
    }
}
