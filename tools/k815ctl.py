#!/usr/bin/env python3
"""Configure the MKESPN K815 macro keyboard from Linux.

The device speaks a vendor HID protocol over feature reports with report ID 7.
Frames are 16 bytes internally, but a real K815 accepts/expects 8 bytes on the
wire (ID + 7 payload); see tools/k815_proto.py for the layout and `--wire-len`.  This tool can:

  list        enumerate matching devices
  info        perform the startup "device info" read the app does
  dump        read bytes from device memory (sub=0x05 + get_feature);
              --json saves a page-compatible dump, --preset decodes one
  replay      send a captured upload session to the device
  keymap      build a full upload from a KBconfig.ini-style 8x20 matrix and
              send it (this is the end goal for keyboard configuration)
  led         show/change the LED palette and effect of the template session
  switches    show/change the four option switches (phase-B byte 0x0001)
  encoder     show/change the encoder media functions (USCROLL0/USCROLL1)
  preset      export the whole state of a template as a JSON preset, or
              decode a device dump with --from-dump
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
    python3 tools/k815ctl.py dump --json dump.json --preset current.json
    python3 tools/k815ctl.py preset --from-dump k815-memory-*.json --out current.json
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


def send_report(dev, raw: bytes, wire_len=P.WIRE_REPORT_LEN):
    """Send one protocol report as a feature report.

    The device's HID descriptor declares report 7 with seven payload bytes, so
    the wire frame is 8 bytes; the internal 16-byte frame is truncated (see
    `k815_proto.wire`).  Pass ``wire_len=16`` for the 16-byte uhid emulator.
    """
    raw = P.wire(raw, wire_len)
    return dev.send_feature_report(bytes(raw))


def get_report(dev, report_id=P.REPORT_ID, size=P.READ_ANSWER_LEN):
    return bytes(dev.get_feature_report(report_id, size))


def host_handshake(dev, wire_len=P.WIRE_REPORT_LEN):
    """The startup INIT + COMMIT the vendor application sends after opening."""
    send_report(dev, P.report(P.OP_MEM, P.SUB_INIT), wire_len)
    send_report(dev, P.report(P.OP_MEM, P.SUB_IDLE), wire_len)


def read_byte(dev, addr, wire_len=P.WIRE_REPORT_LEN, wait=0.001):
    """Read one byte: load the address, GET_FEATURE, then close the read.

    This mirrors the application exactly (`SUB_READ`, a short sleep, the GET,
    then `SUB_COMMIT`).  Without the closing commit the firmware answers the
    next request with `0xb4` instead of memory.
    """
    if send_report(dev, P.report(P.OP_MEM, P.SUB_READ, 0, addr), wire_len) < 0:
        raise SystemExit("send_feature_report failed (is a kernel driver bound?)")
    if wait:
        time.sleep(wait)
    ans = get_report(dev)
    send_report(dev, P.report(P.OP_MEM, P.SUB_COMMIT), wire_len)
    return ans[1] if len(ans) > 1 else 0


def send_frames(args, frames, capture=None) -> int:
    """Open the device and send a list of 16-byte frames.

    The measured pauses of the captured session are reproduced by default
    (`--no-delays` turns them off): the firmware needs them, especially the
    200 ms gap after the session start.  ``--pace`` adds a fixed delay after
    every frame on top of that.
    """
    delays = {}
    if capture and not getattr(args, "no_delays", False):
        delays = {i: ms / 1000.0 for i, ms in P.capture_delays(capture, frames)}
        if delays:
            print(f"using {len(delays)} measured frame delays from "
                  f"{os.path.basename(capture)}")
    dev, info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                            path=args.device)
    print(f"-> {info['path'].decode()}")
    for i, f in enumerate(frames):
        if i in delays:
            time.sleep(delays[i])
        if send_report(dev, f, getattr(args, "wire_len", P.WIRE_REPORT_LEN)) < 0:
            raise SystemExit(f"send failed at frame {i}")
        if args.pace:
            time.sleep(args.pace)
    print(f"sent {len(frames)} frames")
    return 0


def pick_stable(values) -> int:
    """The value seen most often; ties go to the latest sample."""
    if not values:
        return 0xff
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    best, best_n = values[-1], 0
    for v, n in counts.items():
        if n >= best_n:
            best, best_n = v, n
    return best


def read_byte_verified(dev, addr, wire_len=P.WIRE_REPORT_LEN, tries: int = 5) -> int:
    """Read one byte, repeating it until two answers agree.

    The firmware occasionally answers a memory read with a byte that belongs
    to another address: a whole palette block and keymap slot 49 were once read
    back wrong right after a write.  A second read costs one extra transfer, so
    two matching answers are accepted, and a disagreeing byte is sampled up to
    `tries` times (majority wins).
    """
    seen = [read_byte(dev, addr, wire_len), read_byte(dev, addr, wire_len)]
    if seen[0] == seen[1]:
        return seen[0]
    print(f"read 0x{addr:04X} was unstable ({', '.join(f'0x{v:02X}' for v in seen)}), retrying")
    while len(seen) < tries:
        seen.append(read_byte(dev, addr, wire_len))
        value = pick_stable(seen)
        if seen.count(value) >= 2:
            return value
    return pick_stable(seen)


def dump_range(dev, addr: int, length: int, pace: float = 0.0,
               wire_len=P.WIRE_REPORT_LEN, warmup: int = 2,
               verify: bool = True) -> bytes:
    """Read `length` bytes from device memory one byte at a time.

    The first reads after a handshake return a stale buffer, so two warm-up
    reads are discarded before the real range starts.  Every byte is read twice
    and the answers must agree (see `read_byte_verified`); pass ``verify=False``
    to accept a single read per byte, which halves the time on a large dump.
    """
    host_handshake(dev, wire_len)
    for _ in range(warmup):
        read_byte(dev, addr, wire_len)
        if pace:
            time.sleep(pace)
    out = bytearray()
    read = read_byte_verified if verify else read_byte
    for a in range(addr, addr + length):
        out.append(read(dev, a, wire_len))
        if pace:
            time.sleep(pace)
    return bytes(out)


BASELINE_TEMPLATE = "captures/baseline_apply.jsonl"
# Captured real-device sessions, keyed by the config base the device reports.
# 0x0118 is the original K815 layout; 0x00E0 is the layout the chip burning
# tool writes (it rewrites the base and the header pointers).
HW_TEMPLATES = {
    0x0118: ("tests/fixtures/hw_apply.jsonl.gz",
             "captures/hw_apply_20261104.jsonl"),
    0x00E0: ("tests/fixtures/hw_apply_e0.jsonl.gz",
             "captures/hw_apply_e0_20261104.jsonl"),
}


def hardware_template(base):
    """The captured real-device session for this config base, if present."""
    for path in HW_TEMPLATES.get(base, ()):
        if os.path.exists(path):
            return path
    return None


def device_config_base(args):
    """Read the phase-B config base from the device's startup answer."""
    dev, _info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                             path=args.device)
    host_handshake(dev, getattr(args, "wire_len", P.WIRE_REPORT_LEN))
    ans = get_report(dev)
    if len(ans) < 16:
        return 0
    return ans[14] | (ans[15] << 8)


