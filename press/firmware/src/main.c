/*
 * Pico Mouse — USB passthrough + recoil compensation
 *
 * Core 0 (native USB-C → PC): TinyUSB Device (HID Mouse + CDC Serial)
 * Core 1 (PIO USB-A ← mouse): TinyUSB Host (reads mouse, sets Razer 1000Hz)
 *
 * Razer DeathAdder V3: sends set_polling_rate2 twice (arg=0x00, 0x01)
 * after mount. The command is accepted but may need OS-level bInterval
 * override to take full effect. interval_override=1 forces 1ms polling.
 *
 * ⚠ MEASURED 2026-08-08: IT DOES NOT TAKE EFFECT. The two ends run at
 * different rates and only one of them is 1 kHz:
 *
 *     Pico -> PC   (device, bInterval=1)   986 Hz   median 1.01 ms, n=14
 *     mouse -> Pico (this PIO host)        125 Hz   median 8.00 ms, n=325
 *                                                   p10 7.92, p90 8.10
 *
 * The 0.18 ms spread across p10..p90 says that is a hard quantisation, not
 * noise. Method: drive CMD_MOVE (bypasses the host path entirely) for the
 * first, drag the physical mouse for the second, and time the cursor's steps
 * on the PC in both cases. So the comment further down about the accumulator
 * being "updated from FIFO at 125Hz" is CURRENT, and this paragraph's claim
 * of 1000 Hz was aspirational.
 *
 * ⚠ IT DOES NOT MISALIGN THE COMPENSATION, and that is worth stating because
 * it looks like it should. fire_start_ms is stamped in send_hid_output on the
 * OUTGOING report, and the game learns the trigger from that same report --
 * so the 8 ms delays the shot and its compensation together. What it costs is
 * the player's finger-to-shot latency, and it means passing a 1 kHz mouse
 * through this device downgrades it to 125 Hz.
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
#include "interval_override.h"   /* last_periodic_interval — see CMD_RAZER_READ */
#include "tusb.h"

static inline uint32_t board_millis(void) {
    return to_ms_since_boot(get_absolute_time());
}

/* ── Protocol commands (PC → Pico via CDC) ─────────────── */
/* CMD_*, CMD_*_LEN, MAX_PATTERN_POINTS and pattern_point_t all come from
 * protocol.h, which is GENERATED from protocol/protocol.toml — the one file
 * this firmware and press/pico_mouse.py both read. They used to be typed out
 * in both places and kept in step by a comment; see protocol.toml for what
 * that cost. Do not add a #define for a wire value here: add it there. */
#include "protocol.h"

/* Why the two readbacks exist.
 *
 * Until now the only way to find out what this firmware does was to fire in
 * the game and measure the screen. That is expensive, needs the window, and
 * it is a CLOSED LOOP: the calibration fits a curve against what it observes,
 * so a systematic error in here gets absorbed into the stored curve and the
 * residual comes out clean. Two real bugs lived behind a clean residual:
 *
 *   rng_float() returned [0, +2) instead of [-1, +1), so "zero-mean jitter"
 *   was a mean +2% and a mean +0.2 counts, always downward -- about +29
 *   counts on an AUG magazine, 2.8% of the pattern
 *
 *   the last bullet's compensation was spread over a hardcoded 100 ms, which
 *   is no weapon's interval -- on a Vector that smears the round with the most
 *   recoil on it over nearly two rounds
 *
 * Both are fixed, and NOTHING GUARDS THEM. Whoever edits this file next can
 * reintroduce either, and the symptom is "every curve is slightly off" in
 * exactly the place a residual cannot see.
 *
 * So: PATTERN_READ reports what was stored and what duration each bullet will
 * be spread over, and RECOIL_SIM runs the real per-bullet maths N times over
 * the stored pattern and reports commanded-vs-emitted totals. Neither touches
 * the HID output, so both run on a bench with no game -- which is what makes
 * them cheap enough to run on every flash. tools/verify_pico.py is the caller.
 */

/* Human-movement reporting. 4 ms is well under one 144 Hz screen frame, so
 * the PC can attribute the hand's motion to the right captured frame. */
#define HUMAN_REPORT_MS    4
#define HUMAN_HEARTBEAT_MS 250

/* HID instance indices — must match the descriptor order in
 * usb_descriptors.c (TinyUSB numbers instances by descriptor order). */
