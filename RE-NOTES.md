# MKESPN K815 — reverse engineering notes

Status: draft research without a physical device.

## 1. What is in the project

`MKESPN K815.EN[20250610].exe` (7 724 214 bytes, sha256
`03167771cb9f96f5d71a981e4ce74d0ee8ae6ec345a4a7ef9db394d5b91f4c0c`).

This is **not the application itself but the Inno Setup 5.5.7 (unicode) installer**:

* PE32 (i386), 8 sections with a Borland layout, `FastMM Borland Edition (c) 2004 - 2008`,
  a typical Inno Setup loader (written in Delphi).
* VERSION resource: `Comments = This installation was built with Inno Setup.`,
  `CompanyName = MKESPN K815`, manifest `JR.Inno.Setup`, `requireAdministrator`.
* The image sections end at offset `0x53A00` (342 528), the remaining ≈7.3 MB is
  overlay = the LZMA-compressed installer payload (entropy 7.997 bits/byte over the whole blob).

Unpacking: `innoextract -d extracted "MKESPN K815.EN[20250610].exe"` → 227 files, 14 MB.

## 2. What is inside the installer

```
app/Gaming Keyboard.exe          3.9 MiB  ← the real application
app/config/config.ini
app/config/KBconfig.ini
app/config/reset_config.ini
app/config/ShortcutMacro.dat
app/skins/                       75 skin files (jpg/bmp/mask)
app/0/skins/0_INI_CN/             device variant 0, Chinese locale
app/1/skins/1_INI_EN/             device variant 1, English locale
```

No `.inf`/`.sys`/`.dll` at all: the driver is not installed, access is pure HID.

## 3. The real application `app/Gaming Keyboard.exe`

* PE32, MSVC/MFC (`?AVCCode_KeyBoardApp@@`, `CMFCToolBarsKeyboardPropertyPage`),
  pdb path `\Release\Gaming Keyboard.pdb`, timestamp 2025-04-24.
  The internal name is `Code_KeyBoard.exe`, version "Copyright (C) 2018", `CompanyName: TODO`
  → this is a white-label engine, rebranded for dozens of models.
* Imports that define the protocol:
  * `HID.DLL`: `HidD_GetAttributes`, `HidD_GetHidGuid`, `HidD_GetPreparsedData`,
    `HidD_FreePreparsedData`, `HidD_GetFeature`, `HidD_SetFeature`,
    `HidD_GetProductString`, `HidP_GetCaps`
  * `SETUPAPI.dll`: `SetupDiGetClassDevsW`, `SetupDiEnumDeviceInterfaces`,
    `SetupDiGetDeviceInterfaceDetailW`, `SetupDiDestroyDeviceInfoList`
* `ReadFile`/`WriteFile` are present in the imports, but those are CRT; the device is
  accessed **only through feature reports**. Neither `HidD_GetInputReport` nor interrupt
  reads. This matters: on Linux `hidraw` + `HIDIOCSFEATURE`/`HIDIOCGFEATURE` is enough,
  on the web `WebHID.sendFeatureReport()/receiveFeatureReport()`.

### 3.1 Device discovery

Function `0x407400`:

1. `HidD_GetHidGuid`
2. `SetupDiGetClassDevsW(GUID, 0, 0, 0x12)` (`DIGCF_PRESENT|DIGCF_DEVICEINTERFACE`)
3. loop `SetupDiEnumDeviceInterfaces` → `SetupDiGetDeviceInterfaceDetailW`
   → `CreateFileW(path, 0, FILE_SHARE_READ|WRITE, NULL, OPEN_EXISTING, 0, NULL)`
4. `HidD_GetAttributes` → comparison with constants in `.data`:
   * `[0x62700C]` = `0x30FA`: VendorID
   * `[0x627008]` = `0x2350`: ProductID
   * `[0x627010]` = `0x2130`: VendorID (second pair)
   * `[0x627014]` = `0x30FA`: ProductID (second pair)
   Next to them, the table holds `[0x627018..0x62701F]`, a third pair `0x2330:0x30FA`
   that this function does not use. A match on the first pair sets the model flag
   `[0x62CE18] = 1`, otherwise `0`.
