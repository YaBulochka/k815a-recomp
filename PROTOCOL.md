# MKESPN K815 protocol (confirmed on the emulator)

This document records what was captured from the real application
(`app/Gaming Keyboard.exe`) running under Wine against a device emulator on
`uhid` (`tools/k815_emulator.py`). All bytes come from intercepting
`HidD_SetFeature`/`HidD_GetFeature`, not from a disassembler.

## 1. Transport

Communication goes **only through feature reports**, the report length is 16
bytes, the report is **numbered, Report ID = 7**. There is no driver and no
interrupt reads.

> **Transport limits on hardware.** The K815 is USB 1.1 low-speed. Both HID
> interrupt endpoints declare `bInterval = 10` (10 ms), so the host polls the
> keyboard at **100 Hz**. Feature reports are 8 bytes on the wire (see section
> 2). There is **no block-read command**: every memory byte needs its own
> `05` -> `GET_FEATURE` -> `01` cycle, about 20 ms per byte. See
> `docs/HARDWARE.md` and `docs/KNOWN-LIMITATIONS.md`.

* Windows: `HidD_SetFeature / HidD_GetFeature`.
* Linux: `hidraw` + `HIDIOCSFEATURE` / `HIDIOCGFEATURE` (or `python-hid`).
* Web: `WebHID` `sendFeatureReport(reportId, data)` / `receiveFeatureReport(reportId)`.

In WebHID the report identifier is passed as a separate argument, not as the
first byte of the buffer: `sendFeatureReport(0x07, frame.subarray(1))`. The
resulting bytes on the wire are the same 16 as with `HidD_SetFeature`
(verified by the end-to-end test `tools/e2e_webhid.py` on the emulator). On
Linux the buffer returned by `receiveFeatureReport` contains the identifier
byte first.

The application opens **two** interfaces with the same VID/PID:

| Usage Page | Purpose                          |
|------------|----------------------------------|
| `0xFF00`   | opens for reading (GENERIC_READ) |
| `0xFF01`   | opens for writing (GENERIC_WRITE); the whole config goes here |

VID `0x30FA`, PID `0x2350`. The firmware stores the config in non-volatile
memory, so the written configuration survives without the software.

## 2. Report format

> **Hardware note (verified on a real K815).** The device's HID descriptor
> declares report ID `0x07` with **seven payload bytes** (8 bytes with the ID).
> The uhid emulator used for the first capture declared fifteen (16 with the
> ID), and the vendor application sizes its buffers from
> `FeatureReportByteLength`. So on hardware every report is 8 bytes; the
> `flag` at offset 14 below exists only in the emulator capture and is never
> sent. The tools keep 16-byte frames internally and truncate them at the
> transport (`k815_proto.wire`, `--wire-len`).

```
offset  field
0       0x07        Report ID
1       opcode      0x18 = memory command, 0x09 = special command
2       subcommand  0x03 write byte, 0x05 load read address,
                    0x00 commit, 0x01 commit, 0x09 end of block,
                    0x81 erase sector, 0x11 init
3       index       offset inside the 8-byte block (0..7)
4,5     address     16-bit little-endian block address
6       data        byte to write
7       length      len-1 (or len+0x5F for another model)
8..13   0
14      flag        0x01 for writes to the keymap/LED area, otherwise 0x00
15      0
```

The actual byte address = `address(4,5) + index(3)`.

## 3. Upload session (one Apply press)

> **Hardware note.** The addresses in this section are the emulator layout
> (phase B starts at `0x0001`). A real device reports its config base in the
> startup `GET_FEATURE` answer, bytes 14..15. The factory K815 used `0x0118`;
> a unit configured with the chip burning tool reports `0x00E0`. The
> application writes phase B at `base + offset`: header `base+0x00`, keymap
> `base+0x48`, palette copy `base+0xE8`, function index `base+0xF8`, record
> payload `base+0x168`, and the two pointers in the header at `base+0x1C` and
> `base+0x1E` carry the keymap and function-table bases. The phase-A addresses
> (`0x10`, `0x20`, `0xD0`) do not move. The repository ships both captured
> sessions (`tests/fixtures/hw_apply.jsonl.gz` for `0x0118` and
> `hw_apply_e0.jsonl.gz` for `0x00E0`); `k815ctl` and the WebHID page pick
> the one matching the reported base.

One Apply press = one session of 663 reports, reproduced exactly by
`tools/k815_proto.py`. A session has two phases, separated by a "verify read"
of address `0x0001`. **The phases write different values to the same
addresses**, so no single memory image satisfies both; we store the bytes of
each block separately.

```
SPECIAL  07 09 41 ...            start of session
--- phase A ---
WRITE 1   @ 0x0010               end + commit
WRITE 160 @ 0x0020  (keymap)     20 blocks of 8 B, end + commit for each
WRITE 8   @ 0x00D0               e1 1e 01 00 f0 0f 00 00 (flag=1), end + commit
WRITE 1   @ 0x0001  (flag=1)
   commit 07 18 00 01 ...        (subcommand 0x00)
   READ   07 18 05 01 ...         load address 0x0001 for reading
   GET -> response, byte 1       the application reads 1 byte
   commit 07 18 01 00 ...
--- phase B ---
WRITE 33  @ 0x0001               0x01..0x21
WRITE 319 @ 0x0049               0x49..0x187 (keymap + LED + macros)
INIT     07 18 11 ...
COMMIT   07 18 00 ...
```

