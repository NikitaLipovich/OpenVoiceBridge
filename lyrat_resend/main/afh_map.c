// Host channel map of Bluetooth AFH from the S3's view of 2.4 GHz Wi-Fi, see afh_map.h. Goal 106.
#include "afh_map.h"

#include <stdio.h>
#include <string.h>

int afh_report_parse(const uint8_t *ack, size_t len, afh_report_t *out)
{
    if (len < AFH_REPORT_HEAD) return 0;
    if (ack[2] != AFH_REPORT_VER) return -1;
    uint8_t n = ack[11];
    if (n > AFH_REPORT_NETS || len < AFH_REPORT_HEAD + 3u * n) return -1;
    if (ack[4] > 14 || ack[5] > 2) return -1;
    afh_report_t r;
    memset(&r, 0, sizeof(r));
    r.gen = ack[3];
    r.ap_primary = ack[4];
    r.ap_second = ack[5];
    r.ap_rssi = (int8_t)ack[6];
    r.flags = ack[7];
    r.scan_age_s = (uint16_t)(ack[8] | (ack[9] << 8));
    r.n = n;
    for (uint8_t i = 0; i < n; i++) {
        const uint8_t *p = ack + AFH_REPORT_HEAD + 3 * i;
        if (p[0] < 1 || p[0] > 14 || p[1] > 2) return -1;
        r.nets[i].primary = p[0];
        r.nets[i].second = p[1];
        r.nets[i].rssi = (int8_t)p[2];
    }
    *out = r;
    return 1;
}

size_t afh_report_encode(const afh_report_t *r, uint8_t *out, size_t cap)
{
    uint8_t n = r->n > AFH_REPORT_NETS ? AFH_REPORT_NETS : r->n;
    size_t need = AFH_REPORT_HEAD - 2 + 3u * n;
    if (cap < need) return 0;
    out[0] = AFH_REPORT_VER;
    out[1] = r->gen;
    out[2] = r->ap_primary;
    out[3] = r->ap_second;
    out[4] = (uint8_t)r->ap_rssi;
    out[5] = r->flags;
    out[6] = (uint8_t)(r->scan_age_s & 0xFF);
    out[7] = (uint8_t)(r->scan_age_s >> 8);
    out[8] = 0;
    out[9] = n;
    for (uint8_t i = 0; i < n; i++) {
        out[10 + 3 * i] = r->nets[i].primary;
        out[11 + 3 * i] = r->nets[i].second;
        out[12 + 3 * i] = (uint8_t)r->nets[i].rssi;
    }
    return need;
}

size_t afh_scan_reduce(const afh_net_t *in, size_t n_in, afh_net_t *out, size_t max)
{
    afh_net_t all[3 * 14];      // every (primary, secondary) pair there is
    size_t n = 0;
    for (size_t i = 0; i < n_in; i++) {
        if (in[i].primary < 1 || in[i].primary > 14 || in[i].second > 2) continue;
        size_t j = 0;
        while (j < n && !(all[j].primary == in[i].primary && all[j].second == in[i].second)) j++;
        if (j == n) all[n++] = in[i];
        else if (in[i].rssi > all[j].rssi) all[j].rssi = in[i].rssi;
    }
    // Insertion sort, strongest first; equal levels keep the lower channel first.
    for (size_t i = 1; i < n; i++) {
        afh_net_t v = all[i];
        size_t j = i;
        while (j > 0 && (all[j - 1].rssi < v.rssi || (all[j - 1].rssi == v.rssi && all[j - 1].primary > v.primary))) {
            all[j] = all[j - 1];
            j--;
        }
        all[j] = v;
    }
    if (n > max) n = max;
    memcpy(out, all, n * sizeof(afh_net_t));
    return n;
}

bool afh_span(uint8_t primary, uint8_t second, bool wide, uint8_t *lo, uint8_t *hi)
{
    if (primary < 1 || primary > 14) return false;
    int fc = primary == 14 ? 2484 : 2407 + 5 * primary;
    int f_lo = fc - 11, f_hi = fc + 11;           // 20 MHz and the 22 MHz of DSSS, 1 MHz of margin
    if (wide && second == 1) f_hi = fc + 31;      // 40 MHz, the secondary above: centre fc + 10 +- 20, + 1
    if (wide && second == 2) f_lo = fc - 31;
    int k_lo = f_lo - 2402, k_hi = f_hi - 2402;
    if (k_lo < 0) k_lo = 0;
    if (k_hi > AFH_BT_CHANNELS - 1) k_hi = AFH_BT_CHANNELS - 1;
    if (k_lo > k_hi) return false;
    *lo = (uint8_t)k_lo;
    *hi = (uint8_t)k_hi;
    return true;
}

