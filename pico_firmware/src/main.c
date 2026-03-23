/*
 * Pico Mouse — USB passthrough + recoil compensation
 *
 * Core 0 (native USB-C → PC): TinyUSB Device (HID Mouse + CDC Serial)
 * Core 1 (PIO USB-A ← mouse): TinyUSB Host (reads mouse, sets Razer 1000Hz)
 *
 * Razer DeathAdder V3: sends set_polling_rate2 twice (arg=0x00, 0x01)
 * after mount. The command is accepted but may need OS-level bInterval
 * override to take full effect. interval_override=1 forces 1ms polling.
 */

#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "pico/time.h"
#include "pico/bootrom.h"
#include "hardware/clocks.h"
#include "pio_usb.h"
#include "tusb.h"

static inline uint32_t board_millis(void) {
    return to_ms_since_boot(get_absolute_time());
}

/* ── Protocol commands (PC → Pico via CDC) ─────────────── */
#define CMD_PATTERN_UPLOAD 0x10
#define CMD_PATTERN_CLEAR  0x11
#define CMD_RECOIL_ENABLE  0x12
#define CMD_MOVE           0x13
#define CMD_CLICK          0x14
#define CMD_MOVE_CLICK     0x15
#define CMD_AIM_MODE       0x16
#define CMD_SET_DELTA      0x17  /* PC sends latest aim delta each frame */
#define CMD_REBOOT_BOOTSEL 0xFF

/* ── Recoil pattern storage ────────────────────────────── */
#define MAX_PATTERN_POINTS 300

typedef struct {
    int16_t  dx;
    int16_t  dy;
    uint16_t t_ms;
} pattern_point_t;

static pattern_point_t pattern[MAX_PATTERN_POINTS];
static volatile uint16_t pattern_len = 0;
static volatile bool recoil_enabled = true;
static volatile bool aim_mode = false;  /* suppress real left click, use injected only */

/* ── Recoil playback state ─────────────────────────────── */
static bool     firing = false;
static uint32_t fire_start_ms = 0;
static uint16_t fire_index = 0;
static float    recoil_accum_x = 0.0f;
static float    recoil_accum_y = 0.0f;

/* ── HID output report ─────────────────────────────────── */
typedef struct __attribute__((packed)) {
    uint8_t buttons;
    int16_t x;
    int16_t y;
    int8_t  wheel;
} mouse_report_out_t;

/* ── CDC receive buffer ────────────────────────────────── */
static uint8_t  cdc_buf[2048];
static uint32_t cdc_len = 0;

/* ── Razer polling rate state ──────────────────────────── */
static uint8_t razer_dev = 0;
static uint8_t razer_inst = 0;
static volatile int razer_state = 0; /* 0=idle, 1=need cmd1, 2=need cmd2, 3=done */

static uint8_t razer_buf[90];

static void razer_set_cb(tuh_xfer_t *xfer) { (void)xfer; }

static void razer_send_poll_cmd(uint8_t arg) {
    memset(razer_buf, 0, 90);
    razer_buf[0] = 0x00; razer_buf[1] = 0x1F;
    razer_buf[5] = 0x02;
    razer_buf[6] = 0x00; razer_buf[7] = 0x40;
    razer_buf[8] = arg;
    razer_buf[9] = 0x08; /* 1000Hz */
    uint8_t c = 0;
    for (int i = 2; i < 88; i++) c ^= razer_buf[i];
    razer_buf[88] = c;

    /* SET_REPORT directly via control xfer, explicit wIndex=0 */
    static tusb_control_request_t const req = {
        .bmRequestType_bit = {
            .recipient = TUSB_REQ_RCPT_INTERFACE,
            .type      = TUSB_REQ_TYPE_CLASS,
            .direction = TUSB_DIR_OUT,
        },
        .bRequest = 0x09,
        .wValue   = 0x0300,
        .wIndex   = 0,    /* interface 0 explicitly */
        .wLength  = 90,
    };
    tuh_xfer_t xfer = {
        .daddr       = razer_dev,
        .ep_addr     = 0,
        .setup       = &req,
        .buffer      = razer_buf,
        .complete_cb = razer_set_cb,
    };
    tuh_control_xfer(&xfer);
}

static void razer_get_cb(tuh_xfer_t *xfer) { (void)xfer; }