Session total: 523 byte writes (phase A 169 + phase B 354), 67
end-of-block, 68 commit.

## 4. Memory map (confirmed by differential captures)

| address          | contents                                              |
|------------------|-------------------------------------------------------|
| `0x0001`         | phase B: switches byte (see below)                    |
| `0x0001..0x0021` | service header (phase B) + LED palette/parameters     |
| `0x0010`         | phase A: first number of `USCROLL0` (phase B: `0x0021`) |
| `0x0020..0x00BF` | keymap, 160 bytes (phase A)                           |
| `0x0049..0x00E8` | keymap, 160 bytes, the same table (phase B)           |
| `0x00D0..0x00D7` | `e1 1e 01 00 f0 0f 00 00` (phase A)                   |
| `0x00E9..0x00F4` | 12 bytes, Fn actions `UFN_F1..UFN_F12`                |
| `0x00F5..0x00F8` | 4 bytes, zeros in the base session                    |
| `0x00F9..0x0187` | function-record table: 22 index entries of 5 bytes + payload (can grow past `0x187`) |

### Keymap

The 160-byte keymap is an 8×20 matrix (`KBconfig.ini`, rows `N_M`),
uploaded transposed: `upload[8*col + row]`.

| key             | cell (col, row) | index         | default value |
|-----------------|-----------------|---------------|---------------|
| 1..4            | `0_1` .. `0_4`  | 8, 16, 24, 32 | `0x59..0x5C`  |
| 5..8            | `1_1` .. `1_4`  | 9, 17, 25, 33 | `0x5D..0x60`  |
| encoder (rot.)  | `1_5`           | 41            | `0xE9`        |
| encoder (press) | `1_6`           | 49            | `0xF4`        |

A cell `A_B` of `KBconfig.ini` belongs to index `8*B + A`: the first number is
the column, the second the row. Verified by editing one cell at a time against a
fresh `config.ini`: `3_0` → 3, `1_1` → 9, `2_1` → 10, `5_1` → 13, `0_2` → 16,
`1_2` → 17, `0_3` → 24, `1_3` → 25, `0_4` → 32, `1_4` → 33. One such edit lands
in **both** phases. (A cell that keeps the value that `config.ini` already
stores for that key does not change the stream — see the merge rule below.)

`0xFF` = "no function". The other values are USB HID usage codes (for example
`0x59` = Keypad 1); the full value space is in the next section.

`config.ini` also holds the arrays `KU_0..KU_159` and `KN_0..KN_159`. The
application addresses the eight keys with its own indices, which are not the
upload slots, and these two arrays carry the assignment of a key:

| key         | 1  | 2  | 3  | 4  | 5  | 6  | 7  | 8  |
|-------------|----|----|----|----|----|----|----|----|
| upload slot | 8  | 16 | 24 | 32 | 9  | 17 | 25 | 33 |
| `KU_`/`KN_` | 87 | 88 | 89 | 71 | 72 | 73 | 54 | 55 |

`KN_<i>` goes to phase A, `KU_<i>` to phase B. Measured in both directions: the
dialog writes exactly these fields (a Consumer function on key 5 writes
`KN_72 = 0xC9 + n`), and editing them by hand moves exactly these slots
(`KU_71 = 0xAA`, `KN_71 = 0xAB` → slot `32` is `0xAA` in phase B and `0xAB` in
phase A). The earlier note that only `87`, `88` and `89` reach the stream was
wrong: those are keys 1..3, and keys 4..8 sit at `71`, `72`, `73`, `54`, `55`.

`KBconfig.ini` is the second source. Its cells `A_B` (column `A`, row `B`) map
to the upload index `8*B + A` and are written to **both** phases — verified for
all eight cells, with F13..F20 placed in `0_1..1_4` and landing in the slots
`8, 9, 16, 17, 24, 25, 32, 33`. The two sources merge like this:

* a cell wins over the assignment in `config.ini` when its value differs from it
  (a `KBconfig.ini` holding F13..F20 beats `KU_87 = 89`; one holding the factory
  `0x59..0x60` loses against a value written by the dialog);
* a cell value that is not a keyboard usage the application knows becomes
  `0x00` (`0xA5..0xA8` in the cells produced four zero slots);
* a profile whose `KU_*` array is all zero and that has no `KN_*` entries at all
  (`CurConfig = 3`/`4`) uploads an all-zero keymap although `KBconfig.ini` has
  values; why is not settled.

In the reference `KN_87 = 255` and `KU_87 = 89`, hence `keymap[8]` is `0xFF` in
phase A and `0x59` in phase B.

### Keymap value space

The 160 bytes are not a plain usage list: every kind of assignment is encoded in
the same byte and the ranges do not overlap. Measured through the assignment
dialog of the application, one run per row:

