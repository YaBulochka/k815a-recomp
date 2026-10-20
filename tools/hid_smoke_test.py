#!/usr/bin/env python3
"""Direct hidapi smoke test against the uhid-emulated K815 device."""
import hid
import sys

VID, PID = 0x30FA, 0x2350

for d in hid.enumerate(VID, PID):
    print(f"path={d['path']} usage_page={d['usage_page']:#06x} usage={d['usage']:#04x} "
          f"iface={d['interface_number']} product={d['product_string']!r}")

targets = hid.enumerate(VID, PID)
if not targets:
    sys.exit("no device found")

for d in targets:
    print(f"\n--- open {d['path']} (usage_page {d['usage_page']:#06x}) ---")
    dev = hid.Device(path=d["path"])
    # write key #0 (address 0x20) = usage 0x29 (Escape)
    req = bytes([0x07, 0x18, 0x03, 0x00, 0x20, 0x00, 0x29, 0x00] + [0] * 8)
    print("send_feature_report:", req.hex())
    dev.send_feature_report(req)
    # read it back: feature report number in the first byte
    for rnum in (0x00, 0x07):
        try:
            got = dev.get_feature_report(rnum, 16)
            print(f"get_feature_report(rnum={rnum:#04x}):", bytes(got).hex())
        except Exception as e:
            print(f"get_feature_report(rnum={rnum:#04x}) failed: {e!r}")
    dev.close()
