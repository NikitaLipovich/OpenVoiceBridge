#!/usr/bin/env python3
"""
patch_f4.py — ручка F4: плата сама инициирует HFP SLC к сохранённым пирам.

Корень проблемы G1: Windows в роли AG никогда не подключается к гарнитуре сам —
он ждёт, что гарнитура переподключится (так делают все реальные наушники).
Прошивка же была чистым пассивным HF unit: esp_hf_client_init() и ожидание,
без единого esp_hf_client_connect(). Никто не инициирует -> дедлок,
endpoint в Windows вечно UNPLUGGED, SCO=idle.

Патч держит MRU-список из двух последних пиров (телефон + ПК) в NVS и по очереди
пробует к ним подключиться, пока SLC не поднят. Уже установленный линк не трогает.
"""
from __future__ import annotations

from pathlib import Path

MARKER = "hf_reconnect_task"

# --- блок состояния + NVS + задача реконнекта -------------------------------
STATE_BLOCK = r'''

// --- F4 (harness): самостоятельный реконнект HFP -------------------------
// Windows-AG не подключается к гарнитуре сам — он ждёт инициативы от гарнитуры.
// Держим до HF_MAX_PEERS последних пиров (телефон + ПК) и по очереди пробуем
// подключиться, пока SLC не поднят. Уже живой линк не перебиваем.
#define HF_MAX_PEERS 2

static esp_bd_addr_t s_peers[HF_MAX_PEERS];
static uint8_t       s_peer_cnt = 0;
static volatile bool s_slc_up   = false;

static void hf_peers_save(void)
{
    nvs_handle_t h;
    if (nvs_open("lyrat", NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u8(h, "peer_cnt", s_peer_cnt);
    nvs_set_blob(h, "peers", s_peers, sizeof(s_peers));
    nvs_commit(h);
    nvs_close(h);
}

static void hf_peers_load(void)
{
    nvs_handle_t h;
    if (nvs_open("lyrat", NVS_READONLY, &h) != ESP_OK) return;
    uint8_t cnt = 0;
    size_t  len = sizeof(s_peers);
    if (nvs_get_u8(h, "peer_cnt", &cnt) == ESP_OK &&
        nvs_get_blob(h, "peers", s_peers, &len) == ESP_OK) {
        s_peer_cnt = (cnt > HF_MAX_PEERS) ? HF_MAX_PEERS : cnt;
    }
    nvs_close(h);

    // Первый запуск после патча: NVS пуст, но бонды в стеке уже есть —
    // засеиваем список пиров из bond list, иначе реконнектить будет некуда.
    if (s_peer_cnt == 0) {
        int num = esp_bt_gap_get_bond_device_num();
        if (num > 0) {
            esp_bd_addr_t *list = calloc((size_t)num, sizeof(esp_bd_addr_t));
            if (list && esp_bt_gap_get_bond_device_list(&num, list) == ESP_OK) {
                for (int i = 0; i < num && s_peer_cnt < HF_MAX_PEERS; i++) {
                    memcpy(s_peers[s_peer_cnt++], list[i], ESP_BD_ADDR_LEN);
                }
                hf_peers_save();
                ESP_LOGI(TAG_BT, "seeded %u peer(s) from bond list", s_peer_cnt);
            }
            free(list);
        }
    }

    for (uint8_t i = 0; i < s_peer_cnt; i++) {
        ESP_LOGI(TAG_BT, "peer[%u] = %02x:%02x:%02x:%02x:%02x:%02x", i,
                 s_peers[i][0], s_peers[i][1], s_peers[i][2],
                 s_peers[i][3], s_peers[i][4], s_peers[i][5]);
    }
}

// MRU: свежий пир — в голову списка, дубликаты схлопываем
static void hf_peer_add(const esp_bd_addr_t bda)
{
    esp_bd_addr_t tmp[HF_MAX_PEERS];
    uint8_t n = 0;
    memcpy(tmp[n++], bda, ESP_BD_ADDR_LEN);
    for (uint8_t i = 0; i < s_peer_cnt && n < HF_MAX_PEERS; i++) {
        if (memcmp(s_peers[i], bda, ESP_BD_ADDR_LEN) == 0) continue;
        memcpy(tmp[n++], s_peers[i], ESP_BD_ADDR_LEN);
    }
    memcpy(s_peers, tmp, sizeof(esp_bd_addr_t) * n);
    s_peer_cnt = n;
    hf_peers_save();
}

static void hf_reconnect_task(void *arg)
{
    (void)arg;
    uint32_t idx = 0;
    vTaskDelay(pdMS_TO_TICKS(4000));          // дать стеку подняться
    for (;;) {
        if (!s_slc_up && s_peer_cnt > 0) {
            uint8_t i = (uint8_t)(idx % s_peer_cnt);
            ESP_LOGI(TAG_BT, "HFP connect try peer[%u] %02x:%02x:%02x:%02x:%02x:%02x",
                     i, s_peers[i][0], s_peers[i][1], s_peers[i][2],
                     s_peers[i][3], s_peers[i][4], s_peers[i][5]);
            esp_hf_client_connect(s_peers[i]);
            idx++;
        }
        vTaskDelay(pdMS_TO_TICKS(8000));
    }
}
'''