5. `SetupDiGetDeviceInterfaceDetailW` (second pass) + `RegisterDeviceNotificationW`:
   subscription to hot-plug.

**Conclusion: VID family `0x30FA`, ProductID ∈ {`0x2350`, `0x2130`, `0x2330`}.**
Which exact PID the K815 has should be confirmed with `lsusb` when it is connected. In the
software the PID is hard-coded as a constant and is not taken from the configs.

6. `HidD_GetPreparsedData` + `HidP_GetCaps` → check that the interface has
   UsagePage == `0xFF00` (vendor-defined) and Usage == 1. The feature report length is
   taken from here as well (`[0x62CEC8]`), `HidD_FreePreparsedData`.

### 3.2 Transport

The report buffer is the global `0x62CF08`, the device handle is `0x627030`.
Sending: `HidD_SetFeature(handle, 0x62CF08, len)`, where `len` = FeatureReportByteLength
(the read code contains a hard-coded `0x10` = 16 bytes, so the report is most likely
16 bytes: `[0]` = ReportID, `[1..15]` = data).

Values written into the buffer (little-endian stores of the form
`mov dword [0x62CF08], imm`):

| imm        | bytes 0..3      | meaning                                  |
|------------|-----------------|------------------------------------------|
| `0x1807`   | `07 18`         | header "command 0x18", then shifted        |
| `0x00011807` | `07 18 01 00` | commit / close the write block           |
| `0x00091807` | `07 18 09 00` | completion (with an address)             |
| `0x00091807` with arg | `07 18 09 41` | start of the write session       |
| `0x00011807` | `07 18 01 00` | ...                                      |
| `0x00410807->0x410907`| `07 18 09 41` | before Sleep(200 ms)                |
| `0x00811807` | `07 18 81 00` | **erase sector**                         |

Full layout of the buffer when writing (from the disassembly of `0x406D30`/`0x406EE0`):

```
byte0 = 0x07        Report ID
byte1 = 0x18        command class (all configuration commands)
byte2 = subcommand  03 = write byte(s), 05 = read, 00/01 = commit,
                    09 = end of session, 81 = erase sector
byte3 = index / 0
byte4 = addr & 0xFF      (address in device memory, 16 bit)
byte5 = addr >> 8
byte6 = data (1 byte per report in the "byte-wise" variant)
byte7 = (len-1) or (len+0x5F) depending on the model ([0x648570])
bytes8..15 = 0 / data
```

Examples of sequences that were read out:

* write block: `07 18 03 <i> <lo> <hi> <data> <n-1>` for each byte, then
  `07 18 09 00 <lo> <hi> 00 <n-1>` and `07 18 01 00 <lo> <hi> 00 00`;
* read: `07 18 05 00 <lo> <hi> 00 <n-1>` → `Sleep(1)` → `HidD_GetFeature`,
  the response is read from byte 1;
* erase: `07 18 81 00 <lo> <hi> 00 60`, step 0x20, range 0x0000..0x0400;
* the "magic" initializer that packs six arguments into byte2
  (function `0x4133C0`): `al = ((((((a0*2+a1)*2+a3)*2+a2)*2+a4)*2+a5)` → byte2;
  start: `07 18 11 00 00 00 00 <60|00>`, then `07 18 00 00 00 00 00 00`.

### 3.3 Device memory map (preliminary)

The master configuration upload function is `0x40FB20` (~14 KB of code). Inside it there is
a transpose loop `0x4101D0..0x41047C` that reads the key table
`0x647530..0x647580` (stride 4, elements spaced by 0x50) and writes 8 bytes at a time,
incrementing the address by 8 starting from `0x20`:

