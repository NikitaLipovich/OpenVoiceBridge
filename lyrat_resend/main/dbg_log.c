// Ring of log lines for the network debug channel, see dbg_log.h.
#include "dbg_log.h"

#include <stdio.h>
#include <string.h>

#ifdef ESP_PLATFORM
#include "esp_log.h"
#include "esp_timer.h"
#define RING_LOCK(r)   portENTER_CRITICAL_SAFE(&(r)->mux)
#define RING_UNLOCK(r) portEXIT_CRITICAL_SAFE(&(r)->mux)
#else
#define RING_LOCK(r)   ((void)0)
#define RING_UNLOCK(r) ((void)0)
#endif

void dbg_ring_init(dbg_ring_t *r, dbg_slot_t *slots, uint32_t n, uint8_t board, uint16_t boot)
{
    memset(r, 0, sizeof(*r));
    memset(slots, 0, sizeof(*slots) * n);
    r->slots = slots;
    r->n = n;
    r->board = board;
    r->boot = boot;
#ifdef ESP_PLATFORM
    portMUX_INITIALIZE(&r->mux);
#endif
}

dbg_slot_t *dbg_ring_reserve(dbg_ring_t *r, uint32_t t_ms)
{
    dbg_slot_t *s = NULL;
    RING_LOCK(r);
    uint32_t seq = r->seq++;
    if (r->count < r->n) {
        s = &r->slots[r->head];
        r->head = (r->head + 1) % r->n;
        r->count++;
        s->state = DBG_SLOT_FILLING;
        s->board = r->board;
        s->boot = r->boot;
        s->seq = seq;
        s->t_ms = t_ms;
        s->lost = r->lost;
        s->len = 0;
    } else {
        r->lost++;
    }
    RING_UNLOCK(r);
    return s;
}

static size_t skip_colour(const char *t, size_t len)
{
    size_t i = 0;
    while (i + 1 < len && t[i] == '\033' && t[i + 1] == '[') {
        size_t j = i + 2;
        while (j < len && t[j] != 'm') j++;
        if (j >= len) break;
        i = j + 1;
    }
    return i;
}

int dbg_line_level(const char *text, size_t len)
{
    size_t i = skip_colour(text, len);
    if (i + 2 >= len || text[i + 1] != ' ' || text[i + 2] != '(') return DBG_LVL_NONE;
    switch (text[i]) {
    case 'E': return DBG_LVL_E;
    case 'W': return DBG_LVL_W;
    case 'I': return DBG_LVL_I;
    case 'D': return DBG_LVL_D;
    case 'V': return DBG_LVL_V;
    default: return DBG_LVL_NONE;
    }
}

// Drops the colour codes and the line end, keeps the rest; a cut line ends with '~'.
static void tidy(dbg_slot_t *s, int formatted_len)
{
    size_t len = formatted_len < 0 ? 0 : (size_t)formatted_len;
    int cut = len >= DBG_TEXT_MAX;
    if (cut) len = DBG_TEXT_MAX - 1;
    s->level = (uint8_t)dbg_line_level(s->text, len);
    size_t w = 0;
    for (size_t i = 0; i < len;) {
        if (s->text[i] == '\033' && i + 1 < len && s->text[i + 1] == '[') {
            size_t j = i + 2;
            while (j < len && s->text[j] != 'm') j++;
            i = j + 1;
            continue;
        }
        s->text[w++] = s->text[i++];
    }
    while (w && (s->text[w - 1] == '\n' || s->text[w - 1] == '\r')) w--;
    if (cut && w) s->text[w - 1] = '~';
    s->len = (uint16_t)w;
}

void dbg_ring_commit(dbg_ring_t *r, dbg_slot_t *s, int formatted_len)
{
    (void)r;
    tidy(s, formatted_len);
#ifdef ESP_PLATFORM
    __sync_synchronize();          // the text is in memory before the other core can see the slot as ready
#endif
    s->state = DBG_SLOT_READY;     // a single byte store: the reader sees it or not
}

int dbg_ring_push(dbg_ring_t *r, uint32_t t_ms, const char *text, size_t len)
{
    dbg_slot_t *s = dbg_ring_reserve(r, t_ms);
    if (!s) return 0;
    size_t n = len < DBG_TEXT_MAX ? len : DBG_TEXT_MAX;
    memcpy(s->text, text, n);
    dbg_ring_commit(r, s, (int)(len < DBG_TEXT_MAX ? len : DBG_TEXT_MAX));
    return 1;
}

int dbg_ring_push_long(dbg_ring_t *r, uint32_t t_ms, const char *text, size_t len)
{
    const size_t cont = sizeof(DBG_CONT) - 1;
    int records = 0;
    size_t at = 0;
    while (at < len) {
        size_t room = DBG_TEXT_MAX - 1 - (records ? cont : 0);
        size_t take = len - at;
        if (take > room) {
            take = room;        // no space in the second half: cut inside a word
            for (size_t i = room; i > room / 2; i--) {
                if (text[at + i] == ' ') {
                    take = i;
                    break;
                }
            }
        }
        dbg_slot_t *s = dbg_ring_reserve(r, t_ms);
        if (!s) return records;
        size_t w = 0;
        if (records) {
            memcpy(s->text, DBG_CONT, cont);
            w = cont;
        }
        memcpy(s->text + w, text + at, take);
        dbg_ring_commit(r, s, (int)(w + take));
        records++;
        at += take;
        while (at < len && text[at] == ' ') at++;
    }
    return records;
}

