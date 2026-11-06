# Changelog

## Unreleased

### Fixed

- Mode 4 (Neon) preview now shows the global colour cycle: one colour for all
  keys, advancing through the palette, instead of a per-key rainbow.

## v1.0.0 — 2026-11-06

First public release.

### Added

- WebHID single-page configurator for the 8 keys, encoder, switches and LED
  palette/effects, with a live preview drawn from the layout schematic.
- `k815ctl` command-line tool over `hidraw` for Linux, with dump, restore,
  preset import/export and read-back verification.
- uhid emulator, generated session templates, offline self-tests and a
  Chromium end-to-end test.
- Documentation: hardware identity (IST83025B/K201), known limitations,
  protocol, RE notes and key assignment notes.

### Fixed

- The keyboard stops sending input after a memory read; the page now sends the
  closing `INIT` + `COMMIT` handshake. A USB bus reset is not a power cycle.
- `react` and `rgbEn` were read one byte early from the phase-B header.
- Presets decoded from a `0x00E0` device dump read the staging copy instead of
  the active bank; page dumps now carry `configBase`/`recordBase`, and the CLI
  accepts `--config-base`/`--record-base`.
- The full dump image is no longer truncated to `0x018F`, so the active keymap
  and encoder records above it decode correctly.
- Faster Read/Verify: verification reads each byte once and re-reads only
  mismatches; `⋯ → Fast read` optionally halves plain reads.
- Encoder/LED colour preview: pure palette colours, values 0-255 in 16 steps,
  picker and quick presets.

### Known limitations

- 100 Hz polling (`bInterval = 10 ms`), encoder detents can be lost.
- No per-key RGB; OpenRGB is only realistic for the global palette/effect.
- Config I/O is one byte per three USB transactions, about 20 ms per byte.
- Applying a new keymap may need a physical replug on this firmware.
- Macro editing (text/custom groups) and media functions on the 8 keys are not
  implemented. See `docs/KNOWN-LIMITATIONS.md`.