* address `0x10`: 1 byte (mode/version);
* `0x20 .. 0xBF`: **160 bytes, one code per key**
  (20 reports of 8 bytes). The table merges `KBconfig.ini` (cell `A_B` →
  index `8*B + A`, both phases) with the per-key arrays of `config.ini`
  (`KU_*`/`KN_*`, section 3.4 and section 6). `0xFF` = "no function";
* `0xC0..0xCF`: ?
* `0xD0..`: a block that starts with the bytes `E1 1E`, then a 16-bit value
  read from the device, then `F0 0F 00 00` (LED config / version);
* then LED parameters (`LED_COL_R/G/B1..7`, `HR_LED_MODE`, `HR_SPEED`,
  `HR_BRIGHT`, `HR_DIRECTION`, `HR_SYMMETRY`, ...), macros, media codes.

The firmware stores everything in its non-volatile memory (the erase commands confirm
this), so the written configuration lives without the software and does not depend on the OS.

### 3.4 Configuration files

`app/config/config.ini`: default values for 6 profiles
(`[Config]`, `[Config0]`, `[Config4]`, `[Config5]`, `[Config4_LED1]`, ...).

Key sections:

* `KU_0..KU_159` / `KN_0..KN_159`: the own key table of the application. The
  eight keys are addressed with the application's indices, which are not the
  upload slots:

  | key         | 1  | 2  | 3  | 4  | 5  | 6  | 7  | 8  |
  |-------------|----|----|----|----|----|----|----|----|
  | upload slot | 8  | 16 | 24 | 32 | 9  | 17 | 25 | 33 |
  | `KU_`/`KN_` | 87 | 88 | 89 | 71 | 72 | 73 | 54 | 55 |

  `KU_*` goes to phase B, `KN_*` to phase A. The dialog writes these fields (a
  Consumer function on key 5 writes `KN_72`), and editing them by hand moves the
  slots (both checked, `PROTOCOL.md`, section 4). The keys 4..8 are therefore
  *not* taken from `KBconfig.ini` only: both sources are merged, and which one
  wins is described in `PROTOCOL.md`;
* `UFN_F1..UFN_F12`: what Fn+F1..Fn+F12 does;
* `USCROLL0/USCROLL1`: encoder functions (in `reset_config.ini` they are written as pairs
  `USCROLL0=11,2`, in `config.ini` as single values);
* `MACRO_MAP0..19`, `MACROGRP_MAP0..19`: binding of a key to a macro;
* `LED_COL_R/G/B1..7`: colors (0..15 per channel!), `HR_*`: effect parameters;
* `SINGLECOL`, `USERCOL`, `BMODELED`, `KEYSMODE`, `BWINLOCK`, `BALTF4`.

`app/config/KBconfig.ini`: the `N_M` matrix (column `N`, row `M`, 20 rows of 8
columns); the upload slot of a cell is `8*M + N`. The cells `0_1..1_4` are the
eight keys and are written to both phases; they are the second source of the
keymap next to `KU_*`/`KN_*` (see `PROTOCOL.md`, section 4).

`defMacro.dat` (in the skin) and `app/config/ShortcutMacro.dat` (the working copy,
`macroNum=5`): macros in a text format, `[Init]` + `[Macros]` + `macroNum=<n>` +
lines `N=<hex>`, where each entry encodes the macro steps (delays + codes).

## 4. Open questions

1. The exact PID of the K815 (`0x2350` / `0x2130` / `0x2330`).
2. The data offset in the read response: in the emulator the response is 16 bytes with
   Report ID 7 (confirmed on Linux in WebHID: `receiveFeatureReport(7)` returns
   16 bytes together with the id), on real hardware the response has not been captured yet.
3. How string macros are encoded (the LED part is already figured out, see 5).
4. Verify that the device exposes the config only through feature reports
   (whether there is another interface).
