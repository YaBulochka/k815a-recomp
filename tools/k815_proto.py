#!/usr/bin/env python3
"""Protocol helpers for the MKESPN K815 vendor HID protocol.

Everything here is derived from captures of the original Windows application
talking to the uhid-emulated device (see tools/k815_emulator.py and
captures/*.jsonl).

Report format (16 bytes, HID report ID 7, feature report):

    offset  field
    0       0x07        report ID
    1       opcode      0x18 = memory command, 0x09 = special command
    2       subcommand  0x03 write byte, 0x05 load read address, 0x00 commit,
                        0x01 commit, 0x09 end-of-block, 0x81 erase, 0x11 init
    3       index       byte offset inside the current 8-byte block
    4,5     address     little-endian 16-bit block base address
    6       data        byte to write
    7       length      len-1 (or len+0x5F for the other model variant)
    8..13   0
    14      flag        0x01 for the keymap/LED block writes, else 0x00
    15      0

Upload sessions
----------------

One "Apply" produces one session shaped like this (addresses are device
memory addresses, blocks are at most 8 bytes):

    SPECIAL 0x41          begin session
    WRITE 1 B  @ 0x0010   end + commit
    WRITE 160 B @ 0x0020  the 8x20 keymap, 20 blocks of 8 B, end + commit each
    WRITE 8 B  @ 0x00D0   end + commit (flag=1)
    WRITE 1 B  @ 0x0001   flag=1, then commit(sub=0x00) + READ + commit
    WRITE 33 B @ 0x0001   phase B: 0x01..0x21, blocks of 8 B
    WRITE 319 B @ 0x0049  phase B: 0x49..0x187 (keymap + LED + macros)
    INIT                  sub=0x11
    COMMIT                sub=0x00
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

REPORT_ID = 0x07
OP_MEM = 0x18
OP_SPECIAL = 0x09

SUB_WRITE = 0x03
SUB_READ = 0x05
SUB_END = 0x09
SUB_COMMIT = 0x01
SUB_IDLE = 0x00
SUB_ERASE = 0x81
SUB_INIT = 0x11
SUB_BEGIN = 0x41

REPORT_LEN = 16
KEYMAP_BASE = 0x20          # phase A
KEYMAP_BASE_B = 0x49        # phase B writes the same 160-byte table here
KEYMAP_LEN = 160
IMAGE_LEN = 0x190

# Which keymap byte belongs to which physical control.  The ids match the WebHID
# driver (`webhid/k815.js`), so a preset moves between the CLI and the browser.
SLOTS = [
    ("k1", "Key 1", 8),
    ("k2", "Key 2", 16),
    ("k3", "Key 3", 24),
    ("k4", "Key 4", 32),
    ("k5", "Key 5", 9),
    ("k6", "Key 6", 17),
    ("k7", "Key 7", 25),
    ("k8", "Key 8", 33),
    ("enc1", "Encoder A", 41),
    ("enc2", "Encoder B", 49),
]

# LED palette: 7 colours live in the phase-B header, two bytes each.
#   byte 0x02+2i = (R << 4) | B      (phase B)
#   byte 0x03+2i = (X << 4) | G      (X is a template nibble we keep)
# Verified byte-for-byte against six differential captures (see LED.md).
LED_PHASE = "B"
LED_PAL_BASE = 0x02
LED_COLORS = 7


def report(op, sub, index=0, addr=0, data=0, ln=0, flag=0, tail=b""):
    b = bytearray(REPORT_LEN)
    b[0] = REPORT_ID
    b[1] = op
    b[2] = sub
    b[3] = index & 0xFF
    b[4] = addr & 0xFF
    b[5] = (addr >> 8) & 0xFF
    b[6] = data & 0xFF
    b[7] = ln & 0xFF
    if tail:
        b[8:8 + len(tail)] = tail
    b[14] = flag & 0xFF
    return bytes(b)


@dataclass
class Frame:
    """One report in a decoded session, kept both parsed and raw."""
    op: int
    sub: int
    idx: int = 0
    addr: int = 0
    data: int = 0
    ln: int = 0
    flag: int = 0
    raw: bytes = b""

    def build(self) -> bytes:
        return report(self.op, self.sub, self.idx, self.addr, self.data,
                      self.ln, self.flag)


@dataclass
class Block:
    """A contiguous write of one memory region, plus its trailer reports.

    `data` is the byte string captured on the wire.  A single session writes
    the same address range twice (phase A and phase B) with *different*
    values, so blocks must carry their own bytes; there is no single memory
    image that satisfies both phases.
    """
    addr: int
    length: int
    data: bytes = b""
    flag: int = 0
    phase: str = "A"
    trailer: list = field(default_factory=list)   # list[Frame]

    def values(self) -> bytes:
        return self.data


@dataclass
class Program:
    """A replayable upload session: blocks interleaved with verbatim frames."""
    items: list = field(default_factory=list)     # list[Block | Frame]

    def texts(self) -> str:
        out = []
        for it in self.items:
            if isinstance(it, Block):
                out.append(f"[{it.phase}] BLOCK {it.length:>3} B @ {it.addr:#06x} "
                           f"flag={it.flag} trailer={'+'.join(f'{f.sub:#04x}' for f in it.trailer)}")
            elif it.sub == SUB_BEGIN:
                out.append(f"BEGIN {it.raw.hex()}")
            elif it.sub == SUB_INIT:
                out.append(f"INIT  {it.raw.hex()}")
            elif it.sub == SUB_READ:
                out.append(f"READ  1 B @ {it.addr:#06x} flag={it.flag}")
            else:
                out.append(f"CMD   sub={it.sub:#04x} addr={it.addr:#06x} "
                           f"flag={it.flag} raw={it.raw.hex()}")
        return "\n".join(out)

    @property
    def blocks(self) -> list:
        return [it for it in self.items if isinstance(it, Block)]

    def patch(self, phase: str, addr: int, data: bytes) -> int:
        """Overwrite bytes at `addr` in every block of `phase` that covers it.

        Returns how many bytes were written.  Used to inject a new keymap or
        LED table into a captured program template.
        """
        n = 0
        for it in self.items:
            if isinstance(it, Block) and it.phase == phase:
                for i, v in enumerate(data):
                    a = addr + i
                    if it.addr <= a < it.addr + it.length:
                        j = a - it.addr
                        it.data = it.data[:j] + bytes([v]) + it.data[j + 1:]
                        n += 1
        return n

    def get(self, phase: str, addr: int) -> int | None:
        """Current byte value at `addr` for `phase` (last covering block wins)."""
        got = None
        for it in self.items:
            if isinstance(it, Block) and it.phase == phase:
                if it.addr <= addr < it.addr + it.length:
                    got = it.data[addr - it.addr]
        return got

    def patch_nibble(self, phase: str, addr: int, which: str, value: int) -> int:
        """Set one 4-bit nibble at `addr`, preserving the other nibble.

        `which` is "hi" or "lo".  The K815 packs 4-bit LED channels two per
        byte, so partial-byte patching is required to keep the untouched
        channel intact.
        """
        cur = self.get(phase, addr)
        if cur is None:
            return 0
        value &= 0x0F
        new = (value << 4) | (cur & 0x0F) if which == "hi" \
            else (cur & 0xF0) | value
        return self.patch(phase, addr, bytes([new]))

    def patch_bits(self, phase: str, addr: int, mask: int, value: int) -> int:
        """Set the bits selected by `mask` at `addr`, preserving the rest."""
        cur = self.get(phase, addr)
        if cur is None:
            return 0
        return self.patch(phase, addr, bytes([(cur & ~mask & 0xFF) | (value & mask)]))


def patch_palette(prog: "Program", colors) -> int:
    """Write the 7-colour LED palette into a captured program template.

    `colors` is an iterable of (r, g, b) 4-bit tuples (0..15).  The high
    nibble of every second byte is a template value and is left untouched.
    Returns the number of bytes changed.
    """
    n = 0
    for i, rgb in enumerate(colors):
        r, g, b = (v & 0x0F for v in rgb)
        a0 = LED_PAL_BASE + 2 * i
        n += prog.patch(LED_PHASE, a0, bytes([(r << 4) | b]))
        n += prog.patch_nibble(LED_PHASE, a0 + 1, "lo", g)
    return n


def read_palette(prog: "Program") -> list:
    """Read back the 7-colour palette as a list of (r, g, b) tuples."""
    out = []
    for i in range(LED_COLORS):
        a0 = LED_PAL_BASE + 2 * i
        b0 = prog.get(LED_PHASE, a0) or 0
        b1 = prog.get(LED_PHASE, a0 + 1) or 0
        out.append((b0 >> 4, b1 & 0x0F, b0 & 0x0F))
    return out


# Effect fields in the phase-B header (all nibble/bit fields).
LED_SPEED_ADDR, LED_SPEED_MASK = 0x19, 0x07
LED_DIR_ADDR, LED_DIR_MASK = 0x19, 0xC0        # direction << 6
LED_MODE_ADDR, LED_MODE_MASK = 0x1A, 0x0F
LED_SINGLE_MASK = 0x10                          # bit 4 of 0x1a
LED_SYM_MASK = 0x80                             # bit 7 of 0x1a
LED_REACT_ADDR, LED_REACT_MASK = 0x10, 0x0F
LED_RGBEN_ADDR, LED_RGBEN_MASK = 0x1B, 0x7F


def apply_led(prog: "Program", palette=None, mode=None, speed=None,
              direction=None, symmetry=None, single=None, react=None,
              rgb_en=None) -> int:
    """Patch the LED palette and/or effect fields of a captured program.

    Any argument left as None is not touched.  Returns the number of bytes
    changed.
    """
    n = 0
    if palette is not None:
        n += patch_palette(prog, palette)
    if mode is not None:
        n += prog.patch_bits(LED_PHASE, LED_MODE_ADDR, LED_MODE_MASK, mode)
    if speed is not None:
        n += prog.patch_bits(LED_PHASE, LED_SPEED_ADDR, LED_SPEED_MASK, speed)
    if direction is not None:
        n += prog.patch_bits(LED_PHASE, LED_DIR_ADDR, LED_DIR_MASK,
                             (direction & 0x03) << 6)
    if symmetry is not None:
        n += prog.patch_bits(LED_PHASE, LED_MODE_ADDR, LED_SYM_MASK,
                             0x80 if symmetry else 0)
    if single is not None:
        n += prog.patch_bits(LED_PHASE, LED_MODE_ADDR, LED_SINGLE_MASK,
                             0x10 if single else 0)
    if react is not None:
        n += prog.patch_bits(LED_PHASE, LED_REACT_ADDR, LED_REACT_MASK, react)
    if rgb_en is not None:
        n += prog.patch_bits(LED_PHASE, LED_RGBEN_ADDR, LED_RGBEN_MASK, rgb_en)
    return n


def read_led(prog: "Program") -> dict:
    """Read back the LED palette and effect fields as a dict."""
    g = lambda a: prog.get(LED_PHASE, a) or 0        # noqa: E731
    b19, b1a, b10, b1b = g(0x19), g(0x1A), g(0x10), g(0x1B)
    return {
        "palette": read_palette(prog),
        "mode": b1a & LED_MODE_MASK,
        "speed": b19 & LED_SPEED_MASK,
        "direction": (b19 & LED_DIR_MASK) >> 6,
        "symmetry": 1 if b1a & LED_SYM_MASK else 0,
        "single": 1 if b1a & LED_SINGLE_MASK else 0,
        "react": b10 & LED_REACT_MASK,
        "rgb_en": b1b & LED_RGBEN_MASK,
    }


# --------------------------------------------------------------------------- #
# option switches: one byte in the phase-B header at 0x0001 (PROTOCOL.md)
# --------------------------------------------------------------------------- #
SWITCH_PHASE = "B"
SWITCH_ADDR = 0x0001
# Field name -> bit.  The names and order match webhid/k815.js and the UI card.
SWITCH_BITS = {
    "allKeysLock": 0x01,        # BALLKEYSLOCK
    "winLock": 0x04,            # BWINLOCK
    "altF4": 0x40,              # BALTF4
    "keyMode": 0x80,            # KEYSMODE
}
SWITCH_ORDER = ("altF4", "winLock", "allKeysLock", "keyMode")


def switch_value(sw) -> int:
    """Encode the switches byte from a dict of booleans or take an int as is.

    Bits outside the four known fields are not set, which is what the captured
    session and `patchSwitches` in webhid/k815.js do.
    """
    if isinstance(sw, int):
        return sw & 0xFF
    v = 0
    for name, bit in SWITCH_BITS.items():
        if sw.get(name):
            v |= bit
    return v


def read_switches(prog: "Program") -> dict:
    """Current switches as a dict of booleans."""
    v = prog.get(SWITCH_PHASE, SWITCH_ADDR) or 0
    return {name: bool(v & bit) for name, bit in SWITCH_BITS.items()}


def apply_switches(prog: "Program", sw) -> int:
    """Write the switches byte.  Returns 1 if it changed, else 0 (like the JS)."""
    cur = prog.get(SWITCH_PHASE, SWITCH_ADDR)
    if cur is None:
        return 0
    nv = switch_value(sw)
    if cur == nv:
        return 0
    prog.patch(SWITCH_PHASE, SWITCH_ADDR, bytes([nv]))
    return 1


# --------------------------------------------------------------------------- #
# encoder media functions (second number of USCROLL0/USCROLL1)
# --------------------------------------------------------------------------- #
ENCODER_PHASE = "B"
# 16-byte record per direction; the Consumer usage sits twice inside it
# (PROTOCOL.md, "Fn actions, encoder, and macros").
ENCODER_RECORDS = {"enc0": 0x0168, "enc1": 0x0178}
ENCODER_CODE_OFFSET = 6
ENCODER_COPY_STRIDE = 6

# (number in [Config1], HID Consumer usage, name).  Measured by differential
# sweep; numbers 17 and up are the application's own functions, which never
# reach the device as Consumer usages.
ENCODER_FUNCTIONS = [
    (0, 0x00CD, "Play/Pause"),
    (1, 0x00EA, "Volume decrement"),
    (2, 0x00E9, "Volume increment"),
    (3, 0x00B5, "Scan next track"),
    (4, 0x00B6, "Scan previous track"),
    (5, 0x0183, "Consumer control configuration"),
    (6, 0x018A, "Email reader"),
    (7, 0x0225, "AC forward"),
    (8, 0x0226, "AC stop"),
    (9, 0x0224, "AC back"),
    (10, 0x0227, "AC refresh"),
    (11, 0x0221, "AC search"),
    (12, 0x00E2, "Mute"),
    (13, 0x0194, "Browser (local machine)"),
    (14, 0x00B7, "Stop"),
    (15, 0x0192, "Calculator"),
    (16, 0x0223, "AC home"),
]
ENCODER_BY_NUMBER = {n: (u, name) for n, u, name in ENCODER_FUNCTIONS}
ENCODER_BY_USAGE = {u: (n, name) for n, u, name in ENCODER_FUNCTIONS}
ENCODER_UNKNOWN = -1


def encoder_number_of(usage: int) -> int:
    """Config number for a Consumer usage, or -1 when it is not in the table."""
    return ENCODER_BY_USAGE.get(usage, (ENCODER_UNKNOWN, ""))[0]


def encoder_usage_of(number: int) -> int | None:
    """Consumer usage for a config number, or None when it is unknown."""
    fn = ENCODER_BY_NUMBER.get(number)
    return None if fn is None else fn[0]


def encoder_copy_addrs(name: str) -> tuple:
    """The two little-endian usage slots inside one direction's record."""
    base = ENCODER_RECORDS[name] + ENCODER_CODE_OFFSET
    return base, base + ENCODER_COPY_STRIDE


