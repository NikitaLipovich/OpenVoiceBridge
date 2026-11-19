// The rules of the two modes, phone and PC (goal 102): see hf_mode.h. No platform call here: the PC tests build it.
#include "hf_mode.h"

#include <stdio.h>
#include <string.h>

static int addr_eq(const uint8_t *a, const uint8_t *b)
{
    return a && b && memcmp(a, b, HFM_ADDR_LEN) == 0;
}

static int in_slot(const hfm_t *m, int mode, const uint8_t *bda)
{
    return m->have[mode] && addr_eq(m->peer[mode], bda);
}

static void slot_set(hfm_t *m, int mode, const uint8_t *bda)
{
    memcpy(m->peer[mode], bda, HFM_ADDR_LEN);
    m->have[mode] = 1;
    // One AG, one slot: the same address in the other slot would make it both.
    if (in_slot(m, 1 - mode, bda)) m->have[1 - mode] = 0;
    m->dirty = 1;
}

void hfm_init(hfm_t *m, int nvs_mode, int default_mode, const uint8_t *phone, const uint8_t *pc, const uint8_t *mru0)
{
    memset(m, 0, sizeof(*m));
    if (nvs_mode == HFM_PHONE || nvs_mode == HFM_PC) {
        m->mode = (uint8_t)nvs_mode;
        m->src = HFM_SRC_NVS;
    } else {
        m->mode = default_mode == HFM_PC ? HFM_PC : HFM_PHONE;
        m->src = HFM_SRC_BUILD;
    }
    if (phone) {
        memcpy(m->peer[HFM_PHONE], phone, HFM_ADDR_LEN);
        m->have[HFM_PHONE] = 1;
    }
    if (pc && !addr_eq(pc, phone)) {
        memcpy(m->peer[HFM_PC], pc, HFM_ADDR_LEN);
        m->have[HFM_PC] = 1;
    }
    // First start after phone-bringup: the last AG of the old list was the phone for weeks of bench work.
    if (nvs_mode < 0 && !phone && !pc && mru0) {
        memcpy(m->peer[HFM_PHONE], mru0, HFM_ADDR_LEN);
        m->have[HFM_PHONE] = 1;
        m->migrated = 1;
        m->dirty = 1;
    }
}

void hfm_on_paired(hfm_t *m, const uint8_t *bda)
{
    memcpy(m->paired, bda, HFM_ADDR_LEN);
    m->have_paired = 1;
}

hfm_slc_t hfm_on_slc(hfm_t *m, const uint8_t *bda)
{
    int mode = m->mode, other = 1 - m->mode;
    if (in_slot(m, mode, bda)) return HFM_KEEP;
    if (m->have_paired && addr_eq(m->paired, bda)) {
        m->have_paired = 0;
        slot_set(m, mode, bda);
        return HFM_ADOPT;
    }
    if (in_slot(m, other, bda)) return HFM_DROP_OTHER;
    if (!m->have[mode]) {
        slot_set(m, mode, bda);
        return HFM_ADOPT;
    }
    return HFM_DROP_UNKNOWN;
}

int hfm_next_target(hfm_t *m, const uint8_t (*bonds)[HFM_ADDR_LEN], int nbonds, uint8_t out[HFM_ADDR_LEN])
{
    int mode = m->mode, other = 1 - m->mode;
    if (m->have[mode]) {
        memcpy(out, m->peer[mode], HFM_ADDR_LEN);
        return 1;
    }
    int n = 0;
    for (int i = 0; i < nbonds; i++) {
        if (!in_slot(m, other, bonds[i])) n++;
    }
    if (n == 0) return 0;
    int pick = (int)(m->turn++ % (uint32_t)n);
    for (int i = 0; i < nbonds; i++) {
        if (in_slot(m, other, bonds[i])) continue;
        if (pick-- == 0) {
            memcpy(out, bonds[i], HFM_ADDR_LEN);
            return 1;
        }
    }
    return 0;
}

