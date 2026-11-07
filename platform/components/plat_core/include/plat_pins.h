// Pins of the three boards of variant D (goal 095), from the wire table of goal 068
// (goals_harness/phone-then-pc/068-node-placement/sandbox/scripts/asm_data.py, list W) and goal 066 REPORT §4.
//
//   U1  ESP32-WROVER DevKitC     HF to the phone (I2S master)
//   U2  ESP32-WROVER DevKitC     HFP AG to the Bluetooth headphones
//   U3  ESP32-S3-DevKitC-1U N16R16VA   relay: Wi-Fi to the PC, recording, OTA of all three
//
// Every wire between two boards goes through a series resistor on a node board (R6-R17 1 kOhm for UART, EN, IO0), the
// wire names are those of the 068 table: "<SIG>_D" is the driven side before the resistor, "<SIG>" the side after it.
// PLAT_PIN_TABLE lists every signal pin with its wire name; plat_pins_check.py (lyrat_resend/tools/platform) compares
// it with the 068 table both ways and checks the pin rules of each chip. Change a pin here and there, never one only.
//
// U3 drives nothing toward U1/U2 on its own (R13): EN and IO0 are open drain (input when released), the control TX
// goes on only after a valid frame from that board, the console TX only while flashing over the ROM loader.
#pragma once

#define PLAT_PIN_EN   (-2)          // the chip's EN (reset) pin, not a GPIO

// ---------------------------------------------------------------------------------------------------------------- U1
#define U1_PIN_I2S0_BCK     27      // out  BCLK_D -> R1 -> bus BCLK
#define U1_PIN_I2S0_WS      25      // out  WS_D   -> R2 -> bus WS
#define U1_PIN_I2S0_DOUT    26      // out  DL_D   -> R3 -> bus DL (far end to the listeners)
#define U1_PIN_I2S0_DIN     35      // in   UL     (microphone to the phone)
#define U1_PIN_I2S1_BCK     32      // in   BCLK   (I2S1 slave)
#define U1_PIN_I2S1_WS      33      // in   WS
#define U1_PIN_I2S1_DIN     34      // in   RET    <- R5 <- U3 GPIO8 (sound from U3/PC into the phone)
#define U1_PIN_CTL_TX       14      // out  C1TX_D -> R6 -> U3 GPIO2   (UART1, control frames)
#define U1_PIN_CTL_RX       13      // in   C1RX   <- R7 <- U3 GPIO1
#define U1_PIN_CON_TX       1       // out  F1TX_D -> R8 -> U3 GPIO40  (UART0: console, ROM loader)
#define U1_PIN_CON_RX       3       // in   F1RX   <- R9 <- U3 GPIO39, R31 100k to 3V3
#define U1_PIN_EN           PLAT_PIN_EN   // EN1 <- R10 <- U3 GPIO21
#define U1_PIN_BOOT         0       // in   B01    <- R11 <- U3 GPIO12 (strapping: 0 at reset = ROM download)

// ---------------------------------------------------------------------------------------------------------------- U2
#define U2_PIN_I2S0_BCK     27      // in   BCLK   (slave of U1's bus)
#define U2_PIN_I2S0_WS      25      // in   WS
#define U2_PIN_I2S0_DIN     35      // in   DL     (far end to the headphones)
#define U2_PIN_I2S0_DOUT    26      // out  U2UL_D -> R4 -> MICD (headphone microphone toward the phone)
#define U2_PIN_I2S1_BCK     32      // in   BCLK
#define U2_PIN_I2S1_WS      33      // in   WS
#define U2_PIN_I2S1_DIN     34      // in   MEMS2  (board microphone, mode selection R21)
#define U2_PIN_CTL_TX       14      // out  C2TX_D -> R12 -> U3 GPIO5  (UART1, control frames)
#define U2_PIN_CTL_RX       13      // in   C2RX   <- R13 <- U3 GPIO4
#define U2_PIN_CON_TX       1       // out  F2TX_D -> R14 -> U3 GPIO42 (UART0)
#define U2_PIN_CON_RX       3       // in   F2RX   <- R15 <- U3 GPIO41, R32 100k to 3V3
#define U2_PIN_EN           PLAT_PIN_EN   // EN2 <- R16 <- U3 GPIO13
#define U2_PIN_BOOT         0       // in   B02    <- R17 <- U3 GPIO14

