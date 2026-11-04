idf_component_register(SRCS "main.c" "aec.c" "spectral_ns.c" "msbc_codec.c" "plc.c" "link_power.c"
                    INCLUDE_DIRS "."
                    REQUIRES
                        nvs_flash
                        bt
                        driver
                        esp_timer
                        heap
)

# The SBC code is compiled inside the bt component as long as A2DP keeps its internal codec. Its headers are private
# there, and the encoder's structure depends on the stack's configuration, so they come from the same place.
idf_component_get_property(bt_dir bt COMPONENT_DIR)
set_source_files_properties(msbc_codec.c PROPERTIES INCLUDE_DIRECTORIES
    "${bt_dir}/host/bluedroid/external/sbc/decoder/include;${bt_dir}/host/bluedroid/external/sbc/encoder/include;${bt_dir}/host/bluedroid/common/include;${bt_dir}/host/bluedroid/stack/include;${bt_dir}/common/include;${bt_dir}/common/osi/include;${bt_dir}/include/esp32/include")
