#!/usr/bin/env python3
"""Build the table "function number in config.ini -> HID usage" from sweep runs.

The second number of `USCROLL0` / `USCROLL1` (section `[Config1]`) is the function
number. The application uploads it into the phase-B encoder record: the code sits
at `0x0168+6` (encoder 0) and `0x0178+6` (encoder 1) as a 16-bit LE value on the
Consumer page (`0x0C`).

Usage:
    python3 tools/media_table.py --dir /tmp/opencode/sweep3 [--json out.json]

Each number is mapped to:
    * `usage` - the code that actually reaches the device (the truth for Linux),
    * `hid`   - the name from the HID Usage Tables (Consumer page),
    * `app`   - the label from `[UniqueName]` of the same skin (for cross-checking; it drifts).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k815_proto as P          # noqa: E402

ENC0_CODE = 0x0168 + 6
ENC1_CODE = 0x0178 + 6

USAGE_JSON = os.path.join(ROOT, "tools/hid_consumer_usages.json")
SKIN_INI = os.path.join(ROOT, "extracted/app/1/skins/1_INI_EN/KbIndexText.ini")


def hid_names() -> dict[int, str]:
    tbl = json.load(open(USAGE_JSON, encoding="utf-8"))
    return {int(k, 16): v[1] for k, v in tbl.items()}


def app_names() -> dict[int, str]:
    raw = open(SKIN_INI, "rb").read()
    txt = raw.decode("utf-16") if raw[:2] == b"\xff\xfe" else raw.decode("gbk", "replace")
    m = re.search(r"\[UniqueName\](.*?)(?=\n\s*\[|\Z)", txt, re.S)
    out = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith(("//", ";")):
            continue
        key, _, val = line.partition("=")
        if not key.strip().isdigit():
            continue
        out[int(key)] = val.split(",")[-2] if val.count(",") >= 2 else val.strip(",")
    return out


def code_at(path: str, addr: int) -> int | None:
    prog, _ = P.program_from_capture(path)
    lo, hi = prog.get("B", addr), prog.get("B", addr + 1)
    if lo is None or hi is None:
        return None
    return lo | (hi << 8)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="/tmp/opencode/sweep3")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    ref = os.path.join(os.path.dirname(args.dir), "sweep_ref.jsonl")
    base = [code_at(ref, ENC0_CODE), code_at(ref, ENC1_CODE)]
    hid, app = hid_names(), app_names()

    found: dict[int, int] = {}
    for path in sorted(glob.glob(os.path.join(args.dir, "media_*.jsonl"))):
        nums = [int(x) for x in re.findall(r"\d+", os.path.basename(path))]
        rec = [code_at(path, ENC0_CODE), code_at(path, ENC1_CODE)]
        for i, n in enumerate(nums):
            if i < len(rec) and rec[i] is not None:
                found[n] = rec[i]

    print(f"reference: encoder0 = 0x{base[0]:04X} ({hid.get(base[0], '?')}), "
          f"encoder1 = 0x{base[1]:04X} ({hid.get(base[1], '?')})")
    print(f"{'number':>6}  {'usage':<7} {'HID usage':<34} {'[UniqueName]':<18}")
    for n in sorted(found):
        u = found[n]
        print(f"{n:>6}  0x{u:04X}  {hid.get(u, '?'):<34} {app.get(n, '—'):<18}")

    missing = [n for n in range(0, 106) if n not in found]
    if missing:
        print("\nnot captured:", ", ".join(map(str, missing)))

    if args.json:
        json.dump({str(n): {"usage": found[n], "hid": hid.get(found[n], ""),
                            "app": app.get(n, "")} for n in sorted(found)},
                  open(args.json, "w"), indent=1, ensure_ascii=False)
        print(f"\ntable written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
