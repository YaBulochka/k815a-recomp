#!/usr/bin/env python3
"""Configure the MKESPN K815 macro keyboard from Linux.

The device speaks a vendor HID protocol over 16-byte feature reports with
report ID 7 (see tools/k815_proto.py for the frame layout).  This tool can:

  list        enumerate matching devices
  info        perform the startup "device info" read the app does
  dump        read bytes from device memory (sub=0x05 + get_feature)
  replay      send a captured upload session to the device
  keymap      build a full upload from a KBconfig.ini-style 8x20 matrix and
              send it (this is the end goal for keyboard configuration)
  led         show/change the LED palette and effect of the template session
  switches    show/change the four option switches (phase-B byte 0x0001)
  encoder     show/change the encoder media functions (USCROLL0/USCROLL1)
  preset      export the whole state of a template as a JSON preset
  apply       patch a JSON preset into a template and send it
  show        print the keymap, LED, switches and encoder of a captured session
  backup      write a rollback artifact (the exact frame stream + a preset)
  restore     send the exact frame stream stored in a backup artifact
  selftest    replay a capture to a *running emulator* and check that the
              emulator received exactly the same bytes

Everything is transport-only: it never guesses device semantics, it replays
byte-accurate sessions produced by tools/k815_proto.py.

Usage examples:

    python3 tools/k815ctl.py list
    python3 tools/k815ctl.py replay captures/baseline_apply.jsonl --dry-run
    python3 tools/k815ctl.py preset --out my.json
    python3 tools/k815ctl.py apply my.json --dry-run
    python3 tools/k815ctl.py switches --set altF4=0 --dry-run
    python3 tools/k815ctl.py encoder --list
    python3 tools/k815ctl.py backup --out backup.json
    python3 tools/k815ctl.py restore backup.json --dry-run
    python3 tools/k815ctl.py selftest captures/baseline_apply.jsonl --log captures/wine4.jsonl

Before the first write to real hardware run `backup` and keep the artifact: it
holds the exact byte stream of the known-good state, and `restore` replays it
unchanged.  The memory area is only read by `--from-device`, best effort.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import k815_proto as P          # noqa: E402

try:
    import hid
except ImportError:             # pragma: no cover
    hid = None

DEFAULT_VID = 0x30FA
DEFAULT_PID = 0x2350
WRITE_USAGE_PAGE = 0xFF01       # the interface the app writes config to


# --------------------------------------------------------------------------- #
# device access
# --------------------------------------------------------------------------- #
def enumerate_devices(vid, pid):
    if hid is None:
        raise SystemExit("hidapi (python 'hid') is not installed")
    return hid.enumerate(vid, pid)


def open_device(vid, pid, usage_page=None, path=None):
    devs = enumerate_devices(vid, pid)
    if not devs:
        raise SystemExit(f"no device with VID {vid:#06x} PID {pid:#06x}")
    if path:
        cand = [d for d in devs if d["path"] == path.encode()]
    elif usage_page is not None:
        cand = [d for d in devs if d.get("usage_page") == usage_page]
    else:
        cand = devs
    if not cand:
        raise SystemExit("no matching device/interface; try `list`")
    dev = hid.Device(path=cand[0]["path"])
    return dev, cand[0]


def send_report(dev, raw: bytes):
    """Send one 16-byte protocol report as a feature report."""
    if len(raw) != P.REPORT_LEN:
        raw = raw.ljust(P.REPORT_LEN, b"\0")
    return dev.send_feature_report(bytes(raw))


def get_report(dev, report_id=P.REPORT_ID, size=P.REPORT_LEN):
    return bytes(dev.get_feature_report(report_id, size))


def send_frames(args, frames) -> int:
    """Open the device and send a list of 16-byte frames, honouring --pace."""
    dev, info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                            path=args.device)
    print(f"-> {info['path'].decode()}")
    for i, f in enumerate(frames):
        if send_report(dev, f) < 0:
            raise SystemExit(f"send failed at frame {i}")
        if args.pace:
            time.sleep(args.pace)
    print(f"sent {len(frames)} frames")
    return 0


def dump_range(dev, addr: int, length: int, pace: float = 0.0) -> bytes:
    """Read `length` bytes from device memory one byte at a time."""
    out = bytearray()
    for a in range(addr, addr + length):
        if send_report(dev, P.report(P.OP_MEM, P.SUB_READ, 0, a, 0, 0)) < 0:
            raise SystemExit("send_feature_report failed (is a kernel driver bound?)")
        ans = get_report(dev)
        out.append(ans[1] if len(ans) > 1 else 0)
        if pace:
            time.sleep(pace)
    return bytes(out)


def load_template(args):
    """Parse the template capture given by --template."""
    prog, _ = P.program_from_capture(args.template)
    return prog


def finish(args, prog, label="patch") -> int:
    """Emit the patched program and either print it (dry run) or send it."""
    frames = P.emit(prog)
    if getattr(args, "dry_run", False):
        print(f"{label}: {len(frames)} frames (dry run)")
        return 0
    return send_frames(args, frames)


# --------------------------------------------------------------------------- #
# backup artifacts: the exact frame stream, plus a decoded preset for review
# --------------------------------------------------------------------------- #
ARTIFACT_KIND = "k815-frames"
ARTIFACT_VERSION = 1


def frames_artifact(frames, template=None, preset=None, memory=None) -> dict:
    """Pack a frame stream into a restorable artifact."""
    blob = b"".join(frames)
    art = {
        "version": ARTIFACT_VERSION,
        "kind": ARTIFACT_KIND,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "template": os.path.basename(template) if template else None,
        "frameCount": len(frames),
        "reportLen": P.REPORT_LEN,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "frames": [f.hex() for f in frames],
    }
    if preset is not None:
        art["preset"] = preset
    if memory is not None:
        art["deviceMemory"] = memory
    return art


def read_frames_artifact(path: str) -> tuple:
    """Load and validate a backup artifact.  Returns ``(artifact, frames)``."""
    art = json.load(open(path, encoding="utf-8"))
    if not isinstance(art, dict) or art.get("kind") != ARTIFACT_KIND:
        raise SystemExit(f"{path}: not a {ARTIFACT_KIND} artifact")
    if not isinstance(art.get("frames"), list):
        raise SystemExit(f"{path}: no frame list")
    frames = []
    for i, h in enumerate(art["frames"]):
        try:
            f = bytes.fromhex(h)
        except ValueError:
            raise SystemExit(f"{path}: frame {i} is not hex")
        if len(f) != P.REPORT_LEN:
            raise SystemExit(f"{path}: frame {i} is {len(f)} bytes, "
                             f"expected {P.REPORT_LEN}")
        if f[0] != P.REPORT_ID:
            raise SystemExit(f"{path}: frame {i} has report id {f[0]:#04x}, "
                             f"expected {P.REPORT_ID:#04x}")
        frames.append(f)
    if len(frames) != art.get("frameCount"):
        raise SystemExit(f"{path}: {len(frames)} frames, artifact says "
                         f"{art.get('frameCount')}")
    got = hashlib.sha256(b"".join(frames)).hexdigest()
    want = art.get("sha256") or ""
    if got != want:
        raise SystemExit(f"{path}: sha256 mismatch ({got[:12]} != {want[:12]}), "
                         "file is corrupt or edited")
    return art, frames


def write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_list(args):
    devs = enumerate_devices(args.vid, args.pid)
    if not devs:
        print("no matching devices")
        return 1
    for d in devs:
        print(f"{d['path'].decode():>16}  usage_page={d.get('usage_page'):#06x} "
              f"usage={d.get('usage'):#x}  {d.get('product_string','')}")
    return 0


def cmd_info(args):
    dev, info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                            path=args.device)
    ans = get_report(dev)
    print(f"device: {info['path'].decode()} usage_page={info.get('usage_page'):#06x}")
    print(f"info answer: {ans.hex()}")
    return 0


def cmd_dump(args):
    """Read device memory.

    The protocol loads a read address with a normal memory frame (sub=0x05)
    and then the host issues a HID GET_FEATURE; the answer byte arrives in
    byte 1 of the report.  We read one byte per iteration.
    """
    dev, info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                            path=args.device)
    out = dump_range(dev, args.addr, args.length, args.pace)
    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(out)
        print(f"wrote {len(out)} bytes to {args.out}")
    base = args.addr
    for off in range(0, len(out), 16):
        chunk = out[off:off + 16]
        print(f"{base + off:04x}: " + " ".join(f"{c:02x}" for c in chunk))
    return 0


def cmd_replay(args):
    prog, _ = P.program_from_capture(args.capture)
    frames = P.emit(prog)
    print(f"{len(frames)} frames from {args.capture}")
    if args.dry_run:
        for f in frames[:args.show]:
            print("  " + f.hex())
        if len(frames) > args.show:
            print(f"  ... ({len(frames) - args.show} more)")
        return 0
    return send_frames(args, frames)


def cmd_keymap(args):
    """Build a full upload from a hardware keymap matrix and send it.

    The template session is a captured baseline upload; its keymap blocks are
    patched with the new 160-byte matrix (phase A at 0x20, phase B at 0x49)
    and the resulting stream is replayed to the device.
    """
    keymap = load_keymap(args.keymap)
    prog, _ = P.program_from_capture(args.template)
    na = prog.patch("A", P.KEYMAP_BASE, keymap)
    nb = prog.patch("B", P.KEYMAP_BASE_B, keymap)
    print(f"patched keymap: {na} B phase A, {nb} B phase B")
    nled = apply_led_args(prog, args)
    if nled:
        print(f"patched LED: {nled} bytes (phase B)")
    frames = P.emit(prog)
    if args.dry_run:
        print(f"{len(frames)} frames (dry run)")
        return 0
    return send_frames(args, frames)


def load_keymap(path: str) -> bytes:
    """Load a 160-byte keymap from a KBconfig.ini-style file or raw data.

    Accepted forms:
      * 160-byte binary file
      * text of 160 hex byte values (whitespace/comma separated)
      * a KBconfig.ini-style matrix: lines ``<A>_<B>=<value>`` (decimal), where
        ``A`` is the column (0..7) and ``B`` the row (0..19) as in that file, or
        8 lines of 20 two-digit hex values (one line per column, ``A``)
    The result is the transposed upload order: ``upload[8*B + A]``.
    """
    raw = open(path, "rb").read()
    if len(raw) == P.KEYMAP_LEN:
        return raw
    text = raw.decode("ascii", errors="ignore")

    # KBconfig.ini form: 0_1=89  (column 0, row 1)
    cells = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line or "_" not in line.split("=", 1)[0]:
            continue
        key, _, val = line.partition("=")
        try:
            a, b = key.strip().split("_")
            cells[(int(a), int(b))] = int(val.strip().split(",")[0], 0) & 0xFF
        except ValueError:
            continue
    if len(cells) >= P.KEYMAP_LEN:
        km = bytearray(P.KEYMAP_LEN)
        for (a, b), v in cells.items():
            if a < 8 and b < 20:
                km[8 * b + a] = v
        return bytes(km)

    lines = [l for l in text.splitlines()
             if l.strip() and not l.strip().startswith(("[", ";", "#"))]
    rows = []
    for l in lines:
        vals = l.replace(",", " ").split()
        if len(vals) == 20 and all(len(v) <= 2 for v in vals):
            rows.append([int(v, 16) for v in vals])
    if len(rows) == 8:
        km = bytearray(P.KEYMAP_LEN)
        for a in range(8):
            for b in range(20):
                km[8 * b + a] = rows[a][b]
        return bytes(km)
    vals = text.replace(",", " ").split()
    if len(vals) == P.KEYMAP_LEN:
        return bytes(int(v, 16) for v in vals)
    raise SystemExit(f"cannot parse keymap from {path}: got {len(raw)} bytes / "
                     f"{len(rows)} matrix rows / {len(vals)} values")


# --------------------------------------------------------------------------- #
# LED helpers
# --------------------------------------------------------------------------- #
def parse_color(text: str) -> tuple:
    """Parse ``#RRGGBB`` / ``RRGGBB`` / ``RGB`` into 4-bit (r, g, b)."""
    t = text.strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6:
        raise SystemExit(f"bad colour {text!r}: use #RRGGBB")
    r, g, b = (int(t[i:i + 2], 16) for i in (0, 2, 4))
    return (r >> 4, g >> 4, b >> 4)


