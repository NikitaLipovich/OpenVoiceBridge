// ESP32-S3-ETH, stage 3: passive listener on the I2S bus of the LyraT, both lines sent to the PC by UDP, and the
// commands of the PC relayed to the LyraT over a framed UART (docs/S3_UART.md).
//
// Every pin wired to the LyraT is an input here, so this firmware cannot drive the codec bus, and
// pins_check() re-reads the hardware registers to prove it. The one exception is GPIO1, the UART TX: it drives the line
// only while the LyraT is proven alive by a valid frame, and goes back to an input when the frames stop. Status goes to
// the USB console.
//
// Network: Wi-Fi station by default (the owner, 2026-11-17: no Ethernet cable, it brings the PC's ground into the I2S
// tap). The network name and password come from main/ota_secrets.h, which git ignores; without them there is no
// network and the board is flashed over USB only. Build with -DS3_NET_ETH=1 for the old W5500 path.
//
// No address is built in. The PC sends "TAP1 <call> <mic>" to UDP port 5006, by broadcast until
// it knows this board, and repeats it. The streams then go to the sender's address, ports 5004 (call) and
// 5005 (mic), until no request came for LEASE_MS or "TAP0" arrives. <call> and <mic> pick the slots: L, R or
// LR. Each request is answered on its source port with one line of JSON counters.
//
// Updates over the network (UDP 5007, also of the LyraT through the UART) and the debug log of both boards (UDP 5008):
// s3_ota.c and s3_dbglog.c, docs/OTA.md.
//
// Control channel on the same port, answered on the source port of the request:
//   UC <seq> <command>   relay one console command; the answer is "UR <seq> <status>\n<text>", the frame's own status
//                        or 3 when the TX is off. The PC repeats on a timeout with the same seq, the LyraT then answers
//                        from its copy without running the command again.
//   UQ                   JSON counters of the UART, without starting the streams
//   UT 0|1|A             TX off, on, or following the LyraT (default)
//   UB <baud>            baud rate of this end
//   UW <hex>             raw bytes onto the line, for tests of the LyraT's parser; refused when the TX is off
//   UX <boot>            restart this board in RESTART_DELAY_MS, only when <boot> is its boot id (a repeat after the
//                        restart is refused): "UX <boot> 1" restarting, "UX <boot> 0" not this boot. Goal 093: without
//                        USB nothing else restarts the S3
//   UP [<qdbm>]          Wi-Fi transmit power cap in 0.25 dBm (8..84) now, until the next start; without a value only
//                        read. "UP <applied qdbm> <esp_err>"; also in the stat line as txp=. -1: no Wi-Fi in this build
//   UA [scan]            what goes to the LyraT for its AFH map (goal 106) as JSON; "scan" asks for a passive scan
//                        now: "UA scan <0 started|1 the LyraT has the headset channel|2 busy|3 no Wi-Fi>"
//   UO <s>               Wi-Fi off in 300 ms for <s> seconds (1..300), then on again by itself with the same power cap
//                        (goal 100: the LyraT's Bluetooth with no 2.4 GHz transmitter next to it). "UO <s> <err>": 0
//                        going off, 1 bad value, 2 already off, 3 no Wi-Fi in this build. Nothing is heard meanwhile
//                        (no log, no commands). Guard (#001): no IP <s> + 90 s after the command restarts the board
//
// Stream packet, little endian, PCM 16 bit after the header:
//   0  'S' '3'   2  version   3  stream (0 call, 1 mic)   4  u32 seq (per stream, since boot)
//   8  u32 frame of the first sample, counted from the start of this I2S channel
//   12 u16 boot id   14 u8 channels   15 u8 frames/8   16 u32 I2S overflows   20 u32 send failures
//
//   LyraT I2S header         S3
//   SCLK   GPIO5             GPIO15
//   LRCK   GPIO25            GPIO16
//   DSDIN  GPIO26 (to phones) GPIO17
//   ASDOUT GPIO35 (from mic)  GPIO18
//   GND                      GND
//   MCLK and 3.3 V stay unconnected.

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "afh_map.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "driver/spi_master.h"
#include "driver/uart.h"
#include "ctl_frame.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_check.h"
#include "esp_app_desc.h"
#include "esp_eth.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "s3_link.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "soc/gpio_periph.h"
#include "soc/gpio_reg.h"
#include "soc/io_mux_reg.h"

static const char *TAG = "s3eth";

#ifndef S3_NET_ETH
#define S3_NET_ETH 0            // 0: Wi-Fi station (default), 1: W5500 Ethernet
#endif
#if __has_include("ota_secrets.h")
#include "ota_secrets.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif
// Optional cap of the Wi-Fi transmit power in 0.25 dBm (8..84), to disturb the LyraT's Bluetooth less; 0 keeps the default.
#ifndef WIFI_MAX_TX_QDBM
#define WIFI_MAX_TX_QDBM 0
#endif

#define PIN_I2S_BCK     GPIO_NUM_15
#define PIN_I2S_WS      GPIO_NUM_16
#define PIN_I2S_DSDIN   GPIO_NUM_17
#define PIN_I2S_ASDOUT  GPIO_NUM_18
#define PIN_UART_TX     GPIO_NUM_1     // an input unless the UART task made it an output
#define PIN_UART_RX     GPIO_NUM_2     // always an input

#define PIN_ETH_MISO    GPIO_NUM_12
#define PIN_ETH_MOSI    GPIO_NUM_11
#define PIN_ETH_SCLK    GPIO_NUM_13
#define PIN_ETH_CS      GPIO_NUM_14
#define PIN_ETH_RST     GPIO_NUM_9
#define PIN_ETH_INT     GPIO_NUM_10

#define SAMPLE_RATE     16000          // same as the LyraT: 16 kHz, 16-bit stereo, Philips
#define DMA_FRAMES      240
#define STATUS_MS       2000
#define READ_TIMEOUT_MS 200

#define PORT_CALL       5004
#define PORT_MIC        5005
#define PORT_REQUEST    5006
#define LEASE_MS        5000
#define HEADER_BYTES    24
#define PACKET_VERSION  1

typedef enum { SLOT_L = 0, SLOT_R = 1, SLOT_LR = 2 } slot_sel_t;

typedef struct {
    const char *name;
    uint8_t stream;
    uint16_t port;
    i2s_chan_handle_t rx;
    volatile slot_sel_t slots;
    volatile uint32_t seq;
    volatile uint32_t total_frames;    // since the channel started
    volatile uint32_t overflows;       // DMA blocks the task did not take in time: lost on this board
    volatile uint32_t sent;
    volatile uint32_t send_fail;
    volatile uint32_t frames;          // stereo frames since the last report
    volatile uint32_t timeouts;
    volatile int peak_l;
    volatile int peak_r;
} listener_t;

