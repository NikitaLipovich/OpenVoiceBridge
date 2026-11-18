// The OTA token of the bench, for the update mode over Wi-Fi (goal 096). One token for the whole bench: it lives in
// s3_eth_bridge/main/ota_secrets.h (git ignores it, first_flash.py makes it), and ota.py reads the same file. CMake gives
// this one file that folder as an include path, so no other file of this firmware sees the S3's other defines.
// Without the file the token is empty and every "upd" and every BEGIN over Wi-Fi is refused.
#include "lyrat_link.h"

#if __has_include("ota_secrets.h")
#include "ota_secrets.h"
#endif
#ifndef OTA_TOKEN
#define OTA_TOKEN ""
#endif

const char *lyrat_ota_token(void) { return OTA_TOKEN; }
