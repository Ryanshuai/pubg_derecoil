#ifndef _TUSB_CONFIG_H_
#define _TUSB_CONFIG_H_

#ifdef __cplusplus
extern "C" {
#endif

#define CFG_TUSB_RHPORT0_MODE (OPT_MODE_DEVICE | OPT_MODE_FULL_SPEED)
#define BOARD_TUH_RHPORT      1
#define CFG_TUSB_RHPORT1_MODE (OPT_MODE_HOST | OPT_MODE_FULL_SPEED)

#define CFG_TUSB_MEM_SECTION
#define CFG_TUSB_MEM_ALIGN    __attribute__((aligned(4)))

#define CFG_TUD_ENDPOINT0_SIZE 64

#define CFG_TUD_CDC     1
/* Two HID instances: 0 = mouse (passthrough + recoil), 1 = keyboard.
 * The keyboard exists so the PC can drive reload (R) during automated
 * training-range calibration, which a mouse-only device cannot do. */
#define CFG_TUD_HID     2
#define CFG_TUD_MSC     0
#define CFG_TUD_MIDI    0
#define CFG_TUD_VENDOR  0

#define CFG_TUD_CDC_RX_BUFSIZE  256
#define CFG_TUD_CDC_TX_BUFSIZE  256
#define CFG_TUD_CDC_EP_BUFSIZE  64
#define CFG_TUD_HID_EP_BUFSIZE  16

#define CFG_TUH_ENUMERATION_BUFSIZE 512
#define CFG_TUH_HUB                 0
#define CFG_TUH_DEVICE_MAX          1
#define CFG_TUH_HID                 4
#define CFG_TUH_HID_EPIN_BUFSIZE    64
#define CFG_TUH_HID_EPOUT_BUFSIZE   64

#define CFG_TUH_RPI_PIO_USB 1

#ifdef __cplusplus
}
#endif

#endif /* _TUSB_CONFIG_H_ */
