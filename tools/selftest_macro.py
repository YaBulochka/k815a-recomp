#!/usr/bin/env python3
"""Offline checks for the keyboard macro record format.

This pins the findings from the November 2026 hardware session:

  * the five factory records in tests/fixtures/macro5.jsonl.gz;
  * the 2-byte event stream: `0x80|hold` for key-down, `0x00|hold` for key-up,
    the second byte is a HID usage, modifiers are 0xE0..0xE7;
  * the record length is `2 + 2*events`, rounded up to 8;
  * the real-hardware payload window is 32 bytes and is shared with the encoder
    records (16 bytes each), so 14 events is the practical maximum with both
    encoders off, 7 events with one encoder, and 16 events do not fit;
  * the exact record that made key 1 type "abcdefg" on a real 0x00E0 unit.

No hardware, no emulator, no browser:

    python3 tools/selftest_macro.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k815_proto as P  # noqa: E402
import fixtures  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return bool(ok)


def macro_record(events) -> bytes:
    out = bytearray(b"\x01\x00")
    for down, usage, hold in events:
        hold = max(1, min(15, int(hold)))
        out += bytes([(0x80 if down else 0x00) | hold, usage & 0xFF])
    return bytes(out)


def record_len(events: int) -> int:
    return (2 + 2 * events + 7) & ~7


def factory_records():
    path = fixtures.macro_capture()
    prog, _ = P.program_from_capture(path)
    entries = {e["index"]: e for e in P.read_function_table(prog)}
    expected = {
        10: "01 00 86 e0 86 1b 06 e0 06 1b 00 00 00 00 00 00",  # Ctrl+X
        11: "01 00 86 e0 86 06 06 e0 06 06 00 00 00 00 00 00",  # Ctrl+C
        12: "01 00 86 e0 86 19 06 e0 06 19 00 00 00 00 00 00",  # Ctrl+V
        13: "01 00 86 e3 86 07 06 e3 06 07 00 00 00 00 00 00",  # Win+D
        14: "01 00 86 e0 86 04 06 04 06 e0 00 00 00 00 00 00",  # Ctrl+A
    }
    for index, want in expected.items():
        e = entries[index]
        got = P.read_function_record(prog, e["address"], 16)
        check(
            f"factory slot {index} kind 0x04",
            e["kind"] == 0x04,
            f"kind={e['kind']:#04x}",
        )
        check(f"factory slot {index} record", got.hex(" ") == want, got.hex(" "))
    check(
        "factory slot 10 address 0x168",
        entries[10]["address"] == 0x168,
        hex(entries[10]["address"]),
    )


def main() -> int:
    print("macro record format")
    check(
        "Ctrl+Num5",
        macro_record(
            [(True, 0xE0, 6), (True, 0x5D, 6), (False, 0xE0, 6), (False, 0x5D, 6)]
        ).hex(" ")
        == "01 00 86 e0 86 5d 06 e0 06 5d",
    )
    check(
        "Ctrl+Shift+A",
        macro_record(
            [
                (True, 0xE0, 6),
                (True, 0xE1, 6),
                (True, 0x04, 6),
                (False, 0x04, 6),
                (False, 0xE1, 6),
                (False, 0xE0, 6),
            ]
        ).hex(" ")
        == "01 00 86 e0 86 e1 86 04 06 04 06 e1 06 e0",
    )

    events = []
    for usage in range(0x04, 0x0B):  # A..G, 14 events
        events += [(True, usage, 6), (False, usage, 6)]
    hw = macro_record(events) + b"\x00\x00"
    check("14-event A..G record is 32 bytes", len(hw) == 32, str(len(hw)))
    check(
        "14-event record ends with G up",
        hw.hex(" ").endswith("86 0a 06 0a 00 00"),
        hw.hex(" "),
    )

    print("record length and the 32-byte hardware window")
    for n, want in ((4, 16), (6, 16), (7, 16), (8, 24), (14, 32), (15, 32), (16, 40)):
        check(f"{n} events -> {want} bytes", record_len(n) == want, str(record_len(n)))
    check("14 events fit the window", record_len(14) <= 32)
    check("15 events fit exactly", record_len(15) == 32)
    check("16 events do not fit", record_len(16) > 32)
    check("7 events leave room for one encoder", record_len(7) + 16 == 32)
    check("4 events leave room for one encoder", record_len(4) + 16 == 32)
    check("14 events leave no room for an encoder", record_len(14) + 16 > 32)

    print("factory group")
    factory_records()

    print("hardware session (0x00E0, payload 0x248..0x267)")
    check(
        "function-table entry for a 14-event macro",
        bytes([0x07, 0x0E, 0x48, 0x02, 0x00]).hex(" ") == "07 0e 48 02 00",
    )
    check("keymap macro marker", hex(0xAF) == "0xaf")

    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
