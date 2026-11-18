// eSCO answer of the board and the parameters the link settled on (goal 100).
#pragma once
#include <stdbool.h>
#include <stdint.h>

// pkt 0: answer as the stack does (default). Otherwise these packet types (HCI mask), max latency in ms and
// retransmission effort replace the stack's values in the answer to the next request of the phone.
void esco_ctl_set(uint16_t pkt, uint16_t latency_ms, uint8_t retrans_effort);
bool esco_ctl_get(uint16_t *pkt, uint16_t *latency_ms, uint8_t *retrans_effort);

typedef struct {
    int      valid;            // 1 after the first link since the start
    uint8_t  status, link_type, tx_interval, retrans_window, air_mode;
    uint16_t rx_len, tx_len;
    uint16_t asked_pkt, asked_lat;   // what the board answered with for this link
    uint8_t  asked_rtx;
    uint32_t links, changes;
} esco_link_t;

void esco_ctl_last(esco_link_t *out);
