// The link of an ESP32 node (U1, U2) to the relay U3 over its control UART (goal 095, docs/PLATFORM_OTA.md):
// heartbeat, console commands, new firmware with rollback (ota_link), the log ring drained in frames, and the frame type
// reserved for the modes of R21. Based on lyrat_ota.c, lyrat_dbglog.c and the control UART task of lyrat_resend/main.c
// (goal 090): the same protocol, the hooks of the call rules are the app's.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint8_t board;                  // DBG_BOARD_U1 / DBG_BOARD_U2
    const char *project;            // PLAT_PROJECT_U1 / U2: images of another project are refused
    int uart, pin_tx, pin_rx;
    // Hooks of the app, NULL: not there yet (the skeleton has no Bluetooth)
    bool (*link_up)(void);          // SLC to the phone (U1) or to the headphones (U2): BEGIN refused without FORCE
    bool (*sco_active)(void);       // audio on: writes pause, no restart
    bool (*app_ready)(void);        // the app's own part of the self-test of a new image
    uint8_t (*hb_flags)(void);      // ctl_hb_t.flags
    // Extra console commands: writes the answer, returns false when the command is not known
    bool (*command)(const char *line, char *out, size_t cap);
} node_cfg_t;

// First thing in app_main: from here every log line also waits in the ring for the PC.
void node_log_init(uint8_t board, uint16_t boot_id);
// After the app's own start: control UART, OTA receiver, self-test of a new image.
void node_start(const node_cfg_t *cfg);

uint32_t node_relay_acks(void);     // heartbeat acks of U3 since start
size_t node_status(char *out, size_t cap);
