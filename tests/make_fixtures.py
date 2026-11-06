#!/usr/bin/env python3
"""Regenerate the committed test fixtures from the raw artifacts.

The fixtures let `tools/run_all.sh` run on a fresh clone, where `captures/` and
`extracted/` (both gitignored) are absent.  Run this only after unpacking the
vendor application and recording the baseline session:

    innoextract -d extracted "MKESPN K815.EN[20250610].exe"
    # ... capture captures/baseline_apply.jsonl ...
    # optionally capture captures/macro5.jsonl (see tests/fixtures/README.md)
    python3 tests/make_fixtures.py
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, "tests/fixtures")
CAPTURE = os.path.join(ROOT, "captures/baseline_apply.jsonl")
MACRO_CAPTURE = os.path.join(ROOT, "captures/macro5.jsonl")
KBCONFIG = os.path.join(ROOT, "extracted/app/config/KBconfig.ini")
CONFIG = os.path.join(ROOT, "extracted/app/config/config.ini")


def write_capture(src, dst_name):
    """Keep the SET reports and their timestamps; drop the rest of the log."""
    dst = os.path.join(HERE, dst_name)
    n = 0
    raw = gzip.GzipFile(dst, "wb", compresslevel=9, mtime=0)
    with io.TextIOWrapper(raw, encoding="utf-8", newline="\n") as out:
        for line in open(src, encoding="utf-8"):
            rec = json.loads(line)
            if rec.get("kind") != "SET" or len(rec.get("raw", "")) != 32:
                continue
            keep = {k: rec[k] for k in ("t", "dev", "kind", "raw") if k in rec}
            out.write(json.dumps(keep, separators=(",", ":")) + "\n")
            n += 1
    print(f"{os.path.relpath(dst, ROOT)}: {n} SET reports")


def write_capture_if_present(path, dst_name):
    if os.path.exists(path):
        write_capture(path, dst_name)
    else:
        print(f"skip {dst_name}: {os.path.relpath(path, ROOT)} not found")


def write_kbconfig():
    """Normalize the factory keymap matrix to one cell per line."""
    cells = {}
    for line in open(KBCONFIG, encoding="utf-8", errors="replace"):
        line = line.strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        try:
            a, b = key.strip().split("_")
            cells[(int(a), int(b))] = int(val.strip().split(",")[0], 0) & 0xFF
        except ValueError:
            continue
    if len(cells) < 160:
        raise SystemExit(f"{KBCONFIG}: expected 160 cells, found {len(cells)}")
    dst = os.path.join(HERE, "KBconfig.ini")
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("[KBconfig]\n")
        for b in range(20):
            for a in range(8):
                fh.write(f"{a}_{b}={cells[(a, b)]}\n")
    print(f"{os.path.relpath(dst, ROOT)}: {len(cells)} cells")


def write_config():
    """Keep the effective KU_* values the emulator seeds its memory with."""
    values = {}
    for line in open(CONFIG, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line.startswith("KU_"):
            continue
        key, _, val = line.partition("=")
        try:
            values[int(key[3:])] = int(val.strip().split(",")[0], 0) & 0xFF
        except ValueError:
            continue
    if not values:
        raise SystemExit(f"{CONFIG}: no KU_* entries")
    dst = os.path.join(HERE, "config.ini")
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("[Config1]\n")
        for i in sorted(values):
            fh.write(f"KU_{i}={values[i]}\n")
    print(f"{os.path.relpath(dst, ROOT)}: {len(values)} KU_* entries")


def main():
    for path in (CAPTURE, KBCONFIG, CONFIG):
        if not os.path.exists(path):
            raise SystemExit(f"missing {path}; unpack the vendor app and record "
                             "the baseline capture first")
    os.makedirs(HERE, exist_ok=True)
    write_capture(CAPTURE, "baseline_apply.jsonl.gz")
    write_capture_if_present(MACRO_CAPTURE, "macro5.jsonl.gz")
    write_kbconfig()
    write_config()


if __name__ == "__main__":
    sys.exit(main())
