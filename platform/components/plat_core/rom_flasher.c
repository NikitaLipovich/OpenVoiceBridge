// ESP32 ROM serial loader, host side. See rom_flasher.h; the protocol is the one esptool speaks to the ROM (no stub).
// Goal 095.
#include "rom_flasher.h"

#include <string.h>

#define SYNC_WAIT_MS        100
#define DRAIN_QUIET_MS      50
#define CMD_MS              3000
#define ERASE_MS_PER_MB     30000u      // esptool ERASE_REGION_TIMEOUT_PER_MB
#define MD5_MS_PER_MB       8000u       // esptool MD5_TIMEOUT_PER_MB
#define BAUD_SETTLE_MS      50
#define EN_LOW_MS           100
#define BOOT_HOLD_MS        250         // EN of a DevKitC rises through 10k x 1uF; the ROM reads IO0 on the way

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static const char *const s_err[] = {"ok", "sync", "chip", "timeout", "rom_status", "baud", "md5", "io", "bundle"};
static const char *const s_phase[] = {"idle", "reset", "sync", "baud", "erase", "write", "verify", "run", "done",
                                      "failed"};

const char *rom_err_name(int err) { return (unsigned)err < ROM_E_COUNT ? s_err[err] : "?"; }
const char *rom_phase_name(int phase) { return (unsigned)phase < ROM_PH_COUNT ? s_phase[phase] : "?"; }

void rom_init(rom_flasher_t *f, const rom_port_t *port)
{
    memset(f, 0, sizeof(*f));
    f->port = port;
    f->sync_tries = 10;
    f->sessions = 2;
    f->segment_tries = 3;
    f->baud = ROM_BAUD_DEFAULT;
}

static uint32_t per_mb(uint32_t ms_per_mb, uint32_t size)
{
    uint32_t t = (uint32_t)((uint64_t)ms_per_mb * size / 0x100000u);
    return t < CMD_MS ? CMD_MS : t;
}

// ---------------------------------------------------------------------------------------------------------------------
// Pins

void rom_enter_download(rom_flasher_t *f)
{
    const rom_port_t *p = f->port;
    p->pins(p->ctx, 0, 1);          // IO0 low first, then the reset
    p->pins(p->ctx, 1, 1);
    p->delay(p->ctx, EN_LOW_MS);
    p->pins(p->ctx, 0, 1);          // EN goes up while IO0 is held: download mode
    p->delay(p->ctx, BOOT_HOLD_MS);
    p->pins(p->ctx, 0, 0);
    p->flush_input(p->ctx);         // "waiting for download" and the boot text
    f->rx_pos = f->rx_len = 0;
    f->in_frame = 0;
}

void rom_reset_run(rom_flasher_t *f)
{
    const rom_port_t *p = f->port;
    p->pins(p->ctx, 0, 0);
    p->pins(p->ctx, 1, 0);
    p->delay(p->ctx, EN_LOW_MS);
    p->pins(p->ctx, 0, 0);
}

// ---------------------------------------------------------------------------------------------------------------------
// SLIP

static size_t slip_put(uint8_t *out, size_t o, const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        if (data[i] == 0xC0) {
            out[o++] = 0xDB;
            out[o++] = 0xDC;
        } else if (data[i] == 0xDB) {
            out[o++] = 0xDB;
            out[o++] = 0xDD;
        } else {
            out[o++] = data[i];
        }
    }
    return o;
}

static int slip_send(rom_flasher_t *f, const uint8_t hdr[8], const uint8_t *data, size_t len)
{
    size_t o = 0;
    f->slip[o++] = 0xC0;
    o = slip_put(f->slip, o, hdr, 8);
    if (len) o = slip_put(f->slip, o, data, len);
    f->slip[o++] = 0xC0;
    return f->port->write(f->port->ctx, f->slip, o) == 0 ? 0 : -1;
}