const char *hfm_addr_short(const uint8_t *bda, char *buf, size_t cap)
{
    if (!bda) {
        snprintf(buf, cap, "none");
    } else {
        snprintf(buf, cap, "..:%02x:%02x", bda[4], bda[5]);
    }
    return buf;
}

const char *hfm_role(const hfm_t *m, const uint8_t *bda)
{
    if (in_slot(m, HFM_PHONE, bda)) return "phone";
    if (in_slot(m, HFM_PC, bda)) return "pc";
    return "unknown";
}

const char *hfm_mode_name(int mode)
{
    return mode == HFM_PC ? "pc" : (mode == HFM_PHONE ? "phone" : "?");
}

const char *hfm_slc_name(hfm_slc_t r)
{
    switch (r) {
    case HFM_KEEP: return "keep";
    case HFM_ADOPT: return "adopt";
    case HFM_DROP_OTHER: return "drop_other";
    case HFM_DROP_UNKNOWN: return "drop_unknown";
    }
    return "?";
}

static int mode_of(const char *word)
{
    if (strcmp(word, "phone") == 0) return HFM_PHONE;
    if (strcmp(word, "pc") == 0) return HFM_PC;
    return -1;
}

void hfm_forget_all(hfm_t *m)
{
    m->have[HFM_PHONE] = m->have[HFM_PC] = 0;
    m->have_paired = 0;
    m->dirty = 1;
}

int hfm_command(hfm_t *m, const char *args, int sco_on, const uint8_t *slc_bda, char *out, size_t cap)
{
    char w1[12] = "", w2[12] = "", w3[4] = "";
    int words = sscanf(args ? args : "", "%11s %11s %3s", w1, w2, w3);
    char a[16], b[16], c[16];
    if (words <= 0) {
        snprintf(out, cap, "mode=%s src=%s phone=%s pc=%s link=%s%s%s", hfm_mode_name(m->mode),
                 m->src == HFM_SRC_NVS ? "nvs" : "build",
                 hfm_addr_short(m->have[HFM_PHONE] ? m->peer[HFM_PHONE] : NULL, a, sizeof(a)),
                 hfm_addr_short(m->have[HFM_PC] ? m->peer[HFM_PC] : NULL, b, sizeof(b)),
                 slc_bda ? hfm_role(m, slc_bda) : "down", slc_bda ? " " : "",
                 slc_bda ? hfm_addr_short(slc_bda, c, sizeof(c)) : "");
        return 0;
    }
    if (strcmp(w1, "forget") == 0 && words == 2 && mode_of(w2) >= 0) {
        int which = mode_of(w2);
        snprintf(out, cap, "mode forget %s: slot was %s", w2,
                 hfm_addr_short(m->have[which] ? m->peer[which] : NULL, a, sizeof(a)));
        m->have[which] = 0;
        m->dirty = 1;
        return HFM_DO_SAVE;
    }
    int want = mode_of(w1);
    int force = words == 2 && strcmp(w2, "1") == 0;
    if (want < 0 || words > 2 || (words == 2 && !force)) {
        snprintf(out, cap, "mode refused: bad request (mode | mode phone|pc [1] | mode forget phone|pc)");
        return 0;
    }
    if (want == m->mode) {
        snprintf(out, cap, "mode %s unchanged", hfm_mode_name(want));
        return 0;
    }
    if (sco_on && !force) {
        snprintf(out, cap, "mode refused: SCO active, 'mode %s 1' forces", hfm_mode_name(want));
        return 0;
    }
    int was = m->mode;
    m->mode = (uint8_t)want;
    m->src = HFM_SRC_NVS;
    m->dirty = 1;
    int act = HFM_DO_SAVE | HFM_DO_KICK;
    if (slc_bda && !in_slot(m, want, slc_bda)) act |= HFM_DO_DROP;
    snprintf(out, cap, "mode %s saved, was %s%s%s", hfm_mode_name(want), hfm_mode_name(was),
             (act & HFM_DO_DROP) ? ", dropping the link to " : "",
             (act & HFM_DO_DROP) ? hfm_role(m, slc_bda) : "");
    return act;
}
