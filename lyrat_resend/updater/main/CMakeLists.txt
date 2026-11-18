# Shared sources of the headset firmware: the image protocol, the log ring, the UART receiver, the rules of upd_mode.
set(SHARED ${CMAKE_CURRENT_LIST_DIR}/../../main)
idf_component_register(SRCS "updater_main.c" "upd_net.c" "lyrat_token.c"
                            "${SHARED}/ota_link.c" "${SHARED}/ota_sha256.c" "${SHARED}/dbg_log.c" "${SHARED}/lyrat_ota.c"
                            "${SHARED}/lyrat_dbglog.c" "${SHARED}/upd_mode.c"
                    INCLUDE_DIRS "." "${SHARED}"
                    REQUIRES nvs_flash esp_wifi esp_netif esp_event lwip driver esp_timer app_update esp_app_format log
                             esp_partition esp_driver_uart)
# The bench's one OTA token lives with the S3 (s3_eth_bridge/main/ota_secrets.h, ignored by git): only this file sees it.
set_source_files_properties(lyrat_token.c PROPERTIES INCLUDE_DIRECTORIES
    "${CMAKE_CURRENT_LIST_DIR}/../../../s3_eth_bridge/main")
