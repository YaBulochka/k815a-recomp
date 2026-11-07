# K815 hardware

This page collects what is known about the physical device. The protocol is in
[../PROTOCOL.md](../PROTOCOL.md); the practical limits that follow from this
hardware are in [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md).

## Controller

| | |
|---|---|
| Part | **IST83025B/K201** |
| Vendor | Wuxi Instant Microelectronics (无锡英斯特微电子有限公司) |
| Class | fully custom keyboard-controller ASIC, not a general-purpose MCU |
| Package | Dice / QFP48 |
| USB | USB 1.1 **low-speed** (1.5 Mbit/s), HID 1.1 |
| Matrix | 20x8, up to 160 keys, 6/19/26-key rollover |
| Config memory | 4-128 kb EEPROM (vendor figure; the unit's size is not measured) |
| Lighting | built-in 7-colour / flowing-light driver, single-wire LED outputs |
| Encoder | one group |
| Supply / current | 4.5-5.5 V, about 3.6 mA |
| Firmware | factory-burned; custom VID/PID and strings; no user firmware, no DFU |

Vendor page: <https://www.instant-sys.com/product/82/detail/41>.

Consequences: the chip is not a Cortex-M board. There is no user firmware, no
bootloader, no per-key LED bus, and its USB stack is minimal. Most of the odd
behaviour in this repository follows from that (low polling rate, slow config
reads, the memory-read state that blocks typing).

## USB descriptors (measured on `30fa:2350`)

```
bcdUSB              1.10
bMaxPacketSize0     8
Interface 0         HID boot keyboard
  endpoint          0x81 IN, interrupt, 8 bytes, bInterval 10
Interface 1         HID, vendor collections 0xFF00/0xFF01 + consumer/system
  endpoint          0x82 IN, interrupt, 8 bytes, bInterval 10
Feature report      report ID 7
  hardware          8 bytes on the wire (ID + 7 payload)
  uhid emulator     16 bytes (the emulator descriptor is larger)
```

For low-speed USB `bInterval` is in 1 ms units, so both interrupt endpoints ask
for a report every **10 ms**: the keyboard is a **100 Hz** device. The feature
reports used for configuration are separate control transfers and are much
slower per byte (see below).

There is **no block-read command**. A memory read is one byte per
`07 18 05` -> `GET_FEATURE` -> `07 18 01` cycle. Experiments with the length
and index bytes of `05` on real hardware returned exactly one data byte; the
rest of the answer is the device-info buffer.

## Interfaces and collections

The device exposes two USB interfaces:

* interface 0: the normal keyboard (boot protocol) and its input endpoint;
* interface 1: HID collections `0xFF00` and `0xFF01` (the vendor/config
  collections), plus consumer-control and system-control collections.

On Linux both vendor collections are on the same `/dev/hidrawN` node. The vendor
application opens `0xFF01` for writing and `0xFF00` for reading; the page takes
`0xFF01` and falls back to `0xFF00`.

## LED topology

The controller has a built-in LED driver for up to three **single-wire** channels
(`SLED0..2` in the sibling IST83010B/IST83020B datasheets; the K815 protocol
exposes the same kind of 7-colour global effect). The host does not send
per-LED data. It stores:

* a palette of **7 colours**, 4 bits per channel (stored inverted: 0 = full,
  15 = off);
* one global effect: mode, speed, direction, symmetry, single-colour,
  reaction and a 7-bit mask of active colours.

The effect is rendered by the ASIC across the keys and the encoder area. There
is no addressable-RGB bus, no per-key colour table and no way to stream a frame
of LED data from the host. See [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md) for
what this means for OpenRGB.

## Encoder

One mechanical encoder is wired to the controller's `W1`/`W2` inputs. Rotation
can be configured as one of 17 Consumer-page functions or as a single ordinary
HID key per direction. The single-key mode is written as a 16-byte macro record
`01 00 8n <usage> 0n <usage>`; the high nibble is down (`0x80`) or up (`0x00`),
and the low nibble is the key hold in 8 ms units. The factory `86`/`06` is a
**48 ms** hold, configurable from 8 to 120 ms. The encoder press is keymap
slot 49; slot 41 is not used by rotation on this firmware.

The encoder is scanned and reported by the ASIC. There is no host-side setting
for scan rate, debounce or acceleration; the key hold above is the only timing
control.

## Config memory, base and identity

* The config base comes from the startup info answer: `0x0118` on the factory
  unit, `0x00E0` after the chip vendor's burning tool.
* The upload is two phases (A and B); the tools select the captured session
  that matches the reported base.
* The hardware only accepts **8-byte** feature reports. The first write attempt
  with the emulator-sized 16-byte reports damaged the identity area; the unit
  came back as `30fa:2360`. The chip vendor's **IST83025B burning tool**
  (`KB_Config.exe`, `20240819112054.zip`) restored PID `0x2350`, the product
  strings and the `0x00E0` layout.
* Treat every write of a malformed session as a potential identity corruption;
  keep a dump/backup and the burning tool at hand.

## What the hardware is not

* Not QMK/ARM: no open firmware, no DFU, no 1000 Hz mode.
* Not a per-key RGB device: one global palette and one effect.
* Not a fast config interface: one byte per three USB transactions, roughly
  20 ms per byte.
* Not a device that survives a vendor session without a closing handshake: the
  firmware stays in the memory-read state and stops sending normal input. The
  page sends `INIT` + `COMMIT` after every read to leave it; a USB bus reset is
  not a power cycle, so a replug used to be the only fix.