#define HID_ITF_MOUSE 0
#define HID_ITF_KBD   1

/* ── Recoil pattern storage ────────────────────────────── */
/* MAX_PATTERN_POINTS and pattern_point_t: protocol.h */
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

/* Per-ms spread: distribute each bullet's delta evenly until next bullet */
static float    spread_dx_per_ms = 0.0f;
static float    spread_dy_per_ms = 0.0f;
static uint16_t spread_until_ms  = 0;   /* elapsed ms when current spread ends */

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
static volatile uint32_t fifo_drop_count = 0;

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

/* ⚠ THIS USED TO BE `(void)xfer;` — the mouse's answer was fetched and thrown
 * away. The firmware asks "what is your polling rate now", the mouse replies,
 * and nothing read it; which is why the file header could only say the command
 * "is accepted but may need an OS-level override to take full effect". That
 * sentence was a guess about a reply that was already on the wire.
 *
 * Measured 2026-08-08: the mouse is set to 1000 Hz in Synapse and arrives at
 * the PC through this device at 125 Hz. Whether the SET fails, is unsupported,
 * or succeeds and something downstream ignores it, is exactly what byte 0 of
 * this reply says — and the argument bytes say which rate encoding is in use,
 * so nobody has to remember whether 0x08 means 1000 (divisor of 8000, the
 * polling_rate2 table) or 125 (divisor of 1000, the classic one). Reading it
 * beats recalling it; I got that encoding wrong twice in one evening. */
static volatile bool    razer_resp_valid = false;
static volatile uint8_t razer_resp_len = 0;
static uint8_t          razer_resp[16];   /* status + id + args, the useful head */

static void razer_get_cb(tuh_xfer_t *xfer) {
    if (!xfer || xfer->result != XFER_RESULT_SUCCESS || !xfer->buffer) {
        razer_resp_len = 0;
        razer_resp_valid = true;      /* a failed GET is an answer too */
        return;
    }
    uint8_t n = sizeof(razer_resp);
    for (uint8_t i = 0; i < n; i++) razer_resp[i] = xfer->buffer[i];
    razer_resp_len = n;
    razer_resp_valid = true;
}

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
        if (!multicore_fifo_push_timeout_us(w1, 0)) {
            fifo_drop_count++; /* FIFO full, drop entire pair to stay aligned */
            goto next;
        }
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
/* Returns random float in [-1.0, +1.0).
 *
 * It used to return [0.0, +2.0): `rng_next() & 0xFFFF` is 0..65535, and 65535
 * / 32768 is 2.0, not 1.0 -- the cast to int32_t cannot make it negative
 * because the mask already cleared the sign bit. The comment was right about
 * the intent and the code did something else, which mattered because both
 * users of this are supposed to be ZERO-MEAN:
 *
 *     dy *= (1.0f + 0.02f * rng_float());   was a mean +2% on every bullet
 *     dy += 0.2f * rng_float();             was a mean +0.2 counts, always down
 *
 * On an AUG magazine that is about +29 counts of extra downward push, 2.8% of
 * the whole pattern, and the calibration loop had been quietly absorbing it
 * into the stored curve. On the first bullet, whose compensation is 0.68
 * counts, the additive term alone was up to +0.4 -- a 60% perturbation that
 * never changed sign.
 */
static float rng_float(void) {
    return ((float)(rng_next() & 0xFFFF) - 32768.0f) / 32768.0f;
}

/* How long bullet `i`'s compensation is spread over, in ms.
 *
 * Pulled out of get_recoil_delta so CMD_PATTERN_READ reports the duration the
 * live path will ACTUALLY use rather than the host's idea of it. A readback
 * that recomputes the rule on the PC would agree with the PC by construction
 * and prove nothing about the firmware.
 *
 * The last bullet has no next one, so its window is the one before it. It
 * used to be a hardcoded 100 ms, which is not any weapon's bullet interval:
 * on the Vector (54.5 ms) that smeared the final round's compensation over
 * nearly two rounds' worth of time, and on the AKM (100 ms) it happened to be
 * right. A magazine's last round is the one with the most recoil on it.
 */
