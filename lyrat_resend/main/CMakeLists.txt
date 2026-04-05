idf_component_register(SRCS "main.c" "aec.c" "spectral_ns.c" "ulaw.c"
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
                        esp_coex
)