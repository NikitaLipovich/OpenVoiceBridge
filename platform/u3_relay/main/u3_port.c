// UART0 of U3: the console of U1 or U2, and their ROM loader (goal 095, docs/PLATFORM_OTA.md, DECISIONS D2-D4).
//
// One UART for two nodes: the GPIO matrix moves U0RXD between F1TX (GPIO40) and F2TX (GPIO42). The console of U3 itself
// is on USB-Serial-JTAG. Normally only RX is connected: the lines a node prints (boot, panic, anything before its
// control UART) go to the PC as board 0x83 / 0x84. U0TXD reaches a node's RX0 only while U3 flashes it through the ROM
// loader; otherwise GPIO39 and GPIO41 are inputs, R31/R32 hold the node's RX0 high.
//
// EN and IO0 of both nodes are open drain: the pin is an input (high impedance) unless pulled low on purpose. Nothing
// here ever drives them high. At reset of U3 all four are inputs anyway, so U3 restarting never resets U1 or U2.
#include <stdio.h>
#include <string.h>

#include "dbg_log.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "plat_pins.h"
#include "plat_proto.h"
#include "u3_link.h"

static const char *TAG = "u3port";

#define PORT_UART       UART_NUM_0
#define PORT_RX_RING    4096
#define LINE_FLUSH_MS   200         // a prompt without newline goes out after this
#define POLL_MS         20

typedef struct {
    int tx, rx, en, boot;
} node_pins_t;

static const node_pins_t s_pins[U3_NODES] = {
    {U3_PIN_F1_TX, U3_PIN_F1_RX, U3_PIN_EN1, U3_PIN_BOOT1},
    {U3_PIN_F2_TX, U3_PIN_F2_RX, U3_PIN_EN2, U3_PIN_BOOT2},
};

static SemaphoreHandle_t s_lock;            // held by the console task while it reads, and by a ROM job throughout
static volatile int s_mode = U3_CON_AUTO;
static volatile int s_listen = -1;          // node whose TX0 is on U0RXD now
static volatile int s_rom_node = -1;
static int s_follow = -1;
static int64_t s_follow_until;
static bool s_was_alive[U3_NODES];
static uint32_t s_lines[U3_NODES];
static bool s_ok;

// ---------------------------------------------------------------------------------------------------------------------
// Open drain

static void od_init(int pin)
{
    gpio_config_t c = {.pin_bit_mask = 1ULL << pin, .mode = GPIO_MODE_INPUT, .pull_up_en = 0, .pull_down_en = 0,
                       .intr_type = GPIO_INTR_DISABLE};
    gpio_config(&c);
    gpio_set_level(pin, 0);                 // the output latch holds 0: enabling the output can only pull low
}