static void razer_recv_response(void) {
    /* GET_REPORT via raw control xfer (reads mouse's response) */
    static uint8_t resp[90];
    static tusb_control_request_t const req = {
        .bmRequestType_bit = {
            .recipient = TUSB_REQ_RCPT_INTERFACE,
            .type      = TUSB_REQ_TYPE_CLASS,
            .direction = TUSB_DIR_IN,
        },
        .bRequest = 0x01, /* GET_REPORT */
        .wValue   = 0x0300, /* Feature, ID 0 */
        .wIndex   = 0,
        .wLength  = 90,
    };
    tuh_xfer_t xfer = {
        .daddr       = razer_dev,
        .ep_addr     = 0,
        .setup       = &req,
        .buffer      = resp,
        .complete_cb = razer_get_cb,
    };
    tuh_control_xfer(&xfer);
}

/* ================================================================
 *  Core 1: USB Host (PIO USB via TinyUSB)
 * ================================================================ */
void core1_main(void) {
    sleep_ms(10);
    pio_usb_configuration_t pio_cfg = PIO_USB_DEFAULT_CONFIG;
    pio_cfg.pin_dp = 12;
    tuh_configure(1, TUH_CFGID_RPI_PIO_USB_CONFIGURATION, &pio_cfg);
    tuh_init(1);

    uint32_t cmd_time = 0;

    while (true) {
        tuh_task();

        /* Razer: wait 2s, then SET(0x00)+GET, SET(0x01)+GET */
        if (razer_state == 1 && cmd_time == 0) cmd_time = board_millis();
        if (razer_state == 1 && board_millis() - cmd_time > 2000) {
            /* First pair: SET(arg=0x00) then immediately GET */
            razer_send_poll_cmd(0x00);
            /* Process transfers */
            for (int i = 0; i < 200; i++) { tuh_task(); busy_wait_us(100); }
            razer_recv_response();
            for (int i = 0; i < 200; i++) { tuh_task(); busy_wait_us(100); }

            /* Second pair: SET(arg=0x01) then immediately GET */
            razer_send_poll_cmd(0x01);
            for (int i = 0; i < 200; i++) { tuh_task(); busy_wait_us(100); }
            razer_recv_response();
            for (int i = 0; i < 200; i++) { tuh_task(); busy_wait_us(100); }

            razer_state = 5; /* done */
            cmd_time = 0;
        }
    }
}

/* ── TinyUSB Host HID callbacks (Core 1) ──────────────── */

void tuh_hid_mount_cb(uint8_t dev_addr, uint8_t instance,
                       uint8_t const *desc_report, uint16_t desc_len) {
    (void)desc_report; (void)desc_len;
    uint8_t const itf_protocol = tuh_hid_interface_protocol(dev_addr, instance);

    if (itf_protocol == HID_ITF_PROTOCOL_MOUSE) {
        tuh_hid_set_protocol(dev_addr, instance, HID_PROTOCOL_REPORT);

        uint16_t vid, pid;
        tuh_vid_pid_get(dev_addr, &vid, &pid);
        if (vid == 0x1532) {
            razer_dev = dev_addr;
            razer_inst = instance;
            razer_state = 1; /* trigger command sequence */
        }
    }

    if (itf_protocol == HID_ITF_PROTOCOL_MOUSE ||
        itf_protocol == HID_ITF_PROTOCOL_KEYBOARD) {
        tuh_hid_receive_report(dev_addr, instance);
    }
}

void tuh_hid_umount_cb(uint8_t dev_addr, uint8_t instance) {
    (void)dev_addr; (void)instance;
}

void tuh_hid_report_received_cb(uint8_t dev_addr, uint8_t instance,
                                 uint8_t const *report, uint16_t len) {
    uint8_t const itf_protocol = tuh_hid_interface_protocol(dev_addr, instance);

    if (itf_protocol == HID_ITF_PROTOCOL_MOUSE) {
        uint8_t buttons;
        int16_t x, y;
        int8_t  wheel = 0;

        if (len >= 8) {
            /* Razer native: [btns:1][vendor:2][wheel:1][x:2LE][y:2LE] */
            buttons = report[0];
            wheel   = (int8_t)report[3];
            x       = (int16_t)(report[4] | (report[5] << 8));
            y       = (int16_t)(report[6] | (report[7] << 8));
        } else if (len >= 3) {
            buttons = report[0];
            x       = (int16_t)(int8_t)report[1];
            y       = (int16_t)(int8_t)report[2];
            wheel   = (len >= 4) ? (int8_t)report[3] : 0;
        } else {
            goto next;
        }

        uint32_t w1 = (uint32_t)buttons | ((uint32_t)(uint16_t)x << 16);
        uint32_t w2 = (uint32_t)(uint16_t)y | ((uint32_t)(uint8_t)wheel << 16);
        multicore_fifo_push_timeout_us(w1, 0);
        multicore_fifo_push_timeout_us(w2, 100);
    }

next:
    tuh_hid_receive_report(dev_addr, instance);
}

