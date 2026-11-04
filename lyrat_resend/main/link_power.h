// The board asks for sniff before the phone does, see link_power.c.
#pragma once
#include <stdbool.h>
#include <stdint.h>

void link_power_init(void);
void link_power_acl(bool up, uint16_t handle);
void link_power_slc(bool up);
void link_power_sco(bool open);
void link_power_mode(bool active);