def load_template(args, probe=False):
    """Parse the template capture, optionally picking the hardware one.

    The emulator's fake device reports config base 0x0001 and needs the
    baseline capture.  A real K815 reports 0x0118 and the phase-B addresses
    are shifted; the committed hardware fixture has those exact addresses.
    """
    path = getattr(args, "template", None)
    if not path or path == "auto":
        path = BASELINE_TEMPLATE
        if probe:
            try:
                base = device_config_base(args)
            except SystemExit:
                base = 0
            if base and base != 0x0001:
                hw = hardware_template(base)
                if not hw:
                    raise SystemExit(
                        f"this device reports config base 0x{base:04X}, but no "
                        "hardware template is available; capture one from the "
                        "vendor application (`tools/app_capture.sh`) or pass "
                        "--template explicitly")
                path = hw
    if probe:
        print(f"template: {path}", file=sys.stderr)
    args._template_path = path
    prog, _ = P.program_from_capture(path)
    return prog


def finish(args, prog, label="patch") -> int:
    """Emit the patched program and either print it (dry run) or send it."""
    frames = P.emit(prog)
    if getattr(args, "dry_run", False):
        print(f"{label}: {len(frames)} frames (dry run)")
        return 0
    return send_frames(args, frames, getattr(args, "_template_path", None))


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
    host_handshake(dev, args.wire_len)
    ans = get_report(dev)
    print(f"device: {info['path'].decode()} usage_page={info.get('usage_page'):#06x}")
    print(f"info answer: {ans.hex()}")
    return 0