/* ================================================================
 *  Core 0: TinyUSB Device — HID mouse + CDC serial
 * ================================================================ */

/* Simple fast PRNG (xorshift32) */
static uint32_t rng_state = 0x12345678;
static uint32_t rng_next(void) {
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 17;
    rng_state ^= rng_state << 5;
    return rng_state;
}
/* Returns random float in [-1.0, +1.0] */
static float rng_float(void) {
    return (float)(int32_t)(rng_next() & 0xFFFF) / 32768.0f;
}

static void get_recoil_delta(int16_t *out_dx, int16_t *out_dy) {
    *out_dx = 0;
    *out_dy = 0;
    if (!firing || !recoil_enabled || pattern_len == 0) return;

    uint32_t elapsed = board_millis() - fire_start_ms;
    while (fire_index < pattern_len && pattern[fire_index].t_ms <= elapsed) {
        float dx = (float)pattern[fire_index].dx;
        float dy = (float)pattern[fire_index].dy;

        /* Micro-jitter: ±2% magnitude + ±0.2 pixel random offset */
        dx *= (1.0f + 0.02f * rng_float());
        dy *= (1.0f + 0.02f * rng_float());
        dx += 0.2f * rng_float();
        dy += 0.2f * rng_float();

        recoil_accum_x += dx;
        recoil_accum_y += dy;
        fire_index++;
    }
    int16_t ix = (int16_t)recoil_accum_x;
    int16_t iy = (int16_t)recoil_accum_y;
    recoil_accum_x -= ix;
    recoil_accum_y -= iy;
    *out_dx = ix;
    *out_dy = iy;
}

#define MAX_MOVE_PER_MS 127

/* Mouse state accumulator (updated from FIFO at 125Hz) */
static int32_t  mouse_accum_x = 0;
static int32_t  mouse_accum_y = 0;
static uint8_t  mouse_buttons = 0;
static int8_t   mouse_wheel_last = 0;

/* Injected click from CDC (duration in ms, 0 = inactive) */
static volatile uint8_t  inject_buttons = 0;
static volatile uint32_t inject_start_ms = 0;  /* when to start pressing */
static volatile uint32_t inject_end_ms = 0;    /* when to release */

/* Aim assist: latest delta from PC (updated every frame) */
static volatile int16_t aim_dx = 0;
static volatile int16_t aim_dy = 0;
static volatile uint32_t aim_delay_until = 0;  /* suppress left click until this time */
static uint8_t  raw_left_prev = 0;             /* previous raw left button state */

/* Drain all pending mouse data from FIFO into accumulator */
static void read_mouse_input(void) {
    uint32_t w1, w2;
    while (multicore_fifo_pop_timeout_us(0, &w1)) {
        if (!multicore_fifo_pop_timeout_us(100, &w2)) break;

        uint8_t raw_buttons = w1 & 0xFF;
        uint8_t raw_left = raw_buttons & 0x01;
        if (aim_mode && raw_left && !raw_left_prev) {
            /* Left click just pressed in aim mode: apply stored delta.
             * Inject a guaranteed click after move completes.
             * Also delay real left button passthrough. */
            mouse_accum_x += aim_dx;
            mouse_accum_y += aim_dy;
            int16_t dist = (aim_dx > 0 ? aim_dx : -aim_dx);
            int16_t dy_abs = (aim_dy > 0 ? aim_dy : -aim_dy);
            if (dy_abs > dist) dist = dy_abs;
            /* delay = move drain time + 1 game frame (~7ms @ 144Hz) */
            uint32_t delay = (uint32_t)(dist / MAX_MOVE_PER_MS) + 10;
            uint32_t now_ms = board_millis();
            aim_delay_until = now_ms + delay;
            /* Inject click to guarantee at least one shot */
            inject_buttons = 0x01;
            inject_start_ms = now_ms + delay;
            inject_end_ms = now_ms + delay + 80;
        }
        raw_left_prev = raw_left;
        mouse_buttons = raw_buttons;
        if (aim_mode && aim_delay_until) {
            /* Suppress real left button until move completes */
            if (board_millis() < aim_delay_until) {
                mouse_buttons &= ~0x01;
            } else {
                aim_delay_until = 0;
            }
        }
        mouse_accum_x += (int16_t)(w1 >> 16);
        mouse_accum_y += (int16_t)(w2 & 0xFFFF);
        mouse_wheel_last = (int8_t)((w2 >> 16) & 0xFF);

        /* firing state is tracked in send_hid_output (includes injected clicks) */
    }
}