5. Why the function-record table (`0xF9..0x166`, see `PROTOCOL.md`, section 4) has
   exactly 22 entries, and what the constant `0x07` at offset 0 of an entry means.
   The other byte that looked constant (`0x01`) turned out to be the middle byte
   of the record address; offset 1 is a kind (`0x00` inert, `0x03` internal
   function, `0x04` Consumer function), and the 20 inert entries are probably the
   per-key slots of the larger model the configuration file comes from.
6. *Closed*: the application keeps the assignment of the keys in `KU_*`/`KN_*` at
   the indices `87`, `88`, `89`, `71`, `72`, `73`, `54`, `55` for the keys 1..8
   (`KU_*` → phase B, `KN_*` → phase A, upload slots `8`, `16`, `24`, `32`, `9`,
   `17`, `25`, `33`). Question 5 of section 3.4 has the table. The merge rule of
   `KBconfig.ini` and `config.ini` is described in `PROTOCOL.md`, section 4; the
   one case that is left is a profile whose `KU_*` array is all zero.
7. *Mostly closed*: the value space of the keymap is measured for the keyboard
   keys (raw HID usage, F13 = `0x68` … F24 = `0x73`), the Consumer functions
   (`0xC9 + n`), the seven light-control items (`0xE8..0xF0`), the special
   function list (`0xF3` Full-key Locked, `0xF1` Fn — two items) and the system
   control list (`0xB0` Power, `0xB1` Sleep — two items), plus "Default" (the
   factory value) and "Disable" (`0xFF`). Still open: the macro list, which the
   factory configuration leaves empty (no macro group), and the macro encoding.

## 5. LED: effects, "silent fields", and interface clicks

Verified by a sweep through the application interface itself (the clicks are performed by
`tools/app_capture.sh`, the fifth argument `X,Y;X,Y`; screenshot via `K815_SHOT`).
The application window is 1113×800, the coordinates are window coordinates (in the
`xdotool --window` frame), a running emulator is required, otherwise there is no window.

| element                       | coordinates       |
|-------------------------------|-------------------|
| Button / Light / Macro tabs   | 102,88 / 180,88 / 258,88 |
| effect dropdown               | 500,187 (open)    |
| list item `k` (k=0..8)        | `543, 219+29.5*k` |
| Apply (green check mark)      | 687,731           |
| Cancel (red cross)            | 768,731           |
| profile selection             | 317,743           |
| color chips 1–4               | 463/508/554/599, 293 |
| color chips 5–8               | 463/508/554/599, 340 |
| color wheel / empty circle    | 643,293 / 643,340 |
| "Frequency" (slider)          | 285…410, 650      |
| "Symmetry" (toggle)           | 854,598           |
| "Direction"                   | 853,653           |

When opened, the list window occupies `y 204..470` (9 rows of about 29.5 px each),
so a click at `y=202` (immediately below the combobox) still lands in the combobox and
selects nothing. The order of the items is by increasing mode number, with no `3`,
`6`, `10`, `12`: `Backlight Off(0)`, `Breathing(1)`, `Constant(2)`, `Neon(4)`,
`Ambilight(5)`, `Single Water(7)`, `Flow Water(8)`, `Flashing(9)`,
`Partition(11)`; verified with clicks on the first and the last item.

What has been resolved:

* **The effect list and the mode numbers.** The names are in the skin
  `extracted/app/1/skins/1_INI_EN/KbIndexText.ini` (UTF-16 LE), the LED mode table
  starts at index 30, the mode number = index − 30. It matched the stream: `0x1a`
  low nibble. The full table 0..12 is in `PROTOCOL.md`, section 4, and in
  `tools/led_modes.json`.
* **Switching the mode changes exactly one byte** of phase B: `0x1a` (9 modes from
  the list + the same 9 modes with edited silent fields).
* **Only `[ConfigN_LED_COLOR]` goes into the upload.** Verified with a marker: a distinct
  color in each of the four sections produced in `0x02` the color of `_LED_COLOR`
  specifically; `_LED1`/`_LED3`/`_LED7` and `Config0` do not get into the stream even
  after editing `HR_LED_MODE` in them.
