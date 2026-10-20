// WebHID driver for the MKESPN K815 (report ID 7, 16-byte feature reports).
// Protocol reference: ../PROTOCOL.md

export const VID = 0x30fa;
export const PID = 0x2350;
export const WRITE_USAGE_PAGE = 0xff01;

// Slot table: which byte of the 160-byte keymap belongs to which physical key.
// keymap index = 8 * column + row (see PROTOCOL.md).
export const SLOTS = [
  { id: "k1", label: "Key 1", idx: 8 },
  { id: "k2", label: "Key 2", idx: 16 },
  { id: "k3", label: "Key 3", idx: 24 },
  { id: "k4", label: "Key 4", idx: 32 },
  { id: "k5", label: "Key 5", idx: 9 },
  { id: "k6", label: "Key 6", idx: 17 },
  { id: "k7", label: "Key 7", idx: 25 },
  { id: "k8", label: "Key 8", idx: 33 },
  { id: "enc1", label: "Encoder A", idx: 41 },
  { id: "enc2", label: "Encoder B", idx: 49 },
];

// Common HID keyboard usages for the dropdowns. 0xff = "no function".
export const USAGES = [
  ["No function", 0xff],
  ["A", 0x04], ["B", 0x05], ["C", 0x06], ["D", 0x07], ["E", 0x08], ["F", 0x09],
  ["G", 0x0a], ["H", 0x0b], ["I", 0x0c], ["J", 0x0d], ["K", 0x0e], ["L", 0x0f],
  ["M", 0x10], ["N", 0x11], ["O", 0x12], ["P", 0x13], ["Q", 0x14], ["R", 0x15],
  ["S", 0x16], ["T", 0x17], ["U", 0x18], ["V", 0x19], ["W", 0x1a], ["X", 0x1b],
  ["Y", 0x1c], ["Z", 0x1d],
  ["1", 0x1e], ["2", 0x1f], ["3", 0x20], ["4", 0x21], ["5", 0x22], ["6", 0x23],
  ["7", 0x24], ["8", 0x25], ["9", 0x26], ["0", 0x27],
  ["Enter", 0x28], ["Escape", 0x29], ["Backspace", 0x2a], ["Tab", 0x2b],
  ["Space", 0x2c], ["-", 0x2d], ["=", 0x2e], ["[", 0x2f], ["]", 0x30], ["\\", 0x31],
  [";", 0x33], ["'", 0x34], ["`", 0x35], [",", 0x36], [".", 0x37], ["/", 0x38],
  ["CapsLock", 0x39],
  ["F1", 0x3a], ["F2", 0x3b], ["F3", 0x3c], ["F4", 0x3d], ["F5", 0x3e], ["F6", 0x3f],
  ["F7", 0x40], ["F8", 0x41], ["F9", 0x42], ["F10", 0x43], ["F11", 0x44], ["F12", 0x45],
  ["PrintScreen", 0x46], ["ScrollLock", 0x47], ["Pause", 0x48], ["Insert", 0x49],
  ["Home", 0x4a], ["PageUp", 0x4b], ["Delete", 0x4c], ["End", 0x4d], ["PageDown", 0x4e],
  ["Right", 0x4f], ["Left", 0x50], ["Down", 0x51], ["Up", 0x52],
  ["NumLock", 0x53], ["KP /", 0x54], ["KP *", 0x55], ["KP -", 0x56], ["KP +", 0x57],
  ["KP Enter", 0x58], ["KP 1", 0x59], ["KP 2", 0x5a], ["KP 3", 0x5b], ["KP 4", 0x5c],
  ["KP 5", 0x5d], ["KP 6", 0x5e], ["KP 7", 0x5f], ["KP 8", 0x60], ["KP 9", 0x61],
  ["KP 0", 0x62], ["KP .", 0x63],
  ["F13", 0x68], ["F14", 0x69], ["F15", 0x6a], ["F16", 0x6b], ["F17", 0x6c], ["F18", 0x6d],
  ["F19", 0x6e], ["F20", 0x6f], ["F21", 0x70], ["F22", 0x71], ["F23", 0x72], ["F24", 0x73],
  ["LeftCtrl", 0xe0], ["LeftShift", 0xe1], ["LeftAlt", 0xe2], ["LeftGUI", 0xe3],
  ["RightCtrl", 0xe4], ["RightShift", 0xe5], ["RightAlt", 0xe6], ["RightGUI", 0xe7],
];