/* Send HID report every 1ms: mouse movement + smooth recoil */
static void send_hid_output(void) {
    static uint32_t last_send = 0;
    uint32_t now = board_millis();
    if (now == last_send) return; /* max 1 report per ms */
    last_send = now;

    if (!tud_hid_ready()) return;

    int16_t rdx = 0, rdy = 0;
    get_recoil_delta(&rdx, &rdy);

    /* Consume accumulated mouse movement (clamped per-report for human-like speed) */
    int16_t mx = (mouse_accum_x > MAX_MOVE_PER_MS) ? MAX_MOVE_PER_MS :
                 (mouse_accum_x < -MAX_MOVE_PER_MS) ? -MAX_MOVE_PER_MS : (int16_t)mouse_accum_x;
    int16_t my = (mouse_accum_y > MAX_MOVE_PER_MS) ? MAX_MOVE_PER_MS :
                 (mouse_accum_y < -MAX_MOVE_PER_MS) ? -MAX_MOVE_PER_MS : (int16_t)mouse_accum_y;
    mouse_accum_x -= mx;
    mouse_accum_y -= my;

    /* Injected click: wait for start, expire at end */
    uint8_t inj = 0;
    if (inject_buttons) {
        if (now >= inject_end_ms) {
            inject_buttons = 0;
        } else if (now >= inject_start_ms) {
            inj = inject_buttons;
        }
    }

    /* Merge real + injected buttons */
    uint8_t btns = mouse_buttons | inj;

    /* Only send if there's something to report (movement, recoil, or button change) */
    static uint8_t last_buttons = 0;
    bool buttons_changed = (btns != last_buttons);
    if (mx == 0 && my == 0 && rdx == 0 && rdy == 0 && mouse_wheel_last == 0 && !buttons_changed)
        return;
    last_buttons = btns;

    /* Track left button for recoil (injected clicks too) */
    bool left_now = (btns & 0x01) != 0;
    if (left_now && !firing) {
        firing = true;
        fire_start_ms = now;
        fire_index = 0;
        recoil_accum_x = 0.0f;
        recoil_accum_y = 0.0f;
        rng_state ^= now;
    } else if (!left_now && firing) {
        firing = false;
    }

    mouse_report_out_t report = {
        .buttons = btns,
        .x       = mx + rdx,
        .y       = my + rdy,
        .wheel   = mouse_wheel_last,
    };
    tud_hid_report(0, &report, sizeof(report));
    mouse_wheel_last = 0; /* wheel is one-shot */
}