| value        | assignment                                                        |
|--------------|-------------------------------------------------------------------|
| `0x04..0xE7` | keyboard usage — `0x29` Escape, `0x68..0x73` F13..F24; the factory values `0x59..0x60` are Keypad 1..8 |
| `0xC9 + n`   | Consumer function number `n = 0..16` (table in the encoder section) |
| `0xE8..0xF0` | light control, the seven items: `0xE8` Backlight Colour Switch, `0xE9` Backlight Mode Switch, `0xEA` Brightness +, `0xEB` Brightness −, `0xEC` Speed +, `0xED` Speed −, `0xF0` Backlight On/Off |
| `0xF1`       | special function: "Fn" (the list has two items)                   |
| `0xF3`       | special function: "Full-key Locked" (first item of the list)       |
| `0xB0`       | system control: "Power" (first item)                              |
| `0xB1`       | system control: "Sleep" (second item)                             |
| `0xAF`       | macro on key 1 (the first key slot)                               |
| `0xB8..0xBE` | macro on keys 2..8 (one value per key slot, not per macro)        |
| `0xFF`       | no function ("Disable")                                           |

The special-function list holds exactly two entries and the system-control list
two as well, so both are swept completely: `0xF3` Full-key Locked, `0xF1` Fn,
`0xB0` Power, `0xB1` Sleep. The order inside the list is not the order of the
values.

The macro category needed a macro group to offer anything: the factory
`macro/macroGrp.ini` has `macGrpNum=0`, so the group combo stayed empty. Adding
`macGrpNum=1` + `0=ShortcutMacro` and the factory `ShortcutMacro.dat` to the
skin's `macro/` directory makes the list appear (Cut, Copy, Paste, Boss,
SelectAll). Assigning a macro to a key then:

* writes the per-key value from the table above (`0xAF` for key 1,
  `0xB8..0xBE` for keys 2..8). The value does **not** depend on which macro was
  chosen: the same Cut macro on keys 1..8 gives `AF B8 B9 BA BB BC BD BE`;
* writes `MACROGRP_MAP<slot>` / `MACRO_MAP<slot>` in `config.ini`, where
  `slot = 10 + key - 1` (group and macro index);
* activates the key's function slot and uploads a 16-byte record per
  macro-assigned key (see the function table below).

The factory `ShortcutMacro.dat` stores the five display strings (`Cut`, `Copy`,
`Paste`, `Boss`, `SelectAll`, each step is a three-digit decimal usage followed
by `000`). The record does not contain that text: the usage byte matches a
single shortcut key. The measured records are consistent with these shortcuts
(the modifier mapping is inferred from the two observed modifier bytes):

| macro      | record (16 bytes)                                     | shortcut (inferred) |
|------------|-------------------------------------------------------|---------------------|
| Cut        | `01 00 86 e0 86 1b 06 e0 06 1b 00 00 00 00 00 00`     | Ctrl+X              |
| Copy       | `01 00 86 e0 86 06 06 e0 06 06 00 00 00 00 00 00`     | Ctrl+C              |
| Paste      | `01 00 86 e0 86 19 06 e0 06 19 00 00 00 00 00 00`     | Ctrl+V              |
| Boss       | `01 00 86 e3 86 07 06 e3 06 07 00 00 00 00 00 00`     | Win+D               |
| SelectAll  | `01 00 86 e0 86 04 06 04 06 e0 00 00 00 00 00 00`     | Ctrl+A              |

The usage byte (offsets 5 and 9) is X/C/V/D/A; the modifier byte is `0xe0` for
the Ctrl shortcuts and `0xe3` for Win+D. The SelectAll record has the two halves
swapped relative to the others (observed once; worth re-checking on hardware).
The first two bytes are `01 00`; bytes 10..15 are zero.

The high nibble of `0x86`/`0x06` is the direction: `0x80` = key down, `0x00` =
key up. The low nibble is the **key hold in 8 ms units**:

```
hold_units = (hold_ms + 3) // 8        # clamped to 1..15
```

The factory `0x86`/`0x06` is therefore `n = 6`, i.e. a **48 ms** hold, not a
zero delay. Measured against the chip vendor's burning tool by changing one
delay in `macro.dat` and diffing the uploaded record:

| hold in macro.dat | low nibble |
|---|---|
| 5 ms | 1 (`0x81`/`0x01`) |
| 20 ms | 2 (`0x82`/`0x02`) |
| 50 ms | 6 (`0x86`/`0x06`) |
| 120 ms | 15 (`0x8f`/`0x0f`) |

The same encoding is used by the encoder's single-key rotation record, so the
key hold is configurable there: `encoderHoldMs` in the JSON preset (8..120 ms,
default 48), `k815ctl.py encoder --macro ... --hold MS`, and the **Key hold**
selector in the WebHID page. On the test unit 24 ms felt snappier than 48 ms;
120 ms made rotation slower, so a longer hold is not a cure for the 100 Hz
polling limit (see `docs/KNOWN-LIMITATIONS.md`).

