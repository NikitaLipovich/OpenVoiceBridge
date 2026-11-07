// PC build of the platform's ROM flasher and bundle check (build_host.cmd), opaque objects for ctypes. Goal 095.
#include <stdlib.h>
#include <string.h>
#include "rom_bundle.h"
#include "rom_flasher.h"

rom_flasher_t *host_rom_new(const rom_port_t *port)
{
    rom_flasher_t *f = (rom_flasher_t *)calloc(1, sizeof(rom_flasher_t));
    rom_init(f, port);
    return f;
}

// baud_high, sync_tries, sessions, segment_tries
void host_rom_set(rom_flasher_t *f, const uint32_t *v)
{
    f->baud_high = v[0]; f->sync_tries = v[1]; f->sessions = v[2]; f->segment_tries = v[3];
}

// phase, seg, done, total, err, rom_status, rom_error, bad_seg, magic, baud, syncs, session_restarts, segment_restarts,
// blocks, ms
void host_rom_info(const rom_flasher_t *f, uint32_t *v)
{
    v[0] = f->phase; v[1] = f->seg; v[2] = f->done; v[3] = f->total; v[4] = f->err; v[5] = f->rom_status;
    v[6] = f->rom_error; v[7] = f->bad_seg; v[8] = f->magic; v[9] = f->baud; v[10] = f->syncs;
    v[11] = f->session_restarts; v[12] = f->segment_restarts; v[13] = f->blocks; v[14] = f->ms;
}

int host_rom_flash(rom_flasher_t *f, const rom_bundle_t *b) { return rom_flash_bundle(f, b); }
int host_rom_sync(rom_flasher_t *f) { return rom_sync(f); }
int host_rom_md5(rom_flasher_t *f, uint32_t offset, uint32_t size, uint8_t *md5) { return rom_md5(f, offset, size, md5); }
void host_rom_enter(rom_flasher_t *f) { rom_enter_download(f); }
void host_rom_reset(rom_flasher_t *f) { rom_reset_run(f); }

rom_bundle_t *host_bundle_new(void) { return (rom_bundle_t *)calloc(1, sizeof(rom_bundle_t)); }

// chip, count, flash_size, total, then per segment offset, size, data_at
void host_bundle_info(const rom_bundle_t *b, uint32_t *v)
{
    v[0] = b->chip; v[1] = b->count; v[2] = b->flash_size; v[3] = b->total;
    for (int i = 0; i < b->count; i++) {
        v[4 + 3 * i] = b->seg[i].offset; v[5 + 3 * i] = b->seg[i].size; v[6 + 3 * i] = b->seg[i].data_at;
    }
}

void host_free(void *p) { free(p); }
