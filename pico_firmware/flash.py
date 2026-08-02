"""Flash the Pico over USB without touching the BOOTSEL button.

The running firmware accepts CMD_REBOOT_BOOTSEL (0xFF) over CDC, so the whole
cycle is scriptable: reboot to bootloader -> picotool load -x -> device comes
back as the composite HID+CDC device.

The physical mouse is passed through this device, so it stops responding for
the ~15 s the Pico spends in the bootloader. If flashing fails the Pico stays
in BOOTSEL and the mouse stays dead until it is flashed again (which still
works — BOOTSEL is a ROM bootloader and cannot be bricked).

    python pico_firmware/flash.py
    python pico_firmware/flash.py --uf2 path/to/other.uf2
"""
import argparse
import os
import subprocess
import sys
import time

import serial
import serial.tools.list_ports as lp

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_UF2 = os.path.join(HERE, 'build', 'pico_mouse.uf2')
PICOTOOL = os.path.expanduser(
    r'~\.pico-sdk\picotool\2.2.0-a4\picotool\picotool.exe')

APP_VID = 0xCAFE                      # running firmware
BOOT_VIDS = {0x2E8A}                  # RP2040/RP2350 ROM bootloader


def find_app_port():
    for p in lp.comports():
        if p.vid == APP_VID:
            return p.device
    return None


def in_bootsel():
    """picotool sees the device; more reliable than guessing a drive letter."""
    try:
        r = subprocess.run([PICOTOOL, 'info'], capture_output=True,
                           text=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--uf2', default=DEFAULT_UF2)
    ap.add_argument('--timeout', type=float, default=25.0)
    args = ap.parse_args()

    if not os.path.exists(args.uf2):
        print(f"[!] no uf2 at {args.uf2}  — build first (ninja in build/)")
        return 1
    if not os.path.exists(PICOTOOL):
        print(f"[!] picotool not found at {PICOTOOL}")
        return 1
    age = time.time() - os.path.getmtime(args.uf2)
    print(f"uf2      : {args.uf2}")
    print(f"           {os.path.getsize(args.uf2)} bytes, built "
          f"{age/60:.1f} min ago")

    if in_bootsel():
        print("state    : already in BOOTSEL")
    else:
        port = find_app_port()
        if not port:
            print("[!] no Pico found — neither running firmware nor BOOTSEL.")
            return 1
        print(f"state    : running firmware on {port}")
        print("           sending CMD_REBOOT_BOOTSEL (0xFF) ...")
        print("           >>> the mouse will stop responding now <<<")
        try:
            s = serial.Serial(port, 115200, timeout=0.5, write_timeout=1.0)
            s.write(bytes([0xFF]))
            s.flush()
            s.close()
        except Exception as e:
            # The device often drops the port mid-write; that is the reboot
            # working, not a failure.
            print(f"           (port closed during reboot: {e})")

        t0 = time.time()
        while time.time() - t0 < args.timeout:
            time.sleep(1.0)
            if in_bootsel():
                print(f"           BOOTSEL detected after "
                      f"{time.time()-t0:.1f}s")
                break
        else:
            print("[!] device never appeared in BOOTSEL.")
            print("    Unplug, hold BOOTSEL, replug, then re-run.")
            return 1

    print("\nflashing ...")
    r = subprocess.run([PICOTOOL, 'load', '-x', args.uf2],
                       capture_output=True, text=True, timeout=180)
    print(r.stdout.strip())
    if r.stderr.strip():
        print(r.stderr.strip())
    if r.returncode != 0:
        print(f"[!] picotool load failed (exit {r.returncode})")
        print("    The Pico is still in BOOTSEL; fix and re-run this script.")
        return 1

    print("\nwaiting for the device to come back ...")
    t0 = time.time()
    while time.time() - t0 < args.timeout:
        time.sleep(1.0)
        port = find_app_port()
        if port:
            print(f"  OK — running firmware on {port} after "
                  f"{time.time()-t0:.1f}s")
            print("  mouse passthrough restored")
            return 0
    print("[!] device did not re-enumerate. Unplug and replug it.")
    return 1


if __name__ == '__main__':
    sys.exit(main())
