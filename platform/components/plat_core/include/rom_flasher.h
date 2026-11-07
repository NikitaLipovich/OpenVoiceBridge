// Writing an ESP32's flash through its ROM serial loader, from U3 over the target's UART0 with EN and IO0 (goal 095,
// docs/PLATFORM_OTA.md). The emergency path: it brings up even a board whose bootloader or both app slots are broken.
// The normal path is ota_link over the control UART.
//
// Only commands of the ROM itself (no stub): SYNC, READ_REG, SPI_ATTACH, SPI_SET_PARAMS, CHANGE_BAUDRATE, FLASH_BEGIN,
// FLASH_DATA, SPI_FLASH_MD5. SLIP framing; request 0x00 op u16 len u32 checksum data; reply 0x01 op u16 len u32 value
// data, the last 4 bytes of data are the ROM's status (status, error, 0, 0).
//
// Plain C behind a port: the PC tests run this file against an emulator of the ROM, with lost and cut bytes.
// The calls block; on U3 they run in their own task.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "rom_bundle.h"

#define ROM_BAUD_DEFAULT     115200
#define ROM_BLOCK            0x400       // FLASH_WRITE_SIZE of the ROM loader
#define ROM_CHIP_MAGIC_REG   0x40001000u
#define ROM_MAGIC_ESP32      0x00F01D83u
#define ROM_STATUS_BYTES     4

enum {
    ROM_OP_FLASH_BEGIN = 0x02,
    ROM_OP_FLASH_DATA = 0x03,
    ROM_OP_FLASH_END = 0x04,
    ROM_OP_SYNC = 0x08,
    ROM_OP_READ_REG = 0x0A,
    ROM_OP_SPI_SET_PARAMS = 0x0B,
    ROM_OP_SPI_ATTACH = 0x0D,
    ROM_OP_CHANGE_BAUD = 0x0F,
    ROM_OP_SPI_FLASH_MD5 = 0x13,
};

typedef enum {
    ROM_OK = 0,
    ROM_E_SYNC,         // no answer to SYNC: not in download mode, wrong wires, no power
    ROM_E_CHIP,         // answered, but not the chip of the bundle
    ROM_E_TIMEOUT,      // a command got no answer in time
    ROM_E_STATUS,       // the ROM answered with a failure, see rom_error
    ROM_E_BAUD,         // nothing at the higher baud
    ROM_E_MD5,          // a segment reads back different
    ROM_E_IO,           // the port could not write, or the bundle could not be read
    ROM_E_BUNDLE,
    ROM_E_COUNT
} rom_err_t;

typedef enum {
    ROM_PH_IDLE = 0, ROM_PH_RESET, ROM_PH_SYNC, ROM_PH_BAUD, ROM_PH_ERASE, ROM_PH_WRITE, ROM_PH_VERIFY, ROM_PH_RUN,
    ROM_PH_DONE, ROM_PH_FAILED, ROM_PH_COUNT
} rom_phase_t;

typedef struct {
    void *ctx;
    int (*write)(void *ctx, const uint8_t *data, size_t len);                 // 0 ok
    size_t (*read)(void *ctx, uint8_t *buf, size_t cap, uint32_t timeout_ms);  // bytes, 0 when none came in time
    void (*flush_input)(void *ctx);
    int (*set_baud)(void *ctx, uint32_t baud);                                // 0 ok
    void (*pins)(void *ctx, int en_low, int boot_low);   // 1 pulls the line to 0, 0 lets it go (open drain)
    void (*delay)(void *ctx, uint32_t ms);
    uint32_t (*now_ms)(void *ctx);
    int (*read_bundle)(void *ctx, uint32_t at, uint8_t *buf, size_t len);     // 0 ok
} rom_port_t;

typedef struct {
    // Settings, defaults from rom_init()
    uint32_t baud_high;         // 0: stay at 115200
    uint32_t sync_tries;        // per session
    uint32_t sessions;          // whole tries from the reset
    uint32_t segment_tries;
    const rom_port_t *port;
    // Progress, readable from another task (words only)
    volatile uint32_t phase, seg, done, total;
    // Result
    uint8_t err, rom_status, rom_error, bad_seg;
    uint32_t magic, baud;
    uint32_t syncs, session_restarts, segment_restarts, blocks, ms;
    // Internal
    uint8_t slip[2 * (8 + 16 + ROM_BLOCK) + 2];
    uint8_t frame[64];
    size_t frame_len;
    uint8_t esc, in_frame, overflow;
    uint8_t rx[64];
    size_t rx_pos, rx_len;
    uint8_t block[16 + ROM_BLOCK];
} rom_flasher_t;

const char *rom_err_name(int err);
const char *rom_phase_name(int phase);

void rom_init(rom_flasher_t *f, const rom_port_t *port);

// The whole job: download mode, sync, chip, baud, every segment of the bundle, MD5 of each, and a normal reset at the
// end whatever the result (the target is never left held in download mode). Returns rom_err_t, details in f.
int rom_flash_bundle(rom_flasher_t *f, const rom_bundle_t *b);

// Pieces, for the tests and for a console command.
void rom_enter_download(rom_flasher_t *f);
void rom_reset_run(rom_flasher_t *f);
int rom_sync(rom_flasher_t *f);
int rom_command(rom_flasher_t *f, uint8_t op, const uint8_t *data, size_t len, uint32_t checksum, uint32_t timeout_ms,
                uint32_t *value, uint8_t *out, size_t *out_len);
int rom_read_reg(rom_flasher_t *f, uint32_t addr, uint32_t *value);
int rom_md5(rom_flasher_t *f, uint32_t offset, uint32_t size, uint8_t md5[16]);
