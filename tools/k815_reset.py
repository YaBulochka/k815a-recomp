#!/usr/bin/env python3
"""Reset the MKESPN K815 USB device (USBDEVFS_RESET).

Used by `tools/serve_webhid.py` after a successful write: closing the HID
handle is not enough on this firmware, a real USB re-enumeration is needed
before the new keymap is active.

The device node `/dev/bus/usb/BBB/DDD` is normally root:root.  The udev rule
in `tools/udev/99-mkespn-k815.rules` grants the desktop user access
(`TAG+="uaccess"` and group `plugdev`); run `sudo sh tools/install_udev.sh`
once, then the local WebHID server can reset the keyboard without root.
"""
from __future__ import annotations

import fcntl
import glob
import os
import sys

USBDEVFS_RESET = 0x5514     # _IO('U', 20)
VID = 0x30FA
PID = 0x2350


def find_device(vid: int = VID, pid: int = PID) -> str | None:
    for path in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(path, "idVendor")) as fh:
                if int(fh.read().strip(), 16) != vid:
                    continue
            with open(os.path.join(path, "idProduct")) as fh:
                if int(fh.read().strip(), 16) != pid:
                    continue
            bus = int(open(os.path.join(path, "busnum")).read().strip())
            dev = int(open(os.path.join(path, "devnum")).read().strip())
            return f"/dev/bus/usb/{bus:03d}/{dev:03d}"
        except (OSError, ValueError):
            continue
    return None


def reset(path: str | None = None) -> str:
    path = path or find_device()
    if not path:
        raise RuntimeError("MKESPN K815 not found on the USB bus")
    fd = os.open(path, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)
    return path


if __name__ == "__main__":
    try:
        print(reset(sys.argv[1] if len(sys.argv) > 1 else None))
    except (OSError, RuntimeError) as e:
        raise SystemExit(f"reset failed: {e}")
