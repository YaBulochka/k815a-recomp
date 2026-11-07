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

import datetime
import gzip
import hashlib
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

REPORT_LEN = 16             # internal frame size used by captures and artifacts
# The real device declares a 7-byte feature report payload (8 bytes with the
# report ID).  The emulator was captured with a 16-byte descriptor, so the
# vendor application padded its buffers to 16 there; only the first 8 bytes are
# meaningful on hardware (`flag` at offset 14 does not exist on the wire).
WIRE_REPORT_LEN = 8
# A GET_FEATURE answer is longer than the declared report: the firmware returns
# a 16-byte device-info header plus command data (the app asks for 21 bytes).
READ_ANSWER_LEN = 21
KEYMAP_BASE = 0x20          # phase A
KEYMAP_BASE_B = 0x49        # phase B writes the same 160-byte table here
KEYMAP_LEN = 160
IMAGE_LEN = 0x190

# Function-record table (phase B).  22 five-byte index entries start at 0xF9;
# 0x167 is a zero separator, the records themselves start at 0x168.  Entry
# layout: tag(1) kind(1) address(3, little-endian).  On the K815 the eight keys
# are slots 10..17 and the two encoder directions 20/21 (PROTOCOL.md, section 4).
FUNCTION_TABLE_BASE = 0xF9
FUNCTION_TABLE_ENTRIES = 22
FUNCTION_ENTRY_LEN = 5
FUNCTION_TABLE_SEPARATOR = 0xF9 + FUNCTION_TABLE_ENTRIES * FUNCTION_ENTRY_LEN
FUNCTION_PAYLOAD_BASE = FUNCTION_TABLE_SEPARATOR + 1
FUNCTION_ENTRY_TAG = 0x07
FUNCTION_KIND_EMPTY = 0x00
FUNCTION_KIND_INTERNAL = 0x03
FUNCTION_KIND_ACTIVE = 0x04     # Consumer function, macro shortcut, media encoder

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


def wire(frame: bytes, length: int = WIRE_REPORT_LEN) -> bytes:
    """Cut an internal 16-byte frame down to the bytes that go on the wire.

    The real HID descriptor declares report ID 7 with seven payload bytes, so
    everything past byte 7 (the zero padding and the emulator-only ``flag``)
    never reaches the device.  Truncating keeps the frame valid for the
    emulator too: its descriptor is 16 bytes, and ``length=16`` reproduces the
    old behaviour.
    """
    frame = bytes(frame)
    return frame[:length].ljust(length, b"\0")


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
    # Where phase B starts.  The emulator template uses 0x0001; a real device
    # reports its own base in the startup GET answer (0x0118 on the K815).
    base: int = 0x0001
    # First byte of the function-record payload (0x168 staging, 0x280 on the
    # real device: its stored bank has a two-byte separator instead of one).
    record_base: int = 0x168

    def b_addr(self, addr: int) -> int:
        """Translate a staging-layout phase-B address to this session's."""
        if addr >= 0x0168:
            return self.record_base + (addr - 0x0168)
        return self.base + (addr - 0x0001)

    def get_b(self, addr: int) -> int | None:
        return self.get("B", self.b_addr(addr))

    def patch_b(self, addr: int, data: bytes) -> int:
        return self.patch("B", self.b_addr(addr), data)

    def patch_bits_b(self, addr: int, mask: int, value: int) -> int:
        return self.patch_bits("B", self.b_addr(addr), mask, value)

    def patch_nibble_b(self, addr: int, which: str, value: int) -> int:
        return self.patch_nibble("B", self.b_addr(addr), which, value)

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
        n += prog.patch_b(a0, bytes([(r << 4) | b]))
        n += prog.patch_nibble_b(a0 + 1, "lo", g)
    return n