def patch_encoder(prog: "Program", enc) -> int:
    """Write the media function of each encoder direction.

    `enc` maps "enc0"/"enc1" to the config number.  A number of 0 has its
    second copy zeroed, exactly as the application does.  Numbers outside the
    measured table (including -1) leave the record alone.  Returns the number
    of changed bytes, matching `patchEncoder` in webhid/k815.js.
    """
    n = 0
    for name in ENCODER_RECORDS:
        num = enc.get(name)
        if num is None:
            continue
        usage = encoder_usage_of(int(num))
        if usage is None:
            continue
        second = 0 if int(num) == 0 else usage
        a0, a1 = encoder_copy_addrs(name)
        for addr, value in ((a0, usage), (a1, second)):
            for k in (0, 1):
                nv = (value >> (8 * k)) & 0xFF
                cur = prog.get(ENCODER_PHASE, addr + k)
                if cur is None or cur == nv:
                    continue
                prog.patch(ENCODER_PHASE, addr + k, bytes([nv]))
                n += 1
    return n


def read_encoder(prog: "Program") -> dict:
    """Current encoder codes: usage, config number and second copy."""
    out = {}
    for name in ENCODER_RECORDS:
        a0, a1 = encoder_copy_addrs(name)

        def g(addr, _p=prog):
            return _p.get(ENCODER_PHASE, addr) or 0

        usage = g(a0) | (g(a0 + 1) << 8)
        out[name] = {
            "usage": usage,
            "number": encoder_number_of(usage),
            "second": g(a1) | (g(a1 + 1) << 8),
        }
    return out


