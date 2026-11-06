# MKESPN K815 — configuration over WebHID

A browser tool for reassigning the 8 keys and the encoder. It speaks the same
feature-report protocol with Report ID 7 as the vendor application. Frames are
16 bytes internally; the page sends the payload length the device declares
(7 bytes on real hardware, 15 on the uhid emulator, see `../PROTOCOL.md`).

## Running

WebHID requires a secure context, so open the page over `localhost` rather than
as a file:

```sh
python3 tools/serve_webhid.py     # http://127.0.0.1:8765/webhid/
```

The local server also implements `POST /reset`, which the page uses to
re-enumerate the keyboard after a write: this firmware only applies a new
keymap after a real USB reset. A plain static server works for editing, but
then the page asks for one physical replug.

Then in Chromium/Chrome: `http://127.0.0.1:8765/webhid/`.

The interface is a single page built from plain HTML/CSS/JS, with no frameworks
and no build step.

### Device permissions on Linux

The keyboard is configured through the vendor HID interface (usage page
`0xFF01`), that is through `/dev/hidrawN`. The nodes belong to `root:input`, and
the ACL that systemd-logind grants to a local session does not always cover the
vendor collection: the browser then does not show the device in the chooser and
the CLI prints `no device with VID ...`. A ready-made rule fixes this without
running anything as root:

```sh
sudo sh tools/install_udev.sh          # installs tools/udev/99-mkespn-k815.rules
# unplug and replug the keyboard
python3 tools/k815ctl.py list          # should show two interfaces
```

The rule file itself lists alternative PIDs (the real pair on hardware is
unconfirmed) and a variant without systemd-logind. Installation needs root, so
it is done by hand.

## Using the tool

1. **Connect device** — the button in the header; the browser shows the chooser.
   The page filters on all three PIDs of the family (`0x2350`, `0x2130`,
   `0x2330`, vendor `0x30FA`) and then takes the collection with usage page
   `0xFF01` (write), falling back to `0xFF00` (read). If the chooser returns
   something else, it is not used silently: the log lists every collection and
   its feature-report ids. On load the page calls `navigator.hid.getDevices()`
   and connects to an already permitted device.
2. A key is selected by clicking it on the device schematic. The selected key is
   outlined, and the inspector on the right shows its name, position and current
   code.
3. Keys are assigned HID usage codes from the list or by hand
   (for example `0x68` = F13, see `../KEYS.md`).
4. **Lighting** — 7-colour palette and the global effect (mode, speed,
   direction, reaction, symmetry, single colour, active-colour mask). The live
   preview is the same SVG keyboard as the Layout card; it paints each key with
   the colour the global effect gives it. There is **no per-key colour storage**
   on this controller: the palette and the effect are global, see
   [`../docs/KNOWN-LIMITATIONS.md`](../docs/KNOWN-LIMITATIONS.md).
   Only the effects the vendor UI exposes are listed (`tools/led_modes.json`,
   `in_ui_list`); a mode already stored in the device but not in that list is
   shown as `(current)` so a read does not lose it. The colour editor has
   0-255 sliders in steps of 17 (the firmware has 16 real levels), a native
   colour picker and 8 quick presets. The header shows 0-255; the raw
   preset/wire value is still `15 - value`, and export keeps it raw
   (see `../PROTOCOL.md`). The inspector filters codes with the
   All/Letters/Digits/Numpad/F-row chips and the search box; the Value field
   accepts a raw usage.
5. **Switches** — the four `[Config1]` fields that the device actually accepts:
   `BALTF4` (block Alt+F4), `BWINLOCK` (block Win), `BALLKEYSLOCK` (block all
   keys) and `KEYSMODE`. All four live in a single phase-B byte (address
   `0x0001`), and the current value is shown next to them.
6. **Encoder** — rotation has two modes: **Media functions** (17 Consumer-page
   functions from Play/Pause to AC Home) and **Keyboard keys** (one ordinary HID
   usage per direction, written as a single-key macro record). The mode switch
   rewrites the full record, so moving between media and keyboard keys works in
   both directions. A separate selector assigns the **encoder press** (keymap
   slot 49); slot 41 is not used by rotation. The current left/right direction
   mapping is shown in the hint. Keyboard-key rotation is a single-key macro
   with key-down/key-up back-to-back; at the 100 Hz endpoint rate a fast spin
   can lose detents, see
   [`../docs/KNOWN-LIMITATIONS.md`](../docs/KNOWN-LIMITATIONS.md).
7. **Check** — shows how many frames will be sent and how many bytes changed
   (without touching the device).
8. **Apply** — sends the whole session (663 reports) to the device, honouring the
   pauses measured from the capture (see below). One write runs at a time; Apply,
   Read, Dump, Check and import are blocked until it finishes. Afterwards the page
   reads back the switches/LED header, the phase-B keymap and the encoder records
   and reports "verified N bytes" or the list of differing addresses. Until the
   first successful **Dump** in a page session, Apply asks for a confirmation.
