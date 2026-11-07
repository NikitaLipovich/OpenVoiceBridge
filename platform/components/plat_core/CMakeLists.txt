# Plain C shared by U1, U2, U3 and the PC tests (goal 095). The image protocol, the log ring and the UART frame are the
# files of goal 090 in lyrat_resend/main: one source for both directions of the project.
set(L "${CMAKE_CURRENT_LIST_DIR}/../../../lyrat_resend/main")
idf_component_register(SRCS "rom_flasher.c" "rom_bundle.c" "${L}/ota_link.c" "${L}/ota_sha256.c" "${L}/dbg_log.c"
                       INCLUDE_DIRS "include" "${L}"
                       REQUIRES log esp_timer freertos)