* **The silent fields are not uploaded in any mode**: `HR_BRIGHT`, `HR_COLOR_SEL`,
  `USERCOL`, `HR_REACT_LED`, `HR_LED_NUM`, `HG/HB_REACT_LED`,
  `HR_LED_MODE_EN0..8`, and also `LEDTYPE` and `RGB`.
* **Bits 4..6 of byte `0x1a`** the application derives from `SINGLECOL` and the mask
  `RGB_HR_COLOR_EN0..6`: the number of the first enabled color plus one.
* **The high nibble `X`** in the palette is not controlled by the config; it is a template
  of the application. It is reproducible for one config, but differs between
  `captures/baseline_apply.jsonl` (`X` of color 1 = `5`) and a fresh run
  (`1`). Therefore the reference for the sweep is a fresh run, not `baseline_apply`.
* Clicks on the color chips do not change the stream: after a click on the fifth chip the
  palette bytes are the same, and the selection in the UI stayed on the first chip, so
  either the click does not select the chip, or the selection does not affect the stream
  (it was not possible to tell them apart).

### The key-assignment dialog (Button tab)

Clicking a key of the device picture opens a **second top-level window** named like
the main one, but `605×686`. Its position depends on the position of the main
window (main `+254,+57`), so clicks are addressed relative to the dialog window
itself. The main window stops redrawing while the dialog is up, so
`import -window <main>` shows black; take the root window or the dialog window.

The hit points of the eight keys in the main window (window coordinates, measured
on a `1113×800` window; each was verified by the name in the dialog title):

| key | point    | key | point    |
|-----|----------|-----|----------|
| 1   | 440,367  | 5   | 440,436  |
| 2   | 506,367  | 6   | 506,436  |
| 3   | 572,367  | 7   | 572,436  |
| 4   | 637,367  | 8   | 637,436  |

The first click after the window appears is often swallowed (the application is
still busy) and the click has to be repeated; `tools/app_capture.sh` retries until
the dialog exists.

Coordinates relative to the dialog:

| element                                     | coordinates      |
|---------------------------------------------|------------------|
| NO FUNCTION / Default / Disable             | 94,87 / 62,179 / 128,179 |
| Media / Macro                               | 61,261 / 128,261 |
| Light control / Keyboard keys               | 59,348 / 126,348 |
| Special function / System control           | 57,435 / 126,435 |
| value box / its dropdown                    | 320,230 / 427,222 |
| OK (green) / Cancel (red)                   | 380,515 / 432,515 |
| close (red cross)                           | 492,13           |
| "Macro group" box / "Macro" box (Macro tab) | 427,222 / 427,354 |

Each category opens its own list in a **third** window named `MainMenuDlg`, whose
size follows its content (a two-item list is a short strip, a 17-item list is
`254×360`): `Media` the 17 Consumer functions in the order of the numbers of
`PROTOCOL.md`, section 4; `Light control` "Brightness +/−, Speed +/−, Backlight
On/Off, Backlight Colour Switch, Backlight Mode Switch"; `Special function`
"Full-key Locked", "Fn"; `System control` "Power", "Sleep"; `Macro` needs a macro
group: with the factory files the group combo stays empty and the list cannot be
opened at all; `Keyboard keys` is different — see below.

Item positions inside the `MainMenuDlg` window differ between lists, so they are
measured per list from a screenshot: the 17-item Media list has the first item at
`y ≈ 10` and a pitch of 20 px, the 7-item Light-control list has `y ≈ 15` and a
pitch of 30 px, and the two-item lists (Special function, System control) put
their first item at `y ≈ 16` with a pitch of 30 px. A click can land one row off
(observed for two keys in one run), so a category is verified through the
resulting keymap byte.

The two-item lists, measured on key 1 (`KN_87`, and the keymap byte `A 0x0028`):

