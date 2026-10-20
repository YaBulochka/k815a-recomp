#!/usr/bin/env python3
"""Decode a capture produced by tools/k815_emulator.py into a readable trace.

    python3 tools/decode_capture.py captures/wine4.jsonl [--collapse] [--mem]

--collapse  group consecutive byte writes into ranges
--mem       replay writes into a 64 KiB image and print a hexdump of nonzero
            regions (shows what the resulting device memory looks like)
"""
from __future__ import annotations

import argparse
import json
import sys

MEM_SIZE = 0x10000


def decode(raw_hex, rnum, size):
    """Turn one 16-byte SET_REPORT payload into protocol fields."""
    b = bytes.fromhex(raw_hex)
    d = {"raw": raw_hex, "rnum": rnum, "size": size, "b": b}
    if len(b) >= 8 and b[0] == 0x07 and b[1] in (0x18, 0x09):
        d["op"] = b[1]
    else:
        # some commands are dwords written whole (e.g. 07 09 41 00)
        d["op"] = b[1]
    b0, op = b[0], b[1]
    if op == 0x18:
        d.update(sub=b[2], idx=b[3], addr=b[4] | (b[5] << 8), data=b[6], ln=b[7],
                 tail=b[8:].hex())
    else:
        d.update(sub=b[2], n=b[3], tail=b[4:].hex())
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--collapse", action="store_true")
    ap.add_argument("--mem", action="store_true")
    ap.add_argument("--from", dest="t0", type=float, default=None)
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(args.path)]
    mem = bytearray(MEM_SIZE)
    written = {}          # addr -> value (last write wins)

    # collapse buffers
    pending = None        # (start_addr, [bytes], ts)

    def flush():
        nonlocal pending
        if pending:
            start, vals, ts = pending
            print(f"  {ts:>8}  WRITE {len(vals):>3} B at {start:#06x}..{start+len(vals)-1:#06x} : "
                  f"{bytes(vals).hex()}")
            pending = None

    prev_ts = 0.0
    for r in recs:
        if r["kind"] == "SET":
            d = decode(r["raw"], r.get("rnum"), r.get("size"))
            if d["op"] == 0x18:
                if d["sub"] == 0x03:
                    a = d["addr"] + d["idx"]
                    mem[a] = d["data"]
                    written[a] = d["data"]
                    if args.collapse:
                        if pending and a == pending[0] + len(pending[1]) and abs(r["t"] - prev_ts) < 0.5:
                            pending[1].append(d["data"])
                        else:
                            flush()
                            pending = (a, [d["data"]], r["t"])
                    else:
                        print(f"{r['t']:>8}  W {d['addr']:#06x}+{d['idx']:>3} = {d['data']:#04x}")
                else:
                    if args.collapse:
                        flush()
                    print(f"{r['t']:>8}  SET sub={d['sub']:#04x} idx={d['idx']:#02x} "
                          f"addr={d['addr']:#06x} data={d['data']:#04x} ln={d['ln']:#04x}")
            else:
                if args.collapse:
                    flush()
                print(f"{r['t']:>8}  OP {d['op']:#04x} sub={d['sub']:#04x} n={d['n']:#04x} "
                      f"raw={d['raw']}")
            prev_ts = r["t"]
        elif r["kind"] == "GET":
            if args.collapse:
                flush()
            print(f"{r['t']:>8}  GET -> {r['kind'] if 'kind' in r else ''} {r['answer']}")
        else:
            print(f"{r['t']:>8}  {r['kind']}")
    if args.collapse:
        flush()

    if args.mem:
        print("\n# memory image (nonzero)")
        for base in range(0, MEM_SIZE, 0x10):
            chunk = mem[base:base + 0x10]
            if any(chunk):
                print(f"{base:04x}: " + " ".join(f"{c:02x}" for c in chunk))
        print("\n# written addresses:", len(written),
              "range", f"{min(written):#06x}..{max(written):#06x}" if written else "-")
        missing = [a for a in range(0, MEM_SIZE) if a not in written]
        print("# never written in 0x00..0x1ff:",
              [f"{a:#04x}" for a in missing if a < 0x200])


if __name__ == "__main__":
    sys.exit(main())