# --------------------------------------------------------------------------- #
# keymap + whole-state presets
# --------------------------------------------------------------------------- #
def read_keymap(prog: "Program", phase: str = "A", base: int | None = None) -> bytes:
    """Read the 160-byte keymap of one phase (phase A by default)."""
    if base is None:
        base = KEYMAP_BASE if phase == "A" else KEYMAP_BASE_B
    return bytes((prog.get(phase, base + i) or 0) for i in range(KEYMAP_LEN))


# The preset format is shared with the WebHID page (webhid/app.js export).
PRESET_VERSION = 3


def preset_from_program(prog: "Program", template: str | None = None) -> dict:
    """Decode the whole configurable state into a browser-compatible preset."""
    led = read_led(prog)
    km = read_keymap(prog, "A", KEYMAP_BASE)
    preset = {
        "version": PRESET_VERSION,
        "keymap": list(km),
        "led": {
            "palette": [list(c) for c in led["palette"]],
            "mode": led["mode"],
            "speed": led["speed"],
            "direction": led["direction"],
            "symmetry": led["symmetry"],
            "single": led["single"],
            "react": led["react"],
            "rgbEn": led["rgb_en"],
        },
        "switches": read_switches(prog),
        "encoder": {name: info["number"]
                    for name, info in read_encoder(prog).items()},
    }
    if template:
        preset["template"] = os.path.basename(template)
    for sid, _label, idx in SLOTS:
        preset[sid] = km[idx]       # human-readable, also read by app.js import
    return preset


