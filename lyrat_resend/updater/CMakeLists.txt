# The updater of the LyraT (goal 096): lives in the factory partition, every start goes through it. Wi-Fi STA and the
# image over UDP 5007 (ota_link of ../main), the image over the control UART from the S3 as the second way, no Bluetooth,
# no sound. It starts the headset firmware (../, in ota_0) after a short window. docs/OTA.md "Updater".
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(lyrat_updater)