def read_palette(prog: "Program") -> list:
    """Read back the 7-colour palette as a list of (r, g, b) tuples."""
    out = []
    for i in range(LED_COLORS):
        a0 = LED_PAL_BASE + 2 * i
        b0 = prog.get_b(a0) or 0
        b1 = prog.get_b(a0 + 1) or 0
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
        n += prog.patch_bits_b(LED_MODE_ADDR, LED_MODE_MASK, mode)
    if speed is not None:
        n += prog.patch_bits_b(LED_SPEED_ADDR, LED_SPEED_MASK, speed)
    if direction is not None:
        n += prog.patch_bits_b(LED_DIR_ADDR, LED_DIR_MASK,
                             (direction & 0x03) << 6)
    if symmetry is not None:
        n += prog.patch_bits_b(LED_MODE_ADDR, LED_SYM_MASK,
                             0x80 if symmetry else 0)
    if single is not None:
        n += prog.patch_bits_b(LED_MODE_ADDR, LED_SINGLE_MASK,
                             0x10 if single else 0)
    if react is not None:
        n += prog.patch_bits_b(LED_REACT_ADDR, LED_REACT_MASK, react)
    if rgb_en is not None:
        n += prog.patch_bits_b(LED_RGBEN_ADDR, LED_RGBEN_MASK, rgb_en)
    return n


def read_led(prog: "Program") -> dict:
    """Read back the LED palette and effect fields as a dict."""
    g = lambda a: prog.get_b(a) or 0                 # noqa: E731
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
    v = prog.get_b(SWITCH_ADDR) or 0
    return {name: bool(v & bit) for name, bit in SWITCH_BITS.items()}


def apply_switches(prog: "Program", sw) -> int:
    """Write the switches byte.  Returns 1 if it changed, else 0 (like the JS)."""
    cur = prog.get_b(SWITCH_ADDR)
    if cur is None:
        return 0
    nv = switch_value(sw)
    if cur == nv:
        return 0
    prog.patch_b(SWITCH_ADDR, bytes([nv]))
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
# The first number of USCROLL0 selects the encoder mode: 11 is the media mode
# and writes 0x44; everything else uses ((n mod 16) << 4) | 0x0B.
ENCODER_MODE_A_ADDR = 0x10
ENCODER_MODE_B_ADDR = 0x21
ENCODER_MODE_MEDIA = 11
ENCODER_MODE_MEDIA_BYTE = 0x44
ENCODER_MODE_BYTE_BASE = 0x0B
# A single-key macro record: 01 00 <down> <usage> <up> <usage> ...
# The low nibble of the down/up tag is the hold time in 8 ms units; the high
# nibble is 8 for key-down and 0 for key-up.  The encoder records in the
# firmware therefore already carry a 48 ms hold (n=6).
ENCODER_MACRO_TAG_DOWN = 0x80
ENCODER_MACRO_TAG_UP = 0x00
ENCODER_MACRO_LEN = 16
ENCODER_HOLD_DEFAULT = 48       # ms, n=6 (the factory record)
ENCODER_HOLD_MIN = 8            # ms, n=1
ENCODER_HOLD_MAX = 120          # ms, n=15 (the field is four bits)

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


def encoder_copy_addrs(prog: "Program", name: str) -> tuple:
    """The two little-endian usage slots inside one direction's record."""
    base = prog.b_addr(ENCODER_RECORDS[name]) + ENCODER_CODE_OFFSET
    return base, base + ENCODER_COPY_STRIDE


def encoder_mode_byte(mode: int) -> int:
    """The phase-A/phase-B byte for an encoder mode number."""
    mode = int(mode) % 16
    if mode == ENCODER_MODE_MEDIA:
        return ENCODER_MODE_MEDIA_BYTE
    return (mode << 4) | ENCODER_MODE_BYTE_BASE


def patch_encoder_mode(prog: "Program", mode: int) -> int:
    """Write the encoder mode byte into phase A and phase B."""
    value = encoder_mode_byte(mode)
    return (prog.patch("A", ENCODER_MODE_A_ADDR, bytes([value]))
            + prog.patch_b(ENCODER_MODE_B_ADDR, bytes([value])))


