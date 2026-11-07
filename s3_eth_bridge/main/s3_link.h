// What main.c (I2S, Ethernet, control UART) and the OTA and log modules give each other. See docs/OTA.md.
#pragma once

#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// --- main.c ---------------------------------------------------------------------------------------------------------

// One frame to the LyraT; false when the TX pin is not ours now (the LyraT is not proven alive) or the write failed.
bool s3_uart_send(uint8_t type, const void *payload, size_t len);
bool s3_uart_tx_on(void);
int s3_uart_get_baud(void);
int s3_uart_set_baud(int baud);
uint16_t s3_boot_id(void);

typedef struct {
    bool eth_ok, got_ip, i2s_call_ok, i2s_mic_ok, uart_ok;
    uint32_t call_frames, mic_frames;   // I2S frames since start
    uint32_t uart_rx_bytes, uart_frames;
} s3_health_t;
void s3_health(s3_health_t *h);

// --- s3_ota.c -------------------------------------------------------------------------------------------------------

void s3_ota_start(bool network);                            // self-test always; with the network UDP 5007 and the relay
void s3_ota_on_lyrat_reply(const uint8_t *payload, size_t len);   // a frame CTL_T_OTA_ACK came
const char *s3_ota_selftest_name(void);

// --- s3_dbglog.c ----------------------------------------------------------------------------------------------------

void s3_log_init(uint16_t boot_id);                         // first thing in app_main: from here every log line is kept
void s3_log_start(void);                                    // after the network: UDP 5008
void s3_log_relay(const uint8_t *payload, size_t len);      // a frame CTL_T_LOG came: the LyraT's records
size_t s3_log_lyrat_ctl(uint8_t out[2]);                    // payload of the heartbeat ack: on, level
void s3_log_line(const char *fmt, ...) __attribute__((format(printf, 1, 2)));   // console and ring, level I
uint32_t s3_log_relay_lost(void);
