# U3 of variant D (goal 095): the relay, ESP32-S3-DevKitC-1U N16R16VA. docs: lyrat_resend/docs/PLATFORM_OTA.md
cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(u3_relay)