// One whole frame into f->frame; 0 when none came within timeout_ms. Bytes outside frames (boot text) are skipped.
static int slip_frame(rom_flasher_t *f, uint32_t timeout_ms)
{
    const rom_port_t *p = f->port;
    uint32_t start = p->now_ms(p->ctx);
    for (;;) {
        while (f->rx_pos < f->rx_len) {
            uint8_t b = f->rx[f->rx_pos++];
            if (b == 0xC0) {
                if (f->in_frame && f->frame_len) {
                    f->in_frame = 0;
                    if (!f->overflow) return 1;
                    continue;
                }
                f->in_frame = 1;            // start, or an empty frame between two ends
                f->frame_len = 0;
                f->overflow = 0;
                f->esc = 0;
                continue;
            }
            if (!f->in_frame) continue;
            if (f->esc) {
                f->esc = 0;
                b = b == 0xDC ? 0xC0 : b == 0xDD ? 0xDB : b;
            } else if (b == 0xDB) {
                f->esc = 1;
                continue;
            }
            if (f->frame_len < sizeof(f->frame)) f->frame[f->frame_len++] = b;
            else f->overflow = 1;
        }
        uint32_t spent = p->now_ms(p->ctx) - start;
        if (spent >= timeout_ms) return 0;
        f->rx_pos = 0;
        f->rx_len = p->read(p->ctx, f->rx, sizeof(f->rx), timeout_ms - spent);
        if (!f->rx_len && p->now_ms(p->ctx) - start >= timeout_ms) return 0;
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// Commands

int rom_command(rom_flasher_t *f, uint8_t op, const uint8_t *data, size_t len, uint32_t checksum, uint32_t timeout_ms,
                uint32_t *value, uint8_t *out, size_t *out_len)
{
    const rom_port_t *p = f->port;
    uint8_t hdr[8] = {0x00, op, (uint8_t)len, (uint8_t)(len >> 8)};
    put32(hdr + 4, checksum);
    if (slip_send(f, hdr, data, len) != 0) return ROM_E_IO;
    uint32_t start = p->now_ms(p->ctx);
    for (;;) {
        uint32_t spent = p->now_ms(p->ctx) - start;
        if (spent >= timeout_ms || !slip_frame(f, timeout_ms - spent)) return ROM_E_TIMEOUT;
        const uint8_t *r = f->frame;
        if (f->frame_len < 8 + ROM_STATUS_BYTES || r[0] != 0x01 || r[1] != op) continue;     // a late answer
        size_t n = (size_t)r[2] | ((size_t)r[3] << 8);
        if (n < ROM_STATUS_BYTES || 8 + n > f->frame_len) continue;
        if (value) *value = get32(r + 4);
        if (out && out_len) {
            size_t take = n - ROM_STATUS_BYTES;
            if (take > *out_len) take = *out_len;
            memcpy(out, r + 8, take);
            *out_len = take;
        }
        f->rom_status = r[8 + n - ROM_STATUS_BYTES];
        f->rom_error = r[8 + n - ROM_STATUS_BYTES + 1];
        return f->rom_status ? ROM_E_STATUS : ROM_OK;
    }
}

int rom_sync(rom_flasher_t *f)
{
    const rom_port_t *p = f->port;
    uint8_t d[36] = {0x07, 0x07, 0x12, 0x20};
    memset(d + 4, 0x55, 32);
    for (uint32_t i = 0; i < f->sync_tries; i++) {
        f->syncs++;
        p->flush_input(p->ctx);
        f->rx_pos = f->rx_len = 0;
        f->in_frame = 0;
        if (rom_command(f, ROM_OP_SYNC, d, sizeof(d), 0, SYNC_WAIT_MS, NULL, NULL, NULL) == ROM_OK) {
            while (slip_frame(f, DRAIN_QUIET_MS)) {
            }       // the ROM answers one SYNC up to eight times
            return ROM_OK;
        }
    }
    return ROM_E_SYNC;
}

int rom_read_reg(rom_flasher_t *f, uint32_t addr, uint32_t *value)
{
    uint8_t d[4];
    put32(d, addr);
    return rom_command(f, ROM_OP_READ_REG, d, sizeof(d), 0, CMD_MS, value, NULL, NULL);
}

static int hexval(uint8_t c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int rom_md5(rom_flasher_t *f, uint32_t offset, uint32_t size, uint8_t md5[16])
{
    uint8_t d[16] = {0};
    put32(d, offset);
    put32(d + 4, size);
    uint8_t out[32];
    size_t n = sizeof(out);
    int r = rom_command(f, ROM_OP_SPI_FLASH_MD5, d, sizeof(d), 0, per_mb(MD5_MS_PER_MB, size), NULL, out, &n);
    if (r) return r;
    if (n == 16) {                  // a stub answers raw bytes
        memcpy(md5, out, 16);
        return ROM_OK;
    }
    if (n != 32) return ROM_E_STATUS;
    for (int i = 0; i < 16; i++) {
        int hi = hexval(out[2 * i]), lo = hexval(out[2 * i + 1]);
        if (hi < 0 || lo < 0) return ROM_E_STATUS;
        md5[i] = (uint8_t)(hi << 4 | lo);
    }
    return ROM_OK;
}

static int change_baud(rom_flasher_t *f, uint32_t baud)
{
    const rom_port_t *p = f->port;
    uint8_t d[8];
    put32(d, baud);
    put32(d + 4, 0);                // 0: the ROM, which does not know its own baud
    int r = rom_command(f, ROM_OP_CHANGE_BAUD, d, sizeof(d), 0, CMD_MS, NULL, NULL, NULL);
    if (r) return r;
    if (p->set_baud(p->ctx, baud) != 0) return ROM_E_IO;
    f->baud = baud;
    p->delay(p->ctx, BAUD_SETTLE_MS);
    p->flush_input(p->ctx);
    f->rx_pos = f->rx_len = 0;
    f->in_frame = 0;
    uint32_t magic = 0;
    if (rom_read_reg(f, ROM_CHIP_MAGIC_REG, &magic) != ROM_OK || magic != f->magic) return ROM_E_BAUD;
    return ROM_OK;
}

static int attach_flash(rom_flasher_t *f, uint32_t flash_size)
{
    uint8_t att[8] = {0};           // hspi_arg 0 (default pins), is_legacy 0: the ROM wants 8 bytes
    int r = rom_command(f, ROM_OP_SPI_ATTACH, att, sizeof(att), 0, CMD_MS, NULL, NULL, NULL);
    if (r) return r;
    uint8_t prm[24];
    put32(prm, 0);                  // flash id
    put32(prm + 4, flash_size);
    put32(prm + 8, 0x10000);        // block
    put32(prm + 12, ROM_SECTOR);
    put32(prm + 16, 0x100);         // page
    put32(prm + 20, 0xFFFF);        // status mask
    return rom_command(f, ROM_OP_SPI_SET_PARAMS, prm, sizeof(prm), 0, CMD_MS, NULL, NULL, NULL);
}

static int flash_segment(rom_flasher_t *f, const rom_segment_t *s)
{
    const rom_port_t *p = f->port;
    uint32_t blocks = (s->size + ROM_BLOCK - 1) / ROM_BLOCK;
    uint8_t d[16];
    put32(d, s->size);              // the ROM erases this many bytes, whole sectors, before it answers
    put32(d + 4, blocks);
    put32(d + 8, ROM_BLOCK);
    put32(d + 12, s->offset);
    f->phase = ROM_PH_ERASE;
    int r = rom_command(f, ROM_OP_FLASH_BEGIN, d, sizeof(d), 0, per_mb(ERASE_MS_PER_MB, s->size), NULL, NULL, NULL);
    if (r) return r;
    f->phase = ROM_PH_WRITE;
    for (uint32_t seq = 0; seq < blocks; seq++) {
        uint32_t at = seq * ROM_BLOCK;
        uint32_t n = s->size - at < ROM_BLOCK ? s->size - at : ROM_BLOCK;
        uint8_t *blk = f->block + 16;
        if (p->read_bundle(p->ctx, s->data_at + at, blk, n) != 0) return ROM_E_IO;
        memset(blk + n, 0xFF, ROM_BLOCK - n);       // the last block is padded: erased flash reads 0xFF anyway
        uint32_t chk = 0xEF;
        for (uint32_t i = 0; i < ROM_BLOCK; i++) chk ^= blk[i];
        put32(f->block, ROM_BLOCK);
        put32(f->block + 4, seq);
        put32(f->block + 8, 0);
        put32(f->block + 12, 0);
        r = rom_command(f, ROM_OP_FLASH_DATA, f->block, 16 + ROM_BLOCK, chk, CMD_MS, NULL, NULL, NULL);
        if (r) return r;
        f->blocks++;
        f->done += n;
    }
    return ROM_OK;
}

// One session: from the reset to the MD5 of every segment.
static int session(rom_flasher_t *f, const rom_bundle_t *b, uint32_t baud_high)
{
    const rom_port_t *p = f->port;
    if (f->baud != ROM_BAUD_DEFAULT) {
        if (p->set_baud(p->ctx, ROM_BAUD_DEFAULT) != 0) return ROM_E_IO;
        f->baud = ROM_BAUD_DEFAULT;
    }
    f->phase = ROM_PH_RESET;
    rom_enter_download(f);
    f->phase = ROM_PH_SYNC;
    int r = rom_sync(f);
    if (r) return r;
    r = rom_read_reg(f, ROM_CHIP_MAGIC_REG, &f->magic);
    if (r) return r;
    if (f->magic != ROM_MAGIC_ESP32) return ROM_E_CHIP;
    if (baud_high && baud_high != ROM_BAUD_DEFAULT) {
        f->phase = ROM_PH_BAUD;
        r = change_baud(f, baud_high);
        if (r) return ROM_E_BAUD;
    }
    r = attach_flash(f, b->flash_size);
    if (r) return r;
    uint32_t done0 = 0;
    for (uint8_t i = 0; i < b->count; i++) {
        f->seg = i;
        for (uint32_t t = 0;; t++) {
            f->done = done0;
            r = flash_segment(f, &b->seg[i]);
            // A failure the ROM reported: the line works, erase and write the segment again. A timeout: the line or
            // the target is gone, which a new session from the reset handles.
            if (r != ROM_E_STATUS || t + 1 >= f->segment_tries) break;
            f->segment_restarts++;
        }
        if (r) return r;
        done0 += b->seg[i].size;
    }
    f->phase = ROM_PH_VERIFY;
    for (uint8_t i = 0; i < b->count; i++) {
        uint8_t md5[16];
        r = rom_md5(f, b->seg[i].offset, b->seg[i].size, md5);
        if (r) return r;
        if (memcmp(md5, b->seg[i].md5, 16) != 0) {
            f->bad_seg = i;
            return ROM_E_MD5;
        }
    }
    return ROM_OK;
}

int rom_flash_bundle(rom_flasher_t *f, const rom_bundle_t *b)
{
    const rom_port_t *p = f->port;
    uint32_t start = p->now_ms(p->ctx);
    f->err = ROM_OK;
    f->rom_status = f->rom_error = 0;
    f->bad_seg = 0xFF;
    f->done = f->seg = 0;
    f->total = 0;
    if (!b || !b->count) {
        f->err = ROM_E_BUNDLE;
        f->phase = ROM_PH_FAILED;
        return ROM_E_BUNDLE;
    }
    for (uint8_t i = 0; i < b->count; i++) f->total += b->seg[i].size;
    uint32_t baud_high = f->baud_high;
    int r = ROM_E_SYNC;
    for (uint32_t s = 0; s < f->sessions; s++) {
        if (s) f->session_restarts++;
        r = session(f, b, baud_high);
        if (r == ROM_OK || r == ROM_E_CHIP || r == ROM_E_IO) break;
        if (r == ROM_E_BAUD) baud_high = 0;     // the next session stays at 115200
    }
    f->phase = ROM_PH_RUN;
    rom_reset_run(f);
    if (f->baud != ROM_BAUD_DEFAULT && p->set_baud(p->ctx, ROM_BAUD_DEFAULT) == 0) f->baud = ROM_BAUD_DEFAULT;
    f->err = (uint8_t)r;
    f->ms = p->now_ms(p->ctx) - start;
    f->phase = r ? ROM_PH_FAILED : ROM_PH_DONE;
    return r;
}
