idf_component_register(SRCS "main.c" "aec.c" "spectral_ns.c"
                    INCLUDE_DIRS "."
                    REQUIRES
                        nvs_flash
                        bt
                        driver
                        esp_timer
                        heap
)