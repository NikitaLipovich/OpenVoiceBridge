# ESP32 node (U1, U2) link to the relay U3, goal 095.
idf_component_register(SRCS "node_link.c"
                       INCLUDE_DIRS "include"
                       REQUIRES plat_core esp_driver_uart app_update esp_app_format esp_partition esp_timer log nvs_flash)