#define CTL_UART        UART_NUM_1
#define CTL_RX_RING     4096
#define CTL_TX_RING     2048
#define CTL_TX_RELEASE_MS 5000         // no valid frame for this long: the LyraT may be off, stop driving its pin
#define CTL_ST_NO_TX    3
#define TX_MODE_OFF     0
#define TX_MODE_ON      1
#define TX_MODE_AUTO    2
#ifndef CTL_TX_MODE_BOOT
#define CTL_TX_MODE_BOOT TX_MODE_AUTO
#endif
#define RESTART_DELAY_MS 500           // "UX": the reply leaves first

static listener_t s_call = {.name = "dsdin", .stream = 0, .port = PORT_CALL, .slots = SLOT_LR};
static listener_t s_mic = {.name = "asdout", .stream = 1, .port = PORT_MIC, .slots = SLOT_LR};

static volatile uint32_t s_dest_ip;    // network order, 0 when nobody asked
static volatile int64_t s_lease_until_us;
static uint16_t s_boot_id;
static volatile bool s_pins_ok;

static volatile bool s_link_up;
static volatile bool s_got_ip;
static volatile bool s_eth_ok;
static esp_err_t s_i2s_call_err = ESP_FAIL, s_i2s_mic_err = ESP_FAIL, s_uart_err = ESP_FAIL;
static char s_ip[16] = "-";

static SemaphoreHandle_t s_tx_lock;    // the state of the TX pin and pins_check() must see each other whole
static volatile int s_tx_mode = CTL_TX_MODE_BOOT;
static volatile bool s_tx_on;
static volatile int s_ctl_baud = CTL_BAUD_DEFAULT;
static ctl_parser_t s_ctl_parser;
static int s_req_sock = -1;
static SemaphoreHandle_t s_peer_lock;
static struct sockaddr_in s_ctl_peer;  // who sent the last command
static volatile bool s_have_peer;
static volatile int64_t s_last_frame_us;
static volatile uint32_t s_ctl_rx_bytes, s_ctl_replies, s_ctl_hb, s_ctl_cmds_sent, s_ctl_refused,
    s_ctl_tx_edges;
static ctl_hb_t s_last_hb;
static volatile bool s_have_hb;

// Goal 106: what the S3 knows of 2.4 GHz Wi-Fi, for the LyraT's AFH map (afh_map.h). The access point's channel always
// (the last one known also while not associated: the router stays where it is), a passive scan now and then, never
// while the LyraT has the headset channel open. Written by the Wi-Fi event and scan tasks, read by the UART task.
#define SCAN_FIRST_MS       20000       // after the first IP
#define SCAN_PERIOD_MS      (10 * 60 * 1000)
#define SCAN_SCO_QUIET_MS   10000       // the headset channel closed at least this long
#define SCAN_CHECK_MS       5000
#define SCAN_DWELL_MS       120         // passive, a beacon interval and some
#define SCAN_HOME_MS        30          // back on the access point's channel between channels
#define SCAN_RECORDS        24
static portMUX_TYPE s_afh_mux = portMUX_INITIALIZER_UNLOCKED;
static afh_report_t s_afh_rep = {.scan_age_s = AFH_SCAN_NONE};
static int64_t s_afh_scan_us;           // when the last scan ended, 0 none
static volatile int64_t s_sco_seen_us;  // the last heartbeat with the headset channel open
static volatile bool s_scan_running, s_scan_asked;
static uint32_t s_scans, s_scan_fail;

// The report bytes after the two log bytes of the ack, with the scan's age as of now.
static size_t afh_report_now(uint8_t *out, size_t cap)
{
    portENTER_CRITICAL(&s_afh_mux);
    afh_report_t r = s_afh_rep;
    int64_t scan_us = s_afh_scan_us;
    portEXIT_CRITICAL(&s_afh_mux);
    if (scan_us) {
        int64_t age = (esp_timer_get_time() - scan_us) / 1000000;
        r.scan_age_s = age >= AFH_SCAN_NONE ? AFH_SCAN_NONE - 1 : (uint16_t)age;
    } else {
        r.scan_age_s = AFH_SCAN_NONE;
    }
    return afh_report_encode(&r, out, cap);
}

static void pins_to_safe_inputs(void)
{
    // No pulls: the lines belong to the LyraT, this board only listens.
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << PIN_I2S_BCK) | (1ULL << PIN_I2S_WS) | (1ULL << PIN_I2S_DSDIN) |
                        (1ULL << PIN_I2S_ASDOUT) | (1ULL << PIN_UART_TX) | (1ULL << PIN_UART_RX),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&cfg));
}

// The rule of this wiring (no series resistors): nothing wired to the LyraT may drive or pull. Read back from
// the registers, because that is what holds whatever any driver was asked to do.
static bool pins_check(void)
{
    static const uint8_t pins[] = {PIN_I2S_BCK, PIN_I2S_WS, PIN_I2S_DSDIN, PIN_I2S_ASDOUT, PIN_UART_TX, PIN_UART_RX};
    xSemaphoreTake(s_tx_lock, portMAX_DELAY);
    uint32_t enabled = REG_READ(GPIO_ENABLE_REG);
    if (s_tx_on) enabled &= ~(1UL << PIN_UART_TX);
    xSemaphoreGive(s_tx_lock);
    bool ok = true;
    for (size_t i = 0; i < sizeof(pins); i++) {
        uint32_t mux = REG_READ(GPIO_PIN_MUX_REG[pins[i]]);
        if ((enabled & (1UL << pins[i])) || (mux & (FUN_PU | FUN_PD))) {
            ESP_LOGE(TAG, "pin %d not a bare input: enable=%08" PRIx32 " mux=%08" PRIx32, pins[i], enabled, mux);
            ok = false;
        }
    }
    return ok;
}

static bool IRAM_ATTR on_overflow(i2s_chan_handle_t handle, i2s_event_data_t *event, void *ctx)
{
    ((listener_t *)ctx)->overflows++;
    return false;
}

static esp_err_t listener_init(listener_t *l, i2s_port_t port, gpio_num_t din)
{
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(port, I2S_ROLE_SLAVE);
    chan.dma_desc_num = 8;
    chan.dma_frame_num = DMA_FRAMES;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan, NULL, &l->rx), TAG, "%s channel", l->name);

    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PIN_I2S_BCK,
            .ws = PIN_I2S_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = din,
        },
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(l->rx, &std), TAG, "%s std mode", l->name);
    i2s_event_callbacks_t callbacks = {.on_recv_q_ovf = on_overflow};
    ESP_RETURN_ON_ERROR(i2s_channel_register_event_callback(l->rx, &callbacks, l), TAG, "%s callback", l->name);
    return i2s_channel_enable(l->rx);
}

