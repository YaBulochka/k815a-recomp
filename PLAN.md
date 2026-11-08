# MKESPN K815 — work plan

Goal: get a working way to configure the keyboard (8 keys plus an encoder) from
Linux and, as the end goal, from a browser. There is no hardware yet, so the
protocol is recovered from the original Windows application.

## What is already known

See `RE-NOTES.md`. In short: the application talks to the device **only through
feature reports** (`HidD_SetFeature` / `HidD_GetFeature`), no driver is needed.
So on Linux `hidraw` + `HIDIOCSFEATURE`/`HIDIOCGFEATURE` are enough, and in the
browser WebHID is enough.

## The chosen approach: interception through uhid

The order:

```
uhid (our emulator)  ->  /dev/hidrawN  ->  winebus.sys  ->  Gaming Keyboard.exe
```

`/dev/uhid` lets a user-space process register a HID device with an arbitrary
descriptor, VID/PID and behaviour. The key point: the emulator does not copy the
firmware, it only **logs** the traffic and answers reads from an artificial
memory. So the accuracy of the device model does not matter: we need the exact
bytes the application SENDS.

What this gives without hardware:

* the exact PID and the presence of a second interface (0xFF00/0xFF01);
* the real length of a feature report;
* the contents of `0xC0..0xCF` and of the whole `0xD0..` region;
* the encoding of LED effects and the factory macro shortcuts.

How it works: under Wine the application opens the device and reads the config,
the user changes a setting and presses Apply, and the emulator records the whole
sequence of `HidD_SetFeature` calls into a log. The difference between "before"
and "after" gives a ready map: what is written and at which address.

## Stages

### Stage 1. Emulator and interception (done)

- [x] `tools/k815_emulator.py` — a fake uhid device with a log.
- [x] Run `Gaming Keyboard.exe` under Wine with the emulator.
- [x] Capture the startup traffic (initialisation plus reading the config).
- [x] Capture the write traffic: Apply with the defaults and with one change (key 1 disabled).

### Stage 2. Memory map

- [x] Lay out `0x10`, `0x20..0xBF`, `0xD0..` from the actual traffic.
- [x] Work out the session structure: two phases separated by a verify read of `0x0001`.
- [x] Reproduce the traffic exactly: `emit(program_from_capture())` == the capture byte for byte.
- [x] Decode the LED: palette `0x02..0x0F`, effects in `0x10/0x19/0x1a/0x1b`
      (see `PROTOCOL.md`). `0xE9..0xF4` are `UFN_F1..F12`, not LED.
- [x] The structure of the `0xF9..` region is worked out: `0xF9..0x166` is an index of
      22 five-byte entries `07 <kind> <addr0..2>`, `0x167` is a zero separator, and the
      payload after it holds 8-/16-byte records. Entry 20 belongs to `USCROLL0`, entry 21
      to `USCROLL1`; `kind = 0x04` marks an active record (Consumer function, macro
      shortcut, media-mode encoder) and `0x03` an internal one, the other 20 entries are
      inert stubs with `kind = 0x00`. `addr0..2` is
      the phase-B address of the record of that entry, so the byte that looked constant
      (`0x01`) is the middle address byte: with the first number of `USCROLL0` equal to `4`
      the second record moves to `0x170` and its entry follows. The payload is not of a
      fixed length — a light-control/system/special assignment appends 8 zero bytes
      per key, and a macro assignment appends a 16-byte shortcut record, which moves
      every address (the end of phase B was seen at `0x187`..`0x1C7`).
      `UFN_F*`, the function numbers of `USCROLL*` and `MACRO_MAP*` leave the index
      alone by themselves; what changes it is the key assignment: a macro key
      activates its slot and inserts a record.
- [~] Why the index has exactly 22 entries: the measured part is 20 non-encoder
      slots plus the two encoder directions (20/21), and the K815 uses slots
      10..17 for keys 1..8. That the other ten slots belong to a larger model is
      the most likely explanation but is not verified on another device.
- [~] Still open: what the constant `0x07` at offset 0 of an entry means. It is a
      tag/version present in every entry, including the inert ones.

### Macros (done for the factory group)

- [x] Make the Macro category usable: `macro/macroGrp.ini` with `macGrpNum=1` and
      `0=ShortcutMacro`, plus `ShortcutMacro.dat` in the same `macro/` directory.
