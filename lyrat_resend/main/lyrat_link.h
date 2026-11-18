// What main.c (Bluetooth, sound, control UART) and the OTA and log modules of the LyraT give each other. docs/OTA.md.
// Shared by the headset firmware (main.c, no Wi-Fi at all, R13) and the updater in factory (updater/main/updater_main.c,
// Wi-Fi, no Bluetooth), goal 096: each of them implements the "main" part below.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "ota_link.h"

// --- main.c ---------------------------------------------------------------------------------------------------------

bool lyrat_sco_active(void);
bool lyrat_slc_up(void);
bool lyrat_bt_ready(void);              // bt_init() returned
uint32_t lyrat_s3_acks(void);           // heartbeat acks from the S3 since start: the S3 hears us and drives the line
uint32_t lyrat_ctl_frames(void);        // valid frames of any type from the S3 since start
void lyrat_ctl_send(uint8_t type, const void *payload, size_t len);
int lyrat_ctl_get_baud(void);
int lyrat_ctl_set_baud(int baud);
void lyrat_ctl_wait_tx(void);
size_t lyrat_mode_text(char *out, size_t cap);  // end of the status line: "mode=app wifi=none", or the updater's state
int lyrat_net_rx_busy(void);                    // the updater's network receiver has a session (headset firmware: 0)
int lyrat_inv_elf(uint8_t out[8]);              // 1: report this ELF as inv= (the updater: a bad firmware), 0: default

// --- lyrat_ota.c ----------------------------------------------------------------------------------------------------

void lyrat_ota_init(void);                                      // after bt_init: receiver task, self-test of a new image
void lyrat_ota_request(const uint8_t *payload, size_t len);     // a frame CTL_T_OTA came; never blocks
size_t lyrat_ota_status(char *out, size_t cap);                 // one line for the console command "ota"
void lyrat_ota_set_app(void);                                   // headset firmware: no receiver, self-test at every start
#define LYRAT_OTA_NET_CTX ((void *)1)                          // ctx of the updater's network receiver
void lyrat_ota_port_copy(ota_rx_port_t *out);                   // the updater: the flash port for its network receiver
int lyrat_ota_uart_busy(void);                                  // the UART receiver has a session
const ota_rx_t *lyrat_ota_rx(void);                             // the UART receiver, for the updater's window
const char *lyrat_ota_token(void);                              // the updater only (lyrat_token.c): "" without the file
int lyrat_ota_boot_to_updater(void);                            // headset firmware, early: the next start goes to factory

// --- lyrat_dbglog.c -------------------------------------------------------------------------------------------------

void lyrat_log_init(uint16_t boot_id);                          // first thing in app_main
void lyrat_log_on_ack(const uint8_t *payload, size_t len);      // heartbeat ack of the S3: on, level
void lyrat_log_drain(void);                                     // from the control UART task: frames CTL_T_LOG
size_t lyrat_log_status(char *out, size_t cap);
