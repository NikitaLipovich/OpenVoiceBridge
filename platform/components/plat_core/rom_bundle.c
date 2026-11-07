// ROM bundle header check, see rom_bundle.h. Goal 095.
#include "rom_bundle.h"

#include <string.h>

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static const char *const s_names[] = {"ok", "short", "magic", "version", "chip", "count", "flash_size", "align",
                                      "range", "overlap", "protected", "size"};

const char *rom_bundle_err_name(int err)
{
    return (unsigned)err < sizeof(s_names) / sizeof(s_names[0]) ? s_names[err] : "?";
}

int rom_bundle_parse(const uint8_t *head, size_t len, uint32_t size, uint8_t chip, uint32_t protect_from,
                     uint32_t protect_to, rom_bundle_t *out)
{
    memset(out, 0, sizeof(*out));
    if (len < ROM_BUNDLE_HEAD) return ROM_BUNDLE_SHORT;
    if (memcmp(head, ROM_BUNDLE_MAGIC, 4) != 0) return ROM_BUNDLE_MAGIC_BAD;
    if (head[4] != ROM_BUNDLE_VERSION) return ROM_BUNDLE_VERSION_BAD;
    if (head[5] != chip) return ROM_BUNDLE_CHIP_BAD;
    uint8_t count = head[6];
    if (count == 0 || count > ROM_BUNDLE_MAX_SEG) return ROM_BUNDLE_COUNT_BAD;
    uint32_t flash = get32(head + 8);
    if (flash < 0x100000u || flash > 0x1000000u || (flash & (flash - 1)) != 0) return ROM_BUNDLE_FLASH_BAD;
    uint32_t total = get32(head + 12);
    uint64_t at = ROM_BUNDLE_HEAD;
    uint64_t end_prev = 0;
    for (uint8_t i = 0; i < count; i++) {
        const uint8_t *e = head + 16 + 24 * i;
        rom_segment_t *s = &out->seg[i];
        s->offset = get32(e);
        s->size = get32(e + 4);
        memcpy(s->md5, e + 8, 16);
        s->data_at = (uint32_t)at;
        if (s->size == 0 || s->offset % ROM_SECTOR) return ROM_BUNDLE_ALIGN;
        uint64_t end = (uint64_t)s->offset + s->size;
        if (s->offset < 0x1000u || end > flash) return ROM_BUNDLE_RANGE;
        if (s->offset < end_prev) return ROM_BUNDLE_OVERLAP;
        // The ROM erases whole sectors: the rounded end of one segment must not reach into the next.
        end_prev = (end + ROM_SECTOR - 1) / ROM_SECTOR * ROM_SECTOR;
        if (protect_to > protect_from && s->offset < protect_to && end_prev > protect_from) return ROM_BUNDLE_PROTECTED;
        at += s->size;
    }
    if (at != total || (size && size != total)) return ROM_BUNDLE_SIZE;
    out->chip = chip;
    out->count = count;
    out->flash_size = flash;
    out->total = total;
    return ROM_BUNDLE_OK;
}