static void listener_send(listener_t *l, int sock, uint8_t *packet, int16_t *pcm, size_t samples)
{
    slot_sel_t slots = l->slots;
    size_t frames = samples / 2;
    size_t channels = slots == SLOT_LR ? 2 : 1;
    if (channels == 1) {
        for (size_t i = 0; i < frames; i++) pcm[i] = pcm[i * 2 + (slots == SLOT_R ? 1 : 0)];
    }
    uint32_t seq = l->seq++;
    uint32_t first = l->total_frames - frames;
    uint32_t overflows = l->overflows, send_fail = l->send_fail;
    packet[0] = 'S';
    packet[1] = '3';
    packet[2] = PACKET_VERSION;
    packet[3] = l->stream;
    memcpy(packet + 4, &seq, 4);
    memcpy(packet + 8, &first, 4);
    memcpy(packet + 12, &s_boot_id, 2);
    packet[14] = (uint8_t)channels;
    packet[15] = (uint8_t)(frames / 8);
    memcpy(packet + 16, &overflows, 4);
    memcpy(packet + 20, &send_fail, 4);
    struct sockaddr_in to = {.sin_family = AF_INET, .sin_port = htons(l->port)};
    to.sin_addr.s_addr = s_dest_ip;
    // Never block on the network: the DMA ring must keep being drained.
    int rc = sendto(sock, packet, HEADER_BYTES + frames * channels * sizeof(int16_t), MSG_DONTWAIT,
                    (struct sockaddr *)&to, sizeof(to));
    if (rc < 0) l->send_fail++;
    else l->sent++;
}

static void listener_task(void *arg)
{
    listener_t *l = arg;
    static const size_t bytes = DMA_FRAMES * 2 * sizeof(int16_t);
    uint8_t *packet = malloc(HEADER_BYTES + bytes);
    configASSERT(packet);
    int16_t *buf = (int16_t *)(packet + HEADER_BYTES);
    int sock = -1;      // created on first use: lwIP starts after these tasks
    for (;;) {
        size_t got = 0;
        // The timeout parameter is already in milliseconds.
        esp_err_t err = i2s_channel_read(l->rx, buf, bytes, &got, READ_TIMEOUT_MS);
        if (err == ESP_ERR_TIMEOUT && got == 0) {
            l->timeouts++;
            continue;
        }
        int peak_l = l->peak_l, peak_r = l->peak_r;
        size_t samples = got / sizeof(int16_t);
        for (size_t i = 0; i + 1 < samples; i += 2) {
            int a = abs(buf[i]), b = abs(buf[i + 1]);
            if (a > peak_l) peak_l = a;
            if (b > peak_r) peak_r = b;
        }
        l->peak_l = peak_l;
        l->peak_r = peak_r;
        l->frames += samples / 2;
        l->total_frames += samples / 2;
        if (s_dest_ip && esp_timer_get_time() < s_lease_until_us) {
            if (sock < 0) sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
            if (sock >= 0) listener_send(l, sock, packet, buf, samples & ~(size_t)1);
            else l->send_fail++;
        }
    }
}

