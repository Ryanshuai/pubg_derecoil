#include "interval_override.h"

volatile uint8_t interval_override = 1;  /* Force 1ms polling (1000Hz) */

/* What the device actually enumerated with, sampled at the point the override
 * replaces it. See the comment at the assignment in pio_usb_host.c. */
volatile uint8_t last_periodic_interval = 0;
volatile uint8_t last_periodic_ep = 0;