One custom single-letter macro ("Z") produced the keymap marker but **no**
record; the application only uploaded the keymap byte. This experiment is
inconclusive: the hand-written `ShortcutMacro.dat` may not have been a valid
custom macro set. Text macros and custom groups stay open until they are
re-checked with the vendor editor or on hardware.

"Default" in the dialog restores the factory value of that key (key 1 →
`0x59`). The tools write the byte as given, so any of these values can be put on
a key.

### LED: palette and effect (phase B)

The LED settings do not sit in a separate region: they are spread across the
header `0x01..0x21` of phase B.

| address / bits    | field                                                 |
|-------------------|-------------------------------------------------------|
| `0x02+2i`         | color `i` (i=0..6): `(R<<4)\|B`                       |
| `0x03+2i`         | color `i`: `(X<<4)\|G`, `X` is the template nibble (see below) |
| `0x10` bits 0..3  | `HR_REACT_TYPE` (`0x80 \| type`)                      |
| `0x19` bits 0..2  | `HR_SPEED`                                            |
| `0x19` bits 6..7  | `HR_DIRECTION` (`direction << 6`)                     |
| `0x1a` bits 0..3  | `HR_LED_MODE`, the effect number (table below)        |
| `0x1a` bits 4..6  | `(first enabled color + 1)` if `SINGLECOL=1`, otherwise 0 |
| `0x1a` bit 7     | `HR_SYMMETRY`                                         |
| `0x1b` bits 0..6  | `RGB_HR_COLOR_EN0..6` (bit mask)                      |

The color channels are 4-bit (0..15) and are stored **inverted**: a stored
nibble of `0` is full brightness, `15` is off. Setting colour 1 to
`R 15 · G 0 · B 0` lit the keys cyan, because the device rendered
`R 0 · G 15 · B 15`; `15/15/15` is black and `0/0/0` is white. The CLI and the
preset files keep the raw wire value, and the WebHID page converts it for
display. Verified byte by byte on 6 differential captures (`cols123`,
`c1_green/red/blue`, `c2_black`, `led1_black`) plus a live write.

The high nibble of every second byte (`X`) is not controlled by any config
field: editing any LED fields does not change it, so it is a template of the
application itself; it must be preserved when patching
(`Program.patch_nibble`). For the same config it is reproducible, but it
depends on the state of the program: in `captures/baseline_apply.jsonl` the
`X` of color 1 equals `5`, while in a fresh run with an untouched `config.ini`
it is `1` (the other `X` values match). Therefore the reference for the sweep
is a **fresh run** with an untouched config, not `baseline_apply`.

The application computes bits 4..6 of byte `0x1a` itself: with `SINGLECOL=1`
it takes the number of the first **enabled** color in the mask
`RGB_HR_COLOR_EN0..6` and writes `number + 1`. Verified: `SINGLECOL=1` →
`0x15`; plus `EN0=0` → `0x25`; plus `EN1=0` → `0x35`; plus `EN2=0` → `0x45`;
but `SINGLECOL=1` + `EN5=0` → `0x15` (disabling a color that is not the first
does not shift the number).

#### Effect modes

The mode number is the low nibble of `0x1a`. The application takes it from
the selected effect; the value equals the index in the skin name table
`extracted/app/1/skins/1_INI_EN/KbIndexText.ini` minus 30:

| number | name (EN / CN)                 | in UI list  |
|-------|--------------------------------|-------------|
| 0     | Backlight Off / 关灯           | yes         |
| 1     | Breathing / 呼吸               | yes         |
| 2     | Constant / 常亮                | yes (single-color, bits 4..6 = 1) |
| 3     | Response / 反应                | no          |
| 4     | Neon / 霓虹                    | yes         |
| 5     | Ambilight / 流光溢彩           | yes         |
| 6     | Comet Tail / 彗星拖尾          | no          |
| 7     | Single Water / 单灯流水        | yes         |
| 8     | Flow Water / 行云流水          | yes         |
| 9     | Flashing / 闪烁                | yes         |
| 10    | Monochromatic Wave / 单色波浪  | no          |
| 11    | Partition / 分区               | yes         |
| 12    | Combination / 组合             | no          |

Verified by a sweep: `HR_LED_MODE=0/1/2/3/4/5/7/8/9/10/11` reaches
`0x1a` as is, and the same icon is shown in the UI list. Values `≥ 13` are
undefined: `HR_LED_MODE=16` showed "Comet Tail", while `0x00` went into the
stream. The UI list contains 9 entries in ascending order of number, without
`3`, `6`, `10`, `12`: `Backlight Off`, `Breathing`, `Constant`, `Neon`,
`Ambilight`, `Single Water`, `Flow Water`, `Flashing`, `Partition`. The modes
`Response (3)`, `Comet Tail (6)`, `Monochromatic Wave (10)` and
`Combination (12)` are selected only by editing the config.

Switching through all nine entries of the list changes **exactly one byte**
in the stream, `0x1a` (verified: 9 runs with clicks on the list + 9 runs with
the same clicks and edits of the silent fields).

#### Which section reaches the upload

