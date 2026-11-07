# U2 of variant D (goal 095): HFP AG to the Bluetooth headphones (skeleton: link, OTA, log). docs: lyrat_resend/docs/PLATFORM_OTA.md
cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(u2_ag)