static uint16_t bullet_duration(uint16_t i) {
    if (i >= pattern_len) return 1;
    uint16_t prev_dur = (i > 0)
        ? (uint16_t)(pattern[i].t_ms - pattern[i - 1].t_ms)
        : 100;
    uint16_t next_t = (i + 1 < pattern_len)
        ? pattern[i + 1].t_ms
        : (uint16_t)(pattern[i].t_ms + prev_dur);
    uint16_t dur = next_t - pattern[i].t_ms;
    return dur < 1 ? 1 : dur;
}

/* Micro-jitter: ±2% magnitude + ±0.2 count random offset.
 *
 * Both terms are supposed to be ZERO-MEAN -- they exist to break up a
 * perfectly repeatable pattern, not to add compensation. Pulled out so
 * CMD_RECOIL_SIM exercises this exact function: a simulator with its own copy
 * of the arithmetic would keep passing after someone changed this one.
 */
static void jitter_bullet(int16_t in_dx, int16_t in_dy,
                          float *out_dx, float *out_dy) {
    float dx = (float)in_dx;
    float dy = (float)in_dy;
    dx *= (1.0f + 0.02f * rng_float());
    dy *= (1.0f + 0.02f * rng_float());
    dx += 0.2f * rng_float();
    dy += 0.2f * rng_float();
    *out_dx = dx;
    *out_dy = dy;
}