def apply_preset(prog: "Program", preset: dict) -> dict:
    """Patch a decoded preset into a captured program template.

    Missing sections are left alone.  The keymap is written into both phases,
    as webhid/app.js does; a template whose phases disagree therefore gets one
    consistent table.  Returns per-section change counters.
    """
    counts = {"keymap": 0, "led": 0, "switches": 0, "encoder": 0}

    km = bytearray(read_keymap(prog, "A", KEYMAP_BASE))
    touched = False
    if isinstance(preset.get("keymap"), list) and len(preset["keymap"]) == KEYMAP_LEN:
        for i, v in enumerate(preset["keymap"]):
            km[i] = int(v) & 0xFF
        touched = True
    else:
        for sid, _label, idx in SLOTS:
            v = preset.get(sid)
            if isinstance(v, int):
                km[idx] = v & 0xFF
                touched = True
    if touched:
        counts["keymap"] = (prog.patch("A", KEYMAP_BASE, bytes(km))
                            + prog.patch("B", KEYMAP_BASE_B, bytes(km)))

    led = preset.get("led")
    if isinstance(led, dict):
        kw = {}
        palette = led.get("palette")
        if isinstance(palette, list) and palette:
            merged = [list(c) for c in read_palette(prog)]
            for i in range(min(len(palette), LED_COLORS)):
                c = palette[i]
                if c:
                    merged[i] = [int(x) & 0x0F for x in c[:3]]
            kw["palette"] = merged
        for name in ("mode", "speed", "direction", "symmetry", "single", "react"):
            if led.get(name) is not None:
                kw[name] = int(led[name])
        rgb_en = led.get("rgbEn", led.get("rgb_en"))
        if rgb_en is not None:
            kw["rgb_en"] = int(rgb_en)
        if kw:
            counts["led"] = apply_led(prog, **kw)

    sw = preset.get("switches")
    if isinstance(sw, dict):
        counts["switches"] = apply_switches(prog, sw)
    elif isinstance(preset.get("switchesByte"), int):
        counts["switches"] = apply_switches(prog, preset["switchesByte"])

    enc = preset.get("encoder")
    if isinstance(enc, dict):
        counts["encoder"] = patch_encoder(prog, enc)

    return counts