- [x] Key assignment: `0xAF` for key 1 and `0xB8..0xBE` for keys 2..8; the value is
      per key slot, not per macro. `MACROGRP_MAP`/`MACRO_MAP` slots 10..17 store the
      group and macro index.
- [x] The function table: key 1 activates entry 10, key 2 entry 11, … key 8 entry 17;
      each macro key adds a 16-byte record and shifts the encoder records.
- [x] Record content for the five factory macros: the usage bytes match
      Cut/Copy/Paste/Boss/SelectAll = Ctrl+X / Ctrl+C / Ctrl+V / Win+D / Ctrl+A.
      The modifier mapping is inferred; the record layout is in `PROTOCOL.md`.
- [x] Regression fixture `tests/fixtures/macro5.jsonl.gz` (five keys) and checks in
      `tools/selftest_cli.py`; `k815ctl show` prints the function table.
- [x] Hardware verification (November 2026): a 14-event A..G record at `0x248`
      with entry 10 `07 0e 48 02 00` and keymap marker `0xAF` made key 1 type
      `abcdefg`. The record format is a 2-byte header plus 2-byte down/up
      events; modifiers are separate events.
- [x] Real payload limit: the function window is **32 bytes** (`0x248..0x267`
      on `0x00E0`, `0x280..0x29F` on `0x0118`) and is shared with the encoder
      records (16 bytes each). Two encoders leave no macro space; one encoder
      leaves a 16-byte record (up to 7 events); no encoders leave a 32-byte
      record (up to 14 events / 7 key presses). The emulator's 127-event
      result is staging-only.
- [~] Still open: macro editing in the CLI/WebHID, text macros beyond a key
      sequence, custom macro groups. The vendor application on `0x00E0` does
      not assign the macro (it writes `Full-key Locked`); the direct protocol
      path works. See `PROTOCOL.md`, `docs/KNOWN-LIMITATIONS.md`,
      `tools/macro_hw_test.py` and `tools/selftest_macro.py`.

### Stage 3. Our own tool

- [x] `tools/k815_proto.py` — report parsing/building, block patching, LED, switches,
      encoder, preset model.
- [x] `tools/k815ctl.py` — `list`/`info`/`dump`/`replay`/`keymap`/`led`/
      `switches`/`encoder`/`preset`/`apply`/`show`/`backup`/`restore`/`selftest`.
- [x] Verification on the emulator: `selftest` accepts 663/663 reports byte for byte.
- [x] `apply` from a human-readable preset: keys, lighting, switches, encoder media
      functions. Macro assignment is understood but not in the preset format yet.
- [x] A WebHID version for cross-platform use: `webhid/` (keys plus lighting).

### Stage 3b. CLI parity and a safety net before hardware (done)

- [x] `switches` and `encoder` in Python: the same patches as in `webhid/k815.js`;
      cross-checked at the "JS versus Python" level in `tools/selftest_webhid.py`.
- [x] `preset` / `apply` — one JSON for both the CLI and the page (version 3).
- [x] `backup` / `restore` — an artifact with the exact frame stream and a `sha256`;
      a tampered or truncated file is rejected.
- [x] `tools/selftest_cli.py` — offline checks with no hardware and no browser
      (55/55): capture round-trip, switches, encoder, preset, a WebHID-style
      export applying like native edits, the generated effect catalogue,
      backup/restore, a dump (page JSON, raw binary, or a
      `backup --from-device` image) decoding into the same preset, and the
      22-entry function table with the five macro records.
- [x] `dump --json` / `dump --preset` and `preset --from-dump`: a device dump
      (page JSON, raw binary, or `backup --from-device` memory) decodes into the
      same preset format; bad sha256/gaps are rejected; the keymap is taken from
      the phase-B copy at `0x49`.
- [x] `tools/udev/99-mkespn-k815.rules` + `tools/install_udev.sh` — access to
      `/dev/hidraw*` of the `0xFF01` interface for the session user.
- [x] A run through the emulator: `apply` and `restore` produced frame for frame what
      the artifacts hold.
- [ ] Check the udev rule on the real keyboard.

### WebHID (done)