static void ctl_tx_set(bool on)
{
    if (on == s_tx_on) return;
    xSemaphoreTake(s_tx_lock, portMAX_DELAY);
    if (on) {
        uart_flush(CTL_UART);
        uart_set_pin(CTL_UART, PIN_UART_TX, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
        s_tx_on = true;
    } else {
        uart_wait_tx_done(CTL_UART, pdMS_TO_TICKS(50));
        gpio_set_direction(PIN_UART_TX, GPIO_MODE_INPUT);
        s_tx_on = false;
    }
    s_ctl_tx_edges++;
    xSemaphoreGive(s_tx_lock);
    ESP_LOGI(TAG, "uart tx %s", on ? "on" : "off");
}

static void ctl_reply_to_pc(const ctl_parser_t *p)
{
    static char reply[32 + CTL_MAX_PAYLOAD];
    size_t len = ctl_frame_len(p);
    if (len == 0 || s_req_sock < 0 || !s_have_peer) return;
    const uint8_t *payload = ctl_frame_payload(p);
    int head = snprintf(reply, sizeof(reply), "UR %u %u\n", ctl_frame_seq(p), payload[0]);
    memcpy(reply + head, payload + 1, len - 1);
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    struct sockaddr_in to = s_ctl_peer;
    xSemaphoreGive(s_peer_lock);
    sendto(s_req_sock, reply, head + len - 1, MSG_DONTWAIT, (struct sockaddr *)&to, sizeof(to));
    s_ctl_replies++;
}

static void ctl_on_frame(const ctl_parser_t *p)
{
    s_last_frame_us = esp_timer_get_time();
    uint8_t type = ctl_frame_type(p);
    if (type == CTL_T_REPLY) {
        ctl_reply_to_pc(p);
    } else if (type == CTL_T_HB) {
        s_ctl_hb++;
        if (ctl_frame_len(p) == sizeof(ctl_hb_t)) {
            memcpy(&s_last_hb, ctl_frame_payload(p), sizeof(s_last_hb));
            s_have_hb = true;
            if (s_last_hb.flags & 2) s_sco_seen_us = esp_timer_get_time();
        }
        if (s_tx_on) {
            // The ack carries whether the LyraT sends its log, and at which level; then the Wi-Fi report (goal 106).
            uint8_t ctl[AFH_REPORT_MAX];
            uint8_t frame[CTL_HEADER_BYTES + sizeof(ctl) + CTL_CRC_BYTES];
            size_t n = s3_log_lyrat_ctl(ctl);
            n += afh_report_now(ctl + n, sizeof(ctl) - n);
            uart_write_bytes(CTL_UART, frame, ctl_frame_build(frame, CTL_T_HB_ACK, ctl_frame_seq(p), ctl, n));
        }
    } else if (type == CTL_T_LOG) {
        s3_log_relay(ctl_frame_payload(p), ctl_frame_len(p));
    } else if (type == CTL_T_OTA_ACK) {
        s3_ota_on_lyrat_reply(ctl_frame_payload(p), ctl_frame_len(p));
    }
}

// Whatever comes in, the worst this task does is count it. The LyraT prints garbage on this line at its start.
static void ctl_uart_task(void *arg)
{
    static uint8_t chunk[256];
    for (;;) {
        int got = uart_read_bytes(CTL_UART, chunk, sizeof(chunk), 1);
        if (got > 0) {
            s_ctl_rx_bytes += (uint32_t)got;
            uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
            for (int i = 0; i < got; i++) {
                if (ctl_parser_feed(&s_ctl_parser, chunk[i], now)) ctl_on_frame(&s_ctl_parser);
            }
        }
        int mode = s_tx_mode;
        bool alive = s_last_frame_us && esp_timer_get_time() - s_last_frame_us < CTL_TX_RELEASE_MS * 1000LL;
        ctl_tx_set(mode == TX_MODE_ON || (mode == TX_MODE_AUTO && alive));
    }
}

static esp_err_t ctl_uart_init(void)
{
    uart_config_t cfg = {
        .baud_rate = CTL_BAUD_DEFAULT,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_RETURN_ON_ERROR(uart_driver_install(CTL_UART, CTL_RX_RING, CTL_TX_RING, 0, NULL, 0), TAG, "uart driver");
    ESP_RETURN_ON_ERROR(uart_param_config(CTL_UART, &cfg), TAG, "uart config");
    // RX only for now. An unset TX stays away from the pin.
    ESP_RETURN_ON_ERROR(uart_set_pin(CTL_UART, UART_PIN_NO_CHANGE, PIN_UART_RX, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE),
                        TAG, "uart pins");
    return ESP_OK;
}

bool s3_uart_tx_on(void) { return s_tx_on; }
int s3_uart_get_baud(void) { return s_ctl_baud; }
uint16_t s3_boot_id(void) { return s_boot_id; }

int s3_uart_set_baud(int baud)
{
    if (baud >= 1200 && baud <= 921600 && uart_set_baudrate(CTL_UART, (uint32_t)baud) == ESP_OK) s_ctl_baud = baud;
    return s_ctl_baud;
}

bool s3_uart_send(uint8_t type, const void *payload, size_t len)
{
    static uint16_t seq;
    uint8_t frame[CTL_MAX_FRAME];
    if (!s_tx_on || len > CTL_MAX_PAYLOAD) return false;
    size_t n = ctl_frame_build(frame, type, seq++, payload, len);
    // One write per frame: the driver's lock keeps frames of different tasks whole.
    return uart_write_bytes(CTL_UART, frame, n) == (int)n;
}

void s3_health(s3_health_t *h)
{
    h->eth_ok = s_eth_ok;
    h->got_ip = s_got_ip;
    h->i2s_call_ok = s_i2s_call_err == ESP_OK;
    h->i2s_mic_ok = s_i2s_mic_err == ESP_OK;
    h->uart_ok = s_uart_err == ESP_OK;
    h->call_frames = s_call.total_frames;
    h->mic_frames = s_mic.total_frames;
    h->uart_rx_bytes = s_ctl_rx_bytes;
    h->uart_frames = s_ctl_parser.frames;
}

static int hex_value(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int wifi_tx_power(void);
static esp_err_t wifi_set_tx_power(int qdbm);
static int wifi_off_for(int seconds);
static int wifi_scan_ask(void);

static void restart_now(void *arg)
{
    esp_restart();
}

static void restart_later(void)
{
    static esp_timer_handle_t timer;
    ESP_LOGW(TAG, "restart asked over the network, in %d ms", RESTART_DELAY_MS);
    if (!timer) {
        const esp_timer_create_args_t args = {.callback = restart_now, .name = "restart"};
        if (esp_timer_create(&args, &timer) != ESP_OK) esp_restart();
    }
    esp_timer_stop(timer);
    esp_timer_start_once(timer, RESTART_DELAY_MS * 1000ULL);
}

// Handles the control words of the request port; false when the text is not one of them.
static bool ctl_request(char *text, int n, const struct sockaddr_in *from, socklen_t from_len, int sock)
{
    static char reply[420];
    static uint8_t frame[CTL_MAX_FRAME];
    int len = -1;
    if (strncmp(text, "UC ", 3) == 0) {
        char *end = NULL;
        unsigned long seq = strtoul(text + 3, &end, 10);
        if (end == text + 3 || *end != ' ' || seq > 0xFFFF) {
            len = snprintf(reply, sizeof(reply), "UR 0 %d\nbad request", CTL_ST_BAD);
        } else if (!s_tx_on) {
            s_ctl_refused++;
            len = snprintf(reply, sizeof(reply), "UR %lu %d\nno_tx", seq, CTL_ST_NO_TX);
        } else {
            size_t size = strlen(end + 1);
            if (size > CTL_MAX_PAYLOAD) size = CTL_MAX_PAYLOAD;
            xSemaphoreTake(s_peer_lock, portMAX_DELAY);
            s_ctl_peer = *from;
            xSemaphoreGive(s_peer_lock);
            s_have_peer = true;
            uart_write_bytes(CTL_UART, frame, ctl_frame_build(frame, CTL_T_CMD, (uint16_t)seq, end + 1, size));
            s_ctl_cmds_sent++;
            return true;        // the answer comes with the LyraT's reply
        }
    } else if (strncmp(text, "UW ", 3) == 0) {
        int bytes = 0;
        for (const char *h = text + 3; h + 1 < text + n && bytes < (int)sizeof(frame); h += 2) {
            int a = hex_value(h[0]), b = hex_value(h[1]);
            if (a < 0 || b < 0) break;
            frame[bytes++] = (uint8_t)(a * 16 + b);
        }
        if (s_tx_on) uart_write_bytes(CTL_UART, frame, bytes);
        else bytes = -1;
        len = snprintf(reply, sizeof(reply), "UW %d", bytes);
    } else if (strncmp(text, "UT ", 3) == 0) {
        if (text[3] == '0') s_tx_mode = TX_MODE_OFF;
        else if (text[3] == '1') s_tx_mode = TX_MODE_ON;
        else if (text[3] == 'A') s_tx_mode = TX_MODE_AUTO;
        len = snprintf(reply, sizeof(reply), "UT %d", s_tx_mode);
    } else if (strncmp(text, "UB ", 3) == 0) {
        len = snprintf(reply, sizeof(reply), "UB %d", s3_uart_set_baud(atoi(text + 3)));
    } else if (strncmp(text, "UX ", 3) == 0) {
        unsigned long boot = strtoul(text + 3, NULL, 10);
        bool now = boot == s_boot_id;
        len = snprintf(reply, sizeof(reply), "UX %lu %d", boot, now);
        if (now) restart_later();
    } else if (strcmp(text, "UP") == 0 || strncmp(text, "UP ", 3) == 0) {
        esp_err_t err = ESP_OK;
        if (text[2] == ' ') err = wifi_set_tx_power(atoi(text + 3));
        len = snprintf(reply, sizeof(reply), "UP %d %d", wifi_tx_power(), (int)err);
    } else if (strcmp(text, "UA") == 0) {
        uint8_t b[AFH_REPORT_MAX];
        afh_report_t r;
        b[0] = b[1] = 0;
        size_t got = 2 + afh_report_now(b + 2, sizeof(b) - 2);
        if (afh_report_parse(b, got, &r) != 1) memset(&r, 0, sizeof(r));
        len = snprintf(reply, sizeof(reply),
                       "{\"ver\":%d,\"gen\":%u,\"ap\":%u,\"second\":%u,\"rssi\":%d,\"assoc\":%d,\"bw40\":%d,"
                       "\"scan_age\":%u,\"scans\":%" PRIu32 ",\"scan_fail\":%" PRIu32 ",\"scanning\":%d,\"nets\":[",
                       AFH_REPORT_VER, r.gen, r.ap_primary, r.ap_second, r.ap_rssi, (r.flags & AFH_FLAG_ASSOC) != 0,
                       (r.flags & AFH_FLAG_BW40) != 0, r.scan_age_s, s_scans, s_scan_fail, s_scan_running);
        for (int i = 0; i < r.n && len < (int)sizeof(reply) - 24; i++) {
            len += snprintf(reply + len, sizeof(reply) - len, "%s[%u,%u,%d]", i ? "," : "", r.nets[i].primary,
                            r.nets[i].second, r.nets[i].rssi);
        }
        len += snprintf(reply + len, sizeof(reply) - len, "]}");
    } else if (strcmp(text, "UA scan") == 0) {
        len = snprintf(reply, sizeof(reply), "UA scan %d", wifi_scan_ask());
    } else if (strncmp(text, "UO ", 3) == 0) {
        int seconds = atoi(text + 3);
        len = snprintf(reply, sizeof(reply), "UO %d %d", seconds, wifi_off_for(seconds));
    } else if (strcmp(text, "UQ") == 0) {
        int64_t now = esp_timer_get_time();
        len = snprintf(reply, sizeof(reply),
            "{\"fw\":\"s3tap\",\"boot\":%u,\"up_ms\":%lld,\"pins_ok\":%d,\"tx_mode\":%d,\"tx_on\":%d,\"tx_edges\":%" PRIu32
            ",\"baud\":%d,\"rx_level\":%d,\"tx_level\":%d,\"rx_bytes\":%" PRIu32 ",\"frames\":%" PRIu32 ",\"bad_hdr\":%" PRIu32
            ",\"bad_crc\":%" PRIu32 ",\"trunc\":%" PRIu32 ",\"noise\":%" PRIu32 ",\"replies\":%" PRIu32 ",\"sent\":%" PRIu32
            ",\"refused\":%" PRIu32 ",\"hb\":%" PRIu32 ",\"last_frame_ms\":%lld,\"lyrat_up_s\":%" PRId64 ",\"lyrat_flags\":%d}",
            s_boot_id, now / 1000, s_pins_ok, s_tx_mode, s_tx_on, s_ctl_tx_edges, s_ctl_baud, gpio_get_level(PIN_UART_RX),
            gpio_get_level(PIN_UART_TX), s_ctl_rx_bytes, s_ctl_parser.frames, s_ctl_parser.bad_header, s_ctl_parser.bad_crc,
            s_ctl_parser.truncated, s_ctl_parser.noise, s_ctl_replies, s_ctl_cmds_sent, s_ctl_refused, s_ctl_hb,
            s_last_frame_us ? (now - s_last_frame_us) / 1000 : -1LL, s_have_hb ? (int64_t)s_last_hb.up_s : -1LL,
            s_have_hb ? s_last_hb.flags : -1);
    } else {
        return false;
    }
    if (len > 0) sendto(sock, reply, len, MSG_DONTWAIT, (const struct sockaddr *)from, from_len);
    return true;
}

static bool parse_slots(const char *text, slot_sel_t *out)
{
    if (strcmp(text, "L") == 0) *out = SLOT_L;
    else if (strcmp(text, "R") == 0) *out = SLOT_R;
    else if (strcmp(text, "LR") == 0) *out = SLOT_LR;
    else return false;
    return true;
}

static void request_task(void *arg)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    configASSERT(sock >= 0);
    struct sockaddr_in me = {.sin_family = AF_INET, .sin_port = htons(PORT_REQUEST), .sin_addr.s_addr = htonl(INADDR_ANY)};
    int bound = bind(sock, (struct sockaddr *)&me, sizeof(me));
    configASSERT(bound == 0);
    s_req_sock = sock;
    static char text[1472];
    char reply[420];
    for (;;) {
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        int n = recvfrom(sock, text, sizeof(text) - 1, 0, (struct sockaddr *)&from, &from_len);
        if (n <= 0) continue;
        text[n] = 0;
        if (ctl_request(text, n, &from, from_len, sock)) continue;
        char word[8] = {0}, call[4] = {0}, mic[4] = {0};
        int fields = sscanf(text, "%7s %3s %3s", word, call, mic);
        bool ok = false;
        if (strcmp(word, "TAP0") == 0) {
            s_dest_ip = 0;
            ok = true;
        } else if (strcmp(word, "TAP1") == 0) {
            slot_sel_t a = SLOT_LR, b = SLOT_LR;
            ok = fields != 2 && (fields == 1 || (parse_slots(call, &a) && parse_slots(mic, &b)));
            if (ok) {
                if (s_dest_ip != from.sin_addr.s_addr) ESP_LOGI(TAG, "stream to %s", inet_ntoa(from.sin_addr));
                s_call.slots = a;
                s_mic.slots = b;
                s_lease_until_us = esp_timer_get_time() + LEASE_MS * 1000LL;
                s_dest_ip = from.sin_addr.s_addr;
            }
        } else {
            continue;   // not addressed to this board
        }
        int len = snprintf(reply, sizeof(reply),
            "{\"fw\":\"s3tap\",\"ok\":%d,\"boot\":%u,\"up_ms\":%lld,\"pins_ok\":%d,"
            "\"call\":{\"frames\":%" PRIu32 ",\"sent\":%" PRIu32 ",\"send_fail\":%" PRIu32 ",\"ovf\":%" PRIu32 ",\"timeouts\":%" PRIu32 "},"
            "\"mic\":{\"frames\":%" PRIu32 ",\"sent\":%" PRIu32 ",\"send_fail\":%" PRIu32 ",\"ovf\":%" PRIu32 ",\"timeouts\":%" PRIu32 "}}",
            ok, s_boot_id, esp_timer_get_time() / 1000, s_pins_ok,
            s_call.total_frames, s_call.sent, s_call.send_fail, s_call.overflows, s_call.timeouts,
            s_mic.total_frames, s_mic.sent, s_mic.send_fail, s_mic.overflows, s_mic.timeouts);
        sendto(sock, reply, len, MSG_DONTWAIT, (struct sockaddr *)&from, from_len);
    }
}

#if S3_NET_ETH

static void on_eth_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (id == ETHERNET_EVENT_CONNECTED) {
        s_link_up = true;
        ESP_LOGI(TAG, "ethernet link up");
    } else if (id == ETHERNET_EVENT_DISCONNECTED) {
        s_link_up = false;
        s_got_ip = false;
        strcpy(s_ip, "-");
        ESP_LOGW(TAG, "ethernet link down");
    }
}

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    const ip_event_got_ip_t *e = data;
    snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&e->ip_info.ip));
    s_got_ip = true;
    ESP_LOGI(TAG, "ethernet ip %s", s_ip);
}