// ---------------------------------------------------------------------------------------------------------------- U3
#define U3_PIN_I2S_BCK      15      // in   BCLK   (listens to U1's bus)
#define U3_PIN_I2S_WS       16      // in   WS
#define U3_PIN_I2S_DL       17      // in   DL
#define U3_PIN_I2S_UL       18      // in   UL
#define U3_PIN_I2S_RET      8       // out  RET_D  -> R5 -> U1 GPIO34
#define U3_PIN_VBAT         9       // in   VBATS  ADC1 CH8, cell voltage through a 1:2 divider
#define U3_PIN_C1_TX        1       // out  C1RX_D -> R7 -> U1 GPIO13  (UART1)
#define U3_PIN_C1_RX        2       // in   C1TX   <- R6 <- U1 GPIO14
#define U3_PIN_C2_TX        4       // out  C2RX_D -> R13 -> U2 GPIO13 (UART2)
#define U3_PIN_C2_RX        5       // in   C2TX   <- R12 <- U2 GPIO14
#define U3_PIN_F1_TX        39      // out  F1RX_D -> R9 -> U1 RX0     (UART0 while flashing U1 only)
#define U3_PIN_F1_RX        40      // in   F1TX   <- R8 <- U1 TX0
#define U3_PIN_F2_TX        41      // out  F2RX_D -> R15 -> U2 RX0    (UART0 while flashing U2 only)
#define U3_PIN_F2_RX        42      // in   F2TX   <- R14 <- U2 TX0
#define U3_PIN_EN1          21      // od   EN1_D  -> R10 -> U1 EN
#define U3_PIN_BOOT1        12      // od   B01_D  -> R11 -> U1 GPIO0  (068: not 47, 1.8 V on N16R16VA)
#define U3_PIN_EN2          13      // od   EN2_D  -> R16 -> U2 EN
#define U3_PIN_BOOT2        14      // od   B02_D  -> R17 -> U2 GPIO0

// Board, wire name of the 068 table, pin. Parsed by plat_pins_check.py: one row per wire end on a board.
#define PLAT_PIN_TABLE(X) \
    X(U1, "BCLK_D", U1_PIN_I2S0_BCK) \
    X(U1, "WS_D", U1_PIN_I2S0_WS) \
    X(U1, "DL_D", U1_PIN_I2S0_DOUT) \
    X(U1, "UL", U1_PIN_I2S0_DIN) \
    X(U1, "BCLK", U1_PIN_I2S1_BCK) \
    X(U1, "WS", U1_PIN_I2S1_WS) \
    X(U1, "RET", U1_PIN_I2S1_DIN) \
    X(U1, "C1TX_D", U1_PIN_CTL_TX) \
    X(U1, "C1RX", U1_PIN_CTL_RX) \
    X(U1, "F1TX_D", U1_PIN_CON_TX) \
    X(U1, "F1RX", U1_PIN_CON_RX) \
    X(U1, "EN1", U1_PIN_EN) \
    X(U1, "B01", U1_PIN_BOOT) \
    X(U2, "BCLK", U2_PIN_I2S0_BCK) \
    X(U2, "WS", U2_PIN_I2S0_WS) \
    X(U2, "DL", U2_PIN_I2S0_DIN) \
    X(U2, "U2UL_D", U2_PIN_I2S0_DOUT) \
    X(U2, "BCLK", U2_PIN_I2S1_BCK) \
    X(U2, "WS", U2_PIN_I2S1_WS) \
    X(U2, "MEMS2", U2_PIN_I2S1_DIN) \
    X(U2, "C2TX_D", U2_PIN_CTL_TX) \
    X(U2, "C2RX", U2_PIN_CTL_RX) \
    X(U2, "F2TX_D", U2_PIN_CON_TX) \
    X(U2, "F2RX", U2_PIN_CON_RX) \
    X(U2, "EN2", U2_PIN_EN) \
    X(U2, "B02", U2_PIN_BOOT) \
    X(U3, "BCLK", U3_PIN_I2S_BCK) \
    X(U3, "WS", U3_PIN_I2S_WS) \
    X(U3, "DL", U3_PIN_I2S_DL) \
    X(U3, "UL", U3_PIN_I2S_UL) \
    X(U3, "RET_D", U3_PIN_I2S_RET) \
    X(U3, "VBATS", U3_PIN_VBAT) \
    X(U3, "C1RX_D", U3_PIN_C1_TX) \
    X(U3, "C1TX", U3_PIN_C1_RX) \
    X(U3, "C2RX_D", U3_PIN_C2_TX) \
    X(U3, "C2TX", U3_PIN_C2_RX) \
    X(U3, "F1RX_D", U3_PIN_F1_TX) \
    X(U3, "F1TX", U3_PIN_F1_RX) \
    X(U3, "F2RX_D", U3_PIN_F2_TX) \
    X(U3, "F2TX", U3_PIN_F2_RX) \
    X(U3, "EN1_D", U3_PIN_EN1) \
    X(U3, "B01_D", U3_PIN_BOOT1) \
    X(U3, "EN2_D", U3_PIN_EN2) \
    X(U3, "B02_D", U3_PIN_BOOT2)
