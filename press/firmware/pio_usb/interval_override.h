#ifndef _INTERVAL_OVERRIDE_H_
#define _INTERVAL_OVERRIDE_H_

#include <stdint.h>

extern volatile uint8_t interval_override;

/* What the device enumerated with, sampled where the override replaces it.
 * See the assignment in pio_usb_host.c for why both numbers are needed. */
extern volatile uint8_t last_periodic_interval;
extern volatile uint8_t last_periodic_ep;

#endif