static esp_err_t net_init(void)
{
    ESP_RETURN_ON_ERROR(gpio_install_isr_service(0), TAG, "isr service");

    spi_bus_config_t bus = {
        .miso_io_num = PIN_ETH_MISO,
        .mosi_io_num = PIN_ETH_MOSI,
        .sclk_io_num = PIN_ETH_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "spi bus");

    spi_device_interface_config_t dev = {
        .mode = 0,
        .clock_speed_hz = 20 * 1000 * 1000,
        .spics_io_num = PIN_ETH_CS,
        .queue_size = 20,
    };
    eth_w5500_config_t w5500 = ETH_W5500_DEFAULT_CONFIG(SPI2_HOST, &dev);
    w5500.int_gpio_num = PIN_ETH_INT;

    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.reset_gpio_num = PIN_ETH_RST;

    esp_eth_mac_t *mac = esp_eth_mac_new_w5500(&w5500, &mac_cfg);
    esp_eth_phy_t *phy = esp_eth_phy_new_w5500(&phy_cfg);
    ESP_RETURN_ON_FALSE(mac && phy, ESP_FAIL, TAG, "w5500 driver objects");

    esp_eth_config_t cfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth = NULL;
    ESP_RETURN_ON_ERROR(esp_eth_driver_install(&cfg, &eth), TAG, "driver install, is the W5500 responding?");

    // The W5500 has no MAC address of its own.
    uint8_t addr[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(addr, ESP_MAC_ETH), TAG, "read mac");
    ESP_RETURN_ON_ERROR(esp_eth_ioctl(eth, ETH_CMD_S_MAC_ADDR, addr), TAG, "set mac");

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "event loop");
    esp_netif_config_t netif_cfg = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *netif = esp_netif_new(&netif_cfg);
    ESP_RETURN_ON_FALSE(netif, ESP_FAIL, TAG, "netif new");
    ESP_RETURN_ON_ERROR(esp_netif_attach(netif, esp_eth_new_netif_glue(eth)), TAG, "netif attach");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, on_eth_event, NULL), TAG, "eth events");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_got_ip, NULL), TAG, "ip events");
    return esp_eth_start(eth);
}

