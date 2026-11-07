idf_component_register(SRCS "main.c" "u3_port.c" "u3_ota.c" "u3_log.c"
                       REQUIRES plat_core app_update esp_app_format esp_driver_gpio esp_driver_uart esp_event esp_netif
                                esp_partition esp_timer esp_wifi log lwip nvs_flash)