def cmd_dump(args):
    """Read device memory.

    The protocol loads a read address with a normal memory frame (sub=0x05)
    and then the host issues a HID GET_FEATURE; the answer byte arrives in
    byte 1 of the report.  We read one byte per iteration.

    --json writes the same record the WebHID page saves, --preset decodes the
    read image into a preset (the image must cover 0x0000..0x018F for that).
    """
    length = args.length
    config_base = getattr(args, "config_base", None)
    record_base = getattr(args, "record_base", None)
    cb_for_decode = config_base if config_base is not None else 0x0001
    rb_for_decode = (record_base if record_base is not None
                     else default_record_base(cb_for_decode))
    if length is None:
        length = (max(P.IMAGE_LEN, rb_for_decode + 0x20)
                  if args.preset else 0x20)
    dev, info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                            path=args.device)
    out = dump_range(dev, args.addr, length, args.pace, args.wire_len,
                     verify=getattr(args, "verify", True))
    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(out)
        print(f"wrote {len(out)} bytes to {args.out}")
    if args.json:
        record = P.make_dump(args.addr, out, args.vid, args.pid,
                             info.get("product_string"),
                             config_base=config_base,
                             record_base=record_base)
        write_json(args.json, record)
        print(f"dump record written: {args.json} (sha256 "
              f"{record['sha256'][:12]}...)")
    if args.preset:
        try:
            if args.addr == P.STORED_BASE:
                preset = P.preset_from_stored(out, args.addr)
            else:
                preset = P.preset_from_image(out, args.addr,
                                             config_base=cb_for_decode,
                                             record_base=rb_for_decode)
        except ValueError as e:
            raise SystemExit(f"cannot build a preset: {e}; read the whole "
                             f"0x0000..0x{P.IMAGE_LEN - 1:04X} area, or the "
                             f"stored bank at 0x{P.STORED_BASE:04X}.."
                             f"0x{P.STORED_BASE + P.STORED_LEN - 1:04X}")
        write_json(args.preset, preset)
        print(f"preset written: {args.preset} (version {preset['version']}, "
              f"{len(preset['keymap'])} keymap bytes)")
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
    return send_frames(args, frames, args.capture)


def cmd_keymap(args):
    """Build a full upload from a hardware keymap matrix and send it.

    The template session is a captured baseline upload; its keymap blocks are
    patched with the new 160-byte matrix (phase A at 0x20, phase B at 0x49)
    and the resulting stream is replayed to the device.
    """
    keymap = load_keymap(args.keymap)
    prog = load_template(args, probe=True)
    na = prog.patch("A", P.KEYMAP_BASE, keymap)
    nb = prog.patch_b(P.KEYMAP_BASE_B, keymap)
    print(f"patched keymap: {na} B phase A, {nb} B phase B")
    nled = apply_led_args(prog, args)
    if nled:
        print(f"patched LED: {nled} bytes (phase B)")
    frames = P.emit(prog)
    if args.dry_run:
        print(f"{len(frames)} frames (dry run)")
        return 0
    return send_frames(args, frames, getattr(args, "_template_path", args.template))


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
    prog = load_template(args, probe=True)
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
    return send_frames(args, frames, getattr(args, "_template_path", args.template))


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
        if info.get("macro") is not None:
            hold = info.get("hold")
            tail = f"  hold={hold} ms" if hold is not None else ""
            print(f"  {name}: keyboard macro  usage={info['macro']:#04x}{tail}")
            continue
        label = P.ENCODER_BY_NUMBER.get(info["number"], (None, "unknown"))[1]
        print(f"  {name}: number={info['number']:>3}  "
              f"usage={info['usage']:#06x}  second={info['second']:#06x}  {label}")


