idf_component_register(SRCS "main.c"
                       PRIV_INCLUDE_DIRS "../../lyrat_resend/main"
                       REQUIRES esp_driver_gpio esp_driver_i2s esp_driver_spi esp_driver_uart esp_eth esp_event esp_netif esp_timer lwip)
