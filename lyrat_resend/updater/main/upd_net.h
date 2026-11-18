// Network part of the LyraT updater: Wi-Fi STA and the image over UDP 5007 (upd_net.c). Goal 096.
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "ota_link.h"

void upd_net_start(bool wrong_pass);            // Wi-Fi and the receiver task; without Wi-Fi the rest goes on
const ota_rx_t *upd_net_rx(void);
size_t upd_net_text(char *out, size_t cap);     // "wifi=up|connecting|fail:<reason>|off ip=a.b.c.d port=5007"