def print_function_table(prog):
    table = P.read_function_table(prog)
    print(f"function table (phase B, {len(table)} x "
          f"{P.FUNCTION_ENTRY_LEN} B at 0x{prog.b_addr(P.FUNCTION_TABLE_BASE):04X}):")
    for e in table:
        state = "inert" if e.get("inert") else "active"
        print(f"  {e['index']:2d} {e['slot']:<11} tag={e['tag']:02x} "
              f"kind={e['kind']:02x} {state:<6} -> 0x{e['address']:06X}")
    print(f"  payload starts at 0x{prog.b_addr(P.FUNCTION_PAYLOAD_BASE):04X}, "
          f"separator 0x{prog.b_addr(P.FUNCTION_TABLE_SEPARATOR):04X}="
          f"{prog.get_b(P.FUNCTION_TABLE_SEPARATOR) or 0:02x}")


def cmd_show(args):
    """Print the keymap, LED, switches, encoder and function table."""
    prog = load_template(args)
    km = P.read_keymap(prog, "A", P.KEYMAP_BASE)
    print_led(P.read_led(prog))
    print_switches(prog)
    print("encoder media functions:")
    print_encoder(prog)
    print_function_table(prog)
    print("keymap (upload order, 8*col+row):")
    for r in range(0, P.KEYMAP_LEN, 16):
        chunk = km[r:r + 16]
        print(f"  {r:3d}: " + " ".join(f"{c:02x}" for c in chunk))
    return 0


def cmd_switches(args):
    """Show and/or change the four option switches of a template session."""
    prog = load_template(args, probe=True)
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
    prog = load_template(args, probe=True)
    print("encoder media functions:")
    print_encoder(prog)
    if getattr(args, "macro", None):
        hold = getattr(args, "hold", P.ENCODER_HOLD_DEFAULT)
        if not P.ENCODER_HOLD_MIN <= hold <= P.ENCODER_HOLD_MAX:
            raise SystemExit(f"--hold must be {P.ENCODER_HOLD_MIN}.."
                             f"{P.ENCODER_HOLD_MAX} ms, got {hold}")
        keys = {}
        for spec in args.macro:
            name, _, val = spec.partition("=")
            name = name.strip()
            if name not in P.ENCODER_RECORDS:
                raise SystemExit(f"unknown encoder {name!r}; expected enc0 or enc1")
            if not val:
                raise SystemExit(f"--macro expects enc0|enc1=USAGE, got {spec!r}")
            usage = parse_int(val, f"encoder macro {name}")
            if not 0 <= usage <= 0xFF:
                raise SystemExit(f"encoder macro usage {usage} is not a byte")
            keys[name] = usage
        n = P.patch_encoder_mode(prog, P.ENCODER_MODE_MEDIA)
        n += P.patch_encoder_macro(prog, keys, hold_ms=hold)
        for name in keys:
            n += P.patch_encoder_entry(prog, name, True)
        print(f"patched encoder keyboard macros: {n} bytes "
              f"(hold {P.encoder_hold_ms(hold)} ms)")
        print_encoder(prog)
        return finish(args, prog)
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


def default_record_base(config_base: int) -> int:
    """First function-record byte for the known phase-B layouts."""
    return 0x0248 if config_base == 0x00E0 else 0x0168


