// Host build of the Bluedroid SBC codec (build_msbc.cmd): only the switches the codec reads.
#pragma once
#include <stdint.h>
#include <stdbool.h>
#ifndef TRUE
#define TRUE 1
#endif
#ifndef FALSE
#define FALSE 0
#endif
#define SBC_DEC_INCLUDED TRUE
#define SBC_ENC_INCLUDED TRUE
#define PLC_INCLUDED TRUE
typedef uint8_t UINT8;
typedef uint16_t UINT16;
typedef uint32_t UINT32;
typedef int8_t INT8;
typedef int16_t INT16;
typedef int32_t INT32;
typedef uint8_t BOOLEAN;
#define APPL_TRACE_EVENT(...) ((void)0)
