"""Set Razer mouse DPI without Synapse.

Uses the Razer USB HID protocol (reverse-engineered by razerqdhid).
Plug mouse directly into PC, run this script, then plug into Pico.

Usage:
    python razer_dpi.py 2000        # set both X/Y to 2000
    python razer_dpi.py 1600 800    # set X=1600, Y=800
    python razer_dpi.py             # just read current DPI

Requires: pip install hidapi
"""

import sys
import struct
import hid

RAZER_VID = 0x1532


def _crc(buf):
    """XOR checksum over bytes 2..87."""
    c = 0
    for b in buf[2:88]:
        c ^= b
    return c


def _make_report(command, data=b''):
    """Build a 90-byte Razer HID feature report."""
    report = bytearray(90)
    report[0] = 0x00   # status
    report[1] = 0x1F   # transaction id
    report[2] = 0x00   # remaining packets hi
    report[3] = 0x00   # remaining packets lo
    report[4] = 0x00   # protocol type
    report[5] = len(data)  # data size
    report[6] = (command >> 8) & 0xFF  # command hi
    report[7] = command & 0xFF         # command lo
    report[8:8+len(data)] = data
    report[88] = _crc(report)
    return bytes(report)


def _send_recv(dev, command, data=b''):
    """Send command and receive response."""
    report = _make_report(command, data)
    dev.send_feature_report(b'\x00' + report)
    resp = dev.get_feature_report(0x00, 91)
    return resp


def find_razer_mouse():
    """Find a Razer mouse HID interface for control commands."""
    for d in hid.enumerate(RAZER_VID):
        if d.get('interface_number', -1) == 0:
            return d
    devices = hid.enumerate(RAZER_VID)
    return devices[0] if devices else None


def get_dpi(dev):
    """Read current DPI (X, Y)."""
    data = struct.pack('>B', 0x00) + b'\x00' * 4
    resp = _send_recv(dev, 0x0485, data)
    # resp[0] is report ID, actual data starts at resp[1]
    dpi_x = (resp[10] << 8) | resp[11]
    dpi_y = (resp[12] << 8) | resp[13]
    return dpi_x, dpi_y


def set_dpi(dev, dpi_x, dpi_y):
    """Set DPI for X and Y axes."""
    data = struct.pack('>BHHxx', 0x00, dpi_x, dpi_y)
    _send_recv(dev, 0x0405, data)


def main():
    info = find_razer_mouse()
    if not info:
        print("No Razer mouse found. Make sure it's plugged directly into PC.")
        sys.exit(1)

    print(f"Found: {info.get('product_string', '?')} "
          f"(PID: 0x{info['product_id']:04X})")

    dev = hid.device()
    dev.open_path(info['path'])

    try:
        cur = get_dpi(dev)
        print(f"Current DPI: X={cur[0]}, Y={cur[1]}")

        if len(sys.argv) >= 2:
            dpi_x = int(sys.argv[1])
            dpi_y = int(sys.argv[2]) if len(sys.argv) >= 3 else dpi_x
            set_dpi(dev, dpi_x, dpi_y)
            print(f"Set DPI: X={dpi_x}, Y={dpi_y}")

            new = get_dpi(dev)
            print(f"Verified: X={new[0]}, Y={new[1]}")
    finally:
        dev.close()


if __name__ == '__main__':
    main()
