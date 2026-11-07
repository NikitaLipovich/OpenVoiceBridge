idf_component_register(SRCS "main.c" "s3_ota.c" "s3_dbglog.c"
                            "../../lyrat_resend/main/ota_link.c" "../../lyrat_resend/main/ota_sha256.c"
                            "../../lyrat_resend/main/dbg_log.c"
                       PRIV_INCLUDE_DIRS "../../lyrat_resend/main"
                       REQUIRES app_update esp_app_format esp_driver_gpio esp_driver_i2s esp_driver_spi esp_driver_uart
                                esp_eth esp_event esp_netif esp_partition esp_timer log lwip nvs_flash)