def cmd_preset(args):
    """Export the whole state of a template session or a device dump as JSON.

    With --from-dump the input is a memory image instead of a capture: either
    the JSON saved by the WebHID page / `dump --json`, or a raw binary image
    written by `dump --out` (then `--base` gives its first address).  The
    keymap is taken from the phase-B copy at 0x49, the only one that survives
    intact in a device dump.
    """
    if args.from_dump:
        try:
            image, address, record = P.read_dump(args.from_dump, args.base)
            config_base = getattr(args, "config_base", None)
            if config_base is None:
                config_base = int(record.get("configBase", 0x0001))
            record_base = getattr(args, "record_base", None)
            if record_base is None:
                record_base = int(record.get("recordBase",
                                             default_record_base(config_base)))
            if address == P.STORED_BASE:
                preset = P.preset_from_stored(image, address)
            else:
                preset = P.preset_from_image(image, address,
                                             config_base=config_base,
                                             record_base=record_base)
        except (OSError, ValueError) as e:
            raise SystemExit(f"cannot read dump {args.from_dump}: {e}")
        source = args.from_dump
        if record.get("created"):
            print(f"dump created: {record['created']}", file=sys.stderr)
        if record.get("sha256"):
            print(f"dump sha256: {record['sha256']}", file=sys.stderr)
    else:
        prog = load_template(args)
        preset = P.preset_from_program(prog, args.template)
        source = args.template
    if args.out:
        write_json(args.out, preset)
        print(f"preset written: {args.out} (version {preset['version']}, "
              f"{len(preset['keymap'])} keymap bytes, from {source})")
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
    prog = load_template(args, probe=True)
    counts = P.apply_preset(prog, preset)
    print("applied preset: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if args.out:
        write_json(args.out, frames_artifact(P.emit(prog), getattr(args, "_template_path", None),
                                             P.preset_from_program(prog, args.template)))
        print(f"frames written: {args.out}")
    return finish(args, prog, "apply")


def cmd_backup(args):
    """Write a rollback artifact: the exact frame stream plus a preset."""
    prog = load_template(args, probe=args.from_device)
    frames = P.emit(prog)
    preset = P.preset_from_program(prog, getattr(args, "_template_path", None))
    memory = None
    if args.from_device:
        dev, info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                                path=args.device)
        data = dump_range(dev, args.addr, args.length, args.pace, args.wire_len,
                          verify=getattr(args, "verify", True))
        memory = {"base": args.addr, "length": len(data), "hex": data.hex()}
        print(f"read {len(data)} bytes from {info['path'].decode()}")
    art = frames_artifact(frames, getattr(args, "_template_path", None), preset, memory)
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
    capture = None
    name = art.get("template")
    if name:
        for base in (os.path.dirname(os.path.abspath(args.backup)),
                     "captures", "."):
            cand = os.path.join(base, name)
            if os.path.exists(cand):
                capture = cand
                break
    return send_frames(args, frames, capture)


