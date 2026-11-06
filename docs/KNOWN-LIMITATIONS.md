# Known limitations

Everything here follows from the IST83025B controller and its fixed firmware.
None of these are bugs in the WebHID page or in `k815ctl`; they are properties
of the hardware. Hardware details are in [HARDWARE.md](HARDWARE.md).

## Summary

| Area | Limit |
|---|---|
| Polling rate | **100 Hz** (`bInterval = 10 ms` on both interrupt endpoints) |
| Encoder | fast detents are dropped; key down/up in the macro record has no delay |
| Per-key lighting | **not available**; one 7-colour palette + one global effect |
| Addressable RGB / OpenRGB per key | not possible on this controller |
| Config read speed | ~20 ms per byte, ~4.8 s for the 225-byte config (single pass) |
| Config write | 663 reports, ~1 s, plus mandatory protocol pauses |
| Read side effect | the keyboard stops typing until a closing `INIT` + `COMMIT` |
| Applying a write | needs a real USB re-enumeration/power cycle |
| Firmware | fixed-function ASIC; no user firmware, no DFU |
| Wrong wire length | a 16-byte write can corrupt the identity area |

## USB and polling

The device is USB 1.1 **low-speed**, and both HID interrupt endpoints declare
`bInterval = 10` (10 ms). For low-speed USB that means the host polls the
keyboard at most once every 10 ms: **100 Hz**. The endpoint interval is part of
the device descriptor; the vendor config (`config.ini`) has no polling, scan
rate, debounce or response-time field.

The HID input report is a **state snapshot**, not an event queue. If a key or
the encoder is pressed and released inside one 10 ms window, the host may see
only the final "all released" state. Events are lost before any userspace
software can see them.

The only cheap host-side experiment is the Linux `usbhid.kbpoll` override:

```sh
cat /sys/module/usbhid/parameters/kbpoll   # 0 = use the endpoint interval
sudo modprobe -r usbhid && sudo modprobe usbhid kbpoll=1
```

It makes the host poll every 1 ms instead of 10 ms. It helps only if the
firmware actually updates its state faster than the advertised interval; if it
scans at 10 ms, the extra IN tokens are simply NAKed. Unloading `usbhid` affects
all HID devices for a moment. To make it permanent, use
`options usbhid kbpoll=1` in `/etc/modprobe.d/` and reboot. Windows has no
equivalent HID polling override for this device.

## Encoder

The encoder is read by the ASIC. Rotation is either an internal Consumer
function or a single-key macro. The single-key record is

```
01 00 86 <usage> 06 <usage> 00 ...
```

i.e. key down immediately followed by key up, with no delay. Combined with the
10 ms report interval, a detent can be completely missed: the host polls after
the key is already released. This is why a quick spin "activates" only a few
times.

There is no setting for encoder scan rate, debounce, acceleration or key-hold
time. Options:

* use **Media functions** for rotation instead of a keyboard key; the internal
  Consumer function may be held long enough to be seen more reliably;
* try `usbhid.kbpoll=1` (above);
* insert a delay between down and up in the macro record: the `0x86`/`0x06`
  opcodes are not fully decoded and a wrong record can corrupt the device, so
  this is experimental, not supported by the tools;
* accept the limit or replace the controller (see below).

The encoder press is keymap slot 49. Slot 41 is not used by rotation.

## Lighting

There is **no per-key colour storage**. The device has a 7-colour palette, one
global effect and a mask of active colours. The ASIC renders the effect across
all keys. The WebHID preview shows the colours the global effect paints on each
key; it does not write per-key colours.

The vendor UI exposes these effects (the page filters the list the same way):

`Backlight Off`, `Breathing`, `Constant`, `Neon`, `Ambilight`, `Single Water`,
`Flow Water`, `Flashing`, `Partition`.

Numbers outside that list are not offered by the vendor application; the page
shows a mode already stored in the device as `(current)` so a read does not lose
it. Colours are 4 bits per channel; the page exposes them as 0-255 in steps of
17 because the firmware has 16 real levels. The stored wire value is inverted
(0 = full, 15 = off); presets and the CLI keep the raw value.

## Config memory and read speed

The configuration is a two-phase memory image. Reads are one byte per three USB
transactions (`05` load address, `GET_FEATURE`, `01` close); there is no block
read (tested on hardware with different length/index values). The measured cost
on this unit is about **20 ms per byte**:

| Operation | Bytes | Single pass | Two passes |
|---|---|---|---|
| Read config (header + keymap + encoder) | 225 | ~4.8 s | ~9.9 s |
| Dump (whole image, `0x0000..0x0267`) | 616 | ~13 s | ~27 s |
| Write (Apply) | 663 reports | ~1 s + protocol pauses | n/a |

The page therefore has a `⋯ → Fast read (one pass)` option. It halves the read
time but a rare corrupted byte is no longer detected; leave it off for values
you intend to apply. The post-write verification always uses one pass and
re-reads only bytes that disagree with what was sent, which is safe because the
expected value is known.

## The memory-read state and keyboard input

