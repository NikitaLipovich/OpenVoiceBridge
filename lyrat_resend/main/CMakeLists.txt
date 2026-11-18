idf_component_register(SRCS "voice_ns.c" "main.c" "aec.c" "spectral_ns.c" "msbc_codec.c" "plc.c" "link_power.c" "voice_ns.c"
                         "ota_link.c" "ota_sha256.c" "dbg_log.c" "lyrat_ota.c" "lyrat_dbglog.c" "upd_mode.c" "hf_mode.c"
                    INCLUDE_DIRS "."
                    REQUIRES
                        nvs_flash
                        bt
                        driver
                        esp_timer
                        heap
                        app_update
                        esp_app_format
                        log
)

# The SBC code is compiled inside the bt component as long as A2DP keeps its internal codec. Its headers are private
# there, and the encoder's structure depends on the stack's configuration, so they come from the same place.
idf_component_get_property(bt_dir bt COMPONENT_DIR)
set_source_files_properties(msbc_codec.c PROPERTIES INCLUDE_DIRECTORIES
    "${bt_dir}/host/bluedroid/external/sbc/decoder/include;${bt_dir}/host/bluedroid/external/sbc/encoder/include;${bt_dir}/host/bluedroid/common/include;${bt_dir}/host/bluedroid/stack/include;${bt_dir}/common/include;${bt_dir}/common/osi/include;${bt_dir}/include/esp32/include")

# Goal 102: the build "pc" starts in the PC mode while NVS holds no mode (idf.py -D LYRAT_DEFAULT_MODE=pc); the
# default build starts in the phone mode. fw.py --variant pc does the same through bench_variant.h.
if(DEFINED LYRAT_DEFAULT_MODE AND LYRAT_DEFAULT_MODE STREQUAL "pc")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE BENCH_DEFAULT_MODE_PC=1)
endif()
