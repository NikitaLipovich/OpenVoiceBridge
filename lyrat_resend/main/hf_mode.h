// Two modes of the headset, phone and PC (goal 102, docs/DUAL_MODE.md): the rules, without platform calls.
//
// Bluedroid's HF client holds one link at a time (one control block), so the headset serves one audio gateway (AG) at a
// time: the mode says which one it calls and whose link it keeps. Each mode has a slot with the address of its AG. An
// AG gets the slot of the mode it first came up in (its class of device is not given by the stack for a bonded peer):
//   - SLC from the AG of this mode: keep;
//   - SLC from the AG of the other mode: drop, the headset belongs to the AG of its mode;
//   - SLC from an AG paired in this boot: keep, it takes the slot (a new pairing replaces the old one of this mode);
//   - SLC from any AG while the slot of this mode is empty: keep, it takes the slot;
//   - anything else (an unknown AG while the slot has one): drop.
// Whom to call while no link is up: the AG of the slot; with an empty slot every bonded AG except the other mode's.
// The console command "mode" shows and switches the mode; a switch during SCO is refused unless forced.
// main.c glues this to the stack, NVS and the reconnect task; the PC tests build it too (otahost.dll, tests/hfp_sim.py).
#pragma once

#include <stddef.h>
#include <stdint.h>

#define HFM_ADDR_LEN   6
#define HFM_PHONE      0
#define HFM_PC         1

// NVS keys in the namespace "lyrat" (beside "peers" of the reconnect list)
#define HFM_NVS_MODE   "mode"       // u8: HFM_PHONE or HFM_PC, written on every change
#define HFM_NVS_PHONE  "peer_ph"    // blob of 6: the AG of the phone mode; no key: empty slot
#define HFM_NVS_PC     "peer_pc"    // blob of 6: the AG of the PC mode

// Where the mode at boot came from
#define HFM_SRC_BUILD  0            // no mode in NVS: the default of this build
#define HFM_SRC_NVS    1

typedef enum {
    HFM_KEEP = 0,           // the AG of this mode
    HFM_ADOPT,              // kept, and it takes the slot of this mode now
    HFM_DROP_OTHER,         // the AG of the other mode: disconnect
    HFM_DROP_UNKNOWN,       // an AG of neither slot while this mode has one: disconnect
} hfm_slc_t;

// What the glue must do after hfm_command(); bits
#define HFM_DO_SAVE    0x01         // write mode and slots to NVS
#define HFM_DO_DROP    0x02         // disconnect the live link (its AG is not the one of the new mode)
#define HFM_DO_KICK    0x04         // wake the reconnect task now instead of after its period

typedef struct {
    uint8_t  mode;                  // HFM_PHONE or HFM_PC
    uint8_t  src;                   // HFM_SRC_*
    uint8_t  have[2];               // slot filled, by mode
    uint8_t  peer[2][HFM_ADDR_LEN]; // slots, by mode
    uint8_t  have_paired;           // an AG paired in this boot
    uint8_t  paired[HFM_ADDR_LEN];
    uint8_t  dirty;                 // mode or slots changed since the last save
    uint8_t  migrated;              // the phone slot came from the old reconnect list at this boot
    uint32_t turn;                  // round robin over the bond list while the slot is empty
} hfm_t;

// nvs_mode: the stored mode, -1 none. phone/pc: the stored slots, NULL empty. default_mode: of this build.
// mru0: the first of the old reconnect list ("peers", the last AG with SLC), NULL none. With no mode in NVS at all
// (first start after phone-bringup) and both slots empty, the phone slot is seeded from it.
void hfm_init(hfm_t *m, int nvs_mode, int default_mode, const uint8_t *phone, const uint8_t *pc, const uint8_t *mru0);

// The AG with this address finished pairing (GAP auth complete, success).
void hfm_on_paired(hfm_t *m, const uint8_t *bda);

// SLC came up with this AG: keep or drop. HFM_ADOPT fills the slot and sets dirty.
hfm_slc_t hfm_on_slc(hfm_t *m, const uint8_t *bda);

// The AG to call now (no link up). bonds: the stack's bond list. Returns 1 and fills out, 0 nobody to call.
int hfm_next_target(hfm_t *m, const uint8_t (*bonds)[HFM_ADDR_LEN], int nbonds, uint8_t out[HFM_ADDR_LEN]);

// The console command "mode [phone|pc [1]] | mode forget phone|pc" (args: the text after "mode"). sco_on: an audio link
// is open; slc_bda: the AG of the live link, NULL when none. Writes the reply into out and returns HFM_DO_* bits.
int hfm_command(hfm_t *m, const char *args, int sco_on, const uint8_t *slc_bda, char *out, size_t cap);

// Forget both slots ("unpair").
void hfm_forget_all(hfm_t *m);

// "phone", "pc" or "unknown": the slot this address is in
const char *hfm_role(const hfm_t *m, const uint8_t *bda);
const char *hfm_mode_name(int mode);
const char *hfm_slc_name(hfm_slc_t r);
// "..:a1:b2" (the last two bytes, as the logs of the PC keep them), "none" for NULL
const char *hfm_addr_short(const uint8_t *bda, char *buf, size_t cap);