static uint16_t get16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

int dbg_ring_push_record(dbg_ring_t *r, const uint8_t *rec, size_t len, size_t *used)
{
    if (len < DBG_REC_HDR) return 0;
    size_t tlen = get16(rec + 16);
    if (tlen > DBG_TEXT_MAX || DBG_REC_HDR + tlen > len) return 0;
    *used = DBG_REC_HDR + tlen;
    dbg_slot_t *s = NULL;
    RING_LOCK(r);
    r->seq++;
    if (r->count < r->n) {
        s = &r->slots[r->head];
        r->head = (r->head + 1) % r->n;
        r->count++;
        s->state = DBG_SLOT_FILLING;
    } else {
        r->lost++;
    }
    RING_UNLOCK(r);
    if (!s) return -1;
    s->board = rec[0];
    s->level = rec[1];
    s->boot = get16(rec + 2);
    s->seq = get32(rec + 4);
    s->t_ms = get32(rec + 8);
    s->lost = get32(rec + 12);
    s->len = (uint16_t)tlen;
    memcpy(s->text, rec + DBG_REC_HDR, tlen);
#ifdef ESP_PLATFORM
    __sync_synchronize();
#endif
    s->state = DBG_SLOT_READY;
    return 1;
}

size_t dbg_rec_encode(uint8_t *out, size_t cap, const dbg_slot_t *s)
{
    size_t need = DBG_REC_HDR + s->len;
    if (cap < need) return 0;
    out[0] = s->board;
    out[1] = s->level;
    put16(out + 2, s->boot);
    put32(out + 4, s->seq);
    put32(out + 8, s->t_ms);
    put32(out + 12, s->lost);
    put16(out + 16, s->len);
    memcpy(out + DBG_REC_HDR, s->text, s->len);
    return need;
}

size_t dbg_ring_pack(dbg_ring_t *r, uint8_t *out, size_t cap, uint32_t *count)
{
    size_t used = 0;
    uint32_t n = 0;
    for (;;) {
        dbg_slot_t *s = NULL;
        RING_LOCK(r);
        if (r->count && r->slots[r->tail].state == DBG_SLOT_READY) s = &r->slots[r->tail];
        RING_UNLOCK(r);
        if (!s || cap - used < DBG_REC_HDR + (size_t)s->len) break;
        used += dbg_rec_encode(out + used, cap - used, s);
        n++;
        RING_LOCK(r);
        s->state = DBG_SLOT_FREE;
        r->tail = (r->tail + 1) % r->n;
        r->count--;
        r->taken++;
        RING_UNLOCK(r);
    }
    if (count) *count = n;
    return used;
}

uint32_t dbg_ring_pending(dbg_ring_t *r)
{
    RING_LOCK(r);
    uint32_t c = r->count;
    RING_UNLOCK(r);
    return c;
}

size_t dbg_udp_pack(uint8_t *buf, size_t cap, dbg_ring_t *first, dbg_ring_t *second)
{
    uint32_t a = 0, b = 0;
    if (cap <= DBG_UDP_HDR) return 0;
    size_t used = DBG_UDP_HDR;
    used += dbg_ring_pack(first, buf + used, cap - used, &a);
    if (second) used += dbg_ring_pack(second, buf + used, cap - used, &b);
    if (a + b == 0) return 0;
    buf[0] = 'U';
    buf[1] = 'L';
    buf[2] = DBG_UDP_VERSION;
    buf[3] = (uint8_t)(a + b);
    return used;
}

int dbg_udp_round(dbg_udp_t *u, dbg_ring_t *first, dbg_ring_t *second, int burst, dbg_send_fn send, void *ctx)
{
    int sent = 0;
    while (sent < burst) {
        if (!u->len) u->len = dbg_udp_pack(u->buf, sizeof(u->buf), first, second);
        if (!u->len) break;
        if (send(ctx, u->buf, u->len) != 0) {
            u->send_fail++;         // kept in u->buf: the next round sends it again
            break;
        }
        u->len = 0;
        u->datagrams++;
        sent++;
    }
    return sent;
}

#ifdef ESP_PLATFORM
static dbg_ring_t *s_hook_ring;
static vprintf_like_t s_prev_vprintf;

static int dbg_vprintf(const char *fmt, va_list ap)
{
    va_list copy;
    va_copy(copy, ap);
    int ret = s_prev_vprintf ? s_prev_vprintf(fmt, ap) : vprintf(fmt, ap);
    dbg_ring_t *r = s_hook_ring;
    dbg_slot_t *s = r ? dbg_ring_reserve(r, (uint32_t)(esp_timer_get_time() / 1000)) : NULL;
    if (s) dbg_ring_commit(r, s, vsnprintf(s->text, DBG_TEXT_MAX, fmt, copy));
    va_end(copy);
    return ret;
}

void dbg_log_install(dbg_ring_t *r)
{
    s_hook_ring = r;
    vprintf_like_t prev = esp_log_set_vprintf(dbg_vprintf);
    if (prev != dbg_vprintf) s_prev_vprintf = prev;
}
#endif
