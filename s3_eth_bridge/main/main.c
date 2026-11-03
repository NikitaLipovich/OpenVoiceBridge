// ESP32-S3-ETH, stage 2: passive listener on the I2S bus of the LyraT, both lines sent to the PC by UDP.
//
// Every pin wired to the LyraT is an input here, so this firmware cannot drive the codec bus, and
// pins_check() re-reads the hardware registers to prove it. The UART pins stay plain inputs until the
// UART stage. Status goes to the USB console.
//
// No address or password is built in. The PC sends "TAP1 <call> <mic>" to UDP port 5006, by broadcast until
// it knows this board, and repeats it. The streams then go to the sender's address, ports 5004 (call) and
// 5005 (mic), until no request came for LEASE_MS or "TAP0" arrives. <call> and <mic> pick the slots: L, R or
// LR. Each request is answered on its source port with one line of JSON counters.
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

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "driver/spi_master.h"
#include "esp_random.h"
#include "esp_check.h"
#include "esp_eth.h"
#include "esp_event.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "soc/gpio_periph.h"
#include "soc/gpio_reg.h"
#include "soc/io_mux_reg.h"

static const char *TAG = "s3eth";

#define PIN_I2S_BCK     GPIO_NUM_15
#define PIN_I2S_WS      GPIO_NUM_16
#define PIN_I2S_DSDIN   GPIO_NUM_17
#define PIN_I2S_ASDOUT  GPIO_NUM_18
#define PIN_UART_TX     GPIO_NUM_1     // reserved, input until the UART stage
#define PIN_UART_RX     GPIO_NUM_2     // reserved, input until the UART stage

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

static listener_t s_call = {.name = "dsdin", .stream = 0, .port = PORT_CALL, .slots = SLOT_LR};
static listener_t s_mic = {.name = "asdout", .stream = 1, .port = PORT_MIC, .slots = SLOT_LR};

static volatile uint32_t s_dest_ip;    // network order, 0 when nobody asked
static volatile int64_t s_lease_until_us;
static uint16_t s_boot_id;
static volatile bool s_pins_ok;

static volatile bool s_link_up;
static volatile bool s_eth_ok;
static char s_ip[16] = "-";

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
    uint32_t enabled = REG_READ(GPIO_ENABLE_REG);
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
    char text[64];
    char reply[420];
    for (;;) {
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        int n = recvfrom(sock, text, sizeof(text) - 1, 0, (struct sockaddr *)&from, &from_len);
        if (n <= 0) continue;
        text[n] = 0;
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

static void on_eth_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (id == ETHERNET_EVENT_CONNECTED) {
        s_link_up = true;
        ESP_LOGI(TAG, "ethernet link up");
    } else if (id == ETHERNET_EVENT_DISCONNECTED) {
        s_link_up = false;
        strcpy(s_ip, "-");
        ESP_LOGW(TAG, "ethernet link down");
    }
}

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    const ip_event_got_ip_t *e = data;
    snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&e->ip_info.ip));
    ESP_LOGI(TAG, "ethernet ip %s", s_ip);
}

static esp_err_t ethernet_init(void)
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

static void report(listener_t *l, uint32_t dt_ms)
{
    uint32_t frames = l->frames;
    l->frames = 0;
    int peak_l = l->peak_l, peak_r = l->peak_r;
    l->peak_l = 0;
    l->peak_r = 0;
    printf(" %s rate=%" PRIu32 " peak=%d/%d", l->name, frames * 1000 / dt_ms, peak_l, peak_r);
}

void app_main(void)
{
    pins_to_safe_inputs();
    s_boot_id = (uint16_t)esp_random();

    esp_err_t a = listener_init(&s_call, I2S_NUM_0, PIN_I2S_DSDIN);
    esp_err_t b = listener_init(&s_mic, I2S_NUM_1, PIN_I2S_ASDOUT);
    if (a == ESP_OK) xTaskCreatePinnedToCore(listener_task, "i2s_call", 4096, &s_call, 10, NULL, 1);
    if (b == ESP_OK) xTaskCreatePinnedToCore(listener_task, "i2s_mic", 4096, &s_mic, 10, NULL, 1);

    esp_err_t e = ethernet_init();
    s_eth_ok = (e == ESP_OK);
    if (!s_eth_ok) ESP_LOGE(TAG, "ethernet init failed: %s", esp_err_to_name(e));

    s_pins_ok = pins_check();
    if (s_eth_ok) xTaskCreatePinnedToCore(request_task, "tap_req", 4096, NULL, 5, NULL, 0);

    printf("s3eth stage2 i2s0=%s i2s1=%s eth=%s pins_ok=%d\n", esp_err_to_name(a), esp_err_to_name(b), esp_err_to_name(e),
           s_pins_ok);

    int64_t last = esp_timer_get_time();
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(STATUS_MS));
        int64_t now = esp_timer_get_time();
        uint32_t dt_ms = (uint32_t)((now - last) / 1000);
        last = now;
        printf("stat up=%" PRIu32 " eth=%s link=%d ip=%s", (uint32_t)(now / 1000000), s_eth_ok ? "ok" : "fail",
               s_link_up, s_ip);
        report(&s_call, dt_ms);
        report(&s_mic, dt_ms);
        s_pins_ok = pins_check();
        bool streaming = s_dest_ip && now < s_lease_until_us;
        printf(" timeouts=%" PRIu32 "/%" PRIu32 " ovf=%" PRIu32 "/%" PRIu32 " send_fail=%" PRIu32 "/%" PRIu32
               " sent=%" PRIu32 "/%" PRIu32 " streaming=%d pins_ok=%d\n",
               s_call.timeouts, s_mic.timeouts, s_call.overflows, s_mic.overflows, s_call.send_fail, s_mic.send_fail,
               s_call.sent, s_mic.sent, streaming, s_pins_ok);
    }
}