The sections `[ConfigN_LED1]`, `[ConfigN_LED3]`, `[ConfigN_LED7]` and
`[ConfigN_LED_COLOR]` do **not reach** the stream: the application always
takes the active section `[ConfigN_LED_COLOR]` (verified with a marker: a
distinct color in each of the four sections produced, in `0x02`, the color
from `_LED_COLOR`). The section `[Config0]` is not read either (its palette
does not reach the stream). Editing `HR_LED_MODE` in `_LED1`, `_LED3`,
`_LED7` does not affect the choice of section.

#### Fields that are not uploaded

0 stream changes for each edit (each field is a separate run; the LED
fields were also checked in all nine modes of the list):

* `HR_BRIGHT`, `HR_COLOR_SEL`, `USERCOL`, `HR_REACT_LED`, `HR_LED_NUM`,
  `HG_REACT_LED`, `HB_REACT_LED`, `HR_LED_MODE_EN0..8`;
* `SINGLECOL` by itself is silent too, but the application derives bits 4..6
  of byte `0x1a` from it (see above).

The tool does not need to support these fields: the device does not receive
them.

### Switches (phase B, `0x0001`)

| bit    | field              | check                                |
|--------|--------------------|--------------------------------------|
| 0x01   | `BALLKEYSLOCK`     | `=1` → `0x40` becomes `0x41`         |
| 0x04   | `BWINLOCK`         | `=1` → `0x44`                        |
| 0x40   | `BALTF4`           | `=0` → `0x00`                        |
| 0x80   | `KEYSMODE`         | `=1` → `0xC0`                        |

All four fields live in the section `[Config1]`. In the base config the byte
equals `0x40` (only `BALTF4` is enabled). `BMODELED` does not affect the
stream.

Not uploaded at all: `ConfgCnt`, `ConfigName1..5`, `LEDTYPE`, `RGB` (section
`[Init]`); editing any of them individually changes no byte.

### Profile selection

`[Init].CurConfig` selects the profile: the application takes the sections
`[ConfigN]` and `[ConfigN_LED*]`. Swept for `CurConfig = 1..5` against a fresh
reference run:

* `2` — `[Config2]` differs from `[Config1]` only in `KU_87..KU_89`, `KN_87` and
  the two `USCROLL*`: the keymap follows (`A 0x0028` becomes `0x59`), the kind
  byte of both encoder entries changes from `0x04` to `0x03` (their numbers are
  `17` and `18`, the internal functions), and both records are rewritten with the
  body layout of an internal function (`01 00 81 E0 01 F7 <n> 00 02 E0 …`).
  Nothing else moves.
* `3` and `4` — `[Config3]`/`[Config4]` have `KU_* = 0`, no `KN_*` entries at
  all, `UFN_F* = 255` and `USCROLL0/1 = 3`/`2`: the keymap is all zeros, `UFN` is
  `0xFF`, the switches byte becomes `0xC0`, both encoder entries lose their
  record (`07 00 68 01 00`) and the payload shrinks to one `0x00` byte. Phase A
  `0x0010` is `0x32` (that profile has no second number in `USCROLL0`).
* `5` — like `3`/`4` but with its own `UFN_F*` values: the region `0xED..0xF4`
  follows `[Config5]`.

`ConfgCnt` has no effect on this.

### Fn actions and encoder

* `0xE9..0xF4` = `UFN_F1..UFN_F12` from the section `[Config1]`
  (`config.ini`). Verified: `UFN_F1=170` → `0xE9` changes from `0xCE` to
  `0xAA`. The fields `FN_F1..` from `KBconfig.ini` do not affect the upload.