9. **Dump** — a read-only copy of the device memory (`0x0000..0x0267` on the `0x00E0` layout, the same
   `07 18 05 …` command and one-byte GET answer as `tools/k815ctl.py dump`). The
   bytes and their SHA-256 are saved to `k815-memory-<time>.json` (keys: version,
   VID/PID, time, address → bytes, sha256), and the page shows how many of the
   bytes that Apply would write differ from the dump, grouped by keymap, LED,
   switches, encoder and other.
   The saved JSON is compatible with the CLI: it carries `configBase` and
   `recordBase`, and `python3 tools/k815ctl.py preset --from-dump
   k815-memory-*.json --out current.json` decodes the active bank into the same
   preset format that **Export/Import JSON** and `k815ctl apply` use. For a raw
   binary or an older dump without that metadata, pass `--config-base 0x00E0
   --record-base 0x0248` (burned layout) or use the stored-bank path.
10. **KBconfig.ini…** — load the current matrix from the vendor software.
11. **Export/Import JSON** — save the layout, the lighting, the switches and the
    encoder mode (media numbers or keyboard usages). Export is a copy of the
    *page*, not of the device memory; use Dump for the latter. The format
    matches `python3 tools/k815ctl.py preset`/`apply` (version 3), so a preset
    moves between the browser and the console unchanged.

The edit counter in the header shows how many keys and LED fields differ from the
last written state (or from the loaded template before the first write). The
"Extra delay per frame" box adds an optional pause after every report during
Apply; the template already carries the pauses that were measured in
the hardware capture (only gaps over 20 ms) plus 1 ms before every memory-read
answer.

## Files

| file | purpose |
|------|-----------|
| `index.html` | page markup and the inline SVG device schematic |
| `style.css` | theme: palette, layout, responsive down to 900px |
| `app.js` | logic: key selection, code editor, lighting, switches, encoder, Dump, sending, verification |
| `k815.js` | driver: constants, key table, usage codes, encoder media functions, device/interface selection, ini parser |
| `k815_session.js` | generated hardware session (663 reports, base/record addresses, patch points and measured frame delays) |

Below 900px the columns stack: the tabs stay on top, the inspector moves under the
schematic, and the Lighting cards become a single column.

## How it works

`k815_session.js` is generated by `tools/build_webhid.py` from the hardware
session (`tests/fixtures/hw_apply_e0.jsonl.gz` by default; pass `--capture` to
use another one). It holds:

* `blob` — base64 of `663 × 16` bytes (the exact upload session);
* `base` / `recordBase` — the config base (`0x00E0` on the burned layout,
  `0x0001` on the emulator) and the first function-record byte;
* `keymapA` / `keymapB` — 160 pairs of `[frame index, keymap offset]` for both
  phases (phase A writes at `0x20`, phase B at `base+0x48`, i.e. `0x128`);
* `ledTargets` — the `address → frame index` map for the phase-B header
  (`0x01..0x21`), which receives the palette, the effect parameters and the
  switch byte;
* `encTargets` — the same for the eight encoder code bytes in phase B
  (`0x016E/1`, `0x0174/5`, `0x017E/F`, `0x0184/5`);
* `encRecordTargets` — the full 16 bytes of both rotation records
  (`0x168..0x187`), so the page can rewrite a media record as a macro and back;
* `entryTargets` — the 22 five-byte function-table entries (`0xF9..0x166`);
* `modeTargets` — the encoder mode byte in phase A (`0x10`) and phase B
  (`0x21`);
* `defaultKeymap` — 160 bytes from `KBconfig.ini`;
* `defaultLed` — the current palette and effect parameters;
* `defaultSwitches` — the switch byte `0x0001` from the capture;
* `defaultEncoder` — the encoder media functions from the capture (usage, not
  number);
* `defaultEncoderKeys` — the single-key macro usage per direction, or `null`;
* `delays` — the measured pauses between frames (`[[index, ms], ...]`, only gaps
  over 20 ms) and `readPauseMs` (1 ms before a memory-read answer).

On "Apply" the page does not rebuild the protocol from scratch; it takes the
verified template and replaces only the data bytes in the record frames for the
keymap, LED, switches and encoder
(`patchKeymap` + `patchLed` + `patchSwitches` + `patchEncoderRotation`).
`patchEncoderRotation` writes the complete 16-byte rotation record, its
function-table entry and the mode byte, so media↔keyboard switching is a plain
Apply. This is the same approach as in `tools/k815ctl.py`, so the byte stream
matches the vendor
application.

## Checks

Everything can be run in one go, from the repository root:

