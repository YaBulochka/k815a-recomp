#!/usr/bin/env python3
"""Diff two capture files by effective device address.

Usage:
    python3 tools/diff_captures.py BASE.jsonl TEST.jsonl [--phase A|B|both]
                                                      [--all]

Prints, for each phase, the addresses whose byte value differs between the
two upload sessions, as ``addr: base -> test``.  This is how the LED and
macro memory maps were reverse engineered from differential captures.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import k815_proto as P          # noqa: E402


def phase_map(path: str) -> dict:
    rep = P._set_reports(path)
    prog, _ = P.program_from_reports(rep)
    ph = {"A": {}, "B": {}}
    for it in prog.blocks:
        for i, v in enumerate(it.data):
            ph[it.phase][it.addr + i] = v
    return ph


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base")
    ap.add_argument("test")
    ap.add_argument("--phase", choices=("A", "B", "both"), default="both")
    args = ap.parse_args(argv)

    a, b = phase_map(args.base), phase_map(args.test)
    phases = ("A", "B") if args.phase == "both" else (args.phase,)
    total = 0
    for phase in phases:
        diffs = [x for x in sorted(set(a[phase]) | set(b[phase]))
                 if a[phase].get(x) != b[phase].get(x)]
        total += len(diffs)
        print(f"phase {phase}: {len(diffs)} differing bytes")
        for x in diffs:
            print(f"    {x:#06x}: {a[phase].get(x, 0):#04x} -> {b[phase].get(x, 0):#04x}")
    return 0 if total else 0


if __name__ == "__main__":
    sys.exit(main())