def parse_report(b: bytes) -> Frame | None:
    if len(b) < 8 or b[0] != REPORT_ID:
        return None
    return Frame(op=b[1], sub=b[2], idx=b[3], addr=b[4] | (b[5] << 8),
                 data=b[6], ln=b[7], flag=b[14], raw=bytes(b))


def _set_reports(path: str) -> list:
    """Every 16-byte SET_REPORT payload from a capture, in order."""
    out = []
    for line in open(path):
        rec = json.loads(line)
        if rec.get("kind") == "SET" and "raw" in rec:
            raw = bytes.fromhex(rec["raw"])
            if len(raw) == REPORT_LEN:
                out.append(raw)
    return out


def last_session(reports: list) -> list:
    """Slice out the last full upload session (from its BEGIN frame)."""
    starts = [i for i, b in enumerate(reports)
              if b[1] == OP_SPECIAL and b[2] == SUB_BEGIN]
    if starts:
        return reports[starts[-1]:]
    # fall back: first INIT after any keymap traffic
    starts = [i for i, b in enumerate(reports)
              if b[1] == OP_MEM and b[2] == SUB_INIT]
    return reports[starts[-1]:] if starts else reports


def program_from_capture(path: str) -> tuple[Program, bytearray]:
    """Rebuild the upload program and resulting image from a capture."""
    return program_from_reports(_set_reports(path))