static void get_recoil_delta(int16_t *out_dx, int16_t *out_dy) {
    *out_dx = 0;
    *out_dy = 0;
    if (!firing || !recoil_enabled || pattern_len == 0) return;

    uint32_t elapsed = board_millis() - fire_start_ms;

    /* Advance to newly reached bullet points, compute spread rate */
    while (fire_index < pattern_len && pattern[fire_index].t_ms <= elapsed) {
        float dx, dy;
        jitter_bullet(pattern[fire_index].dx, pattern[fire_index].dy, &dx, &dy);

        /* Spread this bullet's delta evenly until the next bullet. */
        uint16_t dur = bullet_duration(fire_index);

        spread_dx_per_ms = dx / (float)dur;
        spread_dy_per_ms = dy / (float)dur;
        spread_until_ms  = pattern[fire_index].t_ms + dur;

        fire_index++;
    }

    /* Add per-ms spread (called every 1ms from send_hid_output) */
    if (elapsed < spread_until_ms) {
        recoil_accum_x += spread_dx_per_ms;
        recoil_accum_y += spread_dy_per_ms;
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

/* Injected keypress from CDC (keycode 0 = inactive) */
static volatile uint8_t  key_code = 0;
static volatile uint32_t key_end_ms = 0;

/* Running total of movement that came from the HUMAN's mouse, taken straight
 * off the passthrough FIFO before anything is injected. Recoil calibration
 * measures view motion on screen and subtracts the compensation it applied;
 * whatever the hand did lands in the same number and is indistinguishable
 * from recoil. Reporting it lets the PC subtract it exactly -- and without
 * that the curve can only ever be learned while sitting perfectly still,
 * which defeats the point of learning it from real play. */
static volatile int32_t human_total_x = 0;
static volatile int32_t human_total_y = 0;

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
        int16_t raw_dx = (int16_t)(w1 >> 16);
        int16_t raw_dy = (int16_t)(w2 & 0xFFFF);
        mouse_accum_x += raw_dx;
        mouse_accum_y += raw_dy;
        /* Only the raw passthrough counts as human. The aim-assist delta and
         * CMD_MOVE also land in mouse_accum, and folding those in here would
         * have the PC subtracting its own injections from its own
         * measurement. */
        human_total_x += raw_dx;
        human_total_y += raw_dy;
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
        spread_dx_per_ms = 0.0f;
        spread_dy_per_ms = 0.0f;
        spread_until_ms  = 0;
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

    /* Report FIFO drops once per second via CDC */
    static uint32_t last_drop_report = 0;
    static uint32_t last_reported_count = 0;
    if (now - last_drop_report >= 1000) {
        last_drop_report = now;
        if (fifo_drop_count != last_reported_count && tud_cdc_connected()) {
            char msg[40];
            int len = snprintf(msg, sizeof(msg), "[fifo] drops=%lu\r\n",
                               (unsigned long)fifo_drop_count);
            tud_cdc_write(msg, len);
            tud_cdc_write_flush();
            last_reported_count = fifo_drop_count;
        }
    }
}

/* Publish the human-movement totals so the PC can subtract the hand from what
 * it sees on screen.
 *
 * Cumulative, not per-interval: a dropped or coalesced packet then costs
 * nothing, because the next one still carries the whole story. The PC only
 * ever reads the newest value. */
static void send_human_report(void) {
    static uint32_t last_send = 0;
    static int32_t last_x = 0, last_y = 0;
    uint32_t now = board_millis();
    if (now - last_send < HUMAN_REPORT_MS) return;

    int32_t x = human_total_x, y = human_total_y;
    /* Idle costs one line every HUMAN_HEARTBEAT_MS so the PC can tell a still
     * hand from a dead link. */
    bool moved = (x != last_x || y != last_y);
    if (!moved && now - last_send < HUMAN_HEARTBEAT_MS) return;
    if (!tud_cdc_connected()) return;

    last_send = now;
    last_x = x;
    last_y = y;
    char msg[48];
    int len = snprintf(msg, sizeof(msg), "[hid] %ld %ld %lu\r\n",
                       (long)x, (long)y, (unsigned long)now);
    tud_cdc_write(msg, len);
    tud_cdc_write_flush();
}

/* ── Readbacks (CMD_PATTERN_READ / CMD_RECOIL_SIM) ────────
 *
 * Emitted a line at a time from the main loop, never in a burst from inside
 * process_cdc. A 40-round pattern is ~40 lines, which is more than the CDC
 * FIFO holds; writing them all at once would either block the USB task or
 * silently drop the tail, and a readback that drops its tail is worse than no
 * readback -- the test would pass on a truncated answer.
 */
static uint16_t pat_dump_next = 0;      /* next bullet index to emit */
static bool     pat_dump_active = false;

static void service_reports(void) {
    if (!pat_dump_active) return;
    if (!tud_cdc_connected()) { pat_dump_active = false; return; }
    /* One line per pass, and only when there is room for it. The main loop
     * spins fast, so a 40-line dump still lands in a few milliseconds. */
    if (tud_cdc_write_available() < 64) return;

    char msg[64];
    int len;
    if (pat_dump_next < pattern_len) {
        uint16_t i = pat_dump_next;
        len = snprintf(msg, sizeof(msg), "[pat] i %u %u %d %d %u\r\n",
                       (unsigned)i, (unsigned)pattern[i].t_ms,
                       (int)pattern[i].dx, (int)pattern[i].dy,
                       (unsigned)bullet_duration(i));
        pat_dump_next++;
    } else {
        /* The enable flag rides out on the end line, and it is the only way
         * anyone can ask for it: CMD_RECOIL_ENABLE is a one-way write, so
         * FireDriver.disarm() had no confirmation to check and returned True
         * whether or not the byte arrived. press/pico_mouse._write swallows
         * SerialTimeoutException -- the CDC backpressure it documents as
         * normal -- so a dropped disarm left the pattern running silently,
         * and calibration/sweep.py's `--no-comp` guard (`if not disarm():
         * raise`) could never fire. Appended rather than given its own
         * command so an old host still parses this line: the token is extra,
         * and the host's end-of-dump test is a prefix match. */
        len = snprintf(msg, sizeof(msg), "[pat] end %u\r\n",
                       (unsigned)(recoil_enabled ? 1 : 0));
        pat_dump_active = false;
    }
    tud_cdc_write(msg, len);
    tud_cdc_write_flush();
}

/* Run the real per-bullet maths `iters` times over the stored pattern and
 * report commanded vs emitted totals. Emits nothing to the HID interface: the
 * cursor does not move, so this runs on a bench with the game closed.
 *
 * Totals are in milli-counts because the jitter is fractional and the whole
 * point is to detect a bias far below one count -- the bug this guards
 * against was +0.2 counts a bullet.
 */
static void run_recoil_sim(uint16_t iters) {
    int64_t cmd_x = 0, cmd_y = 0;
    double emit_x = 0.0, emit_y = 0.0;
    for (uint16_t k = 0; k < iters; k++) {
        for (uint16_t i = 0; i < pattern_len; i++) {
            float dx, dy;
            jitter_bullet(pattern[i].dx, pattern[i].dy, &dx, &dy);
            cmd_x += pattern[i].dx;
            cmd_y += pattern[i].dy;
            emit_x += dx;
            emit_y += dy;
        }
    }
    char msg[128];
    int len = snprintf(msg, sizeof(msg),
                       "[sim] %u %u %lld %lld %lld %lld\r\n",
                       (unsigned)iters, (unsigned)pattern_len,
                       (long long)cmd_x, (long long)cmd_y,
                       (long long)(emit_x * 1000.0),
                       (long long)(emit_y * 1000.0));
    tud_cdc_write(msg, len);
    tud_cdc_write_flush();
}

static void process_cdc(void) {
    uint32_t avail = tud_cdc_available();
    if (avail == 0 && cdc_len == 0) return;  /* nothing pending */
    if (avail > 0) {
        uint32_t free = sizeof(cdc_buf) - cdc_len;
        if (free > 0) {
            if (avail > free) avail = free;
            cdc_len += tud_cdc_read(cdc_buf + cdc_len, avail);
        }
    }

    uint32_t pos = 0;
    while (pos < cdc_len) {
        uint8_t cmd = cdc_buf[pos];
        if (cmd == CMD_PATTERN_UPLOAD) {
            if (pos + CMD_PATTERN_UPLOAD_LEN > cdc_len) break;
            uint16_t n = (uint16_t)(cdc_buf[pos+1] | (cdc_buf[pos+2] << 8));
            uint32_t total = CMD_PATTERN_UPLOAD_LEN
                           + (uint32_t)n * PATTERN_POINT_SIZE;
            if (pos + total > cdc_len) break;
            uint16_t count = (n > MAX_PATTERN_POINTS) ? MAX_PATTERN_POINTS : n;
            for (uint16_t i = 0; i < count; i++) {
                uint32_t off = pos + CMD_PATTERN_UPLOAD_LEN
                             + i * PATTERN_POINT_SIZE;
                pattern[i].dx   = (int16_t)(cdc_buf[off]   | (cdc_buf[off+1] << 8));
                pattern[i].dy   = (int16_t)(cdc_buf[off+2] | (cdc_buf[off+3] << 8));
                pattern[i].t_ms = (uint16_t)(cdc_buf[off+4] | (cdc_buf[off+5] << 8));
            }
            pattern_len = count;
            pos += total;
        } else if (cmd == CMD_PATTERN_CLEAR) {
            pattern_len = 0;
            pos += CMD_PATTERN_CLEAR_LEN;
        } else if (cmd == CMD_RECOIL_ENABLE) {
            if (pos + CMD_RECOIL_ENABLE_LEN > cdc_len) break;
            recoil_enabled = (cdc_buf[pos+1] != 0);
            pos += CMD_RECOIL_ENABLE_LEN;
        } else if (cmd == CMD_MOVE) {
            if (pos + CMD_MOVE_LEN > cdc_len) break;
            int16_t dx = (int16_t)(cdc_buf[pos+1] | (cdc_buf[pos+2] << 8));
            int16_t dy = (int16_t)(cdc_buf[pos+3] | (cdc_buf[pos+4] << 8));
            mouse_accum_x += dx;
            mouse_accum_y += dy;
            pos += CMD_MOVE_LEN;
        } else if (cmd == CMD_CLICK) {
            /* [0x14][buttons][duration_ms_u16_le] */
            if (pos + CMD_CLICK_LEN > cdc_len) break;
            inject_buttons = cdc_buf[pos+1];
            uint32_t now_ms = board_millis();
            inject_start_ms = now_ms;
            uint16_t dur = (uint16_t)(cdc_buf[pos+2] | (cdc_buf[pos+3] << 8));
            inject_end_ms = now_ms + dur;
            pos += CMD_CLICK_LEN;
        } else if (cmd == CMD_MOVE_CLICK) {
            /* [0x15][dx_i16][dy_i16][buttons][delay_ms_u16][duration_ms_u16] = 10 bytes */
            if (pos + CMD_MOVE_CLICK_LEN > cdc_len) break;
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
            pos += CMD_MOVE_CLICK_LEN;
        } else if (cmd == CMD_AIM_MODE) {
            if (pos + CMD_AIM_MODE_LEN > cdc_len) break;
            aim_mode = (cdc_buf[pos+1] != 0);
            aim_dx = 0;
            aim_dy = 0;
            pos += CMD_AIM_MODE_LEN;
        } else if (cmd == CMD_SET_DELTA) {
            if (pos + CMD_SET_DELTA_LEN > cdc_len) break;
            aim_dx = (int16_t)(cdc_buf[pos+1] | (cdc_buf[pos+2] << 8));
            aim_dy = (int16_t)(cdc_buf[pos+3] | (cdc_buf[pos+4] << 8));
            pos += CMD_SET_DELTA_LEN;
        } else if (cmd == CMD_KEY) {
            if (pos + CMD_KEY_LEN > cdc_len) break;
            key_code = cdc_buf[pos+1];
            uint16_t dur = (uint16_t)(cdc_buf[pos+2] | (cdc_buf[pos+3] << 8));
            key_end_ms = board_millis() + dur;
            pos += CMD_KEY_LEN;
        } else if (cmd == CMD_RAZER_READ) {
            /* [0x1B] -> "[razer] <state> <len> <b0> <b1> ..." — the mouse's own
             * answer to the polling-rate SET, verbatim. byte 0 is the Razer
             * status (0x02 ok / 0x01 busy / 0x03 fail / 0x05 not supported)
             * and the argument bytes carry the rate it actually holds. Sent as
             * raw bytes rather than decoded: the decode is the thing in doubt. */
            char msg[128];
            int ml = snprintf(msg, sizeof(msg),
                              "[razer] state %d valid %d bInterval %u ep %02x len %u",
                              razer_state, (int)razer_resp_valid,
                              (unsigned)last_periodic_interval,
                              (unsigned)last_periodic_ep,
                              (unsigned)razer_resp_len);
            for (uint8_t i = 0; i < razer_resp_len && ml < (int)sizeof(msg) - 6; i++)
                ml += snprintf(msg + ml, sizeof(msg) - ml, " %02x", razer_resp[i]);
            ml += snprintf(msg + ml, sizeof(msg) - ml, "\r\n");
            tud_cdc_write(msg, ml);
            tud_cdc_write_flush();
            pos += CMD_RAZER_READ_LEN;
        } else if (cmd == CMD_PATTERN_READ) {
            /* [0x19] -> "[pat] n <len>", then one line per bullet, "[pat] end" */
            char hdr[32];
            int hl = snprintf(hdr, sizeof(hdr), "[pat] n %u\r\n",
                              (unsigned)pattern_len);
            tud_cdc_write(hdr, hl);
            tud_cdc_write_flush();
            pat_dump_next = 0;
            pat_dump_active = true;
            pos += CMD_PATTERN_READ_LEN;
        } else if (cmd == CMD_RECOIL_SIM) {
            if (pos + CMD_RECOIL_SIM_LEN > cdc_len) break;
            uint16_t iters = (uint16_t)(cdc_buf[pos+1] | (cdc_buf[pos+2] << 8));
            if (iters > 2000) iters = 2000;   /* bounded: this runs inline */
            run_recoil_sim(iters);
            pos += CMD_RECOIL_SIM_LEN;
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

/* ── TinyUSB CDC callback: reset state on disconnect ──── */
void tud_cdc_line_state_cb(uint8_t itf, bool dtr, bool rts) {
    (void)itf; (void)rts;
    if (!dtr) {
        /* Host closed the serial port — reset all injected state */
        aim_mode = false;
        aim_dx = 0;
        aim_dy = 0;
        aim_delay_until = 0;
        inject_buttons = 0;
        key_code = 0;          /* else a held key stays stuck in the game */
        key_end_ms = 0;
        recoil_enabled = true;
        pattern_len = 0;
        firing = false;
    }
}

/* ── Keyboard output ──────────────────────────────────────
 * Only emits on a state change (press / release) rather than every poll:
 * a keyboard that keeps re-reporting the same keycode looks like auto-repeat
 * to the host, which would fire multiple reloads from one command.
 */
static void send_kbd_output(void) {
    static bool down = false;
    if (!tud_hid_n_ready(HID_ITF_KBD)) return;

    bool want = (key_code != 0) && (board_millis() < key_end_ms);
    if (want == down) return;

    if (want) {
        uint8_t kc[6] = { key_code, 0, 0, 0, 0, 0 };
        if (tud_hid_n_keyboard_report(HID_ITF_KBD, 0, 0, kc)) down = true;
    } else {
        if (tud_hid_n_keyboard_report(HID_ITF_KBD, 0, 0, NULL)) {
            down = false;
            key_code = 0;
        }
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
        send_kbd_output();    /* reload keypresses for automated calibration */
        send_human_report();  /* let the PC subtract the hand from the screen */
        service_reports();    /* drain a pattern readback, one line per pass */
        process_cdc();
    }
    return 0;
}