| list             | first item       | second item        |
|------------------|------------------|--------------------|
| Special function | `0xF3` Full-key Locked | `0xF1` Fn    |
| System control   | `0xB0` Power     | `0xB1` Sleep       |

Both were reproduced (`0xF3` twice), so these lists are swept completely; the
value order inside the Special-function list is reversed relative to the list
order, which is why a single row-off click had earlier produced a wrong label.
The reference run for a category click is `+440,367;@<category>;@427,222;%127,16`
(Special function: `@57,435`, System control: `@126,435`) plus `@380,515` for OK.

`Keyboard keys` is not a list but a **key capture box**: click the value box and
press the wanted key on the keyboard (or send it with `xdotool key`); the box
shows the name of that key. The assignment is the plain HID usage: F13 → `0x68`,
F24 → `0x73`, Escape → `0x29`.

What an assignment changes:

* the value lands in the **keymap**: `Media → Play/Pause` on key 1 writes `0xC9` at `A
  0x0028`, the item labelled `Calculator` writes `0xD9`. The labels drift (from number 12
  on, the list shows the label of the next number), so `Calculator` really is number `16`
  (AC Home) and `0xD9 = 0xC9 + 16`: **a Consumer function is `0xC9 + number`**;
* the application saves it in `config.ini`, in `KN_<i>` (the value written to phase B
  stays in `KU_<i>`), and never rewrites `KBconfig.ini`. The index is the application's
  key index, not the upload slot (`87 88 89 71 72 73 54 55` for the keys 1..8, see
  section 3.4);
* the other categories: `Default` restores the factory value, `Disable` writes `0xFF`,
  `Keyboard keys` the raw usage, `Light control` one of `0xE8..0xF0`, `Special function`
  `0xF3`/`0xF1`, `System control` `0xB0`/`0xB1`. A macro assignment was seen once
  writing `0xAF`, but the factory macro files leave the macro list empty, so it is
  not reproducible. The whole table is in `PROTOCOL.md`, section 4;
* assigning a light-control/system/special/macro function also grows the payload of the
  function-record table by eight bytes per such key (`PROTOCOL.md`, section 4).

The clicks are reproducible through `tools/app_capture.sh`, whose click list
understands the prefixes `+X,Y` (a key of the device picture, retried until the
dialog shows up), `@X,Y` (relative to the dialog), `%X,Y` (relative to the
`MainMenuDlg` pop-up), `!KEYSYM` (a key press for the capture box) and `#X,Y`
(absolute). `K815_KEEP=<dir>` copies the configuration files that the application
wrote to `<dir>` before they are restored — that is how the `KU_*`/`KN_*` indices
were found.

## 6. What the emulator confirmed (uhid + Wine)

See `PROTOCOL.md`: the full protocol is there. Briefly, what has been resolved:

1. **PID**: the application looks for the pair VID `0x30FA` / PID `0x2350` (the first pair
   of the three in the table `[0x627008..]`) and opens **two** interfaces with this
   VID/PID: usage page `0xFF00` for reading and `0xFF01` for writing. The whole config
   goes to `0xFF01`.
2. **Report length**: exactly 16 bytes, the report is **numbered, Report ID = 7**
   (with an unnumbered descriptor Wine zeroes byte 0 and `0x07` disappears;
   with Report ID 7 the byte is preserved, `UHID_START` reports `dev_flags=0x1`).
3. **`0xC0..0xCF`**: there is no separate region; this range lies entirely inside
   phase B `0x49..0xE8`, that is, these are keymap bytes (the question is closed). The
   application writes the regions `0x01..0x21` (header + LED), `0x20..0xBF` (keymap, phase A),
   `0x49..0xE8` (keymap, phase B), `0xE9..0xF4` (`UFN_F1..F12`),
   `0xF5..0xF8` (zeros), `0xF9..` (function-record table; the payload can extend past
   `0x187`), `0xD0..0xD7`.
4. **There is no second interface with a different protocol**: only feature reports,
   as was expected from the imports.

