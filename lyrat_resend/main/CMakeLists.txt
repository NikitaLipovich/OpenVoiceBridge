idf_component_register(SRCS "main.c"
                    INCLUDE_DIRS "."
                    REQUIRES
                        nvs_flash
                        bt
                        esp_wifi
                        esp_netif
                        esp_event
                        lwip
                        driver
                        esp_timer
                        heap
)