def encoder_hold_units(hold_ms: int) -> int:
    """The four-bit hold field for a millisecond value (1..15).

    Measured against the burning tool: 5 ms -> 1, 20 ms -> 2, 50 ms -> 6,
    120 ms -> 15, i.e. ``(ms + 3) // 8`` clamped to the four-bit field.
    """
    return max(1, min(15, (int(hold_ms) + 3) // 8))


def encoder_hold_ms(hold_ms: int) -> int:
    """The quantized hold time in milliseconds."""
    return encoder_hold_units(hold_ms) * 8


def encoder_macro_record(usage: int,
                         hold_ms: int = ENCODER_HOLD_DEFAULT) -> bytes:
    """A 16-byte record that presses and releases one keyboard usage."""
    usage = int(usage) & 0xFF
    hold = encoder_hold_units(hold_ms)
    return bytes([0x01, 0x00, ENCODER_MACRO_TAG_DOWN | hold, usage,
                  ENCODER_MACRO_TAG_UP | hold, usage] + [0] * 10)


def _encoder_macro_bytes(prog: "Program", name: str) -> bytes:
    addr = prog.b_addr(ENCODER_RECORDS[name])
    return bytes((prog.get(ENCODER_PHASE, addr + i) or 0)
                 for i in range(ENCODER_MACRO_LEN))


def _is_single_key_macro(rec: bytes) -> bool:
    return (rec[0:2] == b"\x01\x00"
            and (rec[2] & 0xF0) == ENCODER_MACRO_TAG_DOWN
            and (rec[4] & 0xF0) == ENCODER_MACRO_TAG_UP
            and rec[3] == rec[5] and not any(rec[6:]))


def read_encoder_macro(prog: "Program", name: str) -> int | None:
    """The keyboard usage of a single-key macro record, or None.

    Media records (``01 00 01 f1 ...``) do not match; neither do longer
    macros with several events.
    """
    rec = _encoder_macro_bytes(prog, name)
    return rec[3] if _is_single_key_macro(rec) else None


def read_encoder_hold(prog: "Program", name: str) -> int | None:
    """The quantized hold time of a single-key macro record, or None."""
    rec = _encoder_macro_bytes(prog, name)
    if not _is_single_key_macro(rec):
        return None
    down = rec[2] & 0x0F
    up = rec[4] & 0x0F
    if down != up or not 1 <= down <= 15:
        return None
    return down * 8


def patch_encoder_macro(prog: "Program", keys,
                        hold_ms: int = ENCODER_HOLD_DEFAULT) -> int:
    """Write a single-key macro into each encoder direction.

    ``keys`` maps ``"enc0"/"enc1"`` to a keyboard HID usage (0..0xFF).  This is
    the only mode in which rotation can send an ordinary keyboard key: the
    keymap slots 41/49 are not used by rotation, and slot 49 is the press.
    ``hold_ms`` is the key hold encoded in the record (8..120 ms, quantized to
    8 ms units).
    """
    n = 0
    for name, usage in keys.items():
        if name not in ENCODER_RECORDS:
            continue
        addr = prog.b_addr(ENCODER_RECORDS[name])
        for i, value in enumerate(encoder_macro_record(usage, hold_ms)):
            cur = prog.get(ENCODER_PHASE, addr + i)
            if cur is None or cur == value:
                continue
            prog.patch(ENCODER_PHASE, addr + i, bytes([value]))
            n += 1
    return n


def patch_encoder_entry(prog: "Program", name: str, active: bool) -> int:
    """Mark one encoder function-table entry active or inert."""
    index = 20 if name == "enc0" else 21
    addr = prog.b_addr(FUNCTION_TABLE_BASE) + index * FUNCTION_ENTRY_LEN
    kind = FUNCTION_KIND_ACTIVE if active else FUNCTION_KIND_EMPTY
    n = 0
    for offset, value in ((0, FUNCTION_ENTRY_TAG), (1, kind)):
        cur = prog.get("B", addr + offset)
        if cur is None or cur == value:
            continue
        prog.patch("B", addr + offset, bytes([value]))
        n += 1
    if active:
        target = prog.b_addr(ENCODER_RECORDS[name])
        for i, value in enumerate(target.to_bytes(3, "little")):
            offset = 2 + i
            cur = prog.get("B", addr + offset)
            if cur is None or cur == value:
                continue
            prog.patch("B", addr + offset, bytes([value]))
            n += 1
    return n


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
        a0, a1 = encoder_copy_addrs(prog, name)
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
        a0, a1 = encoder_copy_addrs(prog, name)

        def g(addr, _p=prog):
            return _p.get(ENCODER_PHASE, addr) or 0

        usage = g(a0) | (g(a0 + 1) << 8)
        out[name] = {
            "usage": usage,
            "number": encoder_number_of(usage),
            "second": g(a1) | (g(a1 + 1) << 8),
            "macro": read_encoder_macro(prog, name),
            "hold": read_encoder_hold(prog, name),
        }
    return out


# --------------------------------------------------------------------------- #
# function-record table
# --------------------------------------------------------------------------- #
def function_slot_label(index: int) -> str:
    """The physical control a function slot belongs to on this 8-key model."""
    if 10 <= index <= 17:
        return f"key {index - 9}"
    if index == 20:
        return "encoder 0"
    if index == 21:
        return "encoder 1"
    return "unused"


def read_function_table(prog: "Program", phase: str = "B") -> list:
    """Decode the 22 five-byte index entries of the function-record table.

    Every entry is ``tag kind addr0 addr1 addr2``; ``addr`` is a 24-bit
    little-endian pointer into the payload at ``0x168``.  An empty entry
    (kind 0) still points at the current payload start.  The K815 uses slots
    10..17 for keys 1..8 and 20/21 for the two encoder directions.
    """
    entries = []
    for i in range(FUNCTION_TABLE_ENTRIES):
        base = prog.b_addr(FUNCTION_TABLE_BASE) + i * FUNCTION_ENTRY_LEN
        b = [prog.get(phase, base + k) for k in range(FUNCTION_ENTRY_LEN)]
        if any(v is None for v in b):
            raise ValueError(f"function table: slot {i} is outside the program")
        raw_addr = b[2] | (b[3] << 8) | (b[4] << 16)
        entries.append({
            "index": i,
            "slot": function_slot_label(i),
            "tag": b[0],
            "kind": b[1],
            # The upload stream marks active slots with kind 3/4; the firmware
            # re-encodes them when storing (kind 0, high bit of the 24-bit
            # address set on inert slots).  Normalise the "inert" view below.
            "inert_raw": bool(raw_addr & 0x8000),
            "address": raw_addr & 0x7FFF,
        })
    app_format = any(e["kind"] in (FUNCTION_KIND_INTERNAL,
                                   FUNCTION_KIND_ACTIVE) for e in entries)
    for e in entries:
        e["inert"] = (e["kind"] == FUNCTION_KIND_EMPTY) if app_format \
            else e["inert_raw"]
    return entries


def read_function_record(prog: "Program", address: int,
                         length: int = 16, phase: str = "B") -> bytes:
    """The raw record bytes an active table entry points at."""
    out = bytes((prog.get(phase, address + i) or 0) for i in range(length))
    return out


# --------------------------------------------------------------------------- #
# keymap + whole-state presets
# --------------------------------------------------------------------------- #
def read_keymap(prog: "Program", phase: str = "A", base: int | None = None) -> bytes:
    """Read the 160-byte keymap of one phase (phase A by default)."""
    if base is None:
        base = KEYMAP_BASE if phase == "A" else prog.b_addr(KEYMAP_BASE_B)
    return bytes((prog.get(phase, base + i) or 0) for i in range(KEYMAP_LEN))


# The preset format is shared with the WebHID page (webhid/app.js export).
PRESET_VERSION = 3


def preset_from_program(prog: "Program", template: str | None = None,
                        keymap_phase: str = "A",
                        keymap_base: int | None = None) -> dict:
    """Decode the whole configurable state into a browser-compatible preset.

    A captured session keeps two copies of the keymap (phase A at 0x20, phase B
    at 0x49).  The default reads phase A, which is what a template carries.  A
    device dump is a single memory image and must use phase B: the later header
    write overlaps 0x20..0x21 and the phase-B keymap overlaps 0x49..0xE8, so
    only the phase-B copy survives intact.
    """
    led = read_led(prog)
    km = read_keymap(prog, keymap_phase, keymap_base)
    enc_numbers = {}
    enc_keys = {}
    enc_hold = None
    for name, info in read_encoder(prog).items():
        if info["macro"] is not None:
            enc_keys[name] = info["macro"]
            if enc_hold is None and info.get("hold") is not None:
                enc_hold = info["hold"]
        elif info["number"] >= 0:
            enc_numbers[name] = info["number"]
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
        "encoder": enc_numbers,
    }
    if enc_keys:
        preset["encoderKeys"] = enc_keys
        if enc_hold is not None:
            preset["encoderHoldMs"] = enc_hold
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
    counts = {"keymap": 0, "led": 0, "switches": 0, "encoder": 0,
              "encoderKeys": 0, "encoderMode": 0}

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
                            + prog.patch_b(KEYMAP_BASE_B, bytes(km)))

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

    # A keyboard macro in the rotation record is the working way to make the
    # encoder send ordinary keys.  It implies the media-mode byte and active
    # function-table entries; the record itself carries the key.
    enc_keys = preset.get("encoderKeys")
    if isinstance(enc_keys, dict) and enc_keys:
        hold = preset.get("encoderHoldMs", ENCODER_HOLD_DEFAULT)
        if not isinstance(hold, (int, float)):
            hold = ENCODER_HOLD_DEFAULT
        counts["encoderMode"] = patch_encoder_mode(prog, ENCODER_MODE_MEDIA)
        counts["encoderKeys"] = patch_encoder_macro(prog, enc_keys, hold_ms=hold)
        for name in enc_keys:
            if name in ENCODER_RECORDS:
                counts["encoderKeys"] += patch_encoder_entry(prog, name, True)

    mode = preset.get("encoderMode")
    if isinstance(mode, int):
        counts["encoderMode"] = patch_encoder_mode(prog, mode)

    return counts


