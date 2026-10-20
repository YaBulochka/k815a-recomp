#!/usr/bin/env python3
"""Sweep config.ini fields: run the application under Wine -> see which addresses change.

The idea: we have a reference capture of an upload session. We change one or a few
fields in config.ini, run the original application again (it presses Apply itself
through the click that tools/app_capture.sh performs) and see which bytes in the
stream differ from the reference. This recovers the "field -> memory address" map.

Examples:

    # a single field: UFN_F1 in section Config1 becomes 0xAB
    python3 tools/sweep_config.py --set Config1.UFN_F1=171 --log /tmp/opencode/t1.jsonl

    # a batch of experiments from a file (a JSON list of objects {field: value})
    python3 tools/sweep_config.py --batch /tmp/opencode/batch.json --outdir /tmp/opencode/sweep

    # an experiment that walks the UI: the Light tab, the dropdown, the 3rd item
    python3 tools/sweep_config.py --set Config1.LEDTYPE=0 --log /tmp/opencode/t2.jsonl \
        --clicks "180,90;500,187;543,278"

A field is written as SECTION.KEY=VALUE. The value is substituted exactly as
written, so hexadecimal and composite values are used verbatim:
`Config1.UFN_F1=171`, `Config1.USCROLL0=11,2`. The file is read and written
byte for byte (latin-1), so GBK text is not corrupted.

Clicks: `--clicks "X,Y;X,Y"` for a single experiment, or the `_clicks` key of an
experiment in a batch file. The application opens on the Button tab, so, for
example, an LED experiment starts with a click on the Light tab. The coordinate
map is in `RE-NOTES.md`, section 5: the Light tab is `180,90`, the effect dropdown
is `500,187`, its item number `k` is `543, 219+29.5*k`, Apply is `687,731`.
The `K815_SHOT=/path.png` environment variable takes a window screenshot after
the clicks (handy for checking that a click landed).

Reference (`--ref`): a fresh run with an untouched config only. The capture
`captures/baseline_apply.jsonl` differs from it by the bytes `A 0x0028` and
`B 0x0003`, so it does not work as a sweep reference. In `/tmp/opencode` the
reference lives until a reboot, after which it must be rebuilt:

    python3 tools/sweep_config.py --reference --base /tmp/opencode/config.base2.ini \
        --log /tmp/opencode/sweep_ref.jsonl --display :99
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import diff_captures as DC        # noqa: E402

APP_CONFIG = os.path.join(ROOT, "extracted/app/config/config.ini")
BASE_DEFAULT = "/tmp/opencode/config.base.ini"
REF_DEFAULT = os.path.join(ROOT, "captures/baseline_apply.jsonl")


# --------------------------------------------------------------------------- #
# ini editing
# --------------------------------------------------------------------------- #
def read_ini(path):
    return open(path, encoding="latin-1").read().splitlines(keepends=True)


def write_ini(path, lines):
    open(path, "w", encoding="latin-1").writelines(lines)


def set_field(lines, section, key, value):
    """Replace key=value inside the given section. Returns the number of edits."""
    cur, hit = None, 0
    for i, ln in enumerate(lines):
        m = re.match(r"\s*\[(.+?)\]", ln)
        if m:
            cur = m.group(1).strip()
            continue
        m = re.match(r"(\s*)([A-Za-z_0-9]+)\s*=(.*?)(\r?\n)?$", ln)
        if m and cur == section and m.group(2) == key:
            lines[i] = f"{m.group(1)}{key}={value}{m.group(4) or ''}"
            hit += 1
    return hit


def parse_value(raw):
    """The value is substituted exactly as written (including "11,2")."""
    return raw


def apply_spec(lines, spec):
    """spec: a list of strings of the form SECTION.KEY=VALUE."""
    done = []
    for item in spec:
        left, _, raw = item.partition("=")
        section, _, key = left.rpartition(".")
        if not section or not key:
            raise ValueError(f"expected SECTION.KEY=VALUE, got {item!r}")
        value = parse_value(raw)
        n = set_field(lines, section, key, value)
        if not n:
            raise KeyError(f"{section}.{key} not found")
        done.append((f"{section}.{key}", value, n))
    return done


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def ensure_display(display):
    """Start Xvfb if the display is missing. Returns the process or None."""
    if not display:
        return None
    dnum = display.lstrip(":")
    if os.path.exists(f"/tmp/.X11-unix/X{dnum}"):
        return None
    p = subprocess.Popen(["Xvfb", display, "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    time.sleep(2)
    print(f"started Xvfb {display}")
    return p


def run_capture(ini_path, log_path, display, clicks=""):
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display
        # Otherwise Wine with a Wayland session starts waylanddrv and the window
        # lands outside Xvfb, so neither the screenshot nor the coordinate click hits.
        env.pop("WAYLAND_DISPLAY", None)
    r = subprocess.run(["sh", os.path.join(ROOT, "tools/app_capture.sh"),
                        log_path, ini_path, "", "", clicks],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    return r


def compare(ref_map, test_path):
    """Addresses that differ from the reference."""
    try:
        t = DC.phase_map(test_path)
    except Exception as e:
        return [], f"cannot parse {test_path}: {e}"
    out = []
    for phase in ("A", "B"):
        for addr in sorted(set(ref_map[phase]) | set(t[phase])):
            a, b = ref_map[phase].get(addr), t[phase].get(addr)
            if a != b:
                out.append((phase, addr, a, b))
    return out, None


def one(lines, spec, base, log, display, ref_map, keep_ini=None, clicks=""):
    tmp = keep_ini or os.path.join("/tmp/opencode", "sweep.ini")
    write_ini(tmp, lines)
    r = run_capture(tmp, log, display, clicks)
    if r.returncode != 0 or not os.path.exists(log):
        return None, f"the run failed (exit code {r.returncode}); tail of the output:\n" + \
            "\n".join(r.stdout.splitlines()[-6:])
    diffs, err = compare(ref_map, log)
    return diffs, err


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE_DEFAULT if os.path.exists(BASE_DEFAULT) else APP_CONFIG)
    ap.add_argument("--ref", default=REF_DEFAULT)
    ap.add_argument("--log", default="/tmp/opencode/sweep.jsonl")
    ap.add_argument("--display", default=":99")
    ap.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE")
    ap.add_argument("--batch", help="JSON file with a list of experiments")
    ap.add_argument("--clicks", default="",
                    help="clicks before Apply: \"X,Y;X,Y\" (tabs/dropdowns)")
    ap.add_argument("--outdir", default="/tmp/opencode/sweep")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reference", action="store_true",
                    help="take the reference from an untouched base (a single run)")
    args = ap.parse_args()

    base_lines = read_ini(args.base)

    xvfb = ensure_display(args.display)
    if xvfb:
        atexit.register(lambda: os.killpg(xvfb.pid, signal.SIGTERM))

    experiments = []
    if args.reference:
        experiments.append({"name": "reference-base", "set": [], "clicks": args.clicks})
    if args.set:
        experiments.append({"name": "single", "set": args.set, "clicks": args.clicks})
    if args.batch:
        for i, exp in enumerate(json.load(open(args.batch))):
            if not isinstance(exp, dict):
                continue
            spec = exp.get("_set") or [f"{k}={v}" for k, v in exp.items()
                                       if not k.startswith("_")]
            experiments.append({"name": exp.get("_name", f"experiment{i+1}"),
                                "set": spec,
                                "clicks": exp.get("_clicks", "")})
    if not experiments:
        print("nothing to do: --set or --batch is required")
        return 2

    if args.dry_run:
        for exp in experiments:
            lines = list(base_lines)
            done = apply_spec(lines, exp["set"])
            print(f"[{exp['name']}] " + ", ".join(f"{k}={v}" for k, v, _ in done))
        return 0

    ref_map = DC.phase_map(args.ref)
    os.makedirs(args.outdir, exist_ok=True)
    print(f"reference: {os.path.relpath(args.ref, ROOT)}")
    print(f"base: {os.path.relpath(args.base, ROOT)}"
          + ("" if os.path.exists(BASE_DEFAULT) else "  (no reference copy, using the current one)"))

    bad = 0
    single = len(experiments) == 1
    for exp in experiments:
        lines = list(base_lines)
        done = apply_spec(lines, exp["set"])
        log = args.log if single else os.path.join(
            args.outdir, re.sub(r"[^A-Za-z0-9_.-]+", "_", exp["name"]) + ".jsonl")
        diffs, err = one(lines, exp["set"], args.base, log, args.display, ref_map,
                         clicks=exp.get("clicks", ""))
        head = ", ".join(f"{k}={v}" for k, v, _ in done)
        if err:
            bad += 1
            print(f"[{exp['name']}] {head}\n    ERROR: {err}")
            continue
        if not diffs:
            print(f"[{exp['name']}] {head}\n    no changes")
            continue
        print(f"[{exp['name']}] {head}")
        for phase, addr, a, b in diffs[:24]:
            print(f"    {phase} {addr:#06x}: {a if a is not None else '—'} -> "
                  f"{b if b is not None else '—'}")
        if len(diffs) > 24:
            print(f"    ... {len(diffs) - 24} more")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