After a batch of memory reads the firmware stays in a read/configuration state
and **stops sending normal keyboard input**. Observed facts:

* closing the HID handle does not help;
* a `USBDEVFS_RESET` (bus reset) does not help: the kernel keeps the same device
  number, so this is not a power cycle;
* a physical replug (VBUS cycle) does help;
* sending the startup handshake **`INIT` (`07 18 11`) + `COMMIT` (`07 18 00`)
  + GET** after the reads restores input immediately, without unplugging.

The WebHID page does exactly that in `readRanges()`: a `finally` block sends the
closing handshake even if a read failed. `k815ctl dump` does not send that
handshake itself; `k815ctl info` can be used after a dump to restore input.

This is why "read the config, then the keyboard does not type until replug" was
seen with the page and with the CLI. It is a firmware quirk, not a driver bug.

## Applying a write

The upload session is 663 feature reports with mandatory pauses measured from
the vendor capture: 200 ms after the first frame and 22-23 ms at a few fixed
points. These pauses are part of the protocol; skipping them can make the
device miss the session. The write is about a second.

A new keymap is applied only after re-enumeration. `tools/serve_webhid.py`
exposes `POST /reset` (`USBDEVFS_RESET`) for that. On this firmware a bus reset
may not re-enumerate the device; if the new keymap does not take effect, one
physical replug is still required. The page says so in the log and status bar.

## Firmware identity and bricking risk

The hardware accepts **8-byte** feature reports (ID + 7 payload). The emulator's
16-byte reports were used once and corrupted the identity area: the unit came
back as `30fa:2360` and needed the chip vendor's IST83025B burning tool
(`KB_Config.exe`) to restore `30fa:2350`, the strings and the `0x00E0` layout.
There is no bootloader or user recovery in the field beyond that tool.

Keep a dump before experimenting and do not hand-patch macro/record bytes
without a backup and the burning tool available.

## Configuration semantics

* **Keys**: 8 keys + encoder press are keymap slots. Media functions on the 8
  keys are possible in the vendor application (`0xC9 + n`) but are not exposed
  by the CLI/page; the tools assign media to encoder rotation only.
* **Encoder rotation**: either a Consumer function or a single ordinary HID
  usage per direction. Longer macros, modifiers or multi-step sequences are not
  exposed for rotation.
* **Encoder press**: keymap slot 49. Slot 41 is a no-op on this firmware.
* **Macros**: the factory Cut/Copy/Paste/Boss/SelectAll shortcuts are understood
  and preserved, but not editable in the tools. Text macros and custom groups are
  still open.
* **Switches**: four bits (`BALTF4`, `BWINLOCK`, `BALLKEYSLOCK`, `KEYSMODE`) in
  one byte.
* **Session**: the whole write is sent at once; there are no incremental writes.

## Browser and OS limits

* WebHID requires Chromium/Chrome on `localhost` or HTTPS; Firefox/Safari do
  not have it.
* On Linux the vendor interface needs a udev rule; otherwise the chooser does
  not show the device (see [../webhid/README.md](../webhid/README.md)).
* `POST /reset` needs `tools/serve_webhid.py` and write access to
  `/dev/bus/usb/...`; a plain static server only supports editing.
* The Chromium end-to-end test skips while a physical K815 is connected: the
  emulator and the real unit share VID/PID, so the chooser cannot reliably pick
  the emulator.
* The uhid emulator uses 16-byte feature reports and a fake config base
  (`0x0001`); it is for protocol tests, not a substitute for the real timing.

## OpenRGB and other third-party software

**Per-key RGB is impossible on this controller.** There is no addressable LED
bus and no per-key colour table; the ASIC renders one global effect. OpenRGB
cannot invent zones the hardware does not have.

A **device-specific OpenRGB driver is technically possible for the global
palette/effect**: detect `30FA:2350`, speak the feature-report protocol, read
the palette, write the LED header, expose the 9 modes, 7 colours, speed,
direction and reaction. But it would be a single-zone device with a global
effect, and it would have to reproduce the whole session/patched frames and the
config base (`0x00E0` or `0x0118`). It would also have to:

* send the closing `INIT` + `COMMIT` after reads or leave the keyboard dead;
* trigger a USB re-enumeration/power cycle to apply the write (OpenRGB cannot
  cut VBUS, so a replug may be needed);
* avoid corrupting the identity area on a bad frame.

For a $10 device this is a lot of fragile work for one global zone. A safer
integration is a small bridge (CLI or `serve_webhid.py`) that applies the
existing preset/LED patch, with an OpenRGB plugin or script talking to it,
rather than a raw HID driver. Treat a raw driver as an experiment, not a
supported feature.

## What is not a limitation

These are expected and correct:

* the encoder press is slot 49, not slot 41;
* the page shows the template until the device config has been read (the
  `Not read from device` chip);
* the live preview is an approximation of the per-key effect;
* Apply asks for confirmation until a successful Dump/Read in the session;
* the post-write verification is a read-back of the written ranges, not a full
  memory dump.