* `USCROLL0` / `USCROLL1` are the two directions of encoder rotation, a value
  of the form `"first,second"`. Analyzed by a differential sweep (56 runs,
  `tools/media_table.py`).

  **The second number** is the function number. The numbers `0…16` give
  Consumer page functions (`0x0C`); they are sent to the device as a
  little-endian byte pair:

  | number | usage  | HID Usage Tables            | label in `[UniqueName]`  |
  |-------|--------|-----------------------------|--------------------------|
  | 0     | 0x00CD | Play/Pause                  | Play/Pause               |
  | 1     | 0x00EA | Volume Decrement            | Vol−                     |
  | 2     | 0x00E9 | Volume Increment            | Vol+                     |
  | 3     | 0x00B5 | Scan Next Track             | Previous                 |
  | 4     | 0x00B6 | Scan Previous Track         | Next                     |
  | 5     | 0x0183 | AL Consumer Control Config. | Player                   |
  | 6     | 0x018A | AL Email Reader             | Mail                     |
  | 7     | 0x0225 | AC Forward                  | WWW Forward              |
  | 8     | 0x0226 | AC Stop                     | WWW Stop                 |
  | 9     | 0x0224 | AC Back                     | WWW Back                 |
  | 10    | 0x0227 | AC Refresh                  | WWW Refresh              |
  | 11    | 0x0221 | AC Search                   | WWW Research             |
  | 12    | 0x00E2 | Mute                        | WWW Favorite ← shift     |
  | 13    | 0x0194 | AL Local Machine Browser    | Mute                     |
  | 14    | 0x00B7 | Stop                        | My Computer              |
  | 15    | 0x0192 | AL Calculator               | Stop                     |
  | 16    | 0x0223 | AC Home                     | Calculator               |

  Starting from number `12` the `[UniqueName]` labels diverge from what
  actually goes to the device: the application has no entry `收藏（网页）`
  (WWW Favorite) in its code table, so everything after that shifts by one. In
  practice this means that a "bookmark" cannot be obtained through the native
  software; Mute takes that place. From `17` on come the internal functions of
  the program (full key lock, lighting, mouse buttons, macro editor items):
  the encoder entry changes its appearance, and they do not go out as Consumer
  page usages.

  The pair is written twice in two 16-byte entries: encoder-0 occupies
  `0x0168` (the code at offsets +6 and +12), encoder-1 occupies `0x0178`. For
  number `0` the second copy is zeroed; the application behaves the same way,
  and the tool repeats this.

  **The first number** of `USCROLL0` is a mode selection, not a parameter. It
  goes out as a single byte to phase A `0x0010` and phase B `0x0021` by the
  rule `((n mod 16) << 4) | 0x0B`, but for `n = 11` (the default value)
  `0x44` is written instead. Any other value breaks the media mode: the record
  of `USCROLL1` (`0x0178..0x0187`, 16 bytes) is not written, and the single
  record that remains at `0x0168` carries `0x00EA` (function number `1`, the
  value of `USCROLL1` in the default configuration) instead of `0x00E9` (38
  runs); the index then marks entry 20 empty and points entry 21 at `0x0168`
  (see below). The first number of `USCROLL1` does not affect the stream at all
  (23 runs). Therefore the tool does not touch the first number.

  The encoder keys are a separate mechanism: the cells `1_5` and `1_6` of the
  file `KBconfig.ini` (slots `41` and `49` of the matrix, "Encoder A/B" on the
  diagram, values `0xE9` and `0xF4`). **Hardware result:** rotation does not
  use slot `41`; slot `49` is the encoder **press**. To make rotation send an
  ordinary keyboard key, put a single-key macro record in the direction's
  record: `01 00 86 <usage> 06 <usage>` followed by ten zero bytes. With
  `enc0 = 01 00 86 56 06 56 ...` and `enc1 = 01 00 86 57 06 57 ...` the unit
  sends Keypad `-`/`+`, has no media function, and the press follows the
  keymap slot `49`. This is the basis of
  `presets/k815-f13-20-encoder.json`.

* `0xF9..0x187` (and further) is the function-record table: an index of 22
  five-byte entries followed by a payload area. The ranges below are the
  default session; an active macro or another function inserts records and
  shifts the payload.

  | range          | content (default session)                          |
  |----------------|----------------------------------------------------|
  | `0xF9..0x166`  | 22 entries of 5 bytes                              |
  | `0x167`        | separator, always `0x00`                           |
  | `0x168..0x177` | 16-byte record of `USCROLL0` (encoder rotation A)  |
  | `0x178..0x187` | 16-byte record of `USCROLL1` (encoder rotation B)  |

  Every entry has the shape `07 <kind> <addr0> <addr1> <addr2>`: offset 0 is the
  constant `0x07`, offset 1 is a kind (see below) and `addr0..addr2` is the
  absolute phase-B address of the record of that entry as a 24-bit little-endian
  value. The middle address byte is `0x01` only because the payload happens to
  live at `0x1xx`; it is part of the address, not a constant.

  | `kind` | meaning                                   |
  |--------|-------------------------------------------|
  | `0x00` | no record (the entry is inert)            |
  | `0x03` | the entry refers to an internal function  |
  | `0x04` | an active record: a Consumer function, a macro shortcut, or the media-mode encoder |

  * In the default configuration (media mode) the entries 0..19 read
    `07 00 68 01 00`, entry 20 reads `07 04 68 01 00` (address `0x000168`, the
    record of `USCROLL0`) and entry 21 reads `07 04 78 01 00` (address
    `0x000178`, the record of `USCROLL1`). The 20 other entries carry the same
    address `0x168` but are inert, because their kind byte is `0x00`.
  * **The 22 entries.** The 20 non-encoder entries look like the key slots of
    the model family (20 keyboard keys); the K815 uses slots **10..17 for keys
    1..8** (measured: assigning a macro to key 1 activates entry 10, key 2
    entry 11, … key 8 entry 17). Slots 0..9 and 18/19 are not reachable on this
    8-key model. Together with the two encoder directions (20/21) this explains
    the number `22`; the larger model hypothesis is the most likely one but is
    not verified on another device. In a device dump the empty entries all
    point at the current payload start; the pointer is not meaningful while
    `kind = 0`.
  * The constant `0x07` at offset 0 is present in every entry, including the
    inert ones. It is a tag/version field of the 5-byte entry, not a value
    derived from the record. Its exact meaning is still unknown (it is not the
    report id as far as the protocol shows; the same byte is used even when
    the entry is empty).
  * Assigning a macro to a key activates its slot (`kind = 0x04`), inserts a
    16-byte record and moves the following addresses by 16. Five macro keys
    give entries 10..14 = `07 04 68/78/88/98/A8 01 00`, entries 15..19 inert
    at `0x01B8`, and the encoders at `0x01B8`/`0x01C8`. The keymap then holds
    the per-key macro markers from the previous section.
  * The payload is not of a fixed length. A record is 8 or 16 bytes and the
    application inserts and removes them, so the addresses in the index move:
    with eight bytes more the whole payload shifts by eight (entries 0..10 point
    at `0x168` and the rest at `0x170`), and with `CurConfig = 3`/`4` the payload
    collapses to one byte. Measured range of the end of phase B: `0x187` (base)
    to `0x1C7` (five extra 16-byte macro records).
  * `UFN_F1..UFN_F12` do not touch the table: with `UFN_F1=170` and the media
    mode still on, only `0x00E9` changes (`0xCE` → `0xAA`).
  * The function number only rewrites the record body: `USCROLL0=11,3` changes
    `0x016E` and `0x0174` (`0x00E9` → `0x00B5`) and leaves the index untouched.
  * The first number of `USCROLL0` selects the mode. With the default `11` both
    records exist. With any other value the record of `USCROLL1` disappears, the
    code of the remaining record becomes `0x00EA`, and the index becomes
    `… 00 68 01 00 | 04 68 01 00` (verified here for the first numbers `0`, `1`,
    `2`, `3`, `5` and `9`, second number fixed at `2`).
  * With the first number `4` the payload shifts by 8 bytes: the record of
    `USCROLL1` moves to `0x170` and entry 21 becomes `07 04 70 01 00`. This
    shows that the third field is an address and not a constant.
  * `MACRO_MAP*`/`MACROGRP_MAP*` and `ShortcutMacro.dat` do not directly change
    the table; what changes it is the resulting key assignment. The macro
    records described above are uploaded by the application, contrary to an
    earlier note.