static int wifi_rssi(void) { return 0; }
static int wifi_tx_power(void) { return -1; }
static esp_err_t wifi_set_tx_power(int qdbm) { (void)qdbm; return ESP_ERR_NOT_SUPPORTED; }
static int wifi_off_for(int seconds) { (void)seconds; return 3; }
static int wifi_scan_ask(void) { return 3; }
static void wifi_scan_start_task(void) {}

#else

// Each failed attempt scans every channel at full power next to the LyraT's Bluetooth. With the access point gone (router
// off) that went on without a break; now the first attempts are at once, then one every WIFI_RETRY_MS. Goal 094 #003.
#define WIFI_RETRY_AT_ONCE  3
#define WIFI_RETRY_MS       5000

static esp_timer_handle_t s_wifi_retry;
static uint32_t s_wifi_fails;           // attempts in a row without an IP; only the event task touches it
static volatile bool s_wifi_off;        // "UO": stopped on purpose, no reconnect attempts until the timer starts it
static int s_wifi_cap_qdbm = WIFI_MAX_TX_QDBM;   // the cap to apply again after each start, 0 = driver default

static void wifi_retry_now(void *arg)
{
    esp_err_t err = esp_wifi_connect();
    // No attempt started means no event will come either: try again later rather than never.
    if (err != ESP_OK && esp_timer_start_once(s_wifi_retry, WIFI_RETRY_MS * 1000ULL) != ESP_OK) {
        ESP_LOGE(TAG, "wifi retry: %s", esp_err_to_name(err));
    }
}

static void wifi_retry(void)
{
    if (++s_wifi_fails <= WIFI_RETRY_AT_ONCE || !s_wifi_retry) {
        esp_wifi_connect();
    } else {
        esp_timer_stop(s_wifi_retry);
        esp_timer_start_once(s_wifi_retry, WIFI_RETRY_MS * 1000ULL);
    }
}

// The access point's channel, secondary and level, and this station's bandwidth, into the report.
static void afh_ap_update(void)
{
    wifi_ap_record_t ap;
    wifi_bandwidth_t bw = WIFI_BW_HT20;
    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK) return;
    esp_wifi_get_bandwidth(WIFI_IF_STA, &bw);
    portENTER_CRITICAL(&s_afh_mux);
    bool changed = s_afh_rep.ap_primary != ap.primary || s_afh_rep.ap_second != (uint8_t)ap.second;
    s_afh_rep.ap_primary = ap.primary;
    s_afh_rep.ap_second = ap.second <= 2 ? (uint8_t)ap.second : 0;
    s_afh_rep.ap_rssi = ap.rssi;
    s_afh_rep.flags = (uint8_t)(AFH_FLAG_ASSOC | (bw == WIFI_BW_HT40 ? AFH_FLAG_BW40 : 0));
    if (changed) s_afh_rep.gen++;
    portEXIT_CRITICAL(&s_afh_mux);
    if (changed) ESP_LOGI(TAG, "afh ap channel=%u second=%u bw=%d", ap.primary, (unsigned)ap.second, (int)bw);
}

static void afh_scan_done(void *data)
{
    static wifi_ap_record_t recs[SCAN_RECORDS];
    afh_net_t nets[SCAN_RECORDS], top[AFH_REPORT_NETS];
    const wifi_event_sta_scan_done_t *d = data;
    uint16_t n = SCAN_RECORDS;
    int64_t took_ms = s_scan_running ? 0 : -1;
    s_scan_running = false;
    if (!d || d->status != 0 || esp_wifi_scan_get_ap_records(&n, recs) != ESP_OK) {
        esp_wifi_clear_ap_list();
        s_scan_fail++;
        ESP_LOGW(TAG, "wifi scan failed");
        return;
    }
    for (uint16_t i = 0; i < n; i++) {
        nets[i].primary = recs[i].primary;
        nets[i].second = recs[i].second <= 2 ? (uint8_t)recs[i].second : 0;
        nets[i].rssi = recs[i].rssi;
    }
    size_t k = afh_scan_reduce(nets, n, top, AFH_REPORT_NETS);
    portENTER_CRITICAL(&s_afh_mux);
    s_afh_rep.n = (uint8_t)k;
    memcpy(s_afh_rep.nets, top, k * sizeof(afh_net_t));
    s_afh_rep.gen++;
    s_afh_scan_us = esp_timer_get_time();
    portEXIT_CRITICAL(&s_afh_mux);
    s_scans++;
    char text[AFH_REPORT_NETS * 12] = "";
    int used = 0;
    for (size_t i = 0; i < k; i++) {
        used += snprintf(text + used, sizeof(text) - used, "%s%u%s:%d", i ? "," : "", top[i].primary,
                         top[i].second == 1 ? "+" : top[i].second == 2 ? "-" : "", top[i].rssi);
    }
    (void)took_ms;
    ESP_LOGI(TAG, "wifi scan aps=%u nets=%u top=%s", n, (unsigned)k, k ? text : "-");
}

// Whether a scan may start now: 0 yes, 1 the LyraT has the headset channel (or had it just now), 2 busy (a scan, an
// update, the streams, the Wi-Fi off or not up).
static int wifi_scan_may(void)
{
    int64_t now = esp_timer_get_time();
    if (s_sco_seen_us && now - s_sco_seen_us < SCAN_SCO_QUIET_MS * 1000LL) return 1;
    bool streaming = s_dest_ip && now < s_lease_until_us;
    if (s_scan_running || !s_got_ip || s_wifi_off || streaming || s3_ota_busy()) return 2;
    return 0;
}

