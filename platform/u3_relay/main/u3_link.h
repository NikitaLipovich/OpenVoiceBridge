// What the files of U3 give each other (goal 095, docs/PLATFORM_OTA.md). Based on s3_eth_bridge/main/s3_link.h.
#pragma once

#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "rom_flasher.h"

#define U3_NODES 2      // node 0 = U1 (C1, UART1), node 1 = U2 (C2, UART2)

// --- main.c: Wi-Fi, control links ------------------------------------------------------------------------------------

bool u3_node_send(int node, uint8_t type, const void *payload, size_t len);   // false: TX not ours or write failed
bool u3_node_tx_on(int node);
bool u3_node_alive(int node);           // a valid frame within PLAT_SILENT_MS
bool u3_node_ever_alive(int node);
int u3_node_get_baud(int node);
int u3_node_set_baud(int node, int baud);
void u3_node_hold(int node, bool hold); // while the node is in ROM download mode: TX off, nothing sent
size_t u3_node_json(int node, char *out, size_t cap);
uint16_t u3_boot_id(void);
const char *u3_node_name(int node);     // "u1", "u2"

typedef struct {
    bool wifi_started, got_ip, uart_ok[U3_NODES];
    uint32_t rx_bytes[U3_NODES], frames[U3_NODES];
    char ip[16];
    int rssi;
} u3_health_t;
void u3_health(u3_health_t *h);

// --- u3_port.c: UART0, the console / ROM loader port of U1 and U2 ----------------------------------------------------

enum { U3_CON_AUTO = 0, U3_CON_U1, U3_CON_U2, U3_CON_OFF };
void u3_port_init(void);
void u3_port_set_mode(int mode);
int u3_port_mode(void);
int u3_port_listening(void);            // node the console reads now, -1 none
const char *u3_port_mode_name(int mode);
// The port for the ROM loader of a node: the console stops, TX goes onto the node's RX0, EN and IO0 belong to the
// flasher. read_bundle comes from the caller. Release puts TX back to input and the console back on.
const rom_port_t *u3_port_rom_take(int node, int (*read_bundle)(void *ctx, uint32_t at, uint8_t *buf, size_t len),
                                   void *bundle_ctx);
void u3_port_rom_release(void);
uint32_t u3_port_con_lines(int node);

// --- u3_ota.c --------------------------------------------------------------------------------------------------------

void u3_ota_start(bool network);
void u3_ota_on_reply(int node, const uint8_t *payload, size_t len);   // CTL_T_OTA_ACK from a node
const char *u3_ota_selftest_name(void);
bool u3_ota_rom_running(int node);

// --- u3_log.c --------------------------------------------------------------------------------------------------------

void u3_log_init(uint16_t boot_id);
void u3_log_start(void);
void u3_log_relay(int node, const uint8_t *payload, size_t len);      // CTL_T_LOG: the node's records
size_t u3_log_node_ctl(int node, uint8_t out[2]);                     // payload of a heartbeat ack: on, level
void u3_log_node_frame(int node, uint8_t type, const uint8_t *payload, size_t len);  // CTL_T_REPLY, CTL_T_MODE_ACK
void u3_log_console(int node, const char *text, size_t len);          // a line of a node's UART0 console
void u3_log_line(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