# --------------------------------------------------------------------------- #
# device memory dumps
# --------------------------------------------------------------------------- #
# The browser page (webhid/app.js, "Dump") writes this shape, and so does
# `k815ctl.py dump --json`; either file can be converted into a preset.
DUMP_KIND = "k815-memory-dump"
DUMP_VERSION = 1


def make_dump(address: int, data: bytes, vendor_id: int | None = None,
              product_id: int | None = None,
              product: str | None = None,
              config_base: int | None = None,
              record_base: int | None = None) -> dict:
    """Build a WebHID-compatible dump record for a memory image."""
    data = bytes(data)
    record = {
        "version": DUMP_VERSION,
        "kind": DUMP_KIND,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "address": address,
        "length": len(data),
        "reportId": REPORT_ID,
        "sha256": hashlib.sha256(data).hexdigest(),
        "memory": {},
    }
    if config_base is not None:
        record["configBase"] = config_base
    if record_base is not None:
        record["recordBase"] = record_base
    if vendor_id is not None:
        record["vendorId"] = f"0x{vendor_id:04X}"
    if product_id is not None:
        record["productId"] = f"0x{product_id:04X}"
    if product:
        record["product"] = product
    for off in range(0, len(data), 16):
        record["memory"][f"0x{address + off:04X}"] = list(data[off:off + 16])
    return record


