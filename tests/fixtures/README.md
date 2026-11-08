# Test fixtures

Inputs the offline suite needs. They are committed because `captures/` and
`extracted/` are gitignored: without them `tools/run_all.sh` cannot run on a
fresh clone.

| File | Contents |
|------|----------|
| `baseline_apply.jsonl.gz` | SET reports of the reference Apply session (663-frame upload plus the startup handshake), gzipped with the original timestamps |
| `macro5.jsonl.gz` | the session with keys 1..5 assigned to the five factory macros; pins the function-table slots, keymap markers and 16-byte records |
| `KBconfig.ini` | the factory 8×20 keymap matrix, normalized to one `A_B=<value>` cell per line |
| `config.ini` | the effective `KU_0..KU_159` values, used by the emulator to seed its memory in the end-to-end test |

## How they are used

`tools/fixtures.py` resolves a path: the real artifact in `captures/` or
`extracted/` wins when it exists, otherwise the fixture is used. The tests never
depend on both being present.

`k815_proto.capture_records()` reads the gzipped file transparently, so the same
code path handles a full JSONL capture and the fixture.

## Regenerating

After unpacking the vendor application and recording the baseline capture:

```sh
innoextract -d extracted "MKESPN K815.EN[20250610].exe"
# record captures/baseline_apply.jsonl with tools/app_capture.sh
# optionally record the macro session as captures/macro5.jsonl (see below)
python3 tests/make_fixtures.py
sh tools/run_all.sh
```

`macro5.jsonl.gz` is optional: `make_fixtures.py` regenerates it only when
`captures/macro5.jsonl` is present and otherwise leaves the committed file
alone.

## Hardware macro test

`tools/macro_hw_test.py` is a manual test, not part of `tools/run_all.sh`. It
reads the current device state, writes a 14-event A..G record into the 32-byte
function payload, asks for a physical key press, and restores the exact
pre-test state. On a `0x00E0` unit it verified that the firmware executes
multi-step macros: key 1 typed `abcdefg`. The same run showed that the payload
window ends at `0x267` (writes at `0x268` are ignored).

`tools/selftest_macro.py` pins the record format, the factory records and the
32-byte budget offline; it runs as part of `tools/run_all.sh`.

The macro session was produced with the Macro category enabled (see
`PROTOCOL.md`, section 4): enable `macro/macroGrp.ini` (`macGrpNum=1`,
`0=ShortcutMacro`), copy `ShortcutMacro.dat` into the skin's `macro/` directory,
then assign Cut, Copy, Paste, Boss and SelectAll to keys 1..5 with
`tools/app_capture.sh` and save the log as `captures/macro5.jsonl`.

The script fails loudly when a source artifact is missing. It does not invent
data: the fixtures are a filtered copy of the recorded session and of the
factory configuration.
