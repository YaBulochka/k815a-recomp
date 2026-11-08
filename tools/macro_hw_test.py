#!/usr/bin/env python3
"""Safe hardware test for a multi-step macro on a real K815.

The test preserves the device's current keymap, LED/switches header, function
table and encoder records, replaces the 32-byte function payload with one
macro record, and restores the exact pre-test state afterwards.

The real firmware has a fixed 32-byte payload window (0x248..0x267 on the
0x00E0 layout, 0x280..0x29F on 0x0118).  The window is shared with the encoder
records, 16 bytes each.  A record is 2 + 2*events bytes, rounded up to 8:

    events  record  what still fits
    4       16      one encoder
    6       16      one encoder
    7       16      one encoder
    8       24      no encoder
    14      32      no encoder
    15      32      no encoder (ends with a key-down)
    16      40      does not fit the window

Usage (the device must be connected):

    # read the current state, build the test session, do not send
    python3 tools/macro_hw_test.py prepare --text abcdefg --dry-run

    # build, send, wait for a physical key press, restore
    python3 tools/macro_hw_test.py run --key 1 --text abcdefg

    # the same steps separately
    python3 tools/macro_hw_test.py prepare --key 1 --text abcdefg
    python3 tools/macro_hw_test.py send
    python3 tools/macro_hw_test.py restore

`run` always calls `restore` in a `finally` block, so Ctrl-C still rolls back.
The state and the generated frames are written to /tmp/opencode by default;
use --state and --frames to keep them elsewhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.chdir(ROOT)

import k815_proto as P  # noqa: E402
import k815ctl as C  # noqa: E402

DEFAULT_VID, DEFAULT_PID = 0x30FA, 0x2350
DEFAULT_USAGE = 0xFF01
DEFAULT_WIRE = P.WIRE_REPORT_LEN
DEFAULT_STATE = "/tmp/opencode/k815-macro-hw-state.json"
DEFAULT_FRAMES = "/tmp/opencode/k815-macro-hw-frames.json"

# Upload-order keymap slots for keys 1..8 (see PROTOCOL.md, section 4).
KEY_SLOT = {1: 8, 2: 16, 3: 24, 4: 32, 5: 9, 6: 17, 7: 25, 8: 33}

# HID usages for A..Z, used by the default text.
LETTERS = {chr(ord("a") + i): 0x04 + i for i in range(26)}


def device_args(a) -> SimpleNamespace:
    return SimpleNamespace(
        vid=a.vid,
        pid=a.pid,
        usage_page=a.usage_page,
        device=a.device,
        wire_len=a.wire_len,
    )


def open_device(a):
    return C.open_device(a.vid, a.pid, usage_page=a.usage_page, path=a.device)


def record_len(events: int) -> int:
    """Wire length of a keyboard macro record, rounded up to 8."""
    return (2 + 2 * events + 7) & ~7


def macro_record(events) -> bytes:
    """Build `01 00 <tag> <usage> ...` from (down, usage, hold) tuples."""
    out = bytearray(b"\x01\x00")
    for down, usage, hold in events:
        hold = max(1, min(15, int(hold)))
        out += bytes([(0x80 if down else 0x00) | hold, usage & 0xFF])
    return bytes(out)


def text_events(text: str, hold_units: int = 6):
    """A down/up pair per character.  Only a-z/A-Z are supported here."""
    events = []
    for ch in text:
        usage = LETTERS.get(ch.lower())
        if usage is None:
            raise SystemExit(f"unsupported character in --text: {ch!r}")
        events += [(True, usage, hold_units), (False, usage, hold_units)]
    return events


def read_state(a) -> dict:
    """Read the parts of the device a restore session needs."""
    base = C.device_config_base(device_args(a))
    if not base:
        raise SystemExit("cannot read the device config base")
    template = C.hardware_template(base)
    if not template:
        raise SystemExit(f"no hardware template for config base {base:#06x}")
    prog, _ = P.program_from_capture(template)
    rec_base = prog.record_base
    dev, info = open_device(a)
    print(
        f"device {info['path'].decode()}, config base {base:#06x}, "
        f"payload {rec_base:#06x}..{rec_base + 0x1F:#06x}"
    )
    km_a = C.dump_range(
        dev, P.KEYMAP_BASE, P.KEYMAP_LEN, wire_len=a.wire_len, verify=False
    )
    km_b = C.dump_range(
        dev,
        prog.b_addr(P.KEYMAP_BASE_B),
        P.KEYMAP_LEN,
        wire_len=a.wire_len,
        verify=False,
    )
    table = C.dump_range(
        dev,
        prog.b_addr(P.FUNCTION_TABLE_BASE),
        P.FUNCTION_TABLE_ENTRIES * P.FUNCTION_ENTRY_LEN + 2,
        wire_len=a.wire_len,
        verify=False,
    )
    payload = C.dump_range(dev, rec_base, 32, wire_len=a.wire_len, verify=False)
    memory = C.dump_range(dev, 0, P.IMAGE_LEN, wire_len=a.wire_len, verify=False)
    C.host_handshake(dev, a.wire_len)
    state = {
        "configBase": base,
        "template": template,
        "recordBase": rec_base,
        "keymapA": km_a.hex(),
        "keymapB": km_b.hex(),
        "table": table.hex(),
        "payload": payload.hex(),
        "memory": memory.hex(),
    }
    return state


def build(state: dict, key: int, events, with_macro: bool) -> P.Program:
    """Patch a hardware template with the saved state and optionally a macro."""
    prog, _ = P.program_from_capture(state["template"])
    assert prog.base == state["configBase"]
    rec_base = state["recordBase"]
    assert prog.record_base == rec_base

    mem = bytes.fromhex(state["memory"])
    for it in prog.blocks:
        if (
            it.phase == "A"
            and it.addr + it.length <= prog.base
            and it.addr + it.length <= len(mem)
        ):
            prog.patch("A", it.addr, mem[it.addr : it.addr + it.length])
        elif (
            it.phase == "B" and it.addr >= prog.base and it.addr + it.length <= len(mem)
        ):
            prog.patch("B", it.addr, mem[it.addr : it.addr + it.length])

    km_a = bytearray(bytes.fromhex(state["keymapA"]))
    km_b = bytearray(bytes.fromhex(state["keymapB"]))
    if with_macro:
        slot = KEY_SLOT[key]
        km_a[slot] = 0xAF
        # Some firmware modes use the phase-B copy; mark both for the test.
        km_b[slot] = 0xAF
    prog.patch("A", P.KEYMAP_BASE, bytes(km_a))
    prog.patch_b(P.KEYMAP_BASE_B, bytes(km_b))

    table = prog.b_addr(P.FUNCTION_TABLE_BASE)
    prog.patch("B", table, bytes.fromhex(state["table"]))
    old_payload = bytes.fromhex(state["payload"])
    assert len(old_payload) == 32

    if not with_macro:
        prog.patch("B", rec_base, old_payload)
        return prog

    rec = macro_record(events)
    rlen = record_len(len(events))
    if rlen > 32:
        raise SystemExit(
            f"{len(events)} events need {rlen} bytes, the hardware payload is 32 bytes"
        )
    # Encoders are 16 bytes each and live after the macro in the same window.
    enc0 = old_payload[0:16] if rlen + 16 <= 32 else b""
    enc1 = old_payload[16:32] if rlen + 32 <= 32 else b""
    payload = rec + b"\x00" * (rlen - len(rec)) + enc0 + enc1
    payload += b"\x00" * (32 - len(payload))

    idx = 10 + key - 1
    prog.patch(
        "B",
        table + idx * P.FUNCTION_ENTRY_LEN,
        bytes([P.FUNCTION_ENTRY_TAG, len(events) & 0x7F])
        + rec_base.to_bytes(3, "little"),
    )
    # entry 20 = enc0, entry 21 = enc1
    for n, enc in ((20, enc0), (21, enc1)):
        base = table + n * P.FUNCTION_ENTRY_LEN
        if enc:
            addr = rec_base + rlen + (0 if n == 20 else 16)
            prog.patch(
                "B",
                base,
                bytes([P.FUNCTION_ENTRY_TAG, 0x04]) + addr.to_bytes(3, "little"),
            )
        else:
            prog.patch("B", base + 1, bytes([0x00]))
    prog.patch("B", rec_base, payload)
    return prog


def save_frames(prog: P.Program, path: str) -> None:
    frames = P.emit(prog)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([f.hex() for f in frames], fh)
    print(f"frames: {path} ({len(frames)})")


def send_frames(a, path: str) -> None:
    frames = [bytes.fromhex(h) for h in json.load(open(path, encoding="utf-8"))]
    dev, info = open_device(a)
    print(f"sending {len(frames)} frames to {info['path'].decode()}")
    for i, f in enumerate(frames):
        if i == 1:
            time.sleep(0.2)  # measured gap after BEGIN
        if C.send_report(dev, f, a.wire_len) < 0:
            raise SystemExit(f"send failed at frame {i}")
        time.sleep(a.pace)
    print("sent")


def verify_restore(a, state: dict) -> bool:
    dev, _ = open_device(a)
    rec_base = state["recordBase"]
    prog, _ = P.program_from_capture(state["template"])
    slot = KEY_SLOT[a.key]
    idx = 10 + a.key - 1
    entry_addr = prog.b_addr(P.FUNCTION_TABLE_BASE) + idx * P.FUNCTION_ENTRY_LEN
    km = C.dump_range(dev, P.KEYMAP_BASE + slot, 1, wire_len=a.wire_len, verify=False)
    table = C.dump_range(dev, entry_addr, 5, wire_len=a.wire_len, verify=False)
    payload = C.dump_range(dev, rec_base, 32, wire_len=a.wire_len, verify=False)
    C.host_handshake(dev, a.wire_len)
    want_km = bytes.fromhex(state["keymapA"])[slot : slot + 1]
    want_table = bytes.fromhex(state["table"])[idx * 5 : (idx + 1) * 5]
    want_payload = bytes.fromhex(state["payload"])
    ok = km == want_km and table == want_table and payload == want_payload
    print(f"keymap key {a.key}:", km.hex(), "want", want_km.hex())
    print(f"entry {idx}:", table.hex(" "), "want", want_table.hex(" "))
    print("payload:", payload.hex(" "))
    print("RESTORE", "PASS" if ok else "FAIL")
    return ok


def cmd_prepare(a) -> int:
    state = read_state(a)
    state["key"] = a.key
    events = text_events(a.text, a.hold_units)
    prog = build(state, a.key, events, with_macro=True)
    rlen = record_len(len(events))
    enc = []
    if rlen + 16 <= 32:
        enc.append("enc0")
    if rlen + 32 <= 32:
        enc.append("enc1")
    print(
        f"{len(events)} events, {rlen}-byte record; "
        f"encoders: {', '.join(enc) if enc else 'both off'}"
    )
    if len(enc) < 2:
        print("note: an encoder direction is disabled by this test")
    with open(a.state, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    save_frames(prog, a.frames)
    if a.dry_run:
        print("dry run: nothing sent")
    return 0


def cmd_send(a) -> int:
    send_frames(a, a.frames)
    return 0


def cmd_restore(a) -> int:
    state = json.load(open(a.state, encoding="utf-8"))
    a.key = state.get("key", a.key)
    prog = build(state, a.key, [], with_macro=False)
    save_frames(prog, a.frames)
    send_frames(a, a.frames)
    return 0 if verify_restore(a, state) else 1


def cmd_run(a) -> int:
    state = read_state(a)
    state["key"] = a.key
    events = text_events(a.text, a.hold_units)
    prog = build(state, a.key, events, with_macro=True)
    with open(a.state, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    save_frames(prog, a.frames)
    send_frames(a, a.frames)
    print(f"\nkey {a.key} now sends {a.text!r} ({len(events)} events).")
    print("Open a text field, press the key, and check the result.")
    try:
        input("Press Enter to restore the pre-test state... ")
    finally:
        print("restoring")
        cmd_restore(a)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--vid", type=lambda s: int(s, 0), default=DEFAULT_VID)
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=DEFAULT_PID)
    ap.add_argument("--usage-page", type=lambda s: int(s, 0), default=DEFAULT_USAGE)
    ap.add_argument("--device", default=None)
    ap.add_argument("--wire-len", type=int, default=DEFAULT_WIRE)
    ap.add_argument("--key", type=int, choices=range(1, 9), default=1)
    ap.add_argument(
        "--text",
        default="abcdefg",
        help="a-z/A-Z only; 14 events is the 32-byte maximum",
    )
    ap.add_argument(
        "--hold-units",
        type=int,
        default=6,
        help="hold in 8 ms units (1..15, default 6 = 48 ms)",
    )
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--frames", default=DEFAULT_FRAMES)
    ap.add_argument("--pace", type=float, default=0.02)
    ap.add_argument("--dry-run", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    prep = sub.add_parser("prepare", help="read state and build the test session")
    prep.add_argument("--dry-run", action="store_true")
    sub.add_parser("send", help="send the prepared session")
    sub.add_parser("restore", help="restore the saved pre-test state")
    sub.add_parser("run", help="prepare, send, wait, restore")
    a = ap.parse_args(argv)
    cmd = a.cmd or "prepare"
    return {
        "prepare": cmd_prepare,
        "send": cmd_send,
        "restore": cmd_restore,
        "run": cmd_run,
    }[cmd](a)


if __name__ == "__main__":
    sys.exit(main())