static int wifi_scan_go(void)
{
    int why = wifi_scan_may();
    if (why) return why;
    wifi_scan_config_t cfg = {
        .scan_type = WIFI_SCAN_TYPE_PASSIVE,
        .scan_time.passive = SCAN_DWELL_MS,
        .home_chan_dwell_time = SCAN_HOME_MS,
    };
    s_scan_running = true;
    esp_err_t err = esp_wifi_scan_start(&cfg, false);
    if (err != ESP_OK) {
        s_scan_running = false;
        s_scan_fail++;
        ESP_LOGW(TAG, "wifi scan start: %s", esp_err_to_name(err));
        return 2;
    }
    return 0;
}

static int wifi_scan_ask(void)
{
    int why = wifi_scan_may();
    if (!why) s_scan_asked = true;
    return why;
}

// One scan SCAN_FIRST_MS after the first IP, then every SCAN_PERIOD_MS, each when the LyraT has no headset channel.
static void wifi_scan_task(void *arg)
{
    int64_t ip_since = 0, last = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(s_scan_asked ? 100 : SCAN_CHECK_MS));
        int64_t now = esp_timer_get_time();
        if (!s_got_ip) {
            ip_since = 0;
            continue;
        }
        if (!ip_since) ip_since = now;
        if (s_link_up) afh_ap_update();           // the level now and then; a changed channel bumps gen
        bool due = s_scan_asked || (last ? now - last >= SCAN_PERIOD_MS * 1000LL : now - ip_since >= SCAN_FIRST_MS * 1000LL);
        if (!due) continue;
        if (wifi_scan_go() == 0) last = now;
        s_scan_asked = false;
    }
}

static void wifi_scan_start_task(void)
{
    xTaskCreatePinnedToCore(wifi_scan_task, "wifi_scan", 3072, NULL, 3, NULL, 0);
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        // After a stop ("UO") the driver starts with its defaults: no power save and the cap as before.
        esp_wifi_set_ps(WIFI_PS_NONE);
        if (s_wifi_cap_qdbm) esp_wifi_set_max_tx_power((int8_t)s_wifi_cap_qdbm);
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        s_link_up = true;
        ESP_LOGI(TAG, "wifi associated");
        afh_ap_update();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_SCAN_DONE) {
        afh_scan_done(data);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *d = data;
        if (s_link_up || s_got_ip) ESP_LOGW(TAG, "wifi lost (reason %d), reconnecting", d ? d->reason : -1);
        if (s_wifi_fails == WIFI_RETRY_AT_ONCE) {
            ESP_LOGW(TAG, "wifi: no network (reason %d), next attempts every %d ms", d ? d->reason : -1, WIFI_RETRY_MS);
        }
        s_link_up = false;
        s_got_ip = false;
        strcpy(s_ip, "-");
        portENTER_CRITICAL(&s_afh_mux);
        s_afh_rep.flags &= (uint8_t)~AFH_FLAG_ASSOC;      // the channel stays: the router is still there
        portEXIT_CRITICAL(&s_afh_mux);
        if (!s_wifi_off) wifi_retry();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&e->ip_info.ip));
        s_got_ip = true;
        s_wifi_fails = 0;
        ESP_LOGI(TAG, "wifi ip %s", s_ip);
    }
}

static esp_err_t net_init(void)
{
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "event loop");
    ESP_RETURN_ON_FALSE(WIFI_SSID[0], ESP_ERR_NOT_FOUND, TAG, "no WIFI_SSID in main/ota_secrets.h");
    const esp_timer_create_args_t retry = {.callback = wifi_retry_now, .name = "wifi_retry"};
    ESP_RETURN_ON_ERROR(esp_timer_create(&retry, &s_wifi_retry), TAG, "retry timer");
    ESP_RETURN_ON_FALSE(esp_netif_create_default_wifi_sta(), ESP_FAIL, TAG, "netif new");
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&init), TAG, "wifi init");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL), TAG, "wifi events");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi_event, NULL), TAG, "ip events");
    ESP_RETURN_ON_ERROR(esp_wifi_set_storage(WIFI_STORAGE_RAM), TAG, "storage");   // the password stays out of nvs
    wifi_config_t cfg = {0};
    snprintf((char *)cfg.sta.ssid, sizeof(cfg.sta.ssid), "%s", WIFI_SSID);
    snprintf((char *)cfg.sta.password, sizeof(cfg.sta.password), "%s", WIFI_PASS);
    cfg.sta.threshold.authmode = WIFI_PASS[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &cfg), TAG, "config");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "start");
    // No power save: the requests, the UART relay and the update come from the PC at any time, and a dozing radio holds
    // them until the next beacon. Only receiving is added; what we send does not change.
    esp_wifi_set_ps(WIFI_PS_NONE);
    if (WIFI_MAX_TX_QDBM) {
        esp_err_t p = esp_wifi_set_max_tx_power(WIFI_MAX_TX_QDBM);
        if (p != ESP_OK) ESP_LOGW(TAG, "tx power %d: %s", WIFI_MAX_TX_QDBM, esp_err_to_name(p));
    }
    return ESP_OK;
}

static int wifi_rssi(void)
{
    wifi_ap_record_t ap;
    return s_got_ip && esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
}

// The cap the driver holds now, in 0.25 dBm; -1 when it cannot be read (Wi-Fi not started).
static int wifi_tx_power(void)
{
    int8_t q = 0;
    return esp_wifi_get_max_tx_power(&q) == ESP_OK ? q : -1;
}

// A new cap while running, for the experiments of goal 091 with the LyraT's Bluetooth; gone at the next start.
static esp_err_t wifi_set_tx_power(int qdbm)
{
    if (qdbm < 8 || qdbm > 84) return ESP_ERR_INVALID_ARG;
    esp_err_t err = esp_wifi_set_max_tx_power((int8_t)qdbm);
    if (err == ESP_OK) s_wifi_cap_qdbm = qdbm;
    ESP_LOGI(TAG, "tx power cap %d: %s, now %d", qdbm, esp_err_to_name(err), wifi_tx_power());
    return err;
}

static volatile bool s_wifi_off_busy;
static esp_timer_handle_t s_wifi_off_guard;

// Goal 100 #001: a "UO" must never leave the board off the network. The guard is an esp_timer of its own, armed with
// the command for <s> + UO_GUARD_S seconds; when it fires without an IP (the start failed, the task never woke, the
// AP was not found) the board restarts, which brings the Wi-Fi up through the normal boot.
#define UO_GUARD_S  90

static void wifi_off_guard_fire(void *arg)
{
    if (s_got_ip && !s_wifi_off) return;
    ESP_LOGE(TAG, "wifi still off %d s after the planned return, restarting", UO_GUARD_S);
    esp_restart();
}