In addition:

* One Apply = 663 reports; `emit(program_from_capture())` reproduces the capture
  **byte for byte** (`tools/k815_proto.py`).
* The session has two phases separated by a read of address `0x0001`; the phases write
  different values to the same addresses, so we store the bytes of each block separately.
* Keymap = a transposed 8×20 matrix (`upload[8*col+row]`). Two sources feed it:
  `KBconfig.ini` (cell `A_B` → index `8*B + A`, both phases) and the per-key
  assignment arrays of `config.ini` (`KN_*` phase A, `KU_*` phase B, the app's own
  index: `87 88 89 71 72 73 54 55` for the keys 1..8). A `KBconfig.ini` cell that
  differs from the `config.ini` value wins over it — verified by putting
  F13..F20 into the cells `0_1..1_4`: all eight slots followed, in both phases.
  A cell value the application does not know as a keyboard usage becomes `0x00`
  (`0xA5..0xA8` produced four zero slots). With the factory `KBconfig.ini` the
  values written by the dialog win (`KN_87 = 0xC9` → `A 0x0028 = 0xC9`).
  `PROTOCOL.md`, section 4, has the details.
* Key 1 = `keymap[8]`, keys 2..4 = `16/24/32`, keys 5..8 = `9/17/25/33`, the
  encoder = `41` and `49`; disabling key 1 changes 1 byte to `0xFF` (in phase A,
  because of the override above).
* The function-record table (`0xF9..`) is an index of 22 five-byte entries
  `07 <kind> <addr24>`. Only the last two entries are active on this model (the
  two encoder records); their `kind` is `0x04` for a Consumer function and `0x03`
  for an internal one. The payload behind the index is not fixed: the application
  inserts and removes 8-byte records, which moves the addresses, so the end of
  phase B was seen anywhere between `0x187` and `0x1A7`. The 20 inert entries are
  probably the per-key records of the larger model the configuration comes from.
* `tools/k815ctl.py` `selftest` against the emulator: 663/663 byte for byte.
* The switches (byte `0x0001`) and the encoder (`USCROLL0/1`) are figured out and carried
  into both tools: `patchSwitches`/`patchEncoder` in `webhid/k815.js`,
  `apply_switches`/`patch_encoder` in `tools/k815_proto.py`. The parity of JS and Python
  is checked by `tools/selftest_webhid.py`.
* The preset (JSON, version 3) is shared by the CLI and the page: `preset`/`apply`. The
  backup stores the exact frame stream with `sha256`, `restore` rejects a tampered file.
  Offline checks without hardware and a browser: `tools/selftest_cli.py` (35 checks,
  including a page-style JSON export applied through the CLI path) and
  `tools/selftest_webhid.py`; `tools/run_all.sh` runs the whole suite
  (`--full` adds the Chromium end-to-end test).

The mapping of LED and Fn actions is settled with differential captures (see
`PROTOCOL.md`, section 4): the palette is `0x02..0x0F`, the effects are `0x10/0x19/0x1a/0x1b`,
`0xE9..0xF4` = `UFN_F1..F12`. The second number of `USCROLL*` is fully figured out:
the numbers `0…16` are 17 Consumer-page functions, followed by internal functions of the
application. The first number of `USCROLL0` is a mode switch: `11` (the default
value) enables the media mode and produces byte `0x44` at `0x0010` (phase A) and
`0x0021` (phase B); any other value produces `((n mod 16) << 4) | 0x0B` and breaks the
media mode: the second encoder entry disappears, the codes become `0xEA`.
The first number of `USCROLL1` does not affect the stream. `0xF9..x` is an index
of 22 five-byte entries plus a payload of 8-/16-byte records; the structure and
the measured dependencies are in `PROTOCOL.md`, section 4. Still open there: the
count 22 and the meaning of the `0x07` at offset 0 of an entry. Everything still
has to be verified on real hardware.

## 7. Plan for further work

See `PLAN.md`.