static void od_set(int pin, int low)
{
    if (low) {
        gpio_set_level(pin, 0);
        gpio_set_direction(pin, GPIO_MODE_OUTPUT);
    } else {
        gpio_set_direction(pin, GPIO_MODE_INPUT);
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// Console lines

typedef struct {
    char text[DBG_TEXT_MAX];
    size_t len;
    int64_t since_us;
} line_t;

static line_t s_line[U3_NODES];

static void line_flush(int node)
{
    line_t *l = &s_line[node];
    if (!l->len) return;
    u3_log_console(node, l->text, l->len);
    s_lines[node]++;
    l->len = 0;
}

static void line_feed(int node, const uint8_t *data, int n)
{
    line_t *l = &s_line[node];
    for (int i = 0; i < n; i++) {
        uint8_t c = data[i];
        if (c == '\n') {
            line_flush(node);
            continue;
        }
        if (c == '\r') continue;
        if (!l->len) l->since_us = esp_timer_get_time();
        l->text[l->len++] = (c >= 0x20 && c < 0x7F) || c == '\t' ? (char)c : '.';
        if (l->len >= sizeof(l->text) - 1) line_flush(node);
    }
}

static void listen_to(int node)
{
    if (node == s_listen) return;
    if (s_listen >= 0) line_flush(s_listen);
    if (node >= 0) {
        uart_set_pin(PORT_UART, UART_PIN_NO_CHANGE, s_pins[node].rx, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
        uart_flush_input(PORT_UART);
    }
    s_listen = node;
    ESP_LOGI(TAG, "console: %s", node >= 0 ? u3_node_name(node) : "off");
}

// Which node the console should read now (mode auto): U1, unless a node whose control link was alive went silent
// lately; then that one for PLAT_CON_FOLLOW_MS, to catch its panic and its start.
static int auto_choice(void)
{
    int64_t now = esp_timer_get_time();
    for (int i = 0; i < U3_NODES; i++) {
        bool alive = u3_node_alive(i);
        if (s_was_alive[i] && !alive && !u3_ota_rom_running(i)) {
            s_follow = i;
            s_follow_until = now + PLAT_CON_FOLLOW_MS * 1000LL;
        }
        s_was_alive[i] = alive;
    }
    if (s_follow >= 0 && now < s_follow_until) return s_follow;
    s_follow = -1;
    return 0;
}

static void console_task(void *arg)
{
    static uint8_t chunk[128];
    for (;;) {
        int want = s_mode == U3_CON_U1 ? 0 : s_mode == U3_CON_U2 ? 1 : s_mode == U3_CON_OFF ? -1 : auto_choice();
        if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(POLL_MS)) != pdTRUE) continue;     // a ROM job owns the port
        listen_to(want);
        int got = uart_read_bytes(PORT_UART, chunk, sizeof(chunk), pdMS_TO_TICKS(POLL_MS));
        if (got > 0 && s_listen >= 0) line_feed(s_listen, chunk, got);
        if (s_listen >= 0 && s_line[s_listen].len &&
            esp_timer_get_time() - s_line[s_listen].since_us > LINE_FLUSH_MS * 1000LL) {
            line_flush(s_listen);
        }
        xSemaphoreGive(s_lock);
        if (got <= 0) vTaskDelay(1);
    }
}

void u3_port_init(void)
{
    for (int i = 0; i < U3_NODES; i++) {
        od_init(s_pins[i].en);
        od_init(s_pins[i].boot);
        gpio_set_direction(s_pins[i].tx, GPIO_MODE_INPUT);
    }
    uart_config_t cfg = {
        .baud_rate = ROM_BAUD_DEFAULT,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    s_lock = xSemaphoreCreateMutex();
    esp_err_t err = s_lock ? uart_driver_install(PORT_UART, PORT_RX_RING, 0, 0, NULL, 0) : ESP_ERR_NO_MEM;
    if (err == ESP_OK) err = uart_param_config(PORT_UART, &cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "uart0: %s, no console and no ROM loader of the nodes", esp_err_to_name(err));
        return;
    }
    s_ok = true;
    xTaskCreatePinnedToCore(console_task, "console", 3072, NULL, 3, NULL, 0);
}

void u3_port_set_mode(int mode)
{
    if (mode >= U3_CON_AUTO && mode <= U3_CON_OFF) s_mode = mode;
}

int u3_port_mode(void) { return s_mode; }
int u3_port_listening(void) { return s_rom_node >= 0 ? -1 : s_listen; }
uint32_t u3_port_con_lines(int node) { return node >= 0 && node < U3_NODES ? s_lines[node] : 0; }

const char *u3_port_mode_name(int mode)
{
    static const char *const names[] = {"auto", "u1", "u2", "off"};
    return mode >= 0 && mode <= U3_CON_OFF ? names[mode] : "?";
}

// ---------------------------------------------------------------------------------------------------------------------
// Port of the ROM flasher

typedef struct {
    int node;
    int (*read_bundle)(void *ctx, uint32_t at, uint8_t *buf, size_t len);
    void *bundle_ctx;
} rom_ctx_t;

static rom_ctx_t s_rom_ctx;

static int rp_write(void *ctx, const uint8_t *data, size_t len)
{
    if (uart_write_bytes(PORT_UART, data, len) != (int)len) return -1;
    return uart_wait_tx_done(PORT_UART, pdMS_TO_TICKS(1000)) == ESP_OK ? 0 : -1;
}

static size_t rp_read(void *ctx, uint8_t *buf, size_t cap, uint32_t timeout_ms)
{
    int n = uart_read_bytes(PORT_UART, buf, cap, pdMS_TO_TICKS(timeout_ms ? timeout_ms : 1));
    return n > 0 ? (size_t)n : 0;
}

static void rp_flush(void *ctx) { uart_flush_input(PORT_UART); }

static int rp_baud(void *ctx, uint32_t baud) { return uart_set_baudrate(PORT_UART, baud) == ESP_OK ? 0 : -1; }

static void rp_pins(void *ctx, int en_low, int boot_low)
{
    const node_pins_t *p = &s_pins[((rom_ctx_t *)ctx)->node];
    // IO0, then EN, in each call; rom_flasher.c makes the order of the edges with separate calls.
    od_set(p->boot, boot_low);
    od_set(p->en, en_low);
}

static void rp_delay(void *ctx, uint32_t ms) { vTaskDelay(pdMS_TO_TICKS(ms ? ms : 1)); }
static uint32_t rp_now(void *ctx) { return (uint32_t)(esp_timer_get_time() / 1000); }

static int rp_bundle(void *ctx, uint32_t at, uint8_t *buf, size_t len)
{
    rom_ctx_t *c = (rom_ctx_t *)ctx;
    return c->read_bundle(c->bundle_ctx, at, buf, len);
}

static const rom_port_t s_rom_port = {
    .ctx = &s_rom_ctx, .write = rp_write, .read = rp_read, .flush_input = rp_flush, .set_baud = rp_baud,
    .pins = rp_pins, .delay = rp_delay, .now_ms = rp_now, .read_bundle = rp_bundle,
};

const rom_port_t *u3_port_rom_take(int node, int (*read_bundle)(void *ctx, uint32_t at, uint8_t *buf, size_t len),
                                   void *bundle_ctx)
{
    if (!s_ok || node < 0 || node >= U3_NODES) return NULL;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (s_listen >= 0) line_flush(s_listen);
    s_rom_node = node;
    s_rom_ctx.node = node;
    s_rom_ctx.read_bundle = read_bundle;
    s_rom_ctx.bundle_ctx = bundle_ctx;
    uart_set_baudrate(PORT_UART, ROM_BAUD_DEFAULT);
    uart_set_pin(PORT_UART, s_pins[node].tx, s_pins[node].rx, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    s_listen = node;
    uart_flush_input(PORT_UART);
    ESP_LOGW(TAG, "%s: port taken for the ROM loader", u3_node_name(node));
    return &s_rom_port;
}

void u3_port_rom_release(void)
{
    int node = s_rom_node;
    if (node < 0) return;
    uart_wait_tx_done(PORT_UART, pdMS_TO_TICKS(200));
    gpio_set_direction(s_pins[node].tx, GPIO_MODE_INPUT);      // TX off the node's RX0 again
    od_set(s_pins[node].en, 0);
    od_set(s_pins[node].boot, 0);
    uart_set_baudrate(PORT_UART, ROM_BAUD_DEFAULT);
    uart_flush_input(PORT_UART);
    s_rom_node = -1;
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "%s: port back to the console", u3_node_name(node));
}
