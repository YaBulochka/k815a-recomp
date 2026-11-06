#!/usr/bin/env python3
"""Offline checks for the device model and the CLI helpers in tools/.

No hardware, no emulator, no browser: everything runs against a captured
session.  Covered here:

  * the capture still round-trips byte for byte through tools/k815_proto.py;
  * the switches and encoder helpers write the same bytes the WebHID driver
    does (the expected values are the ones tools/selftest_webhid.py feeds to
    the JS driver, so the two implementations stay pinned to each other);
  * a preset exported from a template applies back to the identical stream;
  * a WebHID-style JSON export (the file the browser page writes) applies
    exactly like the equivalent native CLI edits — this pins the
    "export from the page, apply with k815ctl" round trip;
  * a backup artifact restores its stream and rejects a tampered file;
  * a device dump (page JSON or raw binary) decodes into the same preset,
    rejecting a bad sha256 or a gap;
  * the 22-entry function table and the five factory macro records.

    python3 tools/selftest_cli.py [capture.jsonl]
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k815_proto as P          # noqa: E402
import k815ctl as C             # noqa: E402
import fixtures                 # noqa: E402

# Single source of truth for the expected patch values: the same constants the
# WebHID selftest hands to webhid/k815.js.
from selftest_webhid import (TEST_ENC, TEST_ENC_BYTES, TEST_LED, TEST_SW,  # noqa: E402
                             TEST_SW_BYTE)

DEFAULT_CAPTURE = fixtures.capture()

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return bool(ok)


def _apply_without_slots(capture, browser):
    """Apply a browser export with the per-slot scalar keys removed."""
    trimmed = {k: copy.deepcopy(v) for k, v in browser.items()
               if k not in {sid for sid, _l, _i in P.SLOTS}}
    prog, _ = P.program_from_capture(capture)
    P.apply_preset(prog, trimmed)
    return prog


def main():
    capture = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CAPTURE
    print(f"capture: {os.path.relpath(capture, ROOT)}")

    # 1. the model still reproduces the capture byte for byte
    ok, got, want, bad = P.verify(capture)
    check("emit(program_from_capture) == capture", ok,
          f"{got}/{want}" + (f", first bad {bad}" if bad is not None else ""))

    prog, mem = P.program_from_capture(capture)
    baseline = P.emit(prog)
    same_phases = (P.read_keymap(prog, "A")
                   == P.read_keymap(prog, "B", P.KEYMAP_BASE_B))

    # 2. switches helper (phase-B byte 0x0001)
    check("switch_value(TEST_SW) == TEST_SW_BYTE",
          P.switch_value(TEST_SW) == TEST_SW_BYTE, hex(P.switch_value(TEST_SW)))
    check("all four switches on == 0xc5",
          P.switch_value({k: True for k in P.SWITCH_BITS}) == 0xC5)
    check("empty switches dict == 0x00", P.switch_value({}) == 0x00)

    p2, _ = P.program_from_capture(capture)
    n = P.apply_switches(p2, TEST_SW)
    check("apply_switches changes exactly one byte", n == 1, f"n={n}")
    check("read_switches round-trips the byte",
          P.read_switches(p2) == {k: bool(TEST_SW_BYTE & b)
                                 for k, b in P.SWITCH_BITS.items()})
    check("apply_switches is idempotent", P.apply_switches(p2, TEST_SW) == 0)

    # 3. encoder helper (media functions, phase-B records 0x168/0x178)
    p3, _ = P.program_from_capture(capture)
    n = P.patch_encoder(p3, TEST_ENC)
    check("patch_encoder writes the expected bytes",
          all(p3.get("B", a) == (v & 0xFF) and p3.get("B", a + 1) == (v >> 8)
              for a, v in TEST_ENC_BYTES.items()), f"n={n}")
    check("patch_encoder change count == 6", n == 6, f"n={n}")
    check("patch_encoder is idempotent", P.patch_encoder(p3, TEST_ENC) == 0)
    enc_read = P.read_encoder(p3)
    check("read_encoder finds the numbers",
          [enc_read["enc0"]["number"], enc_read["enc1"]["number"]] == [0, 16],
          str({k: v["number"] for k, v in enc_read.items()}))

    p4, _ = P.program_from_capture(capture)
    P.patch_encoder(p4, {"enc0": 0, "enc1": 16})
    check("number 0 zeroes the second copy",
          p4.get("B", 0x0174) == 0x00 and p4.get("B", 0x0175) == 0x00)
    check("number 0 still writes the first copy",
          p4.get("B", 0x016E) == 0xCD and p4.get("B", 0x016F) == 0x00)

    p5, _ = P.program_from_capture(capture)
    check("unknown encoder numbers are skipped",
          P.patch_encoder(p5, {"enc0": -1, "enc1": 99}) == 0
          and P.emit(p5) == baseline)

    # rotation can send a keyboard key only as a single-key macro record
    p5m, _ = P.program_from_capture(capture)
    P.patch_encoder_macro(p5m, {"enc0": 0x56, "enc1": 0x57})
    check("encoder macro records are written",
          P.encoder_macro_record(0x56)[:6].hex() == "010086560656")
    check("read_encoder_macro finds the usages",
          [P.read_encoder_macro(p5m, "enc0"),
           P.read_encoder_macro(p5m, "enc1")] == [0x56, 0x57])
    enc_preset = P.preset_from_program(p5m)
    check("preset exports encoderKeys",
          enc_preset.get("encoderKeys") == {"enc0": 0x56, "enc1": 0x57})
    p5r, _ = P.program_from_capture(capture)
    P.apply_preset(p5r, {"encoderKeys": {"enc0": 0x56, "enc1": 0x57}})
    check("encoderKeys applies back to the same stream",
          P.emit(p5r) == P.emit(p5m))

    # 4. preset: export -> apply must be a no-op on the captured template
    preset = P.preset_from_program(prog, capture)
    check("preset carries 160 keymap bytes", len(preset["keymap"]) == 160)
    check("preset mirrors the keymap into the slot ids",
          all(preset[sid] == preset["keymap"][idx] for sid, _l, idx in P.SLOTS))
    check("preset switches match the capture byte",
          P.switch_value(preset["switches"]) == (prog.get("B", 0x0001) or 0))
    check("preset encoder holds config numbers",
          all(isinstance(v, int) for v in preset["encoder"].values()))

    if same_phases:
        p6, _ = P.program_from_capture(capture)
        counts = P.apply_preset(p6, preset)
        check("preset round-trip is byte-identical", P.emit(p6) == baseline,
              str(counts))
    else:
        print("  skip  preset round-trip (this capture writes different "
              "keymaps in phases A and B)")

    # a preset with one section touches only that section
    p7, _ = P.program_from_capture(capture)
    P.apply_preset(p7, {"switches": {"altF4": False, "winLock": True}})
    check("partial preset leaves the keymap alone",
          P.read_keymap(p7, "A") == P.read_keymap(prog, "A"))
    check("partial preset writes the switches",
          P.read_switches(p7)["winLock"] and not P.read_switches(p7)["altF4"])

    # the LED section accepts the browser's field names (rgbEn)
    p8, _ = P.program_from_capture(capture)
    P.apply_preset(p8, {"led": TEST_LED})
    led = P.read_led(p8)
    check("preset LED palette applies",
          [list(c) for c in led["palette"]] == TEST_LED["palette"])
    check("preset LED effect fields apply",
          all(led[k] == TEST_LED[j] for k, j in
              [("mode", "mode"), ("speed", "speed"), ("direction", "direction"),
               ("symmetry", "symmetry"), ("single", "single"),
               ("react", "react"), ("rgb_en", "rgbEn")]),
          str({k: led[k] for k in ("mode", "speed", "direction", "rgb_en")}))

    # 5. backup / restore artifacts
    art = C.frames_artifact(baseline, capture, preset)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "b.json")
        C.write_json(path, art)
        got_art, frames = C.read_frames_artifact(path)
        check("restore returns the same frames", frames == baseline)
        check("artifact keeps the decoded preset",
              got_art["preset"]["encoder"] == preset["encoder"])

        tampered = copy.deepcopy(art)
        tampered["frames"][10] = "07" + "00" * 15
        C.write_json(path, tampered)
        try:
            C.read_frames_artifact(path)
            check("tampered artifact is rejected", False)
        except SystemExit:
            check("tampered artifact is rejected", True)

        short = copy.deepcopy(art)
        short["frames"][10] = "0700"
        short["sha256"] = ""
        C.write_json(path, short)
        try:
            C.read_frames_artifact(path)
            check("short frame is rejected", False)
        except SystemExit:
            check("short frame is rejected", True)

    # 6. the KBconfig.ini loader agrees with the captured keymap
    km = C.load_keymap(fixtures.kbconfig())
    check("KBconfig.ini keymap is 160 bytes", len(km) == 160)
    check("KBconfig.ini keymap matches the template phase A",
          bytes(km) == P.read_keymap(prog, "A"))

    # 7. a WebHID-style export (version 3, no "template" key, one scalar per
    #    slot, camelCase LED fields) must apply exactly like native edits
    browser = {k: copy.deepcopy(v) for k, v in preset.items() if k != "template"}
    browser["keymap"][8] = 0x68                  # key 1 -> F13
    browser["keymap"][9] = 0x2C                  # key 5 -> Space
    browser["switches"] = {"allKeysLock": False, "winLock": True,
                           "altF4": True, "keyMode": True}
    browser["led"] = copy.deepcopy(TEST_LED)
    browser["encoder"] = dict(TEST_ENC)
    for sid, _label, idx in P.SLOTS:
        browser[sid] = browser["keymap"][idx]

    p9, _ = P.program_from_capture(capture)
    counts = P.apply_preset(p9, browser)

    p10, _ = P.program_from_capture(capture)
    edited = bytes(browser["keymap"])
    p10.patch("A", P.KEYMAP_BASE, edited)
    p10.patch("B", P.KEYMAP_BASE_B, edited)
    P.apply_switches(p10, browser["switches"])
    P.apply_led(p10, palette=[list(c) for c in TEST_LED["palette"]],
                mode=TEST_LED["mode"], speed=TEST_LED["speed"],
                direction=TEST_LED["direction"], symmetry=TEST_LED["symmetry"],
                single=TEST_LED["single"], react=TEST_LED["react"],
                rgb_en=TEST_LED["rgbEn"])
    P.patch_encoder(p10, TEST_ENC)

    emitted = P.emit(p9)
    check("WebHID-style export == native CLI edits", emitted == P.emit(p10),
          f"{len(emitted)} frames, " + str(counts))
    check("WebHID-style export reaches the keymap",
          P.read_keymap(p9, "A")[8] == 0x68 and P.read_keymap(p9, "A")[9] == 0x2C)
    check("WebHID-style export reaches the encoder",
          P.read_encoder(p9)["enc1"]["number"] == TEST_ENC["enc1"])
    check("an export without the slot ids still applies",
          P.emit(_apply_without_slots(capture, browser)) == emitted)

    # 8. the generated page data and the CLI show the same effect catalogue
    js = open(os.path.join(ROOT, "webhid/k815_session.js"),
              encoding="utf-8").read()
    m = re.search(r"export const LED_EFFECTS = (\[.*?\]);", js, re.S)
    page_modes = {e["mode"]: e["en"] for e in (json.loads(m.group(1)) if m else [])}
    names = C.led_effect_names()
    check("page LED catalogue == tools/led_modes.json",
          bool(names) and page_modes == names, f"{len(names)} effects")
    check("every catalogued mode has an in_ui_list flag",
          bool(m) and all(isinstance(e.get("in_ui_list"), bool)
                          for e in json.loads(m.group(1))))

    # 9. a device dump decodes back into the same preset
    dump_record = P.make_dump(0, mem)
    with tempfile.TemporaryDirectory() as td:
        json_path = os.path.join(td, "dump.json")
        raw_path = os.path.join(td, "dump.bin")
        C.write_json(json_path, dump_record)
        open(raw_path, "wb").write(bytes(mem))

        image, addr, rec = P.read_dump(json_path)
        check("dump JSON round-trips the memory image",
              image == bytes(mem) and addr == 0, f"{len(image)} bytes")
        check("dump sha256 covers the image",
              rec.get("sha256") == hashlib.sha256(bytes(mem)).hexdigest())

        dump_preset = P.preset_from_image(image, addr, os.path.basename(capture))
        check("dump preset == template preset", dump_preset == preset)
        check("dump keymap comes from the phase-B copy",
              dump_preset["keymap"] == list(P.read_keymap(prog, "B", P.KEYMAP_BASE_B)))

        raw_image, raw_addr, _ = P.read_dump(raw_path)
        check("raw dump reads with base 0",
              raw_image == bytes(mem) and raw_addr == 0)
        try:
            P.preset_from_image(image[:100])
            check("a short dump is rejected", False)
        except ValueError:
            check("a short dump is rejected", True)

        backup_path = os.path.join(td, "backup.json")
        C.write_json(backup_path, {
            "kind": "k815-frames",
            "deviceMemory": {"base": 0, "length": len(mem),
                             "hex": bytes(mem).hex()},
        })
        backup_image, backup_addr, _ = P.read_dump(backup_path)
        check("backup --from-device memory decodes as a dump",
              backup_image == bytes(mem) and backup_addr == 0)

        bad = copy.deepcopy(dump_record)
        bad["sha256"] = "00" * 32
        C.write_json(json_path, bad)
        try:
            P.read_dump(json_path)
            check("a dump with a bad sha256 is rejected", False)
        except ValueError:
            check("a dump with a bad sha256 is rejected", True)

        gap = copy.deepcopy(dump_record)
        gap["sha256"] = ""
        gap["memory"].pop("0x0010")
        C.write_json(json_path, gap)
        try:
            P.read_dump(json_path)
            check("a dump with a gap is rejected", False)
        except ValueError:
            check("a dump with a gap is rejected", True)

        C.write_json(json_path, dump_record)
        out = os.path.join(td, "preset.json")
        C.cmd_preset(types.SimpleNamespace(from_dump=json_path, base=None,
                                           out=out, template=capture))
        check("CLI --from-dump writes the same preset",
              json.load(open(out)) == P.preset_from_image(bytes(mem), 0))

    # 10. the 22-entry function table and the macro records
    table = P.read_function_table(prog)
    check("function table has 22 entries",
          len(table) == P.FUNCTION_TABLE_ENTRIES)
    check("every entry carries the constant tag 0x07",
          all(e["tag"] == P.FUNCTION_ENTRY_TAG for e in table))
    check("the table is followed by a zero separator",
          prog.get("B", P.FUNCTION_TABLE_SEPARATOR) == 0x00)
    check("baseline: only the two encoder entries are active",
          [e["index"] for e in table if e["kind"]] == [20, 21])
    check("baseline: encoder records at 0x168 and 0x178",
          [e["address"] for e in table if e["kind"]] == [0x168, 0x0178])

    macro_prog, _ = P.program_from_capture(fixtures.macro_capture())
    mtable = P.read_function_table(macro_prog)
    mactive = [e for e in mtable if e["kind"]]
    check("macro capture: keys 1..5 activate slots 10..14",
          [e["index"] for e in mactive] == [10, 11, 12, 13, 14, 20, 21])
    check("macro capture: records are packed from 0x168",
          [e["address"] for e in mactive] ==
          [0x168, 0x178, 0x188, 0x198, 0x1A8, 0x1B8, 0x1C8])
    mkm = P.read_keymap(macro_prog, "A")
    check("macro keymap markers follow the key slot",
          [mkm[s] for s in (8, 16, 24, 32, 9)] ==
          [0xAF, 0xB8, 0xB9, 0xBA, 0xBB])
    cut_copy = [P.read_function_record(macro_prog, a).hex(" ")
                for a in (0x168, 0x178)]
    check("Cut and Copy records encode different shortcuts",
          cut_copy == [
              "01 00 86 e0 86 1b 06 e0 06 1b 00 00 00 00 00 00",
              "01 00 86 e0 86 06 06 e0 06 06 00 00 00 00 00 00",
          ])
    check("the five factory macros have distinct records",
          len({P.read_function_record(macro_prog, a)
               for a in (0x168, 0x178, 0x188, 0x198, 0x1A8)}) == 5)

    # The firmware occasionally answered a memory read with a byte from another
    # address (one palette block and keymap slot 49 were once read back wrong).
    # The readers repeat a byte until two answers agree; this is the voting.
    class FakeReadDev:
        """Answers every read from a scripted list, one value per GET."""

        def __init__(self, values):
            self.values = list(values)
            self.gets = 0

        def send_feature_report(self, data):
            return len(data)

        def get_feature_report(self, report_id, size):
            v = self.values[self.gets % len(self.values)]
            self.gets += 1
            return bytes([0x07, v]) + bytes(size - 2)

    check("pick_stable: the majority wins",
          C.pick_stable([0x54, 0x70, 0x70]) == 0x70)
    check("pick_stable: a tie keeps the latest sample",
          C.pick_stable([0x54, 0x70]) == 0x70)
    fake = FakeReadDev([0x70, 0x70])
    check("verified read: two matching answers are accepted",
          C.read_byte_verified(fake, 0x0159) == 0x70 and fake.gets == 2)
    fake = FakeReadDev([0x54, 0x70, 0x70])
    check("verified read: a wrong answer is re-read until it agrees",
          C.read_byte_verified(fake, 0x0159) == 0x70 and fake.gets == 3)

    ok = all(results)
    print(f"CLI SELFTEST {'PASS' if ok else 'FAIL'} "
          f"({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