def read_dump(path: str, base: int | None = None) -> tuple:
    """Load a dump file and return ``(image, address, record)``.

    Accepts the JSON written by the WebHID page and by ``dump --json``, or a
    raw binary image as written by ``dump --out`` (pass ``base`` for the
    address of its first byte; it defaults to 0).  Gaps, a length mismatch and
    a bad sha256 are rejected instead of being silently filled with zeros.
    """
    raw = open(path, "rb").read()
    if not raw.lstrip().startswith(b"{"):
        return raw, 0 if base is None else base, {}

    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"{path}: not a JSON dump: {e}")
    if not isinstance(record, dict):
        raise ValueError(f"{path}: expected a JSON object")

    # A `backup --from-device` artifact carries the same image under
    # deviceMemory {base, length, hex}; accept it as a dump too.
    device_memory = record.get("deviceMemory")
    if record.get("kind") != DUMP_KIND and isinstance(device_memory, dict) \
            and "hex" in device_memory:
        try:
            data = bytes.fromhex(device_memory["hex"])
        except (TypeError, ValueError):
            raise ValueError(f"{path}: deviceMemory.hex is not hex")
        address = int(device_memory.get("base", 0))
        if base is not None:
            address = base
        return data, address, record
    if record.get("kind") != DUMP_KIND:
        raise ValueError(f"{path}: not a {DUMP_KIND} record")

    address = int(record.get("address", 0))
    length = int(record.get("length") or 0)
    memory = record.get("memory")
    if not isinstance(memory, dict) or not memory:
        raise ValueError(f"{path}: no memory map")
    if length <= 0:
        length = max(int(k, 0) for k in memory) + len(
            next(iter(memory.values()))) - address

    image = bytearray(length)
    covered = bytearray(length)
    for key, values in memory.items():
        start = int(key, 0) - address
        for i, value in enumerate(values):
            off = start + i
            if not 0 <= off < length:
                raise ValueError(f"{path}: byte 0x{address + off:04X} is "
                                 f"outside address/length")
            image[off] = int(value) & 0xFF
            covered[off] = 1
    missing = [address + i for i, ok in enumerate(covered) if not ok]
    if missing:
        raise ValueError(f"{path}: dump has {len(missing)} gap(s), first at "
                         f"0x{missing[0]:04X}")
    if base is not None:
        address = base

    sha = record.get("sha256")
    if sha:
        got = hashlib.sha256(bytes(image)).hexdigest()
        if got != sha:
            raise ValueError(f"{path}: sha256 mismatch ({got[:12]} != {sha[:12]})")
    return bytes(image), address, record


