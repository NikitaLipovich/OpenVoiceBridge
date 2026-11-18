// Host channel map of Bluetooth AFH from what the S3 sees of 2.4 GHz Wi-Fi (goal 106). Pure C, no ESP-IDF: built into
// the LyraT (map and policy), the S3 (report encoding, scan reduction) and the PC tests (otahost.dll).
//
// The S3 adds a report to the payload of every heartbeat ack (docs/S3_UART.md), after the two log bytes:
//   [2] version 1  [3] gen (+1 on each new scan or access point change)  [4] AP primary channel (0 none)
//   [5] AP secondary (0 none, 1 above, 2 below)  [6] AP rssi (i8)  [7] flags (bit 0 associated, bit 1 own bandwidth 40)
//   [8..9] u16 LE age of the last scan in s (0xFFFF none yet)  [10] 0  [11] n (<= AFH_REPORT_NETS)
//   then n x {primary, secondary, rssi i8}, strongest first
// An ack of 0 or 2 bytes (an S3 before this goal) carries no report: the LyraT then behaves as without an S3.
//
// The map (bit k = Bluetooth channel k at 2402 + k MHz usable, byte 0 = channels 0..7, 79 channels) excludes the band
// of the S3's own access point first, then the strongest networks of the scan, as long as AFH_MIN_KEEP channels stay.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define AFH_BT_CHANNELS     79
#define AFH_MAP_BYTES       10
#define AFH_MIN_KEEP        30      // the spec needs 20; 10 more leave the controller's own assessment room
#define AFH_RSSI_ENTER      (-70)   // a scanned network counts from this level...
#define AFH_RSSI_LEAVE      (-76)   // ...and, once excluded, until it falls below this one
#define AFH_SCAN_MAX_AGE_S  3600    // older scans are not used; the access point's channel always is
#define AFH_STALE_MS        (30u * 60u * 1000u)  // no report for this long: back to all channels
#define AFH_REPORT_VER      1
#define AFH_REPORT_NETS     8
#define AFH_REPORT_HEAD     12      // bytes of the ack up to n, the two log bytes included
#define AFH_REPORT_MAX      (AFH_REPORT_HEAD + 3 * AFH_REPORT_NETS)
#define AFH_SCAN_NONE       0xFFFF
#define AFH_FLAG_ASSOC      0x01
#define AFH_FLAG_BW40       0x02
#define AFH_TEXT_MAX        96

typedef struct {
    uint8_t primary;        // 1..14
    uint8_t second;         // 0 none, 1 above, 2 below
    int8_t rssi;
} afh_net_t;

typedef struct {
    uint8_t gen;
    uint8_t ap_primary, ap_second;
    int8_t ap_rssi;
    uint8_t flags;
    uint16_t scan_age_s;
    uint8_t n;
    afh_net_t nets[AFH_REPORT_NETS];
} afh_report_t;

typedef enum { AFH_MODE_ALL = 0, AFH_MODE_AUTO = 1, AFH_MODE_FIXED = 2 } afh_mode_t;
typedef enum { AFH_SRC_NONE = 0, AFH_SRC_S3 = 1, AFH_SRC_CMD = 2 } afh_src_t;
typedef enum { AFH_WHY_S3 = 0, AFH_WHY_SLC, AFH_WHY_SCO_END, AFH_WHY_STALE, AFH_WHY_CMD } afh_why_t;

// The bytes of a heartbeat ack: 1 a report, 0 none (an older S3), -1 a broken one (ignored, counted by the caller).
int afh_report_parse(const uint8_t *ack, size_t len, afh_report_t *out);
// The report bytes from offset 2 of the ack on (the caller puts the two log bytes first); returns their count.
size_t afh_report_encode(const afh_report_t *r, uint8_t *out, size_t cap);
// Scan records to at most max networks: one per (primary, secondary) with the strongest rssi, strongest first.
size_t afh_scan_reduce(const afh_net_t *in, size_t n_in, afh_net_t *out, size_t max);

// Bluetooth channels lo..hi covered by a Wi-Fi channel; false when none is (a channel out of 1..14).
bool afh_span(uint8_t primary, uint8_t second, bool wide, uint8_t *lo, uint8_t *hi);
void afh_map_all(uint8_t map[AFH_MAP_BYTES]);
int afh_map_count(const uint8_t map[AFH_MAP_BYTES]);
void afh_map_hex(const uint8_t map[AFH_MAP_BYTES], char out[2 * AFH_MAP_BYTES + 1]);
bool afh_map_from_hex(const char *text, uint8_t map[AFH_MAP_BYTES]);
// The map for a report. prev: the map in force, for the hysteresis (NULL: none). scan_ok: the scanned networks may
// be used. excl: text of what was excluded ("ap1,11:-49"), may be NULL. Returns the usable channels.
int afh_build(const afh_report_t *r, bool scan_ok, const uint8_t *prev, uint8_t out[AFH_MAP_BYTES], char *excl,
              size_t excl_cap);

// The LyraT's policy: which map, and when to give it to the controller.
typedef struct {
    afh_mode_t mode;
    uint8_t fixed[AFH_MAP_BYTES];
    bool have;                      // a report came since the start
    afh_report_t rep;
    uint32_t rep_ms;                // when it came
    uint8_t applied[AFH_MAP_BYTES]; // what the controller holds (all at the start)
    afh_src_t applied_src;
    bool force;                     // give the map again even if it is the same (link up, command)
    afh_why_t force_why;
    bool deferred;                  // a change waited for the end of the headset channel
    uint32_t reports, bad, applies, deferrals;
    char excl[AFH_TEXT_MAX];        // of the target last computed
} afh_ctl_t;

void afh_ctl_init(afh_ctl_t *c, afh_mode_t mode);
// An ack of the S3: keeps a valid report. Returns afh_report_parse()'s result.
int afh_ctl_on_ack(afh_ctl_t *c, const uint8_t *ack, size_t len, uint32_t now_ms);
// The map the policy wants now and its source.
afh_src_t afh_ctl_target(afh_ctl_t *c, uint32_t now_ms, uint8_t out[AFH_MAP_BYTES]);
// The link came up: the next poll gives the map again (when it is not all channels).
void afh_ctl_on_slc(afh_ctl_t *c);
// Set the mode; FIXED takes map. The next poll gives the map.
void afh_ctl_set_mode(afh_ctl_t *c, afh_mode_t mode, const uint8_t *map);
// true: give out to the controller now (then afh_ctl_applied); never while sco_active.
bool afh_ctl_poll(afh_ctl_t *c, uint32_t now_ms, bool sco_active, uint8_t out[AFH_MAP_BYTES], afh_src_t *src,
                  afh_why_t *why);
void afh_ctl_applied(afh_ctl_t *c, const uint8_t map[AFH_MAP_BYTES], afh_src_t src);

const char *afh_mode_name(afh_mode_t m);
const char *afh_src_name(afh_src_t s);
const char *afh_why_name(afh_why_t w);