// One short task per "UO": stop and start block until the Wi-Fi task has done them, which the esp_timer task should
// not wait for. The radio comes back by itself, nothing waits for the PC.
static void wifi_off_task(void *arg)
{
    int seconds = (int)(intptr_t)arg;
    vTaskDelay(pdMS_TO_TICKS(300));         // the reply leaves first
    s_wifi_off = true;
    if (s_wifi_retry) esp_timer_stop(s_wifi_retry);
    esp_err_t err = esp_wifi_stop();
    ESP_LOGW(TAG, "wifi off for %d s: %s", seconds, esp_err_to_name(err));
    vTaskDelay(pdMS_TO_TICKS(seconds * 1000));
    s_wifi_off = false;
    s_wifi_fails = 0;
    err = esp_wifi_start();                 // STA_START sets power save and the cap, then connects
    ESP_LOGW(TAG, "wifi on again after %d s off: %s", seconds, esp_err_to_name(err));
    s_wifi_off_busy = false;
    vTaskDelete(NULL);
}

static int wifi_off_for(int seconds)
{
    if (seconds < 1 || seconds > 300) return 1;
    if (s_wifi_off_busy) return 2;
    if (!s_wifi_off_guard) {
        const esp_timer_create_args_t args = {.callback = wifi_off_guard_fire, .name = "uo_guard"};
        if (esp_timer_create(&args, &s_wifi_off_guard) != ESP_OK) return 1;   // no guard, no "UO"
    }
    s_wifi_off_busy = true;
    esp_timer_stop(s_wifi_off_guard);
    esp_timer_start_once(s_wifi_off_guard, (uint64_t)(seconds + UO_GUARD_S) * 1000000ULL);
    if (xTaskCreate(wifi_off_task, "wifi_off", 4096, (void *)(intptr_t)seconds, 5, NULL) != pdPASS) {
        s_wifi_off_busy = false;
        return 1;
    }
    return 0;
}

#endif

static int report(listener_t *l, uint32_t dt_ms, char *out, size_t cap)
{
    uint32_t frames = l->frames;
    l->frames = 0;
    int peak_l = l->peak_l, peak_r = l->peak_r;
    l->peak_l = 0;
    l->peak_r = 0;
    return snprintf(out, cap, " %s rate=%" PRIu32 " peak=%d/%d", l->name, frames * 1000 / dt_ms, peak_l, peak_r);
}

void app_main(void)
{
    s_tx_lock = xSemaphoreCreateMutex();
    s_peer_lock = xSemaphoreCreateMutex();
    configASSERT(s_tx_lock && s_peer_lock);
    pins_to_safe_inputs();
    s_boot_id = (uint16_t)esp_random();
    s3_log_init(s_boot_id);     // from here every log line also waits in the ring for the PC

    esp_err_t nvs = nvs_flash_init();
    if (nvs == ESP_ERR_NVS_NO_FREE_PAGES || nvs == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs = nvs_flash_init();
    }
    if (nvs != ESP_OK) ESP_LOGE(TAG, "nvs: %s", esp_err_to_name(nvs));

    esp_err_t a = listener_init(&s_call, I2S_NUM_0, PIN_I2S_DSDIN);
    esp_err_t b = listener_init(&s_mic, I2S_NUM_1, PIN_I2S_ASDOUT);
    s_i2s_call_err = a;
    s_i2s_mic_err = b;
    if (a == ESP_OK) xTaskCreatePinnedToCore(listener_task, "i2s_call", 4096, &s_call, 10, NULL, 1);
    if (b == ESP_OK) xTaskCreatePinnedToCore(listener_task, "i2s_mic", 4096, &s_mic, 10, NULL, 1);

    esp_err_t e = net_init();
    s_eth_ok = (e == ESP_OK);   // the network driver, Wi-Fi or Ethernet; the name stays for the tools
    if (!s_eth_ok) ESP_LOGE(TAG, "network init failed: %s", esp_err_to_name(e));

    esp_err_t u = ctl_uart_init();
    s_uart_err = u;
    if (u != ESP_OK) ESP_LOGE(TAG, "uart init failed: %s", esp_err_to_name(u));
    s_pins_ok = pins_check();
    if (s_eth_ok) xTaskCreatePinnedToCore(request_task, "tap_req", 4096, NULL, 5, NULL, 0);
    if (s_eth_ok) wifi_scan_start_task();
    if (u == ESP_OK) xTaskCreatePinnedToCore(ctl_uart_task, "ctl_uart", 4096, NULL, 5, NULL, 0);
    // The self-test runs without the network too: a new image without the network must roll back.
    s3_ota_start(s_eth_ok);
    if (s_eth_ok) s3_log_start();

    const esp_app_desc_t *app = esp_app_get_description();
    s3_log_line("s3eth stage3 i2s0=%s i2s1=%s eth=%s uart=%s pins_ok=%d ver=%s", esp_err_to_name(a),
                esp_err_to_name(b), esp_err_to_name(e), esp_err_to_name(u), s_pins_ok, app->version);

    int64_t last = esp_timer_get_time();
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(STATUS_MS));
        int64_t now = esp_timer_get_time();
        uint32_t dt_ms = (uint32_t)((now - last) / 1000);
        last = now;
        // The same text as before on the console (s3.py parse_stat), new fields only at the end; it also goes to the log.
        static char line[512];
        int n = snprintf(line, sizeof(line), "stat up=%" PRIu32 " eth=%s link=%d ip=%s", (uint32_t)(now / 1000000),
                         s_eth_ok ? "ok" : "fail", s_link_up, s_ip);
        n += report(&s_call, dt_ms, line + n, sizeof(line) - n);
        n += report(&s_mic, dt_ms, line + n, sizeof(line) - n);
        s_pins_ok = pins_check();
        bool streaming = s_dest_ip && now < s_lease_until_us;
        snprintf(line + n, sizeof(line) - n,
                 " timeouts=%" PRIu32 "/%" PRIu32 " ovf=%" PRIu32 "/%" PRIu32 " send_fail=%" PRIu32 "/%" PRIu32
                 " sent=%" PRIu32 "/%" PRIu32 " streaming=%d pins_ok=%d uart tx=%d rx=%" PRIu32 " frames=%" PRIu32
                 " bad=%" PRIu32 "/%" PRIu32 " lrelay_lost=%" PRIu32 " selftest=%s net=%s rssi=%d txp=%d",
                 s_call.timeouts, s_mic.timeouts, s_call.overflows, s_mic.overflows, s_call.send_fail, s_mic.send_fail,
                 s_call.sent, s_mic.sent, streaming, s_pins_ok, s_tx_on, s_ctl_rx_bytes, s_ctl_parser.frames,
                 s_ctl_parser.bad_header, s_ctl_parser.bad_crc, s3_log_relay_lost(), s3_ota_selftest_name(),
                 S3_NET_ETH ? "eth" : "wifi", wifi_rssi(), wifi_tx_power());
        s3_log_line("%s", line);
    }
}