def program_from_image(image: bytes, address: int = 0) -> "Program":
    """A read-only Program view over a device memory image.

    The image must cover the staging area ``0x0000..0x018F``.  A full dump of
    the active ``0x00E0`` layout is longer (up to ``0x0267``); keep all bytes,
    because the keymap and function records live above ``0x018F`` there.
    Both phases see the same bytes, which is correct for a dump: it is one
    memory, not the two-phase write stream.  Never pass the result to
    `emit()`; use `preset_from_image()` to decode it.
    """
    image = bytes(image)
    if not image:
        raise ValueError("the dump is empty")
    if address != 0 or len(image) < IMAGE_LEN:
        end = address + len(image) - 1
        raise ValueError(f"a dump must cover at least 0x0000..0x{IMAGE_LEN - 1:04X}, "
                         f"got 0x{address:04X}..0x{end:04X}")
    data = image
    prog = Program()
    prog.items.append(Block(addr=0, length=len(data), data=data, phase="A"))
    prog.items.append(Block(addr=0, length=len(data), data=data, phase="B"))
    return prog


def preset_from_image(image: bytes, address: int = 0,
                      template: str | None = None,
                      config_base: int = 0x0001,
                      record_base: int = 0x0168) -> dict:
    """Decode a device memory image into a preset (keymap from phase B).

    ``config_base`` is the phase-B start reported by the device (0x0001 in the
    staging layout, 0x00E0 on a burned unit).  ``record_base`` is the first
    function-record byte (0x0168 staging, 0x0248 on the 0x00E0 layout).
    """
    prog = program_from_image(image, address)
    prog.base = config_base
    prog.record_base = record_base
    return preset_from_program(prog, template, keymap_phase="B",
                               keymap_base=KEYMAP_BASE_B)


# On real hardware the active configuration is stored in a bank at 0x0118, not
# in the staging area the application writes (0x0001).  The bank is the phase-B
# image shifted by 0x117; only the record payload is one byte lower (0x280,
# because the bank has a two-byte `CF 00` separator where the staging image has
# a single zero).  Verified on one K815 with a full memory read.
STORED_BASE = 0x0118
STORED_LEN = 0x187          # 0x0118..0x029E


def stored_to_staging(image: bytes, address: int = STORED_BASE) -> bytes:
    """Map the stored config bank onto the 0x0001 staging layout."""
    bank = bytes(image)
    if address != STORED_BASE:
        raise ValueError(f"a stored bank starts at 0x{STORED_BASE:04X}, "
                         f"got 0x{address:04X}")
    if len(bank) < STORED_LEN:
        raise ValueError(f"a stored bank is 0x{STORED_LEN:X} bytes, "
                         f"got 0x{len(bank):X}")
    img = bytearray(b"\xFF" * IMAGE_LEN)
    img[0x01:0x167] = bank[0x00:0x166]      # header, keymap, LED, index
    img[0x167] = 0x00                        # single separator
    img[0x168:0x187] = bank[0x168:0x187]    # function records
    return bytes(img)


def preset_from_stored(image: bytes, address: int = STORED_BASE,
                       template: str | None = None) -> dict:
    """Decode the stored (active) config bank of a real device."""
    return preset_from_image(stored_to_staging(image, address), 0, template)


def parse_report(b: bytes) -> Frame | None:
    if len(b) < 8 or b[0] != REPORT_ID:
        return None
    return Frame(op=b[1], sub=b[2], idx=b[3], addr=b[4] | (b[5] << 8),
                 data=b[6], ln=b[7], flag=b[14], raw=bytes(b))


MIN_DELAY_MS = 20