def led_specs(args) -> tuple:
    """Turn the parsed LED options into (palette_or_None, effect_kwargs)."""
    palette = None
    if getattr(args, "color", None):
        palette = [None] * P.LED_COLORS
        for spec in args.color:
            idx_s, _, val = spec.partition("=")
            if not val:
                raise SystemExit(f"--color expects I=RRGGBB, got {spec!r}")
            idx = int(idx_s) - 1
            if not 0 <= idx < P.LED_COLORS:
                raise SystemExit(f"colour index {idx_s} out of range 1..{P.LED_COLORS}")
            palette[idx] = parse_color(val)
    kw = {}
    for name in ("mode", "speed", "direction", "symmetry", "single", "react"):
        v = getattr(args, name, None)
        if v is not None:
            kw[name] = v
    if getattr(args, "rgb_en", None) is not None:
        kw["rgb_en"] = args.rgb_en
    return palette, kw


def apply_led_args(prog, args) -> int:
    """Patch the LED palette/effect fields described by CLI options."""
    palette, kw = led_specs(args)
    if palette is not None:
        base = P.read_palette(prog)
        palette = [p if p is not None else base[i]
                   for i, p in enumerate(palette)]
        kw["palette"] = palette
    return P.apply_led(prog, **kw) if kw else 0