## 5. Startup handshake

After opening the device the application sends:

```
07 18 11 00 00 00 00 00   INIT
07 18 00 00 00 00 00 00   COMMIT
GET_FEATURE               -> the firmware returns up to 21 bytes
```

Verified on hardware: the first 16 bytes are a device-info header,

```
07 21 c1 41 01 01 02 08 01 02 64 02 60 01 18 01
```

and bytes 14..15 are the **config base** (`18 01` = `0x0118` on the factory
unit; `e0 00` = `0x00E0` after the burning tool). The application reads that
16-bit value and writes it into the phase-A service block at `0xD2..0xD3`. The
emulator answers with a fake value, which is why its capture uses base
`0x0001`.

The application **does not read the whole config from the device**: it takes
the settings from `config.ini`/`KBconfig.ini`, and from the device it reads
only the service header (in the session, the read of `base`).

### Reading one byte (verified on hardware)

```
07 18 05 00 <lo> <hi> 00 00   load the read address
   sleep ~1 ms
GET_FEATURE                   answer byte 1 is the data byte
07 18 01 00 00 00 00 00       close the read
```

The closing commit is required: without it the firmware answers the next
request with the status byte `0xb4` instead of memory. The first two answers
after `INIT`/`COMMIT` are a stale buffer and must be discarded. This cycle is
implemented by `k815ctl.py read_byte`/`dump_range` and by `readRanges` in
`webhid/app.js`.

> **Hardware note (November 2026).** A batch of memory reads leaves the firmware
> in the read state: the keyboard stops sending normal input. Closing the HID
> handle does not help, and a `USBDEVFS_RESET` (bus reset) does not help either
> because it does not power-cycle the controller. A physical replug does.
> Sending the startup handshake **`INIT` + `COMMIT` + GET** after the batch
> restores input immediately. `readRanges` in `webhid/app.js` does this in a
> `finally` block; `k815ctl info` after a dump has the same effect.

## 6. Tools

* `tools/k815_emulator.py`: a fake device on `uhid` plus a JSONL log.
* `tools/k815_proto.py`: parsing/assembly of reports, extraction and exact
  reproduction of a session (`program_from_capture` / `emit`), patching of
  blocks and of the LED palette (`patch` / `patch_nibble` / `patch_palette` /
  `read_palette`).
* `tools/decode_capture.py`: human-readable decoding of the log.
* `tools/diff_captures.py`: diff of two captures by address (an RE tool).
* `tools/app_capture.sh`: a run of the real application under Wine against the
  emulator with substitution of `config.ini` / `ShortcutMacro.dat` / `KBconfig.ini`.
  The fifth argument is a list of clicks `X,Y;X,Y` before Apply, so that an
  experiment reaches a tab or an item of a drop-down list that the application
  does not open by itself. The variable `K815_SHOT` (a path to a PNG) takes a
  screenshot of the window after the clicks.
* `tools/k815ctl.py`: the production tool: `list`, `info`, `dump`, `replay`,
  `keymap`, `led`, `switches`, `encoder`, `preset`, `apply`, `show`, `backup`,
  `restore`, `selftest`. The LED numbers are printed with their names
  (`led --list` prints the catalogue of `tools/led_modes.json`; `led` and
  `show` append the name to the mode).