static void process_cdc(void) {
    uint32_t avail = tud_cdc_available();
    if (avail == 0) return;
    if (avail > sizeof(cdc_buf) - cdc_len)
        avail = sizeof(cdc_buf) - cdc_len;
    if (avail == 0) { cdc_len = 0; return; }
    cdc_len += tud_cdc_read(cdc_buf + cdc_len, avail);

    uint32_t pos = 0;
    while (pos < cdc_len) {
        uint8_t cmd = cdc_buf[pos];
        if (cmd == CMD_PATTERN_UPLOAD) {
            if (pos + 3 > cdc_len) break;
            uint16_t n = (uint16_t)(cdc_buf[pos+1] | (cdc_buf[pos+2] << 8));
            uint32_t total = 3 + (uint32_t)n * 6;
            if (pos + total > cdc_len) break;
            uint16_t count = (n > MAX_PATTERN_POINTS) ? MAX_PATTERN_POINTS : n;
            for (uint16_t i = 0; i < count; i++) {
                uint32_t off = pos + 3 + i * 6;
                pattern[i].dx   = (int16_t)(cdc_buf[off]   | (cdc_buf[off+1] << 8));
                pattern[i].dy   = (int16_t)(cdc_buf[off+2] | (cdc_buf[off+3] << 8));
                pattern[i].t_ms = (uint16_t)(cdc_buf[off+4] | (cdc_buf[off+5] << 8));
            }
            pattern_len = count;
            pos += total;
        } else if (cmd == CMD_PATTERN_CLEAR) {
            pattern_len = 0;
            pos += 1;
        } else if (cmd == CMD_RECOIL_ENABLE) {
            if (pos + 2 > cdc_len) break;
            recoil_enabled = (cdc_buf[pos+1] != 0);
            pos += 2;
        } else if (cmd == CMD_MOVE) {
            if (pos + 5 > cdc_len) break;
            int16_t dx = (int16_t)(cdc_buf[pos+1] | (cdc_buf[pos+2] << 8));
            int16_t dy = (int16_t)(cdc_buf[pos+3] | (cdc_buf[pos+4] << 8));
            mouse_accum_x += dx;
            mouse_accum_y += dy;
            pos += 5;
        } else if (cmd == CMD_CLICK) {
            /* [0x14][buttons][duration_ms_u16_le] */
            if (pos + 4 > cdc_len) break;
            inject_buttons = cdc_buf[pos+1];
            uint32_t now_ms = board_millis();
            inject_start_ms = now_ms;
            uint16_t dur = (uint16_t)(cdc_buf[pos+2] | (cdc_buf[pos+3] << 8));
            inject_end_ms = now_ms + dur;
            pos += 4;
        } else if (cmd == CMD_MOVE_CLICK) {
            /* [0x15][dx_i16][dy_i16][buttons][delay_ms_u16][duration_ms_u16] = 10 bytes */
            if (pos + 10 > cdc_len) break;
            int16_t dx = (int16_t)(cdc_buf[pos+1] | (cdc_buf[pos+2] << 8));
            int16_t dy = (int16_t)(cdc_buf[pos+3] | (cdc_buf[pos+4] << 8));
            mouse_accum_x += dx;
            mouse_accum_y += dy;
            inject_buttons = cdc_buf[pos+5];
            uint16_t delay = (uint16_t)(cdc_buf[pos+6] | (cdc_buf[pos+7] << 8));
            uint16_t dur = (uint16_t)(cdc_buf[pos+8] | (cdc_buf[pos+9] << 8));
            uint32_t now_ms = board_millis();
            inject_start_ms = now_ms + delay;
            inject_end_ms = now_ms + delay + dur;
            pos += 10;
        } else if (cmd == CMD_AIM_MODE) {
            /* [0x16][0/1] */
            if (pos + 2 > cdc_len) break;
            aim_mode = (cdc_buf[pos+1] != 0);
            aim_dx = 0;
            aim_dy = 0;
            pos += 2;
        } else if (cmd == CMD_SET_DELTA) {
            /* [0x17][dx_i16_le][dy_i16_le] = 5 bytes */
            if (pos + 5 > cdc_len) break;
            aim_dx = (int16_t)(cdc_buf[pos+1] | (cdc_buf[pos+2] << 8));
            aim_dy = (int16_t)(cdc_buf[pos+3] | (cdc_buf[pos+4] << 8));
            pos += 5;
        } else if (cmd == CMD_REBOOT_BOOTSEL) {
            reset_usb_boot(0, 0);
        } else {
            pos++;
        }
    }
    if (pos > 0) {
        cdc_len -= pos;
        if (cdc_len > 0) memmove(cdc_buf, cdc_buf + pos, cdc_len);
    }
}

/* ── TinyUSB HID Device callbacks ─────────────────────── */
uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id,
                                hid_report_type_t report_type,
                                uint8_t *buffer, uint16_t reqlen) {
    (void)instance; (void)report_id; (void)report_type;
    (void)buffer; (void)reqlen;
    return 0;
}

void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id,
                            hid_report_type_t report_type,
                            uint8_t const *buffer, uint16_t bufsize) {
    (void)instance; (void)report_id; (void)report_type;
    (void)buffer; (void)bufsize;
}

/* ── Main ─────────────────────────────────────────────── */
int main(void) {
    set_sys_clock_khz(120000, true);
    multicore_reset_core1();
    multicore_launch_core1(core1_main);
    sleep_ms(100);
    tud_init(0);

    while (true) {
        tud_task();
        read_mouse_input();   /* drain FIFO → accumulator (125Hz input) */
        send_hid_output();    /* send HID every 1ms (1000Hz output, smooth recoil) */
        process_cdc();
    }
    return 0;
}