void afh_map_all(uint8_t map[AFH_MAP_BYTES])
{
    memset(map, 0xFF, AFH_MAP_BYTES);
    map[AFH_MAP_BYTES - 1] = 0x7F;
}

int afh_map_count(const uint8_t map[AFH_MAP_BYTES])
{
    int n = 0;
    for (int k = 0; k < AFH_BT_CHANNELS; k++) n += (map[k / 8] >> (k % 8)) & 1;
    return n;
}

void afh_map_hex(const uint8_t map[AFH_MAP_BYTES], char out[2 * AFH_MAP_BYTES + 1])
{
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < AFH_MAP_BYTES; i++) {
        out[2 * i] = digits[map[i] >> 4];
        out[2 * i + 1] = digits[map[i] & 15];
    }
    out[2 * AFH_MAP_BYTES] = '\0';
}

static int hex_digit(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

bool afh_map_from_hex(const char *text, uint8_t map[AFH_MAP_BYTES])
{
    if (strlen(text) != 2 * AFH_MAP_BYTES) return false;
    uint8_t m[AFH_MAP_BYTES];
    for (int i = 0; i < AFH_MAP_BYTES; i++) {
        int a = hex_digit(text[2 * i]), b = hex_digit(text[2 * i + 1]);
        if (a < 0 || b < 0) return false;
        m[i] = (uint8_t)(a << 4 | b);
    }
    m[AFH_MAP_BYTES - 1] &= 0x7F;           // channel 79 does not exist
    memcpy(map, m, AFH_MAP_BYTES);
    return true;
}

static void span_clear(uint8_t map[AFH_MAP_BYTES], uint8_t lo, uint8_t hi)
{
    for (int k = lo; k <= hi; k++) map[k / 8] &= (uint8_t)~(1u << (k % 8));
}

static bool span_all_off(const uint8_t *map, uint8_t lo, uint8_t hi)
{
    for (int k = lo; k <= hi; k++) {
        if ((map[k / 8] >> (k % 8)) & 1) return false;
    }
    return true;
}

static void text_add(char *excl, size_t cap, const char *item)
{
    if (!excl || !cap) return;
    size_t used = strlen(excl);
    if (used + 1 >= cap) return;
    snprintf(excl + used, cap - used, "%s%s", used ? "," : "", item);
}

int afh_build(const afh_report_t *r, bool scan_ok, const uint8_t *prev, uint8_t out[AFH_MAP_BYTES], char *excl,
              size_t excl_cap)
{
    uint8_t map[AFH_MAP_BYTES];
    uint8_t lo, hi;
    afh_map_all(map);
    if (excl && excl_cap) excl[0] = '\0';
    // The S3's own access point first, whatever its level: its traffic is next to the LyraT all the time. It always
    // fits: 40 MHz cover 42 channels of 79.
    if (afh_span(r->ap_primary, r->ap_second, (r->flags & AFH_FLAG_BW40) != 0, &lo, &hi)) {
        span_clear(map, lo, hi);
        char item[16];
        bool wide = r->ap_second && (r->flags & AFH_FLAG_BW40);
        snprintf(item, sizeof(item), "ap%d%s", r->ap_primary, !wide ? "" : r->ap_second == 1 ? "+" : "-");
        text_add(excl, excl_cap, item);
    }
    for (uint8_t i = 0; scan_ok && i < r->n && i < AFH_REPORT_NETS; i++) {
        const afh_net_t *w = &r->nets[i];
        if (!afh_span(w->primary, w->second, w->second != 0, &lo, &hi)) continue;
        bool held = prev && span_all_off(prev, lo, hi);
        if (w->rssi < (held ? AFH_RSSI_LEAVE : AFH_RSSI_ENTER)) continue;
        uint8_t trial[AFH_MAP_BYTES];
        memcpy(trial, map, sizeof(trial));
        span_clear(trial, lo, hi);
        if (afh_map_count(trial) < AFH_MIN_KEEP) continue;
        if (memcmp(trial, map, sizeof(map)) != 0) {
            char item[16];
            snprintf(item, sizeof(item), "%d%s:%d", w->primary, !w->second ? "" : w->second == 1 ? "+" : "-", w->rssi);
            text_add(excl, excl_cap, item);
        }
        memcpy(map, trial, sizeof(map));
    }
    memcpy(out, map, AFH_MAP_BYTES);
    return afh_map_count(map);
}

void afh_ctl_init(afh_ctl_t *c, afh_mode_t mode)
{
    memset(c, 0, sizeof(*c));
    c->mode = mode;
    afh_map_all(c->fixed);
    afh_map_all(c->applied);        // the controller's default: the host classifies nothing
    c->applied_src = AFH_SRC_NONE;
}

int afh_ctl_on_ack(afh_ctl_t *c, const uint8_t *ack, size_t len, uint32_t now_ms)
{
    afh_report_t r;
    int got = afh_report_parse(ack, len, &r);
    if (got == 1) {
        c->rep = r;
        c->rep_ms = now_ms;
        c->have = true;
        c->reports++;
    } else if (got < 0) {
        c->bad++;
    }
    return got;
}

afh_src_t afh_ctl_target(afh_ctl_t *c, uint32_t now_ms, uint8_t out[AFH_MAP_BYTES])
{
    c->excl[0] = '\0';
    if (c->mode == AFH_MODE_FIXED) {
        memcpy(out, c->fixed, AFH_MAP_BYTES);
        return AFH_SRC_CMD;
    }
    if (c->mode == AFH_MODE_ALL || !c->have || (uint32_t)(now_ms - c->rep_ms) > AFH_STALE_MS) {
        afh_map_all(out);
        return AFH_SRC_NONE;
    }
    uint32_t since_ms = (uint32_t)(now_ms - c->rep_ms);
    bool scan_ok = c->rep.scan_age_s != AFH_SCAN_NONE &&
                   (uint32_t)c->rep.scan_age_s + since_ms / 1000u <= AFH_SCAN_MAX_AGE_S;
    afh_build(&c->rep, scan_ok, c->applied, out, c->excl, sizeof(c->excl));
    return AFH_SRC_S3;
}

void afh_ctl_on_slc(afh_ctl_t *c)
{
    c->force = true;
    c->force_why = AFH_WHY_SLC;
}

void afh_ctl_set_mode(afh_ctl_t *c, afh_mode_t mode, const uint8_t *map)
{
    c->mode = mode;
    if (mode == AFH_MODE_FIXED && map) memcpy(c->fixed, map, AFH_MAP_BYTES);
    c->force = true;
    c->force_why = AFH_WHY_CMD;
}

bool afh_ctl_poll(afh_ctl_t *c, uint32_t now_ms, bool sco_active, uint8_t out[AFH_MAP_BYTES], afh_src_t *src,
                  afh_why_t *why)
{
    uint8_t want[AFH_MAP_BYTES];
    afh_src_t s = afh_ctl_target(c, now_ms, want);
    bool differs = memcmp(want, c->applied, AFH_MAP_BYTES) != 0;
    uint8_t all[AFH_MAP_BYTES];
    afh_map_all(all);
    // A forced give: a command always, a new link only with something to exclude (without an S3 nothing changes).
    bool forced = c->force && (c->force_why == AFH_WHY_CMD || memcmp(want, all, AFH_MAP_BYTES) != 0);
    if (c->force && !forced) c->force = false;
    if (!differs && !forced) {
        c->deferred = false;
        return false;
    }
    if (sco_active) {
        if (!c->deferred) c->deferrals++;
        c->deferred = true;
        return false;
    }
    if (forced) *why = c->force_why;
    else if (c->deferred) *why = AFH_WHY_SCO_END;
    else if (s == AFH_SRC_NONE && c->mode == AFH_MODE_AUTO && c->have) *why = AFH_WHY_STALE;
    else *why = AFH_WHY_S3;
    memcpy(out, want, AFH_MAP_BYTES);
    *src = s;
    return true;
}

void afh_ctl_applied(afh_ctl_t *c, const uint8_t map[AFH_MAP_BYTES], afh_src_t src)
{
    memcpy(c->applied, map, AFH_MAP_BYTES);
    c->applied_src = src;
    c->force = false;
    c->deferred = false;
    c->applies++;
}

const char *afh_mode_name(afh_mode_t m)
{
    return m == AFH_MODE_AUTO ? "auto" : m == AFH_MODE_FIXED ? "fixed" : "all";
}

const char *afh_src_name(afh_src_t s)
{
    return s == AFH_SRC_S3 ? "s3" : s == AFH_SRC_CMD ? "cmd" : "none";
}

const char *afh_why_name(afh_why_t w)
{
    static const char *names[] = {"s3", "slc", "sco_end", "stale", "cmd"};
    return (unsigned)w < sizeof(names) / sizeof(names[0]) ? names[w] : "?";
}