* `tools/selftest_cli.py`: offline checks of the model and the CLI: exact
  reproduction of a capture, switches, encoder, preset round-trip,
  a WebHID-style JSON export applying like native edits, the generated effect
  catalogue, backup/restore and failure on a tampered file. No hardware or
  browser is needed.
* `tools/run_all.sh`: one entry point for the offline suite (syntax checks of
  the Python, shell and JS files, then `selftest_cli.py` and
  `selftest_webhid.py`; `--full` adds `e2e_webhid.py`). A stage whose tools are
  missing is skipped.
* `tools/udev/99-mkespn-k815.rules` + `tools/install_udev.sh`: a udev rule
  that gives the session user access to `/dev/hidraw*` of the vendor interface
  (`0xFF01`). It is needed both for `k815ctl.py` and for the WebHID page.
  Installation requires root and is performed by the user manually.
* `tools/sweep_config.py`: a sweep of the fields of `config.ini`: it edits one
  field, runs the application under Wine itself (through `app_capture.sh`) and
  prints the addresses that changed relative to the reference. The run goes
  under `Xvfb`, determinism is verified (two identical runs match byte for
  byte). In a batch file (`--batch`) an experiment can define `_clicks`, the
  clicks before Apply; `--clicks` does the same for a single experiment. The
  reference (`--ref`) must be a fresh run with an untouched config:
  `captures/baseline_apply.jsonl` differs from it in the bytes `A 0x0028` and
  `B 0x0003`.
* `tools/media_table.py`: builds the table "number of `USCROLL*` → HID usage"
  from the sweep runs and labels the codes according to HID Usage Tables.
* `tools/hid_consumer_usages.json`: HID Usage Tables (Consumer, 456 entries);
  `tools/led_modes.json`: a catalog of LED effects (mode number → name).
* `tools/selftest_webhid.py`: verification of the WebHID logic in JS against
  `k815_proto.py`.
* `tools/e2e_webhid.py`: an end-to-end test: real Chromium + WebHID + `uhid`.
* `tools/build_webhid.py`: a generator of `webhid/k815_session.js` from a
  capture; it also embeds the effect catalogue of `tools/led_modes.json` as
  `LED_EFFECTS`, which the page uses to label the effect list, and the frame
  pauses measured with the `t` field of the capture (`delays`, gaps over 20 ms,
  plus `readPauseMs`).

Check: `python3 tools/k815ctl.py selftest captures/baseline_apply.jsonl
--log captures/wine4.jsonl` → the emulator accepts 663/663 reports
byte for byte.

## 7. Preset and backup

`preset` uploads the state of the template to JSON, `apply` applies such a JSON
back. The format matches the export of the WebHID page (version 3), so the
file moves between the browser and the CLI without edits:

```json
{
  "version": 3,
  "keymap": [160 numbers 0..255],
  "led": {"palette": [[r, g, b] x7], "mode": 5, "speed": 3, "direction": 1,
          "symmetry": 0, "single": 0, "react": 1, "rgbEn": 127},
  "switches": {"altF4": true, "winLock": false, "allKeysLock": false,
               "keyMode": false},
  "encoder": {"enc0": 2, "enc1": 1},
  "k1": 89, "k2": 90, "enc1": 233, "enc2": 244
}
```

The keys `k1..k8`, `enc1`, `enc2` duplicate the matrix cells for reading by
eye; `encoder` stores the numbers from `[Config1]`.

Two caveats that matter for the first write to real hardware:

* A preset writes the same keymap into both phases, as the WebHID page does.
  If the phases of the captured template diverge, after `apply` the table
  becomes uniform.
* `backup` saves the **stream of frames** that the tool is going to send, not
  a read of the device: the artifact contains the 16-byte reports themselves
  and their `sha256`, so `restore` reproduces a frame-by-frame sequence that
  is known to work. Only `backup --from-device` reads the device memory (best
  effort, requires hardware) and puts the result into the field `deviceMemory`;
  the tool does not attempt to restore from this dump, because the phases write
  different values to the same addresses.

### Dump to preset

`dump` reads device memory one byte at a time. `dump --json FILE` writes the
same record the WebHID page's Dump button produces (`kind: "k815-memory-dump"`,
`address`, `length`, a `memory` map of 16-byte lines and a `sha256`);
`dump --preset FILE` reads the whole image and decodes it directly.

`preset --from-dump FILE` converts a saved dump into this section's preset
format. It accepts the page/CLI JSON, a raw binary image from `dump --out`
(with `--base` for its first address), and the `deviceMemory` of a
`backup --from-device` artifact. A bad `sha256` or a gap in the image is
rejected, not filled with zeros.

A dump is one memory image, not the two-phase stream. The keymap is therefore
taken from the phase-B copy at `0x0049`: the later header write covers
`0x0020..0x0021` and the phase-B keymap covers `0x0049..0x00E8`, so the phase-A
copy at `0x0020` does not survive intact. The switches, LED header and encoder
records are already read from their phase-B addresses.