def program_from_reports(reports: list) -> tuple[Program, bytearray]:
    session = last_session(reports)
    prog = Program()
    mem = bytearray(IMAGE_LEN)
    pend = None      # [addr, bytearray data, flag, phase]
    phase = ["A"]

    def end_block():
        nonlocal pend
        if pend:
            addr, data, flag, ph = pend
            prog.items.append(Block(addr=addr, length=len(data), data=bytes(data),
                                    flag=flag, phase=ph))
            pend = None

    for b in session:
        f = parse_report(b)
        if f is None:
            continue
        if f.op == OP_MEM and f.sub == SUB_WRITE:
            a = f.addr + f.idx
            if pend and a == pend[0] + len(pend[1]) and f.flag == pend[2]:
                pend[1].append(f.data)
            else:
                end_block()
                pend = [a, bytearray([f.data]), f.flag, phase[0]]
            if a < IMAGE_LEN:
                mem[a] = f.data
            continue
        # a non-write frame closes the current block; END/COMMIT attach to it
        if f.op == OP_MEM and f.sub in (SUB_END, SUB_COMMIT):
            end_block()
            if prog.items and isinstance(prog.items[-1], Block):
                prog.items[-1].trailer.append(f)
            else:
                prog.items.append(f)
        elif f.op == OP_MEM and f.sub == SUB_READ:
            end_block()
            prog.items.append(f)
            phase[0] = "B"     # everything after the verify-read is phase B
        else:
            end_block()
            prog.items.append(f)
    end_block()
    return prog, mem


def emit(program: Program) -> list:
    """Turn a program into the exact report stream.

    Write frames are regenerated from each block's own captured `data`; every
    trailer and command frame is replayed verbatim, so the output matches the
    original capture byte-for-byte as long as the block layout is unchanged.
    """
    out = []
    for it in program.items:
        if isinstance(it, Block):
            n = it.length
            vals = it.data
            for i, v in enumerate(vals):
                out.append(report(OP_MEM, SUB_WRITE, i, it.addr, v, n - 1,
                                  it.flag))
            for f in it.trailer:
                out.append(f.build())
        else:
            out.append(bytes(it.raw))
    return out


def verify(path: str) -> tuple[bool, int, int, int | None]:
    """Check that emit(program) reproduces the capture. Returns (ok, got, want, bad)."""
    reports = _set_reports(path)
    prog, mem = program_from_reports(reports)
    session = last_session(reports)
    out = emit(prog)
    bad = None
    if len(session) == len(out):
        for i, (a, b) in enumerate(zip(session, out)):
            if a != b:
                bad = i
                break
    return (len(session) == len(out) and bad is None), len(out), len(session), bad


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        ok, got, want, bad = verify(p)
        print(f"{p}: {'BYTE-EXACT' if ok else 'DIFFERS'} ({got}/{want})"
              + (f" first bad index {bad}" if bad is not None else ""))
