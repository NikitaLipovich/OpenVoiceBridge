// U2 of variant D (goal 095): HFP AG to the Bluetooth headphones. The skeleton has the link to U3 only: Bluetooth, I2S and the
// R21 modes come in later goals.
//
// What it does now: every log line to UART0 (U3 reads it) and into a ring that U3 drains over the control UART;
// heartbeat to U3; new firmware from U3 with rollback (a new image confirms itself once U3 answers); the frame of the
// modes is answered "unsupported". docs: lyrat_resend/docs/PLATFORM_OTA.md.
#include <inttypes.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "node_link.h"
#include "nvs_flash.h"
#include "plat_pins.h"
#include "plat_proto.h"

static const char *TAG = "u2_ag";

static const node_cfg_t s_cfg = {
    .board = DBG_BOARD_U2,
    .project = PLAT_PROJECT_U2,
    .uart = UART_NUM_1,
    .pin_tx = U2_PIN_CTL_TX,
    .pin_rx = U2_PIN_CTL_RX,
};

void app_main(void)
{
    uint16_t boot = (uint16_t)esp_random();
    node_log_init(DBG_BOARD_U2, boot);     // from here every log line also waits in the ring for the PC
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "nvs: %s, erased", esp_err_to_name(err));
        nvs_flash_erase();
        nvs_flash_init();
    }
    const esp_app_desc_t *d = esp_app_get_description();
    ESP_LOGI(TAG, "U2 %s %s, boot %04x, reset %d", d->project_name, d->version, boot, (int)esp_reset_reason());
    if (strcmp(d->project_name, PLAT_PROJECT_U2) != 0) {
        ESP_LOGE(TAG, "project name %s, the relay expects %s", d->project_name, PLAT_PROJECT_U2);
    }
    node_start(&s_cfg);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(60000));
        ESP_LOGI(TAG, "up %" PRId64 " s, relay acks %" PRIu32, esp_timer_get_time() / 1000000, node_relay_acks());
    }
}
