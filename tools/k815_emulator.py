#!/usr/bin/env python3
"""Fake MKESPN vendor HID device for capturing the config protocol on Linux.

Registers one uhid HID device per vendor usage page (default 0xFF01 and
0xFF00), with VID/PID of the MKESPN family, 16-byte unnumbered feature report.
Every HID feature request coming from the host (via winebus) is logged and the
GET_REPORT answers are served from a simulated non-volatile memory image.

The point is not to emulate the firmware faithfully, but to observe the exact
bytes the original Windows application sends, so the protocol can be rebuilt
for Linux/hidraw and WebHID.

Usage:
    python3 tools/k815_emulator.py [--log FILE] [--usage-pages 0xff01,0xff00]

Requires nothing but Python 3 and write access to /dev/uhid.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import struct
import sys
import threading
import time

UHID_PATH = "/dev/uhid"
BUS_USB = 0x03

# uhid event types (linux/uhid.h)
T_DESTROY = 1
T_START = 2
T_STOP = 3
T_OPEN = 4
T_CLOSE = 5
T_OUTPUT = 6
T_GET_REPORT = 9
T_GET_REPORT_REPLY = 10
T_CREATE2 = 11
T_INPUT2 = 12
T_SET_REPORT = 13
T_SET_REPORT_REPLY = 14

# uhid_report_type
RT_FEATURE = 0
RT_OUTPUT = 1
RT_INPUT = 2

RT_NAMES = {RT_FEATURE: "FEATURE", RT_OUTPUT: "OUTPUT", RT_INPUT: "INPUT"}

# sizeof(struct uhid_event): 4 + uhid_create2_req(276 + 4096)
EV_SIZE = 4 + 276 + 4096

HID_REPORT_LEN = 16          # FeatureReportByteLength the app expects (hardcoded 0x10)
FIRST_BYTE = 0x07            # app puts 0x07 at buffer[0] of every request
OPCODE = 0x18                # app puts 0x18 at buffer[1] of every request

MEM_SIZE = 0x10000


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def pad(b: bytes, n: int) -> bytes:
    return b[:n].ljust(n, b"\0")


# --------------------------------------------------------------------------- #
# HID report descriptor
# --------------------------------------------------------------------------- #
def report_descriptor(usage_page: int, report_id: int = 0x07,
                      report_len: int = HID_REPORT_LEN) -> bytes:
    """Vendor feature report.

    The application always puts 0x07 in byte 0 of the report buffer and passes
    FeatureReportByteLength as the length, so the device uses numbered reports
    with Report ID 7.  Total report length stays 16 (1 ID byte + 15 payload).
    """
    items = [
        0x06, usage_page & 0xFF, (usage_page >> 8) & 0xFF,  # Usage Page
        0x09, 0x01,                                          # Usage (1)
        0xA1, 0x01,                                          # Collection (Application)
    ]
    payload_len = report_len
    if report_id:
        items += [0x85, report_id]                           #   Report ID (7)
        payload_len = report_len - 1
    items += [
        0x15, 0x00,                                          #   Logical Minimum (0)
        0x26, 0xFF, 0x00,                                    #   Logical Maximum (255)
        0x75, 0x08,                                          #   Report Size (8)
        0x95, payload_len,                                   #   Report Count
        0x09, 0x01,                                          #   Usage (1)
        0xB2, 0x02, 0x01,                                    #   Feature (Data,Var,Abs,Vol)
        0xC0,                                                # End Collection
    ]
    return bytes(items)


def create2_event(name: bytes, rd: bytes, vendor: int, product: int) -> bytes:
    u = bytearray()
    u += pad(name, 128)
    u += pad(b"", 64)            # phys
    u += pad(b"", 64)            # uniq
    u += struct.pack("<HHIIII", len(rd), BUS_USB, vendor, product, 0x0100, 0)
    u += rd
    ev = struct.pack("<I", T_CREATE2) + bytes(u)
    return ev.ljust(EV_SIZE, b"\0")


# --------------------------------------------------------------------------- #
# Simulated device
# --------------------------------------------------------------------------- #
class K815Device(threading.Thread):
    """One uhid HID device plus the protocol state machine."""

    def __init__(self, usage_page, args, capture):
        super().__init__(daemon=True)
        self.usage_page = usage_page
        self.args = args
        self.cap = capture
        self.tag = f"0x{usage_page:04X}"
        self.dev_name = f"{args.device_name} ({self.tag})".encode()
        self.mem = bytearray(MEM_SIZE)
        self.stop_flag = False
        self.fd = -1
        self.report_count = 0
        # last request state, used to build GET_REPORT answers
        self.last = {"sub": None, "idx": 0, "addr": 0, "len": 0, "kind": "none"}

    # -- protocol ---------------------------------------------------------- #
    def seed(self, ini_path):
        """Fill simulated memory with the app's default keymap so reads look sane."""
        if not ini_path or not os.path.exists(ini_path):
            return
        vals = {}
        with open(ini_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("KU_"):
                    k, _, v = line.partition("=")
                    try:
                        vals[int(k[3:])] = int(v.strip().split(",")[0], 0)
                    except ValueError:
                        pass
        for i, v in vals.items():
            if 0 <= i < 0xA0:
                self.mem[0x20 + i] = v & 0xFF
        log(f"[{self.tag}] seeded {len(vals)} KU_* entries at 0x20")
        # 16-bit device value the app expects to read back at 0xD0
        self.dev_value = 0x0001

    def _answer(self, rnum, rtype):
        buf = bytearray(HID_REPORT_LEN)
        buf[0] = FIRST_BYTE
        sub = self.last["sub"]
        if sub == 0x05:  # single byte read
            addr = (self.last["addr"] + self.last["idx"]) & 0xFFFF
            buf[1] = self.mem[addr]
            self.last["kind"] = "byte-read"
        else:
            # generic "device info" answer: the app takes a 16-bit value from
            # the last two bytes and writes it back to 0xD0.
            val = getattr(self, "dev_value", 1)
            buf[14] = val & 0xFF
            buf[15] = (val >> 8) & 0xFF
            self.last["kind"] = "info"
        return buf

    def _decode(self, data: bytes):
        """Decode a 16-byte request into protocol fields.

        Layout (offset of the whole report buffer):
          0    0x07        constant first byte
          1    0x18        opcode
          2    subcommand
          3    index
          4-5  address (little endian, 16 bit)
          6    data byte
          7    length field (len-1 or len+0x5F, depends on model)
        """
        d = {"raw": data.hex()}
        if len(data) >= 8 and data[0] == FIRST_BYTE and data[1] == OPCODE:
            d.update(
                first=data[0], op=data[1], sub=data[2], idx=data[3],
                addr=data[4] | (data[5] << 8), data=data[6], ln=data[7],
                extra=data[8:].hex(),
            )
        return d

    def _apply(self, d):
        sub = d.get("sub")
        if sub == 0x03:                                   # write byte
            addr = (d["addr"] + d["idx"]) & 0xFFFF
            self.mem[addr] = d["data"]
        elif sub == 0x05:                                 # read byte
            pass
        elif sub == 0x81:                                 # erase sector
            base = d["addr"] & 0xFFE0
            for a in range(base, min(base + 0x20, MEM_SIZE)):
                self.mem[a] = 0xFF
        self.last.update(sub=sub, idx=d.get("idx", 0),
                         addr=d.get("addr", 0), len=d.get("ln", 0))

    # -- uhid plumbing ----------------------------------------------------- #
    def _write(self, ev: bytes) -> bool:
        try:
            os.write(self.fd, ev)
            return True
        except OSError as e:
            log(f"[{self.tag}] write failed: {e}")
            return False

    def run(self):
        try:
            self.fd = os.open(UHID_PATH, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            log(f"[{self.tag}] cannot open {UHID_PATH}: {e}")
            return
        rd = report_descriptor(self.usage_page, self.args.report_id)
        if not self._write(create2_event(self.dev_name, rd, self.args.vendor, self.args.product)):
            return
        log(f"[{self.tag}] registered: VID {self.args.vendor:#06x} PID {self.args.product:#06x}, "
            f"report id {self.args.report_id:#04x}, {len(rd)}-byte report descriptor, "
            f"{HID_REPORT_LEN}-byte feature report")

        while not self.stop_flag:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
            except InterruptedError:
                continue
            if not r:
                continue
            try:
                data = os.read(self.fd, EV_SIZE)
            except OSError:
                continue
            if not data:
                continue
            self._handle(data)

    def _handle(self, data: bytes):
        etype = struct.unpack_from("<I", data, 0)[0]
        if etype == T_START:
            flags = struct.unpack_from("<Q", data, 4)[0] if len(data) >= 12 else 0
            self.cap.event(self.tag, "START", {"dev_flags": flags})
            log(f"[{self.tag}] START (dev_flags={flags:#x})")
        elif etype == T_STOP:
            self.cap.event(self.tag, "STOP", {})
        elif etype == T_OPEN:
            self.cap.event(self.tag, "OPEN", {})
            log(f"[{self.tag}] opened by host")
        elif etype == T_CLOSE:
            self.cap.event(self.tag, "CLOSE", {})
        elif etype == T_SET_REPORT:
            rid = struct.unpack_from("<I", data, 4)[0]
            rnum, rtype, size = struct.unpack_from("<BBH", data, 8)
            payload = data[12:12 + size]
            d = self._decode(payload)
            d["rnum"] = rnum
            d["rtype"] = rtype
            d["size"] = size
            self.cap.event(self.tag, "SET", d)
            self._log_set(d)
            self._apply(d)
            reply = struct.pack("<I", T_SET_REPORT_REPLY) + struct.pack("<IH", rid, 0)
            self._write(reply.ljust(EV_SIZE, b"\0"))
        elif etype == T_GET_REPORT:
            rid = struct.unpack_from("<I", data, 4)[0]
            rnum, rtype = struct.unpack_from("<BB", data, 8)
            buf = self._answer(rnum, rtype)
            self.cap.event(self.tag, "GET", {
                "rnum": rnum, "rtype": rtype, "answer": buf.hex(),
                "kind": self.last["kind"],
                "req": {k: self.last[k] for k in ("sub", "idx", "addr", "len")},
            })
            log(f"[{self.tag}] GET  rnum={rnum} rtype={RT_NAMES.get(rtype, rtype)} "
                f"-> {buf.hex()}   (kind={self.last['kind']})")
            u = struct.pack("<I", rid) + struct.pack("<HH", 0, len(buf)) + buf
            self._write((struct.pack("<I", T_GET_REPORT_REPLY) + u).ljust(EV_SIZE, b"\0"))
        elif etype == T_OUTPUT:
            size = struct.unpack_from("<H", data, 4 + 4096)[0]
            rtype = data[4 + 4098]
            payload = data[4:4 + size]
            self.cap.event(self.tag, "OUTPUT", {"rtype": rtype, "raw": payload.hex()})
            log(f"[{self.tag}] OUTPUT rtype={rtype} {payload.hex()}")
        else:
            self.cap.event(self.tag, "EVENT", {"type": etype, "raw": data[:64].hex()})
            log(f"[{self.tag}] event type={etype}")

    def _log_set(self, d):
        if "sub" not in d:
            log(f"[{self.tag}] SET  (undecoded) {d['raw']}")
            return
        log(f"[{self.tag}] SET  sub={d['sub']:#04x} idx={d['idx']:#04x} "
            f"addr={d['addr']:#06x} data={d['data']:#04x} ln={d['ln']:#04x}   [{d['raw']}]")


# --------------------------------------------------------------------------- #
# Capture sink
# --------------------------------------------------------------------------- #
class Capture:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.fh = open(path, "w", encoding="utf-8") if path else None
        self.t0 = time.time()

    def event(self, tag, kind, payload):
        rec = {"t": round(time.time() - self.t0, 4), "dev": tag, "kind": kind}
        rec.update(payload)
        with self.lock:
            if self.fh:
                self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="captures/session.jsonl",
                    help="JSONL capture file (default: captures/session.jsonl)")
    ap.add_argument("--usage-pages", default="0xff01,0xff00",
                    help="comma separated vendor usage pages to expose")
    ap.add_argument("--vendor", type=lambda s: int(s, 0), default=0x30FA)
    ap.add_argument("--product", type=lambda s: int(s, 0), default=0x2350)
    ap.add_argument("--device-name", default="MKESPN K815")
    ap.add_argument("--report-id", type=lambda s: int(s, 0), default=0x07,
                    help="HID report ID (0 = unnumbered reports)")
    ap.add_argument("--seed-ini",
                    default="extracted/app/config/config.ini",
                    help="config.ini to seed the simulated keymap from")
    ap.add_argument("--pidfile", default=None,
                    help="write the emulator PID here (for safe automation)")
    args = ap.parse_args()

    if args.pidfile:
        with open(args.pidfile, "w") as fh:
            fh.write(str(os.getpid()))

    if not os.path.exists(UHID_PATH):
        log(f"{UHID_PATH} missing: is the uhid module loaded?")
        return 1

    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)
    cap = Capture(args.log)
    log(f"capture -> {args.log}")

    pages = [int(p, 0) for p in args.usage_pages.split(",") if p.strip()]
    devs = []
    for page in pages:
        dev = K815Device(page, args, cap)
        dev.seed(args.seed_ini)
        dev.start()
        devs.append(dev)

    log("ready. Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("stopping...")
    finally:
        for d in devs:
            d.stop_flag = True
        for d in devs:
            d.join(timeout=2)
            if d.fd >= 0:
                try:
                    os.write(d.fd, struct.pack("<I", T_DESTROY).ljust(EV_SIZE, b"\0"))
                except OSError:
                    pass
                os.close(d.fd)
        cap.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