```sh
sh tools/run_all.sh          # syntax checks + the two offline selftests
sh tools/run_all.sh --full   # the above plus the Chromium end-to-end test
```

A stage whose tools are missing (for example no `node`) is reported as skipped;
the exit status is non-zero only when a stage fails. The suite also runs on a
fresh clone with no `captures/` or `extracted/`: it falls back to the committed
fixtures in `tests/fixtures/` (see `tests/fixtures/README.md`).

The frame assembly logic is compared with `tools/k815_proto.py` without a browser
(only `node` is needed):

```sh
python3 tools/selftest_webhid.py
# frames=663 patched=320 ledPatched=21 swPatched=1 encPatched=6 stream=OK baseline=OK delays=[[1, 200], [3, 22], [63, 22], [430, 22], [590, 23]] readPause=1 ini=OK pySw=1 pyEnc=6 pyModel=OK
# WEBHID SELFTEST PASS
```

The script builds the reference with the same Python functions the CLI uses
(`apply_switches`, `patch_encoder`, `apply_led`) and additionally compares the
counters: how many bytes JS changed and how many Python changed. It also checks
that the generated `SESSION.blob` still equals the baseline stream from
`k815_proto.emit()` byte for byte, and that the embedded `delays`/`readPauseMs`
match the timings measured in the capture.

The model and the CLI are checked with no browser and no hardware at all:

```sh
python3 tools/selftest_cli.py
# CLI SELFTEST PASS (63/63)
```

Among other things it applies a JSON export in the shape this page writes (the
"Export" button) through the CLI code path and compares the frames with the
equivalent native edits, so "export from the page, then
`python3 tools/k815ctl.py apply config.json`" is pinned by a test.

The end-to-end check goes through a real browser and a real transport:

```sh
python3 tools/e2e_webhid.py
# E2E WEBHID PASS
```

The script starts Xvfb and the uhid emulator (`tools/k815_emulator.py`), opens
this page in a real Chromium, connects to the emulator over WebHID and repeats the
usual configuration scenario: assigns F13 to key 3, turns off the Alt+F4 switch,
assigns Mute and AC Home to encoders A and B, then presses "Apply". The emulator
log is compared byte for byte with what `tools/k815_proto.py` computes for the
same layout; exactly 9 frames differ from the template. This exercises the whole
chain: clicks in the interface, frame assembly in JS, Chromium WebHID, the kernel,
uhid. The "Read" button is checked as well, and after "Apply" the test waits for
the read-back verification ("verified 225 bytes after write").

### WebHID convention

A frame stores the Report ID (0x07) in its first byte, as `HidD_SetFeature`
expects, but WebHID takes the report id and the payload as two separate arguments:

```js
// the payload length comes from the device descriptor
const n = featurePayloadBytes(device, frame[0]);
await device.sendFeatureReport(frame[0], frame.subarray(1, 1 + n)); // 7 + 7 on hardware
const r = await device.receiveFeatureReport(7);
```

Passing the whole frame at once (`sendFeatureReport(frame)`) raises
`TypeError: 2 arguments required`, so nothing is written at all. On Linux
`receiveFeatureReport` returns the whole buffer, including the id byte.

## Limitations

* You can edit the keys, the lighting, the switches and the encoder media
  functions. Macro editing is not exposed: the factory `ShortcutMacro`
  protocol is recovered (`../PROTOCOL.md`), but the page keeps the template's
  macro area unchanged. The inspector has no Media/Macro tabs: media functions
  on the 8 keys are not exposed either (the vendor app writes `0xC9 + n`), so
  media functions are assigned to encoder rotation only.
* Rotation keyboard keys are limited to a **single key press/release** per
  direction. Longer macros, modifiers or multi-step sequences are not exposed;
  use the vendor application for those.
* The whole session is sent at once; there are no incremental writes.
* Dump reads `0x0000..SESSION.recordBase+0x20` (`0x0267` on the `0x00E0`
  layout). A real device may stop answering outside its memory; the read is
  read-only, so a failure cannot change the configuration. The preset decoder
  in `k815ctl` targets the staging layout; the raw dump is still a full backup.
* After a memory read the page sends the closing `INIT`/`COMMIT` handshake:
  without it this firmware stops sending normal keyboard input until a power
  cycle, and a USB bus reset is not enough. After a verified write the page
  still asks the local server for `USBDEVFS_RESET` to make the new keymap
  active; without the reset service, replug once.
* `⋯ → Fast read (one pass)` reads each byte once instead of twice, about
  twice as fast. A rare corrupted read is then not detected, so leave it off
  for values you intend to apply. The post-write verification always uses one
  pass and re-reads only bytes that disagree with what was sent.
* The browser end-to-end test skips when a physical K815 is connected: the
  emulator and real unit share VID/PID, so Chromium's chooser cannot reliably
  select the emulator. Unplug the keyboard to run the emulator test.
