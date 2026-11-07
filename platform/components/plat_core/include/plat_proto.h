// What the three firmwares of variant D (goal 095) and the PC tools agree on, besides ota_link.h, dbg_log.h and
// ctl_frame.h of goal 090. docs: lyrat_resend/docs/PLATFORM_OTA.md.
#pragma once

#include "dbg_log.h"

// Boards in log records (dbg_log.h): 2 U3, 3 U1, 4 U2; | DBG_BOARD_CON for a line of a board's UART0 console.

// Targets of the UDP header "OT", version, target on U3 port 5007.
#define PLAT_TARGET_U3      0       // U3 itself: its next app slot
#define PLAT_TARGET_U1      1       // U1 app: staged in u1_img, sent over C1 (UART1) with ota_link
#define PLAT_TARGET_U2      2       // U2 app: staged in u2_img, sent over C2 (UART2)
#define PLAT_TARGET_ROM     0x10    // | node: a ROM bundle staged in uN_img, written through the ROM loader
#define PLAT_TARGET_U1_ROM  (PLAT_TARGET_ROM | PLAT_TARGET_U1)
#define PLAT_TARGET_U2_ROM  (PLAT_TARGET_ROM | PLAT_TARGET_U2)

// Project names: the receiver of an image checks it (ota_image_check), so an image of U2 never lands in U1.
#define PLAT_PROJECT_U1     "u1_hf"
#define PLAT_PROJECT_U2     "u2_ag"
#define PLAT_PROJECT_U3     "u3_relay"

#define PLAT_NODE_SLOT      0x1F0000u   // ota_0/ota_1 of U1 and U2 (4 MB flash)
#define PLAT_NODE_FLASH     0x400000u
#define PLAT_STAGE_SIZE     0x200000u   // u1_img / u2_img on U3

// Control link U3 <-> U1/U2: ctl_frame.h frames at CTL_BAUD_DEFAULT.
#define PLAT_HB_MS          1000    // node heartbeat CTL_T_HB
#define PLAT_TX_RELEASE_MS  5000    // U3 lets the control TX pin go after this long without a valid frame
#define PLAT_SILENT_MS      3000    // the console follows a node whose link is silent this long (auto)
#define PLAT_CON_FOLLOW_MS  30000

// R21 (reserved, goal 095 does not implement the modes): CTL_T_MODE payload u8 mode (1..4), u8 flags; the node answers
// CTL_T_MODE_ACK u8 status, u8 mode. Status:
#define PLAT_MODE_OK            0
#define PLAT_MODE_UNSUPPORTED   2
