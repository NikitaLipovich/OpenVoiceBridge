// What main.c (Bluetooth, sound, control UART) and the OTA and log modules of the LyraT give each other. docs/OTA.md.
// No Wi-Fi here (R13): the image and the log go through the S3 over the control UART.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

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

// --- lyrat_ota.c ----------------------------------------------------------------------------------------------------

void lyrat_ota_init(void);                                      // after bt_init: receiver task, self-test of a new image
void lyrat_ota_request(const uint8_t *payload, size_t len);     // a frame CTL_T_OTA came; never blocks
size_t lyrat_ota_status(char *out, size_t cap);                 // one line for the console command "ota"

// --- lyrat_dbglog.c -------------------------------------------------------------------------------------------------

void lyrat_log_init(uint16_t boot_id);                          // first thing in app_main
void lyrat_log_on_ack(const uint8_t *payload, size_t len);      // heartbeat ack of the S3: on, level
void lyrat_log_drain(void);                                     // from the control UART task: frames CTL_T_LOG
size_t lyrat_log_status(char *out, size_t cap);