export function decodeBlob(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function patchKeymap(frames, keymap, session) {
  let n = 0;
  for (const [frameIdx, kmOff] of session.keymapA) {
    frames[frameIdx * 16 + 6] = keymap[kmOff]; n++;
  }
  for (const [frameIdx, kmOff] of session.keymapB) {
    frames[frameIdx * 16 + 6] = keymap[kmOff]; n++;
  }
  return n;
}

// Phase-B byte 0x0001 holds the four option switches (see PROTOCOL.md):
// bit 0 = BALLKEYSLOCK, bit 2 = BWINLOCK, bit 6 = BALTF4, bit 7 = KEYSMODE.
export const SWITCH_BITS = {
  allKeysLock: 0x01,
  winLock: 0x04,
  altF4: 0x40,
  keyMode: 0x80,
};

// Patch the switches byte into a captured frame stream. `sw` has the four
// boolean fields from SWITCH_BITS; the remaining bits are forced to zero, as in
// the captured session.
export function patchSwitches(frames, sw, session) {
  const i = session.ledTargets[0x01];
  if (i === undefined) return 0;
  const o = i * 16 + 6;
  let nv = 0;
  for (const [name, bit] of Object.entries(SWITCH_BITS)) if (sw[name]) nv |= bit;
  if (frames[o] === nv) return 0;
  frames[o] = nv;
  return 1;
}

// Media functions for the two encoder directions: the second number of
// `USCROLL0` / `USCROLL1` in [Config1] selects one of these Consumer usages
// (page 0x0C). Measured by differential sweep, see PROTOCOL.md.
// Fields: [label, number in config.ini, usage].
export const ENCODER_FUNCTIONS = [
  ["Play/Pause", 0, 0x00cd],
  ["Volume decrement", 1, 0x00ea],
  ["Volume increment", 2, 0x00e9],
  ["Scan next track", 3, 0x00b5],
  ["Scan previous track", 4, 0x00b6],
  ["Consumer control configuration", 5, 0x0183],
  ["Email reader", 6, 0x018a],
  ["AC forward", 7, 0x0225],
  ["AC stop", 8, 0x0226],
  ["AC back", 9, 0x0224],
  ["AC refresh", 10, 0x0227],
  ["AC search", 11, 0x0221],
  ["Mute", 12, 0x00e2],
  ["Browser (local machine)", 13, 0x0194],
  ["Stop", 14, 0x00b7],
  ["Calculator", 15, 0x0192],
  ["AC home", 16, 0x0223],
];

export const encoderNumberOf = (usage) =>
  (ENCODER_FUNCTIONS.find((f) => f[2] === usage) || [null, -1])[1];

// Patch the two encoder directions. `enc` maps "enc0"/"enc1" to the config
// number. The usage is written into both copies inside the 16-byte record; for
// function 0 the app zeroes the second copy, so we reproduce that exactly.
export function patchEncoder(frames, enc, session) {
  let n = 0;
  for (const [name, addr] of [["enc0", 0x016e], ["enc1", 0x017e]]) {
    const fn = ENCODER_FUNCTIONS.find((f) => f[1] === enc[name]);
    if (!fn) continue;
    const usage = fn[2];
    const second = fn[1] === 0 ? 0 : usage;
    for (const [base, value] of [[addr, usage], [addr + 6, second]]) {
      for (let b = 0; b < 2; b++) {
        const i = session.encTargets[base + b];
        if (i === undefined) continue;
        const o = i * 16 + 6;
        const nv = (value >> (8 * b)) & 0xff;
        if (frames[o] !== nv) { frames[o] = nv; n++; }
      }
    }
  }
  return n;
}

// LED palette + effect live in the phase-B header (see PROTOCOL.md).
export const LED_LAYOUT = {
  paletteBase: 0x02,
  colours: 7,
  react: { addr: 0x10, mask: 0x0f },
  speed: { addr: 0x19, mask: 0x07 },
  dir: { addr: 0x19, mask: 0xc0, shift: 6 },
  mode: { addr: 0x1a, mask: 0x0f },
  single: { addr: 0x1a, mask: 0x10 },
  sym: { addr: 0x1a, mask: 0x80 },
  rgbEn: { addr: 0x1b, mask: 0x7f },
};

// Patch the LED palette and effect fields into a captured frame stream.
// `led` = { palette: [[r,g,b]...], mode, speed, direction, symmetry, single,
//           react, rgbEn } with 4-bit colour channels.
export function patchLed(frames, led, session) {
  const at = new Map(Object.entries(session.ledTargets).map(([a, i]) => [+a, i]));
  let n = 0;
  const put = (addr, val) => {
    const i = at.get(addr);
    if (i === undefined) return;
    const o = i * 16 + 6;
    if (frames[o] !== val) { frames[o] = val; n++; }
  };
  const bits = (addr, mask, val) => {
    const i = at.get(addr);
    if (i === undefined) return;
    const o = i * 16 + 6;
    const nv = (frames[o] & ~mask & 0xff) | (val & mask);
    if (frames[o] !== nv) { frames[o] = nv; n++; }
  };
  (led.palette || []).forEach((rgb, k) => {
    if (!rgb) return;
    const [r, g, b] = rgb.map((v) => (v | 0) & 0x0f);
    const a0 = LED_LAYOUT.paletteBase + 2 * k;
    put(a0, (r << 4) | b);
    bits(a0 + 1, 0x0f, g);
  });
  const L = LED_LAYOUT;
  if (led.mode != null) bits(L.mode.addr, L.mode.mask, led.mode);
  if (led.speed != null) bits(L.speed.addr, L.speed.mask, led.speed);
  if (led.direction != null) bits(L.dir.addr, L.dir.mask, (led.direction & 3) << L.dir.shift);
  if (led.symmetry != null) bits(L.sym.addr, L.sym.mask, led.symmetry ? 0x80 : 0);
  if (led.single != null) bits(L.single.addr, L.single.mask, led.single ? 0x10 : 0);
  if (led.react != null) bits(L.react.addr, L.react.mask, led.react);
  if (led.rgbEn != null) bits(L.rgbEn.addr, L.rgbEn.mask, led.rgbEn);
  return n;
}

export function keymapFromIni(text, base) {
  const km = Uint8Array.from(base || new Uint8Array(160));
  const re = /^\s*(\d+)_(\d+)\s*=\s*(-?\d+)/;
  let seen = 0;
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(re);
    if (!m) continue;
    const r = +m[1], c = +m[2], v = (+m[3]) & 0xff;
    if (r < 8 && c < 20) { km[8 * c + r] = v; seen++; }
  }
  return { keymap: km, seen };
}

export function isWriteInterface(device) {
  return (device.collections || []).some(
    (c) => c.usagePage === WRITE_USAGE_PAGE);
}

export async function pickDevice() {
  const devices = await navigator.hid.requestDevice({
    filters: [{ vendorId: VID, productId: PID }],
  });
  if (!devices.length) return null;
  return devices.find(isWriteInterface) || devices[0];
}