# --- якоря и вставки --------------------------------------------------------
A_STATE = "static volatile bool s_sco_active = false;"

A_SLC_UP = '            ESP_LOGI(TAG_BT, "*** SLC CONNECTED ***");'
I_SLC_UP = r'''            s_slc_up = true;
            hf_peer_add(param->conn_stat.remote_bda);
'''

A_CONN_LOG = '        ESP_LOGI(TAG_BT, "HFP conn state=%d", param->conn_stat.state);'
I_CONN_LOG = r'''
        if (param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED)
            s_slc_up = false;'''

A_SCAN = "    esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);"
I_SCAN = r'''
    hf_peers_load();
    xTaskCreatePinnedToCore(hf_reconnect_task, "hf_recon", 3072, NULL, 6, NULL, 0);'''

A_STATUS = '    ESP_LOGI(TAG_CMD, "  SCO     = %s", s_sco_active ? "active" : "idle");'
I_STATUS = r'''
    ESP_LOGI(TAG_CMD, "  SLC     = %s", s_slc_up ? "up" : "down");
    for (uint8_t i = 0; i < s_peer_cnt; i++)
        ESP_LOGI(TAG_CMD, "  peer[%u] = %02x:%02x:%02x:%02x:%02x:%02x", i,
                 s_peers[i][0], s_peers[i][1], s_peers[i][2],
                 s_peers[i][3], s_peers[i][4], s_peers[i][5]);'''

ANCHORS = {
    "state": A_STATE,
    "slc_up": A_SLC_UP,
    "conn_log": A_CONN_LOG,
    "scan_mode": A_SCAN,
    "status": A_STATUS,
}


def apply(main_c: Path, dry_run: bool, backup) -> str:
    text = main_c.read_text(encoding="utf-8", errors="replace")
    if MARKER in text:
        return "уже применено"

    missing = [k for k, v in ANCHORS.items() if v not in text]
    if missing:
        return "ЯКОРЯ НЕ НАЙДЕНЫ: %s — патч F4 надо накатить вручную" % missing
    if dry_run:
        return ("dry-run: +hf_reconnect_task (esp_hf_client_connect по очереди к 2 пирам "
                "каждые 8 с, пока нет SLC), +MRU-список пиров в NVS, +SLC/peers в консольном 's'")

    backup(main_c)
    text = text.replace(A_STATE, A_STATE + STATE_BLOCK, 1)
    text = text.replace(A_SLC_UP, I_SLC_UP + A_SLC_UP, 1)
    text = text.replace(A_CONN_LOG, A_CONN_LOG + I_CONN_LOG, 1)
    text = text.replace(A_SCAN, A_SCAN + I_SCAN, 1)
    text = text.replace(A_STATUS, A_STATUS + I_STATUS, 1)
    main_c.write_text(text, encoding="utf-8")
    return "патч F4 применён (2 пира, MRU в NVS)"
