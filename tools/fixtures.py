"""Resolve the input files the tools and tests run against.

The raw artifacts live in `captures/` (recorded sessions) and `extracted/`
(the unpacked vendor application).  Both are gitignored, so a fresh clone has
neither.  The offline tests therefore fall back to the compact fixtures in
`tests/fixtures/`:

* `baseline_apply.jsonl.gz` — the SET reports of the reference Apply session,
  gzipped, with the original timestamps (7 KB instead of 300 KB);
* `macro5.jsonl.gz` — the session with keys 1..5 assigned to the five factory
  macros (function-table slots and records);
* `KBconfig.ini` — the factory keymap matrix in the shape `k815ctl` reads.

If the real artifacts are present they take precedence, so a developer can
point the tests at a new capture simply by dropping it in `captures/` (or by
passing an explicit path on the command line).
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIXTURE_CAPTURE = os.path.join(ROOT, "tests/fixtures/baseline_apply.jsonl.gz")
FIXTURE_MACRO = os.path.join(ROOT, "tests/fixtures/macro5.jsonl.gz")
FIXTURE_KBCONFIG = os.path.join(ROOT, "tests/fixtures/KBconfig.ini")
FIXTURE_CONFIG = os.path.join(ROOT, "tests/fixtures/config.ini")


def _first(*paths):
    for path in paths:
        if path and os.path.exists(path):
            return path
    return paths[-1]


def capture() -> str:
    """The reference Apply session: a real capture if present, else the fixture."""
    return _first(os.path.join(ROOT, "captures/baseline_apply.jsonl"),
                  FIXTURE_CAPTURE)


def hardware_capture() -> str:
    """The captured real-device session for the current config base.

    Prefer the 0x00E0 layout (what a unit burned with the chip tool reports),
    then the 0x0118 layout, then the emulator baseline.  The WebHID selftest
    and the end-to-end test use this so they exercise the same session the page
    actually ships.
    """
    return _first(os.path.join(ROOT, "tests/fixtures/hw_apply_e0.jsonl.gz"),
                  os.path.join(ROOT, "tests/fixtures/hw_apply.jsonl.gz"),
                  capture())


def kbconfig() -> str:
    """The factory keymap matrix: the vendor file or the committed fixture."""
    return _first(os.path.join(ROOT, "extracted/app/config/KBconfig.ini"),
                  FIXTURE_KBCONFIG)


def macro_capture() -> str:
    """The recorded session with five keys assigned to the factory macros."""
    return FIXTURE_MACRO


def app_config() -> str | None:
    """The vendor `config.ini` for the emulator seed, or None if not unpacked."""
    path = os.path.join(ROOT, "extracted/app/config/config.ini")
    if os.path.exists(path):
        return path
    if os.path.exists(FIXTURE_CONFIG):
        return FIXTURE_CONFIG
    return None
