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
* the encoding of macros and LED effects.

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
      to `USCROLL1`; their `kind` is `0x04` for a Consumer function and `0x03` for an
      internal one, the other 20 entries are inert stubs with `kind = 0x00`. `addr0..2` is
      the phase-B address of the record of that entry, so the byte that looked constant
      (`0x01`) is the middle address byte: with the first number of `USCROLL0` equal to `4`
      the second record moves to `0x170` and its entry follows. The payload is not of a
      fixed length — a light-control/system/special/macro assignment appends 8 zero bytes
      per key, which moves every address (the end of phase B was seen at `0x187`..`0x1A7`).
      `UFN_F*`, the function numbers of `USCROLL*`, `MACRO_MAP*` and `ShortcutMacro.dat`
      leave the index alone; the string macros are played by the application itself.
- [~] Still to work out: why the index has exactly 22 entries, and what the `0x07` at
      offset 0 of an entry means.

### Stage 3. Our own tool

- [x] `tools/k815_proto.py` — report parsing/building, block patching, LED, switches,
      encoder, preset model.
- [x] `tools/k815ctl.py` — `list`/`info`/`dump`/`replay`/`keymap`/`led`/
      `switches`/`encoder`/`preset`/`apply`/`show`/`backup`/`restore`/`selftest`.
- [x] Verification on the emulator: `selftest` accepts 663/663 reports byte for byte.
- [x] `apply` from a human-readable preset: keys, lighting, switches, encoder media
      functions. Macros are not uploaded and do not enter the preset.
- [x] A WebHID version for cross-platform use: `webhid/` (keys plus lighting).

### Stage 3b. CLI parity and a safety net before hardware (done)

- [x] `switches` and `encoder` in Python: the same patches as in `webhid/k815.js`;
      cross-checked at the "JS versus Python" level in `tools/selftest_webhid.py`.
- [x] `preset` / `apply` — one JSON for both the CLI and the page (version 3).
- [x] `backup` / `restore` — an artifact with the exact frame stream and a `sha256`;
      a tampered or truncated file is rejected.
- [x] `tools/selftest_cli.py` — offline checks with no hardware and no browser
      (35/35): capture round-trip, switches, encoder, preset, a WebHID-style
      export applying like native edits, the generated effect catalogue,
      backup/restore.
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
- [ ] Macros in WebHID (the device does not receive them through this session).

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

### Stage 4. Verification on hardware

- [ ] `lsusb` → confirm the VID/PID.
- [ ] Compare a dump from the emulator with a dump from the real device.
- [ ] Write a config and make sure it survives without the software.

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
