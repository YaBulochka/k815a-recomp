#!/usr/bin/env python3
"""Check that the WebHID JS logic reproduces the Python protocol output.

Runs `tools/build_webhid.py`'s data through the browser modules under Node and
compares the patched frame stream byte-for-byte with what tools/k815_proto.py
produces for the same keymap.

    python3 tools/selftest_webhid.py

Requires `node`.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k815_proto as P          # noqa: E402
import k815ctl                  # noqa: E402

TEST_KM = {8: 0xFF, 16: 0x68, 24: 0x3A, 32: 0xE0, 9: 0x2C,
           17: 0x4F, 25: 0x4C, 33: 0x39, 41: 0x6E, 49: 0x73}

TEST_LED = {
    "palette": [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12],
                [13, 14, 15], [15, 14, 13], [12, 11, 10]],
    "mode": 7, "speed": 5, "direction": 2, "symmetry": 1, "single": 1,
    "react": 3, "rgbEn": 0x15,
}

# Phase-B byte 0x0001: BALLKEYSLOCK | BWINLOCK | BALTF4 | KEYSMODE
TEST_SW = {"allKeysLock": False, "winLock": True, "altF4": True, "keyMode": True}
TEST_SW_BYTE = 0x04 | 0x40 | 0x80

# Encoder media functions. Encoder 0 is Play/Pause (its second copy of the code
# is zeroed), encoder 1 is AC Home.
TEST_ENC = {"enc0": 0, "enc1": 16}
TEST_ENC_BYTES = {0x016E: 0x00CD, 0x0174: 0x0000, 0x017E: 0x0223, 0x0184: 0x0223}

NODE_TEST = r"""
import { SESSION } from '%(root)s/webhid/k815_session.js';
import { decodeBlob, patchKeymap, patchLed, patchSwitches, patchEncoder, keymapFromIni } from '%(root)s/webhid/k815.js';
import { readFileSync } from 'node:fs';

const exp = JSON.parse(readFileSync('%(expected)s', 'utf8'));
const frames = decodeBlob(SESSION.blob);
const n = patchKeymap(frames, Uint8Array.from(exp.keymap), SESSION);
const nl = patchLed(frames, exp.led, SESSION);
const ns = patchSwitches(frames, exp.sw, SESSION);
const ne = patchEncoder(frames, exp.enc, SESSION);
const want = decodeBlob(exp.frames);
let bad = -1;
if (frames.length !== want.length) bad = -2;
else for (let i = 0; i < frames.length; i++) if (frames[i] !== want[i]) { bad = i; break; }

const ini = keymapFromIni(readFileSync('%(ini)s', 'utf8'), new Uint8Array(160).fill(0xff));
let iniBad = -1;
for (let i = 0; i < 160; i++) if (ini.keymap[i] !== exp.ini[i]) { iniBad = i; break; }

console.log(JSON.stringify({
  frames: SESSION.frameCount, patched: n, ledPatched: nl, swPatched: ns, encPatched: ne,
  streamOk: bad === -1, streamBadAt: bad,
  iniOk: iniBad === -1, iniBadAt: iniBad, iniSeen: ini.seen,
}));
"""


def main():
    node = shutil.which("node")
    if not node:
        print("node not found, skipping WebHID selftest")
        return 0

    # regenerate the session module so the test always matches the source
    subprocess.run([sys.executable, os.path.join(ROOT, "tools/build_webhid.py")],
                   check=True, cwd=ROOT,
                   stdout=subprocess.DEVNULL)

    km = bytearray(k815ctl.load_keymap(
        os.path.join(ROOT, "extracted/app/config/KBconfig.ini")))
    for i, v in TEST_KM.items():
        km[i] = v

    prog, _ = P.program_from_capture(os.path.join(ROOT, "captures/baseline_apply.jsonl"))
    prog.patch("A", P.KEYMAP_BASE, bytes(km))
    prog.patch("B", P.KEYMAP_BASE_B, bytes(km))
    led = TEST_LED
    P.apply_led(prog, palette=led["palette"], mode=led["mode"], speed=led["speed"],
                direction=led["direction"], symmetry=led["symmetry"],
                single=led["single"], react=led["react"], rgb_en=led["rgbEn"])
    # Switches and encoder go through the same helpers the CLI uses, so the JS
    # driver is compared against the Python implementation rather than against
    # a hand-written patch sequence.
    py_sw = P.apply_switches(prog, TEST_SW)
    py_enc = P.patch_encoder(prog, TEST_ENC)
    py_ok = (
        P.switch_value(TEST_SW) == TEST_SW_BYTE
        and py_sw == 1
        and prog.get("B", P.SWITCH_ADDR) == TEST_SW_BYTE
        and all(prog.get("B", a) == (v & 0xFF) and prog.get("B", a + 1) == (v >> 8)
                for a, v in TEST_ENC_BYTES.items())
    )
    frames = P.emit(prog)

    with tempfile.TemporaryDirectory() as td:
        expected = os.path.join(td, "expected.json")
        json.dump({"keymap": list(km),
                   "led": led,
                   "sw": TEST_SW,
                   "enc": TEST_ENC,
                   "ini": list(k815ctl.load_keymap(
                       os.path.join(ROOT, "extracted/app/config/KBconfig.ini"))),
                   "frames": base64.b64encode(b"".join(frames)).decode()},
                  open(expected, "w"))
        script = os.path.join(td, "t.mjs")
        open(script, "w").write(NODE_TEST % {
            "root": ROOT, "expected": expected,
            "ini": os.path.join(ROOT, "extracted/app/config/KBconfig.ini")})
        out = subprocess.run([node, script], capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stdout)
        print(out.stderr)
        return 1
    res = json.loads(out.stdout.strip().splitlines()[-1])
    counts_match = (res["swPatched"] == py_sw and res["encPatched"] == py_enc)
    ok = res["streamOk"] and res["iniOk"] and py_ok and counts_match
    print(f"frames={res['frames']} patched={res['patched']} "
          f"ledPatched={res['ledPatched']} swPatched={res['swPatched']} "
          f"encPatched={res['encPatched']} "
          f"stream={'OK' if res['streamOk'] else f'BAD@{res["streamBadAt"]}'} "
          f"ini={'OK' if res['iniOk'] else f'BAD@{res["iniBadAt"]}'} "
          f"pySw={py_sw} pyEnc={py_enc} pyModel={'OK' if py_ok else 'BAD'}")
    print("WEBHID SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
