#!/usr/bin/env python3
"""Generate the WebHID session blob used by webhid/.

Reads a captured upload session (default captures/baseline_apply.jsonl) and
emits webhid/k815_session.js containing:

  * the exact 663-report session as one base64 blob (16 B per report),
  * a list of "patch targets": for every frame that carries keymap bytes, its
    frame index and its offset inside the 160-byte keymap; the full encoder
    records, the function-table entries and the encoder mode bytes get the same
    address -> frame maps,
  * the default keymap (from KBconfig.ini) so the UI has something to show,
  * the measured pauses between frames (`delays`) and the short pause the page
    uses before reading an answer to a memory-read command.

The browsers then only need to mutate frame[6] of a patch target.

Usage:
    python3 tools/build_webhid.py [--capture FILE] [--keymap FILE]
                                  [--out webhid/k815_session.js]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures                # noqa: E402
import k815_proto as P          # noqa: E402

# address -> keymap offset bases (see PROTOCOL.md / KEYS.md)
KEYMAP_A_BASE = 0x20
KEYMAP_B_BASE = 0x49
KEYMAP_LEN = 160

# Phase-B encoder records: the media function (Consumer usage, little-endian)
# sits twice in each 16-byte record (PROTOCOL.md, "Fn actions, encoder, and macros").
ENCODER_RECORDS = {"enc0": 0x0168, "enc1": 0x0178}
ENCODER_CODE_OFFSET = 6
ENCODER_COPY_STRIDE = 6

# Pause before the host issues GET_FEATURE after a memory-read command
# (sub=0x05); the vendor application leaves a small gap there.
READ_PAUSE_MS = 1

# Pauses shorter than this are jitter of the capture, not intentional gaps.
MIN_DELAY_MS = 20


def session_delays(capture: str, frames: list) -> tuple:
    """Measure the pauses of `frames` in a capture.

    Thin wrapper around `k815_proto.capture_delays`; `read_pause_ms` is the
    short gap the page leaves before GET_FEATURE after a memory-read command.
    """
    return P.capture_delays(capture, frames), READ_PAUSE_MS


def keymap_from_ini(path: str) -> bytes | None:
    if not path or not os.path.exists(path):
        return None
    cells = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if "=" not in line or "_" not in line.split("=", 1)[0]:
            continue
        k, _, v = line.partition("=")
        try:
            r, c = k.strip().split("_")
            cells[(int(r), int(c))] = int(v.strip().split(",")[0], 0) & 0xFF
        except ValueError:
            continue
    if len(cells) < KEYMAP_LEN:
        return None
    km = bytearray(KEYMAP_LEN)
    for (r, c), v in cells.items():
        if r < 8 and c < 20:
            km[8 * c + r] = v
    return bytes(km)


def build(capture: str, keymap_ini: str):
    prog, _ = P.program_from_capture(capture)
    frames = P.emit(prog)
    base = prog.base
    record_base = prog.record_base
    km_b_base = prog.b_addr(KEYMAP_B_BASE)
    enc_records = {name: prog.b_addr(addr)
                   for name, addr in ENCODER_RECORDS.items()}

    keymap_a, keymap_b = [], []
    led_targets = {}
    enc_targets = {}
    enc_record_targets = {}
    entry_targets = {}
    mode_targets = {}
    enc_addrs = {enc_records[name] + ENCODER_CODE_OFFSET
                 + k * ENCODER_COPY_STRIDE + b: (name, k, b)
                 for name in ENCODER_RECORDS
                 for k in (0, 1) for b in (0, 1)}
    table_phys = base + (P.FUNCTION_TABLE_BASE - 0x0001)
    table_len = P.FUNCTION_TABLE_ENTRIES * P.FUNCTION_ENTRY_LEN
    phase = "A"
    for i, f in enumerate(frames):
        if f[1] == P.OP_MEM and f[2] == P.SUB_READ:
            phase = "B"
            continue
        if f[1] != P.OP_MEM or f[2] != P.SUB_WRITE:
            continue
        eff = (f[4] | (f[5] << 8)) + f[3]
        if phase == "A" and eff == P.ENCODER_MODE_A_ADDR:
            mode_targets[P.ENCODER_MODE_A_ADDR] = i
        elif phase == "A" and KEYMAP_A_BASE <= eff < KEYMAP_A_BASE + KEYMAP_LEN:
            keymap_a.append([i, eff - KEYMAP_A_BASE])
        elif phase == "B" and km_b_base <= eff < km_b_base + KEYMAP_LEN:
            keymap_b.append([i, eff - km_b_base])
        elif phase == "B" and base <= eff <= base + 0x20:
            # Keys are the staging addresses 0x01..0x21, independent of base.
            led_targets[eff - base + 0x01] = i
        elif phase == "B" and table_phys <= eff < table_phys + table_len:
            entry_targets[P.FUNCTION_TABLE_BASE + (eff - table_phys)] = i
        elif phase == "B" and eff in enc_addrs:
            logical = 0x0168 + (eff - record_base)
            enc_targets[logical] = i
            enc_record_targets[logical] = i
        elif phase == "B" and record_base <= eff < record_base + 0x20:
            enc_record_targets[0x0168 + (eff - record_base)] = i
    if 0x21 in led_targets:
        mode_targets[0x21] = led_targets[0x21]

    def enc_usage(name: str) -> int:
        at = enc_records[name] + ENCODER_CODE_OFFSET
        lo, hi = prog.get("B", at), prog.get("B", at + 1)
        return (lo or 0) | ((hi or 0) << 8)

    blob = b"".join(frames)
    delays, read_pause_ms = session_delays(capture, frames)
    data = {
        "reportId": P.REPORT_ID,
        "reportLen": P.REPORT_LEN,
        "frameCount": len(frames),
        "base": base,
        "recordBase": record_base,
        "capture": os.path.basename(capture),
        "blob": base64.b64encode(blob).decode(),
        "delays": delays,
        "readPauseMs": read_pause_ms,
        "keymapA": keymap_a,
        "keymapB": keymap_b,
        "ledTargets": led_targets,
        "encTargets": enc_targets,
        # full 16-byte rotation records, the mode byte and the 22-entry table
        "encRecordTargets": enc_record_targets,
        "entryTargets": entry_targets,
        "modeTargets": mode_targets,
        "defaultLed": P.read_led(prog),
        "defaultKeymap": list(keymap_from_ini(keymap_ini) or b""),
        # phase-B byte 0x0001: BALLKEYSLOCK / BWINLOCK / BALTF4 / KEYSMODE
        "defaultSwitches": prog.get_b(0x0001) or 0,
        # media usage per encoder, as captured (the app's own defaults)
        "defaultEncoder": {name: enc_usage(name) for name in ENCODER_RECORDS},
        # keyboard usage in a single-key macro record, or null for media
        "defaultEncoderKeys": {name: P.read_encoder_macro(prog, name)
                               for name in ENCODER_RECORDS},
    }
    if len(keymap_a) != KEYMAP_LEN or len(keymap_b) != KEYMAP_LEN:
        print(f"warning: keymapA={len(keymap_a)} keymapB={len(keymap_b)} "
              f"(expected {KEYMAP_LEN})", file=sys.stderr)
    return data


def led_effects(path: str = None) -> list:
    """The LED effect catalogue for the page (tools/led_modes.json).

    Returns a list of ``{"mode", "en", "in_ui_list"}``; an empty list when the
    catalogue cannot be read, so the page then shows plain numbers.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "led_modes.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError:
        return []
    out = []
    for m in data.get("modes", []):
        out.append({"mode": m["mode"], "en": m["en"],
                    "in_ui_list": bool(m.get("in_ui_list", False))})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture", default=fixtures.capture())
    ap.add_argument("--keymap", default=fixtures.kbconfig())
    ap.add_argument("--led-modes", default=None,
                    help="effect catalogue (default: tools/led_modes.json)")
    ap.add_argument("--out", default="webhid/k815_session.js")
    args = ap.parse_args()

    data = build(args.capture, args.keymap)
    effects = led_effects(args.led_modes)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("// generated by tools/build_webhid.py -- do not edit\n")
        fh.write("export const SESSION = ")
        json.dump(data, fh, separators=(",", ":"))
        fh.write(";\n")
        fh.write("\n// LED effect catalogue from tools/led_modes.json\n")
        fh.write("export const LED_EFFECTS = ")
        json.dump(effects, fh, separators=(",", ":"))
        fh.write(";\n")
    print(f"wrote {args.out}: {data['frameCount']} frames, "
          f"{len(data['blob'])} base64 chars, "
          f"keymapA={len(data['keymapA'])}, keymapB={len(data['keymapB'])}, "
          f"{len(data['delays'])} frame delays, "
          f"{len(effects)} led effects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
