// The board answers the phone's request for an eSCO link with the parameters of the stack's table (HFP T2 for mSBC:
// 2-EV3, 13 ms, retransmission for quality). The board is the central of the link, so its link manager settles the
// interval and the retransmission window from that answer. Goal 100 needs to try other answers without changing
// ESP-IDF: the linker wraps BTM_EScoConnRsp (called from bta_hf_client_sco.c) and the two places where the controller
// reports what was settled (called from btu_hcif.c). See main/CMakeLists.txt.
#include "esco_ctl.h"
#include <string.h>
#include "esp_log.h"

static const char *TAG = "BT";

// Mirrors of stack/btm_api.h of ESP-IDF v5.5 (tBTM_ESCO_PARAMS, tBTM_ESCO_DATA); the sizes are checked below.
typedef struct {
    uint32_t tx_bw;
    uint32_t rx_bw;
    uint16_t max_latency;
    uint16_t voice_contfmt;
    uint16_t packet_types;
    uint8_t  retrans_effort;
} esco_params_t;

typedef struct {
    uint16_t rx_pkt_len;
    uint16_t tx_pkt_len;
    uint8_t  bd_addr[6];
    uint8_t  link_type;
    uint8_t  tx_interval;
    uint8_t  retrans_window;
    uint8_t  air_mode;
} esco_data_t;

_Static_assert(sizeof(esco_params_t) == 16, "tBTM_ESCO_PARAMS layout");
_Static_assert(sizeof(esco_data_t) == 14, "tBTM_ESCO_DATA layout");

void __real_BTM_EScoConnRsp(uint16_t sco_inx, uint8_t hci_status, esco_params_t *p_parms);
void __real_btm_sco_connected(uint8_t hci_status, uint8_t *bda, uint16_t hci_handle, esco_data_t *p_esco_data);
void __real_btm_esco_proc_conn_chg(uint8_t status, uint16_t handle, uint8_t tx_interval, uint8_t retrans_window,
                                   uint16_t rx_pkt_len, uint16_t tx_pkt_len);

static volatile uint16_t s_pkt, s_lat;
static volatile uint8_t  s_rtx;
static esco_link_t       s_last;

void esco_ctl_set(uint16_t pkt, uint16_t latency_ms, uint8_t retrans_effort)
{
    s_pkt = 0;              // never half of a setting in an answer
    s_lat = latency_ms;
    s_rtx = retrans_effort;
    s_pkt = pkt;
}

bool esco_ctl_get(uint16_t *pkt, uint16_t *latency_ms, uint8_t *retrans_effort)
{
    *pkt = s_pkt;
    *latency_ms = s_lat;
    *retrans_effort = s_rtx;
    return s_pkt != 0;
}

void esco_ctl_last(esco_link_t *out)
{
    *out = s_last;
}

void __wrap_BTM_EScoConnRsp(uint16_t sco_inx, uint8_t hci_status, esco_params_t *p_parms)
{
    if (p_parms && hci_status == 0) {
        uint16_t pkt = s_pkt;
        if (pkt) {
            p_parms->packet_types = pkt;
            p_parms->max_latency = s_lat;
            p_parms->retrans_effort = s_rtx;
        }
        s_last.asked_pkt = p_parms->packet_types;
        s_last.asked_lat = p_parms->max_latency;
        s_last.asked_rtx = p_parms->retrans_effort;
        ESP_LOGI(TAG, "ESCO answer mode=%s pkt=0x%04x lat=%u rtx=%u fmt=0x%04x", pkt ? "set" : "stack",
                 p_parms->packet_types, p_parms->max_latency, p_parms->retrans_effort, p_parms->voice_contfmt);
    }
    __real_BTM_EScoConnRsp(sco_inx, hci_status, p_parms);
}

void __wrap_btm_sco_connected(uint8_t hci_status, uint8_t *bda, uint16_t hci_handle, esco_data_t *p)
{
    if (p) {
        s_last.valid = 1;
        s_last.status = hci_status;
        s_last.link_type = p->link_type;
        s_last.tx_interval = p->tx_interval;
        s_last.retrans_window = p->retrans_window;
        s_last.air_mode = p->air_mode;
        s_last.rx_len = p->rx_pkt_len;
        s_last.tx_len = p->tx_pkt_len;
        s_last.links++;
        ESP_LOGI(TAG, "ESCO up st=%u link=%u tesco=%u wesco=%u rx=%u tx=%u air=%u asked=0x%04x/%u/%u", hci_status,
                 p->link_type, p->tx_interval, p->retrans_window, p->rx_pkt_len, p->tx_pkt_len, p->air_mode,
                 s_last.asked_pkt, s_last.asked_lat, s_last.asked_rtx);
    }
    __real_btm_sco_connected(hci_status, bda, hci_handle, p);
}

void __wrap_btm_esco_proc_conn_chg(uint8_t status, uint16_t handle, uint8_t tx_interval, uint8_t retrans_window,
                                   uint16_t rx_pkt_len, uint16_t tx_pkt_len)
{
    s_last.changes++;
    if (status == 0 && tx_interval) {
        s_last.tx_interval = tx_interval;
        s_last.retrans_window = retrans_window;
        s_last.rx_len = rx_pkt_len;
        s_last.tx_len = tx_pkt_len;
    }
    ESP_LOGI(TAG, "ESCO chg st=%u tesco=%u wesco=%u rx=%u tx=%u", status, tx_interval, retrans_window, rx_pkt_len,
             tx_pkt_len);
    __real_btm_esco_proc_conn_chg(status, handle, tx_interval, retrans_window, rx_pkt_len, tx_pkt_len);
}