LED_MODES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "led_modes.json")


def led_effect_names(path: str = LED_MODES_PATH) -> dict:
    """Mode number -> effect name, from ``tools/led_modes.json``.

    Returns an empty mapping when the catalogue is missing, so the tool keeps
    working with plain numbers.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {int(m["mode"]): m["en"] for m in data.get("modes", [])}


def print_led_modes(names: dict):
    """Print the effect catalogue as ``number  name`` lines."""
    print("LED effects (phase-B byte 0x1a, low nibble):")
    for mode in sorted(names):
        print(f"  {mode:2d}  {names[mode]}")


def print_led(led: dict, names: dict | None = None):
    if names is None:
        names = led_effect_names()
    print("palette (4-bit R,G,B per colour):")
    for i, (r, g, b) in enumerate(led["palette"], 1):
        print(f"  colour {i}: R={r:2d} G={g:2d} B={b:2d}   "
              f"#{r * 17:02x}{g * 17:02x}{b * 17:02x}")
    name = names.get(led["mode"])
    effect = f" ({name})" if name else ""
    print(f"mode={led['mode']}{effect} speed={led['speed']} "
          f"direction={led['direction']} symmetry={led['symmetry']} "
          f"single={led['single']} react={led['react']} "
          f"rgb_en={led['rgb_en']:#04x}")


def cmd_led(args):
    """Show and/or change the LED palette and effect of a template session."""
    if args.list:
        print_led_modes(led_effect_names())
        return 0
    prog, _ = P.program_from_capture(args.template)
    effect_names = ("mode", "speed", "direction", "symmetry", "single",
                    "react", "rgb_en")
    has_change = bool(args.color) or any(
        getattr(args, n, None) is not None for n in effect_names)
    if args.show or not has_change:
        print_led(P.read_led(prog))
        if not has_change:
            return 0
    n = apply_led_args(prog, args)
    print(f"patched LED: {n} bytes (phase B)")
    if args.show:
        print_led(P.read_led(prog))
    frames = P.emit(prog)
    if args.dry_run:
        print(f"{len(frames)} frames (dry run)")
        return 0
    return send_frames(args, frames)


def parse_int(text: str, what: str) -> int:
    """Parse a decimal/hex integer coming from a ``--set`` value."""
    try:
        return int(text, 0)
    except ValueError:
        raise SystemExit(f"{what}: expected a number, got {text!r}")


def print_switches(prog):
    sw = P.read_switches(prog)
    on = lambda b: "on" if b else "off"          # noqa: E731
    print(f"switches: byte {P.switch_value(sw):#04x}  "
          + "  ".join(f"{name}={on(sw[name])}" for name in P.SWITCH_ORDER))


def print_encoder(prog):
    enc = P.read_encoder(prog)
    for name in P.ENCODER_RECORDS:
        info = enc[name]
        label = P.ENCODER_BY_NUMBER.get(info["number"], (None, "unknown"))[1]
        print(f"  {name}: number={info['number']:>3}  "
              f"usage={info['usage']:#06x}  second={info['second']:#06x}  {label}")


def cmd_show(args):
    """Print the keymap, LED, switches and encoder of a captured session."""
    prog = load_template(args)
    km = P.read_keymap(prog, "A", P.KEYMAP_BASE)
    print_led(P.read_led(prog))
    print_switches(prog)
    print("encoder media functions:")
    print_encoder(prog)
    print("keymap (upload order, 8*col+row):")
    for r in range(0, P.KEYMAP_LEN, 16):
        chunk = km[r:r + 16]
        print(f"  {r:3d}: " + " ".join(f"{c:02x}" for c in chunk))
    return 0


def cmd_switches(args):
    """Show and/or change the four option switches of a template session."""
    prog = load_template(args)
    print_switches(prog)
    if not args.set and args.byte is None:
        return 0
    if args.byte is not None:
        value = args.byte
    else:
        sw = P.read_switches(prog)
        for spec in args.set:
            name, _, val = spec.partition("=")
            name = name.strip()
            if name not in P.SWITCH_BITS:
                raise SystemExit(f"unknown switch {name!r}; expected one of "
                                 + ", ".join(P.SWITCH_ORDER))
            if not val:
                raise SystemExit(f"--set expects NAME=0|1, got {spec!r}")
            sw[name] = bool(parse_int(val, f"switch {name}"))
        value = sw
    n = P.apply_switches(prog, value)
    print(f"patched switches: {n} byte (byte {P.switch_value(value):#04x})")
    print_switches(prog)
    return finish(args, prog)


def cmd_encoder(args):
    """Show and/or change the encoder media functions of a template session."""
    if args.list:
        print("encoder media functions (second number of USCROLL*):")
        for num, usage, name in P.ENCODER_FUNCTIONS:
            print(f"  {num:2d}  {usage:#06x}  {name}")
        return 0
    prog = load_template(args)
    print("encoder media functions:")
    print_encoder(prog)
    if not args.set:
        return 0
    enc = {name: info["number"]
           for name, info in P.read_encoder(prog).items()}
    for spec in args.set:
        name, _, val = spec.partition("=")
        name = name.strip()
        if name not in P.ENCODER_RECORDS:
            raise SystemExit(f"unknown encoder {name!r}; expected enc0 or enc1")
        if not val:
            raise SystemExit(f"--set expects enc0|enc1=NUMBER, got {spec!r}")
        num = parse_int(val, f"encoder {name}")
        if P.encoder_usage_of(num) is None:
            raise SystemExit(f"number {num} is not one of the measured media "
                             "functions; see `encoder --list`")
        enc[name] = num
    n = P.patch_encoder(prog, enc)
    print(f"patched encoder: {n} bytes")
    print_encoder(prog)
    return finish(args, prog)


def cmd_preset(args):
    """Export the whole state of a template session as a JSON preset."""
    prog = load_template(args)
    preset = P.preset_from_program(prog, args.template)
    if args.out:
        write_json(args.out, preset)
        print(f"preset written: {args.out} (version {preset['version']}, "
              f"{len(preset['keymap'])} keymap bytes)")
    else:
        print(json.dumps(preset, indent=2, ensure_ascii=False))
    return 0


def cmd_apply(args):
    """Patch a JSON preset into a template session and send it."""
    try:
        preset = json.load(open(args.preset, encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read preset {args.preset}: {e}")
    if not isinstance(preset, dict):
        raise SystemExit(f"{args.preset}: expected a JSON object")
    prog = load_template(args)
    counts = P.apply_preset(prog, preset)
    print("applied preset: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if args.out:
        write_json(args.out, frames_artifact(P.emit(prog), args.template,
                                             P.preset_from_program(prog, args.template)))
        print(f"frames written: {args.out}")
    return finish(args, prog, "apply")


def cmd_backup(args):
    """Write a rollback artifact: the exact frame stream plus a preset."""
    prog = load_template(args)
    frames = P.emit(prog)
    preset = P.preset_from_program(prog, args.template)
    memory = None
    if args.from_device:
        dev, info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                                path=args.device)
        data = dump_range(dev, args.addr, args.length, args.pace)
        memory = {"base": args.addr, "length": len(data), "hex": data.hex()}
        print(f"read {len(data)} bytes from {info['path'].decode()}")
    art = frames_artifact(frames, args.template, preset, memory)
    write_json(args.out, art)
    print(f"backup written: {args.out} ({len(frames)} frames, "
          f"sha256 {art['sha256'][:12]}...)")
    if memory is None:
        print("note: this artifact holds the session the tool would send, not a "
              "read of the device; add --from-device for a best-effort dump")
    return 0


def cmd_restore(args):
    """Send the exact frame stream stored in a backup artifact."""
    art, frames = read_frames_artifact(args.backup)
    print(f"{len(frames)} frames from {args.backup} "
          f"(template {art.get('template')}, sha256 ok)")
    if args.dry_run:
        for f in frames[:args.show]:
            print("  " + f.hex())
        if len(frames) > args.show:
            print(f"  ... ({len(frames) - args.show} more)")
        return 0
    return send_frames(args, frames)


def cmd_selftest(args):
    """Replay a capture to the running emulator and compare what it logged."""
    prog, _ = P.program_from_capture(args.capture)
    frames = P.emit(prog)

    size0 = os.path.getsize(args.log) if os.path.exists(args.log) else 0
    dev, info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                            path=args.device)
    print(f"replaying {len(frames)} frames to {info['path'].decode()}")
    for i, f in enumerate(frames):
        if send_report(dev, f) < 0:
            raise SystemExit(f"send failed at frame {i}")
        if args.pace:
            time.sleep(args.pace)

    # read the newly appended SET records
    time.sleep(0.2)
    seen = []
    with open(args.log) as fh:
        fh.seek(size0)
        for line in fh:
            rec = json.loads(line)
            if rec.get("kind") == "SET" and "raw" in rec:
                seen.append(bytes.fromhex(rec["raw"]))
    ok = len(seen) == len(frames)
    if ok:
        for i, (a, b) in enumerate(zip(seen, frames)):
            if a != b:
                ok = False
                print(f"MISMATCH frame {i}: emulator {a.hex()} != sent {b.hex()}")
                break
    print(f"emulator received {len(seen)}/{len(frames)} SET reports")
    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
def add_led_args(p):
    p.add_argument("--color", action="append", default=[], metavar="I=RRGGBB",
                   help="palette colour i (1..7), repeatable")
    p.add_argument("--mode", type=lambda s: int(s, 0), help="HR_LED_MODE")
    p.add_argument("--speed", type=lambda s: int(s, 0), help="HR_SPEED 0..3")
    p.add_argument("--direction", type=lambda s: int(s, 0), help="HR_DIRECTION 0..3")
    p.add_argument("--symmetry", type=lambda s: int(s, 0), help="HR_SYMMETRY 0/1")
    p.add_argument("--single", type=lambda s: int(s, 0), help="SINGLECOL 0/1")
    p.add_argument("--react", type=lambda s: int(s, 0), help="HR_REACT_TYPE")
    p.add_argument("--rgb-en", dest="rgb_en", type=lambda s: int(s, 0),
                   help="RGB_HR_COLOR_EN bitmask 0..127")


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vid", type=lambda s: int(s, 0), default=DEFAULT_VID)
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=DEFAULT_PID)
    ap.add_argument("--usage-page", type=lambda s: int(s, 0),
                    default=WRITE_USAGE_PAGE, help="interface to open")
    ap.add_argument("--device", default=None, help="explicit /dev/hidrawN path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="enumerate matching devices").set_defaults(fn=cmd_list)
    sub.add_parser("info", help="read device info").set_defaults(fn=cmd_info)

    d = sub.add_parser("dump", help="read device memory")
    d.add_argument("--addr", type=lambda s: int(s, 0), required=True)
    d.add_argument("--length", type=lambda s: int(s, 0), default=0x20)
    d.add_argument("--out", help="also write the raw bytes to this file")
    d.set_defaults(fn=cmd_dump)

    r = sub.add_parser("replay", help="replay a captured upload session")
    r.add_argument("capture")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--show", type=int, default=8)
    r.set_defaults(fn=cmd_replay)

    k = sub.add_parser("keymap", help="upload a keymap matrix using a template")
    k.add_argument("--keymap", required=True, help="matrix/hex/binary keymap file")
    k.add_argument("--template", default="captures/baseline_apply.jsonl")
    k.add_argument("--dry-run", action="store_true")
    add_led_args(k)
    k.set_defaults(fn=cmd_keymap)

    l = sub.add_parser("led", help="show/change the LED palette and effect")
    l.add_argument("--template", default="captures/baseline_apply.jsonl")
    l.add_argument("--show", action="store_true", help="print before and after")
    l.add_argument("--list", action="store_true",
                   help="print the measured number -> effect table")
    l.add_argument("--dry-run", action="store_true")
    add_led_args(l)
    l.set_defaults(fn=cmd_led)

    sh = sub.add_parser("show", help="print keymap + LED of a captured session")
    sh.add_argument("--template", default="captures/baseline_apply.jsonl")
    sh.set_defaults(fn=cmd_show)

    sw = sub.add_parser("switches", help="show/change the option switches byte")
    sw.add_argument("--template", default="captures/baseline_apply.jsonl")
    sw.add_argument("--set", action="append", default=[], metavar="NAME=0|1",
                    help="switch to change: " + ", ".join(P.SWITCH_ORDER))
    sw.add_argument("--byte", type=lambda s: int(s, 0),
                    help="write the whole switches byte instead of named fields")
    sw.add_argument("--dry-run", action="store_true")
    sw.set_defaults(fn=cmd_switches)

    en = sub.add_parser("encoder", help="show/change encoder media functions")
    en.add_argument("--template", default="captures/baseline_apply.jsonl")
    en.add_argument("--set", action="append", default=[], metavar="enc0|enc1=N",
                    help="media function number (see --list)")
    en.add_argument("--list", action="store_true",
                    help="print the measured number -> usage table")
    en.add_argument("--dry-run", action="store_true")
    en.set_defaults(fn=cmd_encoder)

    pr = sub.add_parser("preset", help="export a template's state as JSON")
    pr.add_argument("--template", default="captures/baseline_apply.jsonl")
    pr.add_argument("--out", help="write the preset here (default: stdout)")
    pr.set_defaults(fn=cmd_preset)

    app = sub.add_parser("apply", help="patch a JSON preset into a template")
    app.add_argument("preset", help="preset JSON (k815ctl or WebHID export)")
    app.add_argument("--template", default="captures/baseline_apply.jsonl")
    app.add_argument("--out", help="also write the resulting frames as a backup")
    app.add_argument("--dry-run", action="store_true")
    app.set_defaults(fn=cmd_apply)

    bk = sub.add_parser("backup", help="write a rollback artifact")
    bk.add_argument("--template", default="captures/baseline_apply.jsonl")
    bk.add_argument("--out", default="k815-backup.json")
    bk.add_argument("--from-device", action="store_true",
                    help="also dump device memory (best effort, needs hardware)")
    bk.add_argument("--addr", type=lambda s: int(s, 0), default=0)
    bk.add_argument("--length", type=lambda s: int(s, 0), default=P.IMAGE_LEN)
    bk.set_defaults(fn=cmd_backup)

    rs = sub.add_parser("restore", help="send a backup artifact's frame stream")
    rs.add_argument("backup")
    rs.add_argument("--dry-run", action="store_true")
    rs.add_argument("--show", type=int, default=8)
    rs.set_defaults(fn=cmd_restore)

    s = sub.add_parser("selftest", help="replay to the emulator and compare")
    s.add_argument("capture")
    s.add_argument("--log", default="captures/wine4.jsonl",
                   help="the emulator's JSONL log to compare against")
    s.set_defaults(fn=cmd_selftest)

    # add --pace to the commands that talk to hardware
    for p in (r, k, l, s, d, sw, en, app, bk, rs):
        p.add_argument("--pace", type=float, default=0.0,
                       help="seconds to sleep between reports")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
