#include <string.h>
#include "tusb.h"

/* ================================================================
 *  Device Descriptor — CDC + HID composite
 * ================================================================ */
static const tusb_desc_device_t desc_device = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = 0x0200,
    .bDeviceClass       = TUSB_CLASS_MISC,
    .bDeviceSubClass    = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol    = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor           = 0xCAFE,
    .idProduct          = 0x4005,
    .bcdDevice          = 0x0200,
    .iManufacturer      = 0x01,
    .iProduct           = 0x02,
    .iSerialNumber      = 0x03,
    .bNumConfigurations = 0x01,
};

const uint8_t *tud_descriptor_device_cb(void) {
    return (const uint8_t *)&desc_device;
}

/* HID instance indices — must match the order of the HID interface
 * descriptors in desc_configuration below, since TinyUSB numbers its HID
 * instances by descriptor order. */
enum {
    ITF_HID_MOUSE = 0,
    ITF_HID_KEYBOARD,
    ITF_HID_COUNT,
};

/* ================================================================
 *  HID Report Descriptor — Mouse with 16-bit X/Y
 * ================================================================ */
static const uint8_t desc_hid_report[] = {
    0x05, 0x01,        // Usage Page (Generic Desktop)
    0x09, 0x02,        // Usage (Mouse)
    0xA1, 0x01,        // Collection (Application)
    0x09, 0x01,        //   Usage (Pointer)
    0xA1, 0x00,        //   Collection (Physical)
    /* 5 buttons */
    0x05, 0x09,        //     Usage Page (Button)
    0x19, 0x01,        //     Usage Minimum (1)
    0x29, 0x05,        //     Usage Maximum (5)
    0x15, 0x00,        //     Logical Minimum (0)
    0x25, 0x01,        //     Logical Maximum (1)
    0x95, 0x05,        //     Report Count (5)
    0x75, 0x01,        //     Report Size (1)
    0x81, 0x02,        //     Input (Data, Variable, Absolute)
    /* 3 bits padding */
    0x95, 0x01,        //     Report Count (1)
    0x75, 0x03,        //     Report Size (3)
    0x81, 0x01,        //     Input (Constant)
    /* X, Y — 16-bit relative */
    0x05, 0x01,        //     Usage Page (Generic Desktop)
    0x09, 0x30,        //     Usage (X)
    0x09, 0x31,        //     Usage (Y)
    0x16, 0x01, 0x80,  //     Logical Minimum (-32767)
    0x26, 0xFF, 0x7F,  //     Logical Maximum (32767)
    0x75, 0x10,        //     Report Size (16)
    0x95, 0x02,        //     Report Count (2)
    0x81, 0x06,        //     Input (Data, Variable, Relative)
    /* Wheel — 8-bit */
    0x09, 0x38,        //     Usage (Wheel)
    0x15, 0x81,        //     Logical Minimum (-127)
    0x25, 0x7F,        //     Logical Maximum (127)
    0x75, 0x08,        //     Report Size (8)
    0x95, 0x01,        //     Report Count (1)
    0x81, 0x06,        //     Input (Data, Variable, Relative)
    0xC0,              //   End Collection
    0xC0,              // End Collection
};

/* ================================================================
 *  HID Report Descriptor — boot keyboard (instance 1)
 *
 *  Needed only so the PC can send R (reload) during automated
 *  calibration; a mouse-only HID device cannot do that.
 * ================================================================ */
static const uint8_t desc_hid_kbd_report[] = {
    TUD_HID_REPORT_DESC_KEYBOARD()
};

const uint8_t *tud_hid_descriptor_report_cb(uint8_t instance) {
    return (instance == ITF_HID_KEYBOARD) ? desc_hid_kbd_report
                                          : desc_hid_report;
}

/* ================================================================
 *  Configuration Descriptor — CDC (2 itf) + HID (1 itf)
 * ================================================================ */
enum {
    ITF_NUM_CDC = 0,
    ITF_NUM_CDC_DATA,
    ITF_NUM_HID,        /* mouse    -> HID instance 0 */
    ITF_NUM_HID_KBD,    /* keyboard -> HID instance 1 */
    ITF_NUM_TOTAL,
};

#define EPNUM_CDC_NOTIF 0x81
#define EPNUM_CDC_OUT   0x02
#define EPNUM_CDC_IN    0x82
#define EPNUM_HID       0x83
#define EPNUM_HID_KBD   0x84

#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN \
                          + TUD_HID_DESC_LEN + TUD_HID_DESC_LEN)

static const uint8_t desc_configuration[] = {
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0x00, 100),
    TUD_CDC_DESCRIPTOR(ITF_NUM_CDC, 4, EPNUM_CDC_NOTIF, 8,
                       EPNUM_CDC_OUT, EPNUM_CDC_IN, 64),
    TUD_HID_DESCRIPTOR(ITF_NUM_HID, 5, HID_ITF_PROTOCOL_MOUSE,
                       sizeof(desc_hid_report), EPNUM_HID, 16, 1),
    /* 10 ms interval: reload keypresses need nothing like the mouse's 1 kHz,
     * and a slower interval costs the host less bandwidth. */
    TUD_HID_DESCRIPTOR(ITF_NUM_HID_KBD, 6, HID_ITF_PROTOCOL_KEYBOARD,
                       sizeof(desc_hid_kbd_report), EPNUM_HID_KBD, 8, 10),
};

const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
    (void)index;
    return desc_configuration;
}

/* ================================================================
 *  String Descriptors
 * ================================================================ */
static const char *string_desc_arr[] = {
    (const char[]){0x09, 0x04},
    "PicoMouse",
    "RP2350 HID Mouse",
    "000001",
    "Serial Port",
    "Mouse",
    "Keyboard",
};

static uint16_t _desc_str[32 + 1];

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;
    uint8_t chr_count;

    if (index == 0) {
        memcpy(&_desc_str[1], string_desc_arr[0], 2);
        chr_count = 1;
    } else {
        if (index >= sizeof(string_desc_arr) / sizeof(string_desc_arr[0]))
            return NULL;
        const char *str = string_desc_arr[index];
        chr_count = (uint8_t)strlen(str);
        if (chr_count > 31) chr_count = 31;
        for (uint8_t i = 0; i < chr_count; i++)
            _desc_str[1 + i] = str[i];
    }

    _desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * chr_count + 2));
    return _desc_str;
}