- [x] `tools/build_webhid.py` → `webhid/k815_session.js` (session template plus patch points).
- [x] `webhid/index.html`, `webhid/app.js`, `webhid/k815.js` — the UI and the driver.
- [x] `tools/selftest_webhid.py` — JS versus Python byte for byte (PASS, LED included).
- [x] `tools/serve_webhid.sh` — a local server.
- [x] The palette (7 colours) and the effect parameters in WebHID; the effect
      names come from `tools/led_modes.json` (embedded in the generated
      `webhid/k815_session.js` as `LED_EFFECTS`), so the drop-down shows
      "Mode 7 · Single Water" instead of "Mode 7".
- [x] Export/import of the whole configuration as JSON on the page; an export
      from the page applies through `python3 tools/k815ctl.py apply` with the
      same bytes as native CLI edits (checked in `tools/selftest_cli.py`).
- [x] The switches `BALTF4`/`BWINLOCK`/`BALLKEYSLOCK`/`KEYSMODE` (phase-B byte `0x0001`)
      — the "Switches" card, the `patchSwitches` patch, cross-checked in the selftest.
- [x] Encoder media functions (`USCROLL0/1`, numbers `0…16`) — the "Encoder" card,
      the `patchEncoder` patch, cross-checked in the selftest.
- [x] `tools/e2e_webhid.py` — the end-to-end test: Chromium WebHID → hidraw → uhid.
- [x] `tools/run_all.sh` — one command for the whole suite: syntax checks plus
      both offline selftests, and with `--full` the end-to-end test as well
      (6/6 stages pass on this machine).
- [ ] Macro editing in WebHID/CLI. The protocol is recovered and verified on
      hardware (2-byte events, up to 14 events in the 32-byte payload, shared
      with the encoders), but the tools keep the template's macro area
      unchanged. Text macros beyond a key sequence and custom groups are open.

### WebHID end-to-end test (done)

A bug was found and fixed that no earlier test caught: `app.js` called
`sendFeatureReport(frame)` with a single argument, while WebHID requires
`sendFeatureReport(reportId, data)`. Nothing was written at all, the error was
swallowed in `catch` and shown only as a toast.

- [x] `tools/e2e_webhid.py` — Xvfb + emulator + a real Chromium; the whole scenario
      through the interface: key 3 → F13, the Alt+F4 switch off, encoders A and B
      assigned Mute and AC Home; 663 frames compared with the `k815_proto.py`
      computation (9 bytes differ from the template).
- [x] Walking the device chooser through xdotool (Chromium always shows it, and the
      permission does not survive restarts).
- [x] Both writing and reading ("Read") are checked; with the extended scenario, three
      runs in a row PASS.

### Automatic sweep of config.ini parameters (done)

A harness that changes one field in `config.ini`, runs the application under Wine
itself (it presses Apply through `tools/app_capture.sh`) and maps the address to
the value. Closed: `0x0010`/`0x0021` from `USCROLL*`, the "silent" fields
(`HR_BRIGHT`, `HR_COLOR_SEL`, `USERCOL`, `HR_LED_MODE_EN*`, `HR_REACT_LED`,
`HR_LED_NUM`, `HG/HB_REACT_LED`), the switches
`BALLKEYSLOCK`/`BALTF4`/`BWINLOCK`/`KEYSMODE`/`BMODELED`, the profiles
(`ConfgCnt`/`CurConfig`/`ConfigName1..5`) and the LED as a whole (modes, palette,
effect parameters, section selection). The `0xF9..` record table is characterized as
well (Stage 2).

- [x] `tools/sweep_config.py` — edit fields, run, diff the addresses.
- [x] Runs under `Xvfb :99` rather than on the desktop; determinism confirmed
      (two identical runs matched byte for byte).
- [x] Checked against something known: `UFN_F1=171` → phase B `0xE9`.
- [x] A batch of 34 experiments: switches, LED "silent" fields, profiles, `USCROLL*`.
- [x] A batch of 42 experiments: the second number of `USCROLL*` (numbers 11..93) — the
      media function table is closed, `tools/media_table.py`.
- [x] The HID Usage Tables (Consumer) table saved to
      `tools/hid_consumer_usages.json` (456 records).
- [x] Batches of 33 + 23 experiments: the first number of `USCROLL0` (mode) and of
      `USCROLL1` (no effect).