def cmd_selftest(args):
    """Replay a capture to the running emulator and compare what it logged."""
    prog, _ = P.program_from_capture(args.capture)
    frames = P.emit(prog)

    size0 = os.path.getsize(args.log) if os.path.exists(args.log) else 0
    dev, info = open_device(args.vid, args.pid, usage_page=args.usage_page,
                            path=args.device)
    print(f"replaying {len(frames)} frames to {info['path'].decode()}")
    for i, f in enumerate(frames):
        if send_report(dev, f, args.wire_len) < 0:
            raise SystemExit(f"send failed at frame {i}")
        if args.pace:
            time.sleep(args.pace)

    # read the newly appended SET records
    time.sleep(0.2)
    expected = [P.wire(f, args.wire_len) for f in frames]
    seen = []
    with open(args.log) as fh:
        fh.seek(size0)
        for line in fh:
            rec = json.loads(line)
            if rec.get("kind") == "SET" and "raw" in rec:
                seen.append(bytes.fromhex(rec["raw"]))
    ok = len(seen) == len(expected)
    if ok:
        for i, (a, b) in enumerate(zip(seen, expected)):
            if a != b:
                ok = False
                print(f"MISMATCH frame {i}: emulator {a.hex()} != sent {b.hex()}")
                break
    print(f"emulator received {len(seen)}/{len(expected)} SET reports "
          f"({args.wire_len} bytes each)")
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
    ap.add_argument("--wire-len", type=int, default=P.WIRE_REPORT_LEN,
                    help="feature report length on the wire (8 on real hardware, "
                         "16 for the uhid emulator)")
    ap.add_argument("--no-delays", action="store_true",
                    help="do not reproduce the captured frame pauses")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="enumerate matching devices").set_defaults(fn=cmd_list)
    sub.add_parser("info", help="read device info").set_defaults(fn=cmd_info)

    d = sub.add_parser("dump", help="read device memory")
    d.add_argument("--addr", type=lambda s: int(s, 0), default=0)
    d.add_argument("--length", type=lambda s: int(s, 0), default=None,
                   help="bytes to read (default 0x20, or the whole image 0x190 "
                        "with --preset)")
    d.add_argument("--out", help="also write the raw bytes to this file")
    d.add_argument("--json", help="write a WebHID-compatible dump record")
    d.add_argument("--preset", help="decode the read bytes into a preset")
    d.add_argument("--config-base", type=lambda s: int(s, 0), default=None,
                   help="phase-B base of the image (default 0x0001; use 0x00E0 "
                        "for a burned unit)")
    d.add_argument("--record-base", type=lambda s: int(s, 0), default=None,
                   help="first function-record byte (default 0x0248 for base "
                        "0x00E0, else 0x0168)")
    d.set_defaults(fn=cmd_dump)

    r = sub.add_parser("replay", help="replay a captured upload session")
    r.add_argument("capture")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--show", type=int, default=8)
    r.set_defaults(fn=cmd_replay)

    k = sub.add_parser("keymap", help="upload a keymap matrix using a template")
    k.add_argument("--keymap", required=True, help="matrix/hex/binary keymap file")
    k.add_argument("--template", default=None)
    k.add_argument("--dry-run", action="store_true")
    add_led_args(k)
    k.set_defaults(fn=cmd_keymap)

    l = sub.add_parser("led", help="show/change the LED palette and effect")
    l.add_argument("--template", default=None)
    l.add_argument("--show", action="store_true", help="print before and after")
    l.add_argument("--list", action="store_true",
                   help="print the measured number -> effect table")
    l.add_argument("--dry-run", action="store_true")
    add_led_args(l)
    l.set_defaults(fn=cmd_led)

    sh = sub.add_parser("show", help="print keymap, LED and function table")
    sh.add_argument("--template", default=None)
    sh.set_defaults(fn=cmd_show)

    sw = sub.add_parser("switches", help="show/change the option switches byte")
    sw.add_argument("--template", default=None)
    sw.add_argument("--set", action="append", default=[], metavar="NAME=0|1",
                    help="switch to change: " + ", ".join(P.SWITCH_ORDER))
    sw.add_argument("--byte", type=lambda s: int(s, 0),
                    help="write the whole switches byte instead of named fields")
    sw.add_argument("--dry-run", action="store_true")
    sw.set_defaults(fn=cmd_switches)

    en = sub.add_parser("encoder", help="show/change encoder media functions")
    en.add_argument("--template", default=None)
    en.add_argument("--set", action="append", default=[], metavar="enc0|enc1=N",
                    help="media function number (see --list)")
    en.add_argument("--macro", action="append", default=[], metavar="enc0|enc1=USAGE",
                    help="make rotation send a keyboard usage (hex or decimal); "
                         "this is the only way rotation can send ordinary keys")
    en.add_argument("--hold", type=lambda s: int(s, 0), default=P.ENCODER_HOLD_DEFAULT,
                    metavar="MS",
                    help=f"key hold for --macro, {P.ENCODER_HOLD_MIN}.."
                         f"{P.ENCODER_HOLD_MAX} ms (quantized to 8 ms; "
                         f"default {P.ENCODER_HOLD_DEFAULT})")
    en.add_argument("--list", action="store_true",
                    help="print the measured number -> usage table")
    en.add_argument("--dry-run", action="store_true")
    en.set_defaults(fn=cmd_encoder)

    pr = sub.add_parser("preset", help="export a template's state as JSON")
    pr.add_argument("--template", default=None)
    pr.add_argument("--from-dump", metavar="FILE",
                    help="decode a memory dump (page JSON or raw binary) "
                         "instead of a template")
    pr.add_argument("--base", type=lambda s: int(s, 0), default=None,
                    help="address of the first byte of a raw binary dump (default 0)")
    pr.add_argument("--config-base", type=lambda s: int(s, 0), default=None,
                    help="phase-B base of the dump (default: dump metadata, "
                         "or 0x0001 for the staging layout)")
    pr.add_argument("--record-base", type=lambda s: int(s, 0), default=None,
                    help="first function-record byte (default: dump metadata, "
                         "0x0248 for base 0x00E0, else 0x0168)")
    pr.add_argument("--out", help="write the preset here (default: stdout)")
    pr.set_defaults(fn=cmd_preset)

    app = sub.add_parser("apply", help="patch a JSON preset into a template")
    app.add_argument("preset", help="preset JSON (k815ctl or WebHID export)")
    app.add_argument("--template", default=None)
    app.add_argument("--out", help="also write the resulting frames as a backup")
    app.add_argument("--dry-run", action="store_true")
    app.set_defaults(fn=cmd_apply)

    bk = sub.add_parser("backup", help="write a rollback artifact")
    bk.add_argument("--template", default=None)
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

    # reads of device memory: every byte is confirmed by a second read unless
    # --no-verify is given (halves the time of a large dump)
    for p in (d, bk):
        p.add_argument("--no-verify", dest="verify", action="store_false",
                       help="read every byte once instead of twice")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
