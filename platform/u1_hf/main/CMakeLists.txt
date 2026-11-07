idf_component_register(SRCS "main.c"
                       REQUIRES plat_core plat_node esp_driver_uart esp_app_format esp_system esp_timer nvs_flash)