- [x] The table of `USCROLL` functions (`[UniqueName]` → Consumer-page usage).
- [x] The second number of `USCROLL*` fully explained: the numbers `0…16` are 17
      Consumer-page functions, and from `17` on they are internal functions of the
      program (not usages). Starting at `12` the `[UniqueName]` labels are shifted by one.
- [x] Encoder media functions brought into WebHID: the "Encoder" card,
      `patchEncoder`, cross-checked in the selftest.
- [x] The first number of `USCROLL*`: for `USCROLL0` it is the mode switch (`11` is
      media mode, byte `0x44` in phase A `0x0010` and phase B `0x0021`; any other value
      breaks the encoder record), and for `USCROLL1` it has no effect on the stream at all.
- [x] Interface clicks in `app_capture.sh`: the fifth argument takes `X,Y` clicks before
      Apply, with the prefixes `+` (a key of the device picture, retried until the dialog
      appears), `@` (relative to the assignment dialog), `%` (relative to the drop-down
      pop-up), `!KEYSYM` (a key press into the capture box) and `#` (absolute screen
      coordinates); `K815_KEEP=<dir>` keeps the configuration files the application wrote,
      `K815_SHOT` takes a screenshot. The coordinate map is in `RE-NOTES.md`, section 5.
- [x] The key-assignment dialog of the Button tab: categories, their lists, the key hit
      points, Apply, persistence in `config.ini` `KN_*`. The value space of the keymap is
      measured (keyboard usages, Consumer `0xC9 + n`, light control `0xE8..0xF0`, the first
      items of the special-function/system/macro lists, "Default", "Disable"); the click
      map and the results are in `RE-NOTES.md`, section 5 and `PROTOCOL.md`, section 4.
- [x] The keymap source: `KBconfig.ini` cell `A_B` → index `8*B + A` in both phases; the
      application's own per-key indices are `87 88 89 71 72 73 54 55` for keys 1..8
      (`KN_*` → phase A, `KU_*` → phase B) and a `KBconfig.ini` cell that differs from the
      stored assignment wins over it. Closed; the merge rule is in `PROTOCOL.md`, section 4.
- [x] LED: sweeping the nine entries of the list — switching the mode changes exactly one
      byte `0x1a` (the low nibble) in the stream.
- [x] LED: only `[ConfigN_LED_COLOR]` goes into the upload; `_LED1`/`_LED3`/`_LED7`
      and `Config0` do not (verified with a colour marker in the four sections).
- [x] LED: the "silent" fields closed for good — `HR_BRIGHT`, `HR_COLOR_SEL`,
      `USERCOL`, `HR_REACT_LED`, `HR_LED_NUM`, `HG/HB_REACT_LED`,
      `HR_LED_MODE_EN0..8`, `LEDTYPE`, `RGB` produce not a single stream change
      in any of the nine modes.
- [x] LED: the effect table from the skin `KbIndexText.ini` (mode number = index − 30),
      verified against the stream for `0..12`; bits 4..6 of `0x1a` = (the first enabled
      colour + 1) when `SINGLECOL=1`.
- [x] Profiles: `CurConfig = 1..5` swept against a fresh reference. `2` takes `[Config2]`
      (its own keymap, `USCROLL*` and the kind byte of both encoder entries, `0x04` → `0x03`),
      `3`/`4` give an all-zero keymap with `UFN = 0xFF` and no encoder records, `5` adds its
      own `UFN_F*` values. `ConfgCnt` does nothing. See `PROTOCOL.md`, section 4.

Running:

```sh
# a reference from an untouched base (a single run)
python3 tools/sweep_config.py --reference --base /tmp/opencode/config.base2.ini \
    --log /tmp/opencode/sweep_ref.jsonl --display :99

# a batch of experiments
python3 tools/sweep_config.py --batch /tmp/opencode/batch1.json \
    --ref /tmp/opencode/sweep_ref.jsonl --outdir /tmp/opencode/sweep1 --display :99
```

`/tmp/opencode` lives until a reboot: after a reboot `config.base2.ini` is taken
from `extracted/app/config/config.ini` (hash `8878469e…`), and `sweep_ref.jsonl`
is rebuilt by the first run above. `captures/baseline_apply.jsonl` is **not** a
reference: it differs from a fresh run by the bytes `A 0x0028` and `B 0x0003`.

### Reassigning keys and F13–F24

