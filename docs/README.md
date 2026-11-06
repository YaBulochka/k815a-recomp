# Documentation

Start with the project [README](../README.md).

| Document | Contents |
|---|---|
| [HARDWARE.md](HARDWARE.md) | controller (IST83025B/K201), USB descriptors, interfaces, LED topology, encoder, config base, identity recovery |
| [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md) | polling, encoder dropouts, per-key LED limits, read/write timing, input hang, write application, bricking risk, OpenRGB feasibility |
| [../PROTOCOL.md](../PROTOCOL.md) | transport, report format, upload session, memory map, keymap and LED encoding |
| [../RE-NOTES.md](../RE-NOTES.md) | installer and vendor application internals, UI click map |
| [../KEYS.md](../KEYS.md) | reassigning keys, F13-F24 as hotkeys, xkb pitfalls |
| [../PLAN.md](../PLAN.md) | work plan and what each stage proved |
| [../webhid/README.md](../webhid/README.md) | the browser tool in detail |

## Short version

* The K815 is a fixed-function **IST83025B** keyboard ASIC, USB 1.1 low-speed,
  100 Hz polling, 7-colour global lighting, no per-key RGB.
* Config reads are one byte per three USB transactions, about 20 ms per byte.
* A read leaves the firmware in a state where typing stops; the page sends the
  closing `INIT` + `COMMIT` handshake to restore it. A bus reset is not a power
  cycle.
* Writes are 663 feature reports, about a second plus protocol pauses; applying
  a new keymap may need a physical replug.
* OpenRGB is realistic only as a global palette/effect driver, not per-key.