def capture_delays(capture: str, frames: list,
                   min_delay_ms: int = MIN_DELAY_MS) -> list:
    """Measure the pauses of `frames` in a capture (milliseconds).

    Returns ``[[frame index, ms], ...]``; the index is the frame the pause goes
    *before*.  Empty when the capture is missing or does not contain the
    emitted session.  Used by the CLI and `tools/build_webhid.py` so a replay
    reproduces the vendor application's timing, not just its bytes.
    """
    records = []
    try:
        for raw, t in capture_records(capture):
            if t is not None:
                records.append((raw, t))
    except OSError:
        return []

    n = len(frames)
    start = None
    if n:
        for i in range(len(records) - n, -1, -1):
            if all(records[i + k][0] == frames[k] for k in range(n)):
                start = i
                break
    if start is None:
        # A patched session no longer matches byte for byte (the data bytes
        # differ), so fall back to matching the frame structure: opcode,
        # subcommand, index and address are unchanged by a preset patch.
        for i in range(len(records) - n, -1, -1):
            if all(records[i + k][0][:6] == frames[k][:6] for k in range(n)):
                start = i
                break
    if start is None:
        return []

    delays = []
    for i in range(1, n):
        ms = int(round((records[start + i][1] - records[start + i - 1][1]) * 1000.0))
        if ms > min_delay_ms:
            delays.append([i, ms])
    return delays


def _open_capture(path: str):
    """Open a capture, transparently handling the gzipped test fixture."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def capture_records(path: str) -> list:
    """``(raw, timestamp)`` for every 16-byte SET report, in order.

    ``timestamp`` is ``None`` when the capture does not store one.  Malformed
    lines are skipped, so a truncated capture still yields its valid prefix.
    """
    out = []
    with _open_capture(path) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != "SET" or "raw" not in rec:
                continue
            try:
                raw = bytes.fromhex(rec["raw"])
            except ValueError:
                continue
            if len(raw) != REPORT_LEN:
                continue
            t = rec.get("t")
            out.append((raw, float(t) if t is not None else None))
    return out


def _set_reports(path: str) -> list:
    """Every 16-byte SET_REPORT payload from a capture, in order."""
    return [raw for raw, _t in capture_records(path)]


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
    bblocks = [it for it in prog.items if isinstance(it, Block) and it.phase == "B"]
    if bblocks:
        prog.base = min(it.addr for it in bblocks)
        prog.record_base = min(
            (it.addr for it in bblocks if it.addr >= prog.base + 0x166),
            default=prog.base + 0x167)
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


def rebase_phase_b(prog: "Program", new_base: int) -> int:
    """Move a captured phase-B session from one config base to another.

    A real device reports its base in the info answer; when the burning tool
    changes that base (e.g. 0x0118 -> 0x00E0), the addresses and the two
    pointers in the phase-B header must move by the same delta.  The phase-A
    addresses (0x20, 0xD0) are fixed; the service block gets the new base.
    Returns the applied delta.
    """
    old_base = prog.base
    delta = new_base - old_base
    if delta == 0:
        return 0
    for it in prog.items:
        if isinstance(it, Block) and it.phase == "B":
            it.addr += delta
            for f in it.trailer:
                f.addr += delta
        elif isinstance(it, Frame) and it.op == OP_MEM \
                and it.sub in (SUB_IDLE, SUB_READ) and it.addr == old_base:
            # The app verifies the service header at the config base; move it
            # with the base and rebuild the verbatim raw frame.
            it.addr = new_base
            it.raw = it.build().ljust(REPORT_LEN, b"\0")
    # Header pointers: keymap base at +0x48, function table at +0xF8.
    prog.patch("B", new_base + 0x1C,
               (new_base + 0x48).to_bytes(2, "little"))
    prog.patch("B", new_base + 0x1E,
               (new_base + 0xF8).to_bytes(2, "little"))
    # Phase-A service block stores the config base at 0xD2..0xD3.
    prog.patch("A", 0xD2, new_base.to_bytes(2, "little"))
    # Function-table entries carry 24-bit payload addresses.
    for k in range(FUNCTION_TABLE_ENTRIES):
        a = new_base + (FUNCTION_TABLE_BASE - 0x0001) + k * FUNCTION_ENTRY_LEN
        b = [prog.get("B", a + i) or 0 for i in range(FUNCTION_ENTRY_LEN)]
        old = b[2] | (b[3] << 8) | (b[4] << 16)
        new = old + delta
        prog.patch("B", a + 2,
                   bytes([new & 0xFF, (new >> 8) & 0xFF, (new >> 16) & 0xFF]))
    prog.base = new_base
    prog.record_base += delta
    return delta


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