- [x] Parse `KBconfig.ini` → keymap, upload through `k815ctl keymap`.
- [x] Verified: KGlobalAccel accepts F13–F24, and a real press triggers them.
- [x] Found the reason some F-keys do not arrive: `inet(evdev)`.
- [x] `tools/xkb/` + `tools/install_fkeys_xkb.sh` — a user-level layout override.
- [x] `KEYS.md` — the full description.

### Stage 4. Verification on hardware (done)

- [x] `lsusb` → VID/PID `30fa:2350`, two USB interfaces; the vendor collections
      `0xFF00`/`0xFF01` share `/dev/hidraw12` in this unit.
- [x] The real feature report is 8 bytes (ID + 7 payload), not 16: the
      emulator descriptor was 16 bytes, so the first capture carries extra
      padding and a `flag` byte that never reaches hardware.
- [x] Memory read: `07 18 05` → GET (byte 1 is the data) → `07 18 01`
      (close). Without the close the next read returns `0xb4`; the first two
      answers after `INIT`/`COMMIT` are stale.
- [x] The factory config base was `0x0118`; the chip-burning-tool layout is
      `0x00E0`. The app reads the base from the info answer and writes it to
      `0xD2..0xD3`; phase-B header `+0x1C/+0x1E` hold the keymap and table
      bases.
- [x] Full upload through `k815ctl apply`: key 1 → `F13` (`0x68`) appears in
      the active bank (0x168 on the `0x0118` layout, 0x128 on `0x00E0`) and
      survives a second apply / read-back.
- [x] `tests/fixtures/hw_apply.jsonl.gz` (`0x0118`) and
      `tests/fixtures/hw_apply_e0.jsonl.gz` (`0x00E0`) — real hardware
      sessions captured from the vendor application through Wine; `k815ctl`
      and WebHID select the one matching the reported base.
- [x] Recovery after a corrupt write: the first upload used the emulator-sized
      16-byte reports and damaged the identity area; the unit fell back to
      `30fa:2360 USB Keyboard` (standard mode). The chip vendor's IST83025B
      burning tool (`KB_Config.exe`, `20240819112054.zip`) restored PID
      `0x2350`, the `MKESPN K815`/`MKESPN` product info, game mode and the
      `0x00E0` layout.
- [x] WebHID on hardware: the page connects to `USB Keyboard` (0xFF01), writes,
      verifies and closes the HID handle. Automatic USB re-enumeration is
      available through `tools/serve_webhid.py` (`POST /reset`, USBDEVFS_RESET)
      after `sudo sh tools/install_udev.sh`; without it one replug is needed.
- [x] Physical key: `F13` reaches the kernel; the default xkb maps it to
      `XF86Tools` (KDE opens System Settings), and `tools/install_fkeys_xkb.sh`
      restores real F13–F24. A key assigned to `A` (`0x04`) works from the page.
- [x] Encoder rotation: keymap slots 41/49 are not used by rotation (49 is
      the press). A single-key macro record makes rotation send ordinary keys:
      left `0x56`, right `0x57`, press `F21`, with no media layer. The working
      preset is `presets/k815-f13-20-encoder.json`; `encoder --macro` writes it.
- [x] Replug/persistence: the configuration survives a power cycle.
- [x] WebHID e2e: `selftest_webhid.py` uses the e0 session; `e2e_webhid.py`
      uses the e0 session and skips with a clear message when a physical K815
      is connected (VID/PID collision with the emulator in the chooser).
- [x] Hardware limits (November 2026): the controller is an **IST83025B/K201**
      (USB 1.1 low-speed, 100 Hz, 7-colour global LED driver, no per-key RGB).
      Memory reads leave the firmware in a read state; the page sends the
      closing `INIT`+`COMMIT` handshake. Config I/O is ~20 ms/byte. See
      `docs/HARDWARE.md` and `docs/KNOWN-LIMITATIONS.md`.

## Running the emulator and the application

```sh
# terminal 1: the emulator
sudo -v   # not needed: /dev/uhid is world-writable
python3 tools/k815_emulator.py --log captures/session1.jsonl

# terminal 2: the application
cd extracted/app
WINEPREFIX=~/.wine-k815 wine "Gaming Keyboard.exe"
```

The result: `captures/session1.jsonl` plus the human-readable `captures/session1.log`.
