#pragma once
#include <stdlib.h>
#define osi_malloc(n) malloc(n)
#define osi_calloc(n) calloc(1, n)
#define osi_free(p) free(p)
