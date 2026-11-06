// MKESPN K815 — configuration interface.
// Protocol and WebHID: ./k815.js, session template: ./k815_session.js

import { SESSION, LED_EFFECTS } from "./k815_session.js?v=20261105-u18";
import {
  SLOTS, USAGES, decodeBlob, patchKeymap, patchLed, patchSwitches, SWITCH_BITS,
  patchEncoderRotation, ENCODER_FUNCTIONS, encoderNumberOf, keymapFromIni,
  readReport, featurePayloadBytes, chooseDevice, describeDevice, pickDevice,
  pickStable, WRITE_USAGE_PAGE,
} from "./k815.js?v=20261105-u18";

const $ = (id) => document.getElementById(id);
const make = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const hex = (v) => (v & 0xff).toString(16).padStart(2, "0").toUpperCase();
const hex16 = (v) => (v & 0xffff).toString(16).padStart(4, "0").toUpperCase();

// Send one 16-byte session frame as a feature report.  WebHID requires the
// payload to match the report length declared by the device: seven bytes for
// the real keyboard, fifteen for the uhid emulator used in the tests.
async function sendFrame(dev, frame) {
  const declared = featurePayloadBytes(dev, frame[0]);
  const len = Math.min(frame.length - 1, declared ?? frame.length - 1);
  return dev.sendFeatureReport(frame[0], frame.subarray(1, 1 + len));
}

// A bare 07 18 <sub> command (init/commit/close), truncated by sendFrame.
function hostReport(sub, reportId = SESSION.reportId) {
  const f = new Uint8Array(16);
  f[0] = reportId;
  f[1] = 0x18;
  f[2] = sub;
  return f;
}

// --------------------------------------------------------------------------- //
// state
// --------------------------------------------------------------------------- //

const defaultKeymap = Uint8Array.from(SESSION.defaultKeymap);
const keymap = Uint8Array.from(defaultKeymap);
const frames = decodeBlob(SESSION.blob);
const baseline = Uint8Array.from(frames);

const D = SESSION.defaultLed || {};
const LED_KEYS = ["mode", "speed", "direction", "symmetry", "single", "react", "rgbEn"];
const defaultLed = {
  mode: D.mode ?? 0,
  speed: D.speed ?? 0,
  direction: D.direction ?? 0,
  symmetry: D.symmetry ?? 0,
  single: D.single ?? 0,
  react: D.react ?? 0,
  rgbEn: D.rgb_en ?? 0x7f,
};
const led = {
  ...defaultLed,
  palette: (D.palette || [[0, 0, 0]]).map((c) => [c[0], c[1], c[2]]),
};
while (led.palette.length < 7) led.palette.push([0, 0, 0]);

const NAMES = new Map(USAGES.map(([name, code]) => [code, name]));

// Service byte of phase B (address 0x0001): four switches from [Config1].
const defaultSwitches = SESSION.defaultSwitches || 0;
const switches = {
  altF4: !!(defaultSwitches & SWITCH_BITS.altF4),
  winLock: !!(defaultSwitches & SWITCH_BITS.winLock),
  allKeysLock: !!(defaultSwitches & SWITCH_BITS.allKeysLock),
  keyMode: !!(defaultSwitches & SWITCH_BITS.keyMode),
};

function switchesByte() {
  let v = 0;
  for (const [name, bit] of Object.entries(SWITCH_BITS)) if (switches[name]) v |= bit;
  return v;
}

// Encoder media functions: the second number of USCROLL0/USCROLL1. -1 means a
// code that is not in the measured table (then the field is left untouched).
const DENC = SESSION.defaultEncoder || {};
const defaultEncoder = {
  enc0: encoderNumberOf(DENC.enc0),
  enc1: encoderNumberOf(DENC.enc1),
};
const encoder = { ...defaultEncoder };

// Rotation can alternatively send a single keyboard key per direction.  This
// is a single-key macro record, the only mode that makes the encoder send an
// ordinary key (see PROTOCOL.md).
const DENCK = SESSION.defaultEncoderKeys || {};
const defaultEncoderKeys = {
  enc0: Number.isInteger(DENCK.enc0) ? DENCK.enc0 : 0x56,
  enc1: Number.isInteger(DENCK.enc1) ? DENCK.enc1 : 0x57,
};
const encoderKeys = { ...defaultEncoderKeys };
let encoderMode = ((DENCK.enc0 !== null && DENCK.enc0 !== undefined)
  || (DENCK.enc1 !== null && DENCK.enc1 !== undefined)) ? "keys" : "media";

// The edit counter starts from the last written state, not from the factory
// template: a successful Apply moves the "N edits not written" origin.
const dirtyKeymap = Uint8Array.from(defaultKeymap);
const dirtyLed = {
  ...defaultLed,
  palette: led.palette.map((c) => [c[0], c[1], c[2]]),
};
let dirtySwitches = defaultSwitches;
const dirtyEncoder = { ...defaultEncoder };
const dirtyEncoderKeys = { ...defaultEncoderKeys };
let dirtyEncoderMode = encoderMode;

const SLOT_META = {
  k1: { n: 1, pos: "Top row, first from the left", svg: "key-1" },
  k2: { n: 2, pos: "Top row, second from the left", svg: "key-2" },
  k3: { n: 3, pos: "Top row, third from the left", svg: "key-3" },
  k4: { n: 4, pos: "Top row, fourth from the left", svg: "key-4" },
  k5: { n: 5, pos: "Bottom row, first from the left", svg: "key-5" },
  k6: { n: 6, pos: "Bottom row, second from the left", svg: "key-6" },
  k7: { n: 7, pos: "Bottom row, third from the left", svg: "key-7" },
  k8: { n: 8, pos: "Bottom row, fourth from the left", svg: "key-8" },
  enc1: { n: 9, pos: "Encoder key A (not used by rotation)", svg: "enc-a" },
  enc2: { n: 10, pos: "Encoder press · keymap slot 49", svg: "enc-b",
          title: "Encoder press" },
};

// Only the effects the vendor UI exposes are selectable here: the other
// numbers are placeholders on this firmware (see tools/led_modes.json).
const LED_MODES = (() => {
  const known = new Map((LED_EFFECTS || []).map((e) => [e.mode, e]));
  const out = [];
  for (let m = 0; m < 16; m++) {
    const e = known.get(m);
    if (!e || e.in_ui_list === false) continue;
    out.push({ mode: m, label: `Mode ${m} · ${e.en}` });
  }
  return out;
})();

let device = null;
let slot = SLOTS[0];
let colorSel = 0;
let filter = "all";
let query = "";

// One long device operation at a time: "Write", "Dump", "Read" or null.
let busy = null;
// True while a USB reset / reconnect is in flight: keeps the hid "connect"
// event from starting an auto-read that would reset the keyboard again.
let resetting = false;
let dumpOk = false;
let lastDump = null;
// True once the page has successfully read the device config (Connect, Read,
// Dump). While it is false the page still shows the template values, so Apply
// asks for confirmation before writing them.
let configOk = false;

// The device reports its config base in the startup answer.  The session blob
// carries it (0x00E0 on a burned unit, 0x0001 on the emulator).
const SESSION_BASE = SESSION.base ?? 0x0001;
const SESSION_RECORD = SESSION.recordBase ?? 0x0168;

// Dump covers every address a session can write.
const DUMP_ADDR = 0x0000;
const DUMP_LEN = Math.max(0x0190, SESSION_RECORD + 0x20);

// Read back after a write. These ranges are the final (last-write) values:
// switches + LED header, the phase-B keymap, and the encoder records.
const VERIFY_RANGES = [
  [SESSION_BASE, SESSION_BASE + 0x20],
  [SESSION_BASE + 0x48, SESSION_BASE + 0x48 + 0x9f],
  // 22 five-byte function-table entries (logical 0xF9 -> base+0xF8)
  [SESSION_BASE + 0xf8, SESSION_BASE + 0xf8 + 0x6d],
  [SESSION_RECORD, SESSION_RECORD + 0x1f],
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --------------------------------------------------------------------------- //
// log
// --------------------------------------------------------------------------- //

function log(msg, cls = "") {
  const box = $("log");
  const line = make("div", cls, msg);
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
  $("logTail").textContent = msg;
}

function toast(msg, cls) {
  log(msg, cls);
  if (cls === "err" || cls === "warn") openLog();
}

// The log is an overlay pinned above the status bar; opening it never moves
// the rest of the layout.  The panel stays in the DOM while closed, so
// `#log` keeps its content and `log()` can keep appending lines.
function openLog() {
  $("logCard").hidden = false;
  $("btnLog").setAttribute("aria-expanded", "true");
  const box = $("log");
  box.scrollTop = box.scrollHeight;
}

function closeLog() {
  $("logCard").hidden = true;
  $("btnLog").setAttribute("aria-expanded", "false");
}

function toggleLog() {
  if ($("logCard").hidden) openLog();
  else closeLog();
}

function setStatus(text, cls = "") {
  const el = $("opStatus");
  el.hidden = !text;
  el.textContent = text || "";
  el.title = text || "";
  el.className = "op-status" + (cls ? " " + cls : "");
}

function showProgress(label, done, total) {
  $("progText").textContent = `${label} ${done} / ${total}`;
  $("progBar").style.width = total ? `${Math.max(0, Math.min(100, (done / total) * 100))}%` : "0";
}

// One writer at a time: during a write, import, Dump, Read and Check are out.
function syncControls() {
  const connected = !!device;
  const idle = !busy;
  $("btnRead").disabled = !connected || !idle;
  $("btnDump").disabled = !connected || !idle;
  $("btnApply").disabled = !connected || !idle;
  $("btnCheck").disabled = !idle;
  $("btnImport").disabled = !idle;
  $("btnIni").disabled = !idle;
  $("extraDelay").disabled = !idle;
  for (const id of ["encMode", "encFn0", "encFn1",
                    "encKey0", "encKey1"]) {
    $(id).disabled = !idle;
  }
}

function beginOp(label, total) {
  busy = label;
  syncControls();
  $("progress").hidden = false;
  showProgress(label, 0, total);
}

function endOp() {
  busy = null;
  syncControls();
  $("progress").hidden = true;
}

// --------------------------------------------------------------------------- //
// device schematic
// --------------------------------------------------------------------------- //

const KEY_GROUPS = new Map();

// Arrow keys move between the controls of the schematic, in its visual order.
const KEY_NAV = {
  k1: { ArrowRight: "k2", ArrowDown: "k5" },
  k2: { ArrowLeft: "k1", ArrowRight: "k3", ArrowDown: "k6" },
  k3: { ArrowLeft: "k2", ArrowRight: "k4", ArrowDown: "k7" },
  k4: { ArrowLeft: "k3", ArrowRight: "enc2", ArrowDown: "k8" },
  k5: { ArrowRight: "k6", ArrowUp: "k1" },
  k6: { ArrowLeft: "k5", ArrowRight: "k7", ArrowUp: "k2" },
  k7: { ArrowLeft: "k6", ArrowRight: "k8", ArrowUp: "k3" },
  k8: { ArrowLeft: "k7", ArrowRight: "enc2", ArrowUp: "k4" },
  enc2: { ArrowLeft: "k8", ArrowUp: "k4", ArrowDown: "k8" },
};

function initSchematic() {
  for (const s of SLOTS) {
    const g = document.getElementById(SLOT_META[s.id].svg);
    if (!g) continue;
    KEY_GROUPS.set(s.id, g);
    g.addEventListener("click", () => selectSlot(s.id));
    g.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectSlot(s.id);
        return;
      }
      const next = KEY_NAV[s.id] && KEY_NAV[s.id][e.key];
      if (!next) return;
      e.preventDefault();
      const target = KEY_GROUPS.get(next);
      if (target) { target.focus(); selectSlot(next); }
    });
  }
}

function selectSlot(id) {
  slot = SLOTS.find((s) => s.id === id) || SLOTS[0];
  for (const [sid, g] of KEY_GROUPS) g.classList.toggle("sel", sid === slot.id);
  renderInspector();
  writeUiState({ slot: slot.id });
}

function refreshSchematic() {
  for (const s of SLOTS) {
    const g = KEY_GROUPS.get(s.id);
    if (!g) continue;
    const v = keymap[s.idx];
    const name = v === 0xff ? "—" : (NAMES.get(v) || `0x${hex(v)}`);
    const nameEl = g.querySelector(".kname");
    const hexEl = g.querySelector(".khex, .phex");
    if (nameEl) {
      nameEl.textContent = name;
      nameEl.classList.toggle("sm", name.length > 6);
    }
    if (hexEl) hexEl.textContent = "0x" + hex(v);
    g.classList.toggle("mod", v !== dirtyKeymap[s.idx]);
  }
}

// --------------------------------------------------------------------------- //
// inspector
// --------------------------------------------------------------------------- //

function chipOf(code) {
  if (code >= 0x04 && code <= 0x1d) return "letters";
  if (code >= 0x1e && code <= 0x27) return "digits";
  if (code >= 0x53 && code <= 0x63) return "numpad";
  if ((code >= 0x3a && code <= 0x48) || (code >= 0x68 && code <= 0x73)) return "fkeys";
  return "other";
}

function updateInspectorBadge() {
  const m = SLOT_META[slot.id];
  const changed = keymap[slot.idx] !== dirtyKeymap[slot.idx];
  const badge = $("inspBadge");
  badge.textContent = `#${m.n}`;
  badge.hidden = !!m.title && !changed;
  badge.classList.toggle("mod", changed);
  badge.title = changed ? "Changed in this session" : "";
}

function renderInspector() {
  const m = SLOT_META[slot.id];
  const v = keymap[slot.idx];

  $("inspTitle").textContent = m.title ?? `Key ${m.n}`;
  $("inspSub").textContent = m.pos;
  updateInspectorBadge();
  $("codeInput").value = "0x" + hex(v);

  $("prevCap").textContent = m.n;
  $("prevName").textContent = v === 0xff ? "No function" : (NAMES.get(v) || `Code 0x${hex(v)}`);
  $("prevDesc").textContent = v === 0xff
    ? "The key sends nothing"
    : `Pressing it sends 0x${hex(v)} to the computer`;

  renderList();
}

function renderList() {
  const list = $("usageList");
  const note = $("emptyNote");
  list.textContent = "";
  note.hidden = true;
  list.hidden = false;

  const current = keymap[slot.idx];
  const q = query.trim().toLowerCase();
  let shown = 0;
  let active = null;

  for (const [name, code] of USAGES) {
    if (code === 0xff) continue;
    if (filter !== "all" && chipOf(code) !== filter) continue;
    if (q) {
      const hay = `${name} 0x${hex(code)} ${code}`.toLowerCase();
      if (!hay.includes(q)) continue;
    }
    const row = make("button", "usage-row");
    row.type = "button";
    if (code === current) { row.classList.add("active"); active = row; }
    row.append(make("span", "u-name", name), make("span", "u-code", "0x" + hex(code)));
    row.addEventListener("click", () => assign(code));
    list.appendChild(row);
    shown++;
  }

  if (!shown) list.appendChild(make("p", "empty", "Nothing found. Refine the search."));
  else if (active) list.scrollTop = Math.max(0, active.offsetTop - list.clientHeight / 2 + 20);
}

function assign(v) {
  keymap[slot.idx] = v & 0xff;
  renderInspector();
  refreshSchematic();
  if (slot.idx === 49) refreshEncoderControls();
  updateDirty();
}

// --------------------------------------------------------------------------- //
// lighting
// --------------------------------------------------------------------------- //

const rgbCss = (rgb) => `rgb(${rgb[0] * 17}, ${rgb[1] * 17}, ${rgb[2] * 17})`;
const activeColor = (i) => ((led.rgbEn >> i) & 1) === 1;

// The firmware stores palette nibbles inverted: 0 is full brightness and 15 is
// off. Presets and the CLI keep that raw wire value (so a preset still moves
// between the browser and the console unchanged), while the page shows the
// physical intensity: 0/0/0 is black, 15/15/15 is white.
const invertRgb = (rgb) => (rgb || [0, 0, 0]).map((v) => 15 - (v & 0x0f));
const uiPalette = (i) => invertRgb(led.palette[i]);

// Approximate live preview of the global LED effect on all eight keys.
const LED_PREVIEW_KEYS = [];
let ledPreviewInfo = "";
let ledPreviewStart = 0;
let ledPreviewRaf = null;

const clamp01 = (v) => Math.max(0, Math.min(1, v));

function activePalette() {
  const out = [];
  for (let i = 0; i < 7; i++) if (activeColor(i)) out.push(uiPalette(i));
  return out.length ? out : [[0, 0, 0]];
}

// The firmware has seven discrete palette colours; effects move them around
// instead of blending them, so the preview shows a palette entry per key.
function paletteAt(pos) {
  const p = activePalette();
  const i = ((Math.floor(pos) % p.length) + p.length) % p.length;
  return p[i];
}

function hsvRgb(h, s, v) {
  h = ((h % 1) + 1) % 1;
  const i = Math.floor(h * 6);
  const f = h * 6 - i;
  const p = v * (1 - s);
  const q = v * (1 - f * s);
  const t = v * (1 - (1 - f) * s);
  const [r, g, b] = [[v, t, p], [q, v, p], [p, v, t],
                     [p, q, v], [t, p, v], [v, p, q]][i % 6];
  return [r * 15, g * 15, b * 15];
}

function buildLedPreview() {
  const wrap = $("ledPreviewKeys");
  wrap.textContent = "";
  LED_PREVIEW_KEYS.length = 0;
  const src = document.querySelector("#viewKeys .schematic");
  if (!src) return;

  // The preview is the Layout drawing itself. Prefix every id so the clone and
  // the original never collide, and keep the fill/filter references working.
  const svg = src.cloneNode(true);
  svg.removeAttribute("role");
  svg.removeAttribute("aria-label");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("lp-schematic");

  const ids = new Map();
  svg.querySelectorAll("[id]").forEach((el) => {
    const next = "lp-" + el.id;
    ids.set(el.id, next);
    el.id = next;
  });
  svg.querySelectorAll("*").forEach((el) => {
    for (const attr of [...el.attributes]) {
      if (attr.value.includes("url(#")) {
        attr.value = attr.value.replace(/url\(#([^)]+)\)/g,
          (m, id) => `url(#${ids.get(id) || id})`);
      }
    }
  });

  svg.querySelectorAll(".key").forEach((g) => {
    const isKey = (g.getAttribute("data-slot") || "").startsWith("k");
    g.removeAttribute("role");
    g.removeAttribute("tabindex");
    g.removeAttribute("aria-label");
    g.removeAttribute("data-slot");
    if (isKey) LED_PREVIEW_KEYS.push(g);
  });

  wrap.appendChild(svg);
  ledPreviewStart = performance.now();
}

const prefersReducedMotion = () => window.matchMedia
  && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function paintLedPreview(now) {
  if (!LED_PREVIEW_KEYS.length) return;
  const t = (now - ledPreviewStart) / 1000;
  const period = 4.2 / (1 + led.speed * 0.55);
  const dir = led.direction | 0;
  const sign = dir % 2 ? -1 : 1;
  const phase = sign * (t / period) * Math.PI * 2 + dir * Math.PI / 2;
  const p = activePalette();
  const react = 0.35 + (led.react / 15) * 0.65;

  for (let i = 0; i < 8; i++) {
    const idx = led.symmetry ? Math.min(i, 7 - i) : i;
    let color = led.single ? p[0] : p[idx % p.length];
    let alpha = 1;
    switch (led.mode) {
      case 0: // Backlight Off
        alpha = 0;
        color = p[0];
        break;
      case 1: // Breathing
        alpha = 0.12 + 0.88 * (0.5 + 0.5 * Math.sin(phase));
        break;
      case 2: // Constant
        alpha = 1;
        break;
      case 3: // Response
        alpha = 0.15 + 0.85 * react
          * Math.pow(0.5 + 0.5 * Math.sin(phase * 2 + idx * 0.9), 2);
        break;
      case 4: // Neon
        color = hsvRgb(t * 0.12 + idx * 0.07, 1, 1);
        break;
      case 5: // Ambilight
        color = paletteAt(idx * 0.7 + t * 0.35);
        alpha = 0.55 + 0.45 * Math.pow(0.5 + 0.5 * Math.sin(phase + idx * 0.45), 2);
        break;
      case 6: { // Comet Tail
        const pos = (((phase / (Math.PI * 2)) * 8) % 8 + 8) % 8;
        alpha = Math.pow(Math.max(0, 1 - Math.abs(idx - pos) / 3), 2);
        color = p[0];
        break;
      }
      case 7: { // Single Water
        const pos = (((phase / (Math.PI * 2)) * 8) % 8 + 8) % 8;
        const d = Math.min(Math.abs(idx - pos), 8 - Math.abs(idx - pos));
        alpha = Math.max(0, 1 - d / 1.15);
        color = paletteAt(pos * 0.8);
        break;
      }
      case 8: // Flow Water
        alpha = 0.15 + 0.85
          * Math.pow(0.5 + 0.5 * Math.sin(phase - idx * 0.8), 2);
        break;
      case 9: // Flashing
        alpha = Math.sin(phase) > 0 ? 1 : 0.04;
        break;
      case 10: // Monochromatic Wave
        alpha = 0.15 + 0.85
          * Math.pow(0.5 + 0.5 * Math.sin(phase - idx * 0.8), 2);
        color = p[0];
        break;
      case 11: { // Partition: zones measured on the real keyboard (photo)
        // 1,2,8 magenta; 3,4 salmon "light pink"; 5,6,7 orange.
        const zone = [2, 2, 4, 4, 1, 1, 1, 2];
        const pi = zone[idx] % 7;
        color = activeColor(pi) ? uiPalette(pi) : p[idx % p.length];
        alpha = 1;
        break;
      }
      case 12: // Combination
        color = paletteAt(idx * 0.8 + t * 0.4);
        alpha = 0.25 + 0.75
          * Math.pow(0.5 + 0.5 * Math.sin(phase - idx * 0.6), 2);
        break;
      default:
        alpha = 1;
    }

    alpha = clamp01(alpha);
    const key = LED_PREVIEW_KEYS[i];
    key.style.setProperty("--lp-led", rgbCss(color));
    key.style.setProperty("--lp-alpha", String(alpha));
    const lum = (color[0] * 0.299 + color[1] * 0.587 + color[2] * 0.114) * alpha;
    key.style.setProperty("--lp-ink", lum > 7.5 ? "#0B0C0E" : "#ECEAE6");
  }

  const mode = LED_MODES.find((m) => m.mode === led.mode);
  const modeName = mode ? mode.label.replace(/^Mode \d+ · /, "") : `Mode ${led.mode}`;
  const info = `${mode ? mode.label : `Mode ${led.mode}`} · speed ${led.speed} · direction ${led.direction}`;
  if (info !== ledPreviewInfo) {
    ledPreviewInfo = info;
    $("ledPreviewInfo").textContent = info;
    $("lpStatMode").textContent = modeName;
    $("lpStatSpeed").textContent = String(led.speed);
    $("lpStatDir").textContent = String(led.direction);
    $("lpStatReact").textContent = String(led.react);
  }
}

function animateLedPreview(now) {
  paintLedPreview(now);
  ledPreviewRaf = requestAnimationFrame(animateLedPreview);
}

// The preview only runs while the Lighting tab is visible, and never for users
// who asked the system to reduce motion.
function startLedPreview() {
  if (ledPreviewRaf !== null || !LED_PREVIEW_KEYS.length) return;
  if (prefersReducedMotion()) { paintLedPreview(performance.now()); return; }
  ledPreviewStart = performance.now();
  ledPreviewRaf = requestAnimationFrame(animateLedPreview);
}

function stopLedPreview() {
  if (ledPreviewRaf === null) return;
  cancelAnimationFrame(ledPreviewRaf);
  ledPreviewRaf = null;
}

function reflowLedPreview() {
  if (!prefersReducedMotion() || ledPreviewRaf !== null) return;
  if (LED_PREVIEW_KEYS.length) paintLedPreview(performance.now());
}

function buildSwatches() {
  const wrap = $("swatches");
  wrap.textContent = "";
  led.palette.forEach((_, i) => {
    const b = make("button", "swatch");
    b.type = "button";
    const rgb = uiPalette(i);
    b.title = `Colour ${i + 1} · R ${to8(rgb[0])} G ${to8(rgb[1])} B ${to8(rgb[2])}`;
    b.style.setProperty("--c", rgbCss(rgb));
    b.classList.toggle("sel", i === colorSel);

    const chip = make("span", "sw-chip");
    const meta = make("span", "sw-meta");
    const dot = make("span", "on");
    if (activeColor(i)) dot.classList.add("active");
    meta.append(make("span", null, String(i + 1)), dot);

    b.append(chip, meta);
    b.addEventListener("click", () => {
      colorSel = i;
      buildSwatches();
      renderColorEditor();
    });
    wrap.appendChild(b);
  });

  let mask = 0;
  for (let i = 0; i < 7; i++) if (activeColor(i)) mask |= 1 << i;
  const n = led.palette.filter((_, i) => activeColor(i)).length;
  $("maskNote").innerHTML = `${n === 7 ? "All 7 colours are active" : `Active colours: ${n}`}, `
    + `mask <code>0x${hex(mask)}</code>`;
}

const to8 = (v) => (v & 0x0f) * 17;
const from8 = (v) => Math.round(Math.max(0, Math.min(255, v | 0)) / 17);
const rgbHex = (rgb) => "#" + rgb.map((v) => to8(v).toString(16).padStart(2, "0")).join("");
const hexRgb = (hex) => {
  const n = parseInt(String(hex).slice(1), 16) || 0;
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
};

const COLOUR_PRESETS = [
  ["White", [15, 15, 15]], ["Red", [15, 0, 0]], ["Green", [0, 15, 0]],
  ["Blue", [0, 0, 15]], ["Yellow", [15, 15, 0]], ["Cyan", [0, 15, 15]],
  ["Magenta", [15, 0, 15]], ["Off", [0, 0, 0]],
];

function renderColorEditor(keepPick = false) {
  const rgb = uiPalette(colorSel);
  $("ceTitle").textContent = `Colour ${colorSel + 1}`;
  $("ceRgb").textContent = `R ${to8(rgb[0])} · G ${to8(rgb[1])} · B ${to8(rgb[2])}`;
  $("ceSwatch").style.background = rgbCss(rgb);
  if (!keepPick) $("cePick").value = rgbHex(rgb);
  [["ceR", 0], ["ceG", 1], ["ceB", 2]].forEach(([id, i]) => {
    $(id).value = String(to8(rgb[i]));
    $(id + "v").textContent = String(to8(rgb[i]));
  });
  $("ceUse").checked = activeColor(colorSel);
}

function buildColorPresets() {
  const wrap = $("cePresets");
  wrap.textContent = "";
  for (const [name, ui] of COLOUR_PRESETS) {
    const b = make("button", "ce-preset");
    b.type = "button";
    b.title = name;
    b.setAttribute("aria-label", name);
    b.style.background = rgbCss(ui);
    b.addEventListener("click", () => {
      led.palette[colorSel] = ui.map((v) => 15 - v);
      renderColorEditor();
      buildSwatches();
      updateDirty();
    });
    wrap.appendChild(b);
  }
}

function ensureLedModeOption() {
  const sel = $("ledMode");
  if ([...sel.options].some((o) => o.value === String(led.mode))) return;
  const e = (LED_EFFECTS || []).find((x) => x.mode === led.mode);
  const o = document.createElement("option");
  o.value = String(led.mode);
  o.textContent = `Mode ${led.mode} · ${e ? e.en : "unknown"} (current)`;
  sel.appendChild(o);
}

function buildLedControls() {
  const sel = $("ledMode");
  sel.textContent = "";
  for (const m of LED_MODES) {
    const o = document.createElement("option");
    o.value = String(m.mode);
    o.textContent = m.label;
    sel.appendChild(o);
  }
  // Keep a mode the device already holds visible even if the vendor UI hides it.
  ensureLedModeOption();
  sel.value = String(led.mode);
  sel.addEventListener("change", () => {
    led.mode = Number(sel.value) | 0;
    updateDirty();
  });

  const bindRange = (id, key) => {
    const inp = $(id);
    inp.value = String(led[key]);
    $(id + "V").textContent = String(led[key]);
    inp.addEventListener("input", () => {
      led[key] = Number(inp.value) | 0;
      $(id + "V").textContent = String(led[key]);
      updateDirty();
    });
  };
  bindRange("ledSpeed", "speed");
  bindRange("ledReact", "react");

  const dir = $("dirSeg");
  const syncDir = () => {
    for (const b of dir.children) b.classList.toggle("active", Number(b.dataset.dir) === led.direction);
  };
  dir.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    led.direction = Number(b.dataset.dir) | 0;
    syncDir();
    updateDirty();
  });
  syncDir();

  for (const [id, key] of [["ledSym", "symmetry"], ["ledSingle", "single"]]) {
    const el = $(id);
    el.checked = !!led[key];
    el.addEventListener("change", () => {
      led[key] = el.checked ? 1 : 0;
      updateDirty();
    });
  }
}

function setConfigOkUi() {
  const el = $("lpTemplate");
  if (el) el.hidden = configOk;
}

function refreshLedControls() {
  const sel = $("ledMode");
  if (sel) {
    ensureLedModeOption();
    sel.value = String(led.mode);
  }
  for (const [id, key] of [["ledSpeed", "speed"], ["ledReact", "react"]]) {
    const inp = $(id);
    if (!inp) continue;
    inp.value = String(led[key]);
    $(id + "V").textContent = String(led[key]);
  }
  const dir = $("dirSeg");
  if (dir) {
    for (const b of dir.children) {
      b.classList.toggle("active", Number(b.dataset.dir) === led.direction);
    }
  }
  $("ledSym").checked = !!led.symmetry;
  $("ledSingle").checked = !!led.single;
  renderColorEditor();
  setConfigOkUi();
}

function initLighting() {
  buildLedPreview();
  buildLedControls();
  buildSwatches();
  buildColorPresets();
  renderColorEditor();
  setConfigOkUi();

  [["ceR", 0], ["ceG", 1], ["ceB", 2]].forEach(([id, i]) => {
    $(id).addEventListener("input", () => {
      const v = Number($(id).value) | 0;
      led.palette[colorSel][i] = 15 - from8(v);
      const rgb = uiPalette(colorSel);
      $(id + "v").textContent = String(v);
      $("ceRgb").textContent = `R ${to8(rgb[0])} · G ${to8(rgb[1])} · B ${to8(rgb[2])}`;
      $("ceSwatch").style.background = rgbCss(rgb);
      $("cePick").value = rgbHex(rgb);
      const sw = $("swatches").children[colorSel];
      if (sw) sw.style.setProperty("--c", rgbCss(rgb));
      updateDirty();
    });
  });

  $("cePick").addEventListener("input", () => {
    const [r, g, b] = hexRgb($("cePick").value);
    led.palette[colorSel] = [15 - from8(r), 15 - from8(g), 15 - from8(b)];
    renderColorEditor(true);
    buildSwatches();
    updateDirty();
  });
  $("cePick").addEventListener("change", () => renderColorEditor());

  $("ceUse").addEventListener("change", () => {
    const bit = 1 << colorSel;
    led.rgbEn = $("ceUse").checked ? (led.rgbEn | bit) : (led.rgbEn & ~bit & 0xff);
    buildSwatches();
    updateDirty();
  });
}

// --------------------------------------------------------------------------- //
// switches
// --------------------------------------------------------------------------- //

const SWITCH_INPUTS = [
  ["swAltF4", "altF4", SWITCH_BITS.altF4],
  ["swWin", "winLock", SWITCH_BITS.winLock],
  ["swAllKeys", "allKeysLock", SWITCH_BITS.allKeysLock],
  ["swKeyMode", "keyMode", SWITCH_BITS.keyMode],
];

function initSwitches() {
  for (const [id, key] of SWITCH_INPUTS) {
    const el = $(id);
    el.addEventListener("change", () => {
      switches[key] = el.checked;
      refreshSwitches();
      updateDirty();
    });
  }
  refreshSwitches();
}

function refreshSwitches() {
  for (const [id, key] of SWITCH_INPUTS) $(id).checked = !!switches[key];
  $("swByte").textContent = "0x" + hex(switchesByte());
}

// --------------------------------------------------------------------------- //
// encoder
// --------------------------------------------------------------------------- //

const ENC_INPUTS = [["encFn0", "enc0"], ["encFn1", "enc1"]];
const ENC_KEY_INPUTS = [["encKey0", "enc0"], ["encKey1", "enc1"]];

// The closed <select> only has room for the function name (as in the mockup);
// the usage code stays in the option tooltip and the encoder note below.
const ENC_SHORT = new Map([
  ["Volume increment", "Volume up"],
  ["Volume decrement", "Volume down"],
  ["Scan next track", "Next track"],
  ["Scan previous track", "Prev track"],
  ["Consumer control configuration", "Consumer ctrl"],
  ["Browser (local machine)", "Browser"],
]);

function fillEncoderSelect(sel, key) {
  sel.textContent = "";
  for (const [label, num, usage] of ENCODER_FUNCTIONS) {
    const o = document.createElement("option");
    o.value = String(num);
    o.textContent = ENC_SHORT.get(label) || label;
    o.title = `${label} · 0x${hex16(usage)}`;
    sel.appendChild(o);
  }
  if (!ENCODER_FUNCTIONS.some((f) => f[1] === encoder[key])) {
    const o = document.createElement("option");
    o.value = String(encoder[key]);
    o.textContent = "unknown";
    o.title = `unknown · 0x${hex16(DENC[key] || 0)}`;
    sel.appendChild(o);
  }
  sel.value = String(encoder[key]);
  const selOpt = sel.options[sel.selectedIndex];
  sel.title = selOpt ? selOpt.title : "";
}

function fillKeySelect(sel, usage) {
  sel.textContent = "";
  for (const [label, code] of USAGES) {
    const o = document.createElement("option");
    o.value = String(code);
    o.textContent = `${label} · 0x${hex(code)}`;
    o.title = o.textContent;
    sel.appendChild(o);
  }
  if (!USAGES.some(([, code]) => code === usage)) {
    const o = document.createElement("option");
    o.value = String(usage);
    o.textContent = `unknown · 0x${hex(usage)}`;
    o.title = o.textContent;
    sel.appendChild(o);
  }
  sel.value = String(usage);
  const selOpt = sel.options[sel.selectedIndex];
  sel.title = selOpt ? selOpt.title : "";
}

function buildEncoderControls() {
  for (const [id, key] of ENC_INPUTS) {
    const sel = $(id);
    sel.addEventListener("change", () => {
      encoder[key] = Number(sel.value);
      sel.title = sel.options[sel.selectedIndex].title;
      refreshEncoderNote();
      updateDirty();
    });
  }
  for (const [id, key] of ENC_KEY_INPUTS) {
    const sel = $(id);
    sel.addEventListener("change", () => {
      encoderKeys[key] = Number(sel.value);
      sel.title = sel.options[sel.selectedIndex].title;
      refreshEncoderNote();
      updateDirty();
    });
  }
  $("encMode").addEventListener("change", () => {
    encoderMode = $("encMode").value;
    refreshEncoderControls();
    updateDirty();
  });
  refreshEncoderControls();
}

function refreshEncoderControls() {
  const keysMode = encoderMode === "keys";
  $("encMode").value = encoderMode;
  for (const [id, key] of ENC_INPUTS) {
    fillEncoderSelect($(id), key);
    $(id).hidden = keysMode;
  }
  for (const [id, key] of ENC_KEY_INPUTS) {
    fillKeySelect($(id), encoderKeys[key]);
    $(id).hidden = !keysMode;
  }
  refreshEncoderNote();
}

const encoderKeyName = (v) => (v == null || v === 0xff ? "No function" : (NAMES.get(v) || `0x${hex(v)}`));
const encoderFnName = (n) => (ENCODER_FUNCTIONS.find((f) => f[1] === n) || [null, n])[0]
  || `code ${n}`;
const encoderFnUsage = (n) => (ENCODER_FUNCTIONS.find((f) => f[1] === n) || [null, null, null])[2];

function refreshEncoderNote() {
  const keysMode = encoderMode === "keys";
  const left = keysMode
    ? `${encoderKeyName(encoderKeys.enc0)} (0x${hex(encoderKeys.enc0)})`
    : `${encoderFnName(encoder.enc0)} (0x${hex16(encoderFnUsage(encoder.enc0) ?? 0)})`;
  const right = keysMode
    ? `${encoderKeyName(encoderKeys.enc1)} (0x${hex(encoderKeys.enc1)})`
    : `${encoderFnName(encoder.enc1)} (0x${hex16(encoderFnUsage(encoder.enc1) ?? 0)})`;
  $("encNote").textContent =
    `Left: ${left}, right: ${right}. Press: click the encoder on the schematic.`;
}

// --------------------------------------------------------------------------- //
// edits and writing
// --------------------------------------------------------------------------- //

function editCount() {
  let n = 0;
  for (const s of SLOTS) if (keymap[s.idx] !== dirtyKeymap[s.idx]) n++;
  const dp = dirtyLed.palette || [];
  for (let i = 0; i < led.palette.length; i++) {
    const a = led.palette[i];
    const b = dp[i] || [0, 0, 0];
    if (a[0] !== b[0] || a[1] !== b[1] || a[2] !== b[2]) { n++; break; }
  }
  for (const k of LED_KEYS) if (led[k] !== dirtyLed[k]) n++;
  if (switchesByte() !== dirtySwitches) n++;
  if (encoderMode !== dirtyEncoderMode) n++;
  if (encoderMode === "keys") {
    if (encoderKeys.enc0 !== dirtyEncoderKeys.enc0
        || encoderKeys.enc1 !== dirtyEncoderKeys.enc1) n++;
  } else if (encoder.enc0 !== dirtyEncoder.enc0
             || encoder.enc1 !== dirtyEncoder.enc1) n++;
  return n;
}

// A verified write becomes the new origin of the "not written" counter. The
// factory template stays untouched: "Reset to original" still restores it.
function commitWrittenState() {
  configOk = true;
  setConfigOkUi();
  dirtyKeymap.set(keymap);
  dirtyLed.palette = led.palette.map((c) => [c[0], c[1], c[2]]);
  for (const k of LED_KEYS) dirtyLed[k] = led[k];
  dirtySwitches = switchesByte();
  dirtyEncoderMode = encoderMode;
  Object.assign(dirtyEncoder, encoder);
  Object.assign(dirtyEncoderKeys, encoderKeys);
  updateDirty();
}

function updateDirty() {
  const n = editCount();
  const box = $("dirty");
  box.hidden = n === 0;
  if (n === 1) $("dirtyText").textContent = "1 edit not written";
  else if (n > 1) $("dirtyText").textContent = `${n} edits not written`;
  updateInspectorBadge();
  reflowLedPreview();
}

function stage() {
  frames.set(baseline);
  const nk = patchKeymap(frames, keymap, SESSION);
  const nl = patchLed(frames, led, SESSION);
  const ns = patchSwitches(frames, switches, SESSION);
  const rotation = encoderMode === "keys"
    ? { mode: "keys", enc0: encoderKeys.enc0, enc1: encoderKeys.enc1 }
    : { mode: "media", enc0: encoder.enc0, enc1: encoder.enc1 };
  const ne = patchEncoderRotation(frames, rotation, SESSION);
  let changed = 0;
  for (let i = 0; i < frames.length; i++) if (frames[i] !== baseline[i]) changed++;
  return { nk, nl, ns, ne, changed };
}

function validate() {
  if (busy) return;
  const { nk, nl, ns, ne, changed } = stage();
  const keys = SLOTS.map((s) => `${s.label}=0x${hex(keymap[s.idx])}`).join("  ");
  log(`check: ${SESSION.frameCount} frames, ${nk} keymap bytes, ${nl} LED bytes, `
    + `${ns} switch bytes, ${ne} encoder bytes, ${changed} bytes changed`, "ok");
  log(`keys: ${keys}`);
  log(`switches: byte 0x${hex(switchesByte())} `
    + `(Alt+F4 ${switches.altF4 ? "on" : "off"}, Win ${switches.winLock ? "on" : "off"}, `
    + `all keys ${switches.allKeysLock ? "on" : "off"}, KEYSMODE ${switches.keyMode ? "on" : "off"})`);
  if (encoderMode === "keys") {
    const name = (v) => NAMES.get(v) || `0x${hex(v)}`;
    log(`encoder rotation: left = ${name(encoderKeys.enc0)} (0x${hex(encoderKeys.enc0)}), `
      + `right = ${name(encoderKeys.enc1)} (0x${hex(encoderKeys.enc1)}); `
      + `press = ${name(keymap[49])} (slot 49)`);
  } else {
    const fn = (n) => (ENCODER_FUNCTIONS.find((f) => f[1] === n) || ["?", n, 0]);
    log(`encoder media: A = ${fn(encoder.enc0)[0]} (number ${encoder.enc0}), `
      + `B = ${fn(encoder.enc1)[0]} (number ${encoder.enc1})`);
  }
}

// --------------------------------------------------------------------------- //
// memory reads, dump and read-back verification
// --------------------------------------------------------------------------- //

// Read one byte per 07 18 05 command, exactly like tools/k815ctl.py
// (`read_byte`): send the address, wait readPauseMs, then GET_FEATURE, take
// answer byte 1 and close the read with 07 18 01.  Without that closing
// commit the firmware answers the following reads with 0xb4 instead of memory.
async function readByteOnce(dev, addr) {
  await sendFrame(dev, readReport(addr, SESSION.reportId));
  await sleep(SESSION.readPauseMs ?? 1);
  let view = null;
  try {
    const ans = await dev.receiveFeatureReport(SESSION.reportId);
    view = ans instanceof DataView
      ? new Uint8Array(ans.buffer, ans.byteOffset, ans.byteLength)
      : new Uint8Array(ans);
  } finally {
    await sendFrame(dev, hostReport(0x01));
  }
  if (!view || view.length < 2) {
    throw new Error(`answer is ${view ? view.length : 0} byte(s)`);
  }
  return view[1];
}

// The firmware sometimes answers with a value from another address, so every
// byte is read until two answers agree (see `pickStable`). `readRetries` counts
// the bytes that needed more than one read; nothing is hidden.
let readRetries = 0;

async function readByte(dev, addr) {
  const seen = [await readByteOnce(dev, addr), await readByteOnce(dev, addr)];
  if (seen[0] === seen[1]) return seen[0];
  readRetries++;
  for (let i = 0; i < 3; i++) {
    seen.push(await readByteOnce(dev, addr));
    const top = seen.filter((v) => v === pickStable(seen)).length;
    if (top >= 2) break;
  }
  const value = pickStable(seen);
  log(`read 0x${hex16(addr)} was unstable (${seen.map((v) => `0x${hex(v)}`).join(", ")}), using 0x${hex(value)}`, "warn");
  return value;
}

async function readRanges(dev, ranges, onProgress, total, expected = null) {
  const chunks = [];
  let done = 0;
  readRetries = 0;
  // Verification knows the expected value, so one read is enough: a mismatch
  // is re-read a few times to tell a bad read from a bad write. Plain reads
  // keep the two-pass mode unless the user enabled "Fast read".
  const single = !!expected || !!($("fastRead") && $("fastRead").checked);
  try {
    // The application opens a session (INIT + COMMIT) before reading; the first
    // two answers after that are a stale buffer, so they are discarded.
    await sendFrame(dev, hostReport(0x11));
    await sendFrame(dev, hostReport(0x00));
    if (ranges.length) {
      const discard = single ? readByteOnce : readByte;
      await discard(dev, ranges[0][0]);
      await discard(dev, ranges[0][0]);
    }
    for (const [from, to] of ranges) {
      const bytes = new Uint8Array(to - from + 1);
      for (let i = 0; i < bytes.length; i++) {
        const addr = from + i;
        let value;
        try {
          if (expected) {
            const want = expected.get(addr);
            value = await readByteOnce(dev, addr);
            if (want !== undefined && value !== want) {
              const seen = [value];
              for (let k = 0; k < 3; k++) {
                const v = await readByteOnce(dev, addr);
                seen.push(v);
                if (v === want) { value = v; break; }
              }
              if (value === want) readRetries++;
              else value = pickStable(seen);
            }
          } else if (single) {
            value = await readByteOnce(dev, addr);
          } else {
            value = await readByte(dev, addr);
          }
        } catch (e) {
          throw new Error(`read at 0x${hex16(addr)} failed: ${e.message}`);
        }
        bytes[i] = value;
        done++;
        if (onProgress) onProgress(done, total);
      }
      chunks.push({ from, bytes });
    }
    if (readRetries) {
      log(`read: ${readRetries} byte(s) needed a repeated read`, "warn");
    }
    return chunks;
  } finally {
    // Closing handshake. Without it this firmware stays in the memory-read
    // state and stops sending normal keyboard input until a power cycle.
    try {
      await sendFrame(dev, hostReport(0x11));
      await sendFrame(dev, hostReport(0x00));
      await dev.receiveFeatureReport(SESSION.reportId);
      log("read session closed (INIT+COMMIT); keyboard input restored", "muted");
    } catch (e) {
      log(`closing handshake failed: ${e.message}`, "warn");
    }
  }
}

// Last value written to every address by a frame stream. The two phases write
// different bytes to the same address, so "what will be on the device" is the
// last write, not a union of both phases.
function writeImage(frameBytes) {
  const map = new Map();
  for (let i = 0; i + 16 <= frameBytes.length; i += 16) {
    const f = frameBytes.subarray(i, i + 16);
    if (f[1] === 0x18 && f[2] === 0x03) {
      map.set((f[4] | (f[5] << 8)) + f[3], f[6]);
    }
  }
  return map;
}

function regionOf(addr) {
  if (addr === SESSION_BASE) return "switches";
  if (addr > SESSION_BASE && addr <= SESSION_BASE + 0x20) return "led";
  if ((addr >= 0x0020 && addr <= 0x00bf)
      || (addr >= SESSION_BASE + 0x48
          && addr <= SESSION_BASE + 0x48 + 0x9f)) {
    return "keymap";
  }
  if (addr >= SESSION_RECORD && addr <= SESSION_RECORD + 0x1f) return "encoder";
  return "other";
}

const REGION_ORDER = ["keymap", "led", "switches", "encoder", "other"];

function compareDump(memory, expected) {
  const diff = {};
  const total = {};
  let compared = 0;
  let diffs = 0;
  for (const [addr, want] of expected) {
    const off = addr - DUMP_ADDR;
    if (off < 0 || off >= memory.length) continue;
    const region = regionOf(addr);
    compared++;
    total[region] = (total[region] || 0) + 1;
    if (memory[off] !== want) {
      diff[region] = (diff[region] || 0) + 1;
      diffs++;
    }
  }
  return { compared, diffs, diff, total };
}

function diffText(cmp) {
  return REGION_ORDER.map((r) => `${r} ${cmp.diff[r] || 0}`).join(", ");
}

async function sha256Hex(bytes) {
  if (!globalThis.crypto || !crypto.subtle) {
    throw new Error("SHA-256 needs a secure context (open the page on localhost)");
  }
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => hex(b)).join("").toLowerCase();
}

function downloadJson(obj, name) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

function applyDeviceConfig(get) {
  const B = SESSION_BASE;
  const R = SESSION_RECORD;
  for (let i = 0; i < 160; i++) keymap[i] = get(B + 0x48 + i) ?? 0xff;

  const sw = get(B) ?? 0;
  for (const [name, bit] of Object.entries(SWITCH_BITS)) {
    switches[name] = !!(sw & bit);
  }

  for (let i = 0; i < 7; i++) {
    const b0 = get(B + 0x01 + 2 * i) ?? 0;
    const b1 = get(B + 0x02 + 2 * i) ?? 0;
    led.palette[i] = [b0 >> 4, b1 & 0x0f, b0 & 0x0f];
  }
  const h18 = get(B + 0x18) ?? 0;
  const h19 = get(B + 0x19) ?? 0;
  const h1a = get(B + 0x1a) ?? 0;
  // react is phase-B logical 0x10, i.e. base+0x0f (the header is 1-based here)
  const hReact = get(B + 0x0f) ?? 0;
  led.speed = h18 & 0x07;
  led.direction = (h18 & 0xc0) >> 6;
  led.mode = h19 & 0x0f;
  led.single = (h19 & 0x10) ? 1 : 0;
  led.symmetry = (h19 & 0x80) ? 1 : 0;
  led.react = hReact & 0x0f;
  led.rgbEn = h1a & 0x7f;

  const usage = (at) => (get(at) ?? 0) | ((get(at + 1) ?? 0) << 8);
  const macroAt = (at) => {
    const rec = Array.from({ length: 16 }, (_, i) => get(at + i) ?? 0);
    if (rec[0] === 0x01 && rec[1] === 0x00 && rec[2] === 0x86
        && rec[4] === 0x06 && rec[3] === rec[5]
        && rec.slice(6).every((v) => v === 0)) {
      return rec[3];
    }
    return null;
  };
  let sawMacro = false;
  for (const [key, at] of [["enc0", R], ["enc1", R + 0x10]]) {
    const macro = macroAt(at);
    if (macro !== null) {
      encoderKeys[key] = macro;
      sawMacro = true;
    } else {
      encoder[key] = encoderNumberOf(usage(at + 6));
    }
  }
  encoderMode = sawMacro ? "keys" : "media";

  // The loaded state is the new origin of the "not written" counter.
  dirtyKeymap.set(keymap);
  Object.assign(dirtyLed, led);
  dirtyLed.palette = led.palette.map((c) => [c[0], c[1], c[2]]);
  dirtySwitches = switchesByte();
  Object.assign(dirtyEncoder, encoder);
  Object.assign(dirtyEncoderKeys, encoderKeys);
  dirtyEncoderMode = encoderMode;

  refreshSchematic();
  renderInspector();
  refreshSwitches();
  refreshEncoderControls();
  refreshLedControls();
  updateDirty();
}

async function readDeviceConfig(dev) {
  const B = SESSION_BASE;
  const R = SESSION_RECORD;
  const ranges = [[B, B + 0x20],
                  [B + 0x48, B + 0x48 + 0x9f],
                  [R, R + 0x1f]];
  const total = 0x21 + 0xa0 + 0x20;
  const t0 = performance.now();
  beginOp("Read config", total);
  try {
    const chunks = await readRanges(dev, ranges,
      (done, all) => showProgress("Read config", done, all), total);
    const mem = new Map();
    for (const { from, bytes } of chunks) {
      for (let i = 0; i < bytes.length; i++) mem.set(from + i, bytes[i]);
    }
    applyDeviceConfig((a) => mem.get(a));
    configOk = true;
    setConfigOkUi();
    const name = (v) => NAMES.get(v) || `0x${hex(v)}`;
    log(`read the current keymap, LED, switches and encoder from the device`
      + ` (encoder press: ${name(keymap[49])}, slot 49 = 0x${hex(keymap[49])})`
      + ` in ${Math.round(performance.now() - t0)} ms`, "ok");
  } catch (e) {
    configOk = false;
    setConfigOkUi();
    log(`reading the device config failed: ${e.message}`, "err");
    log("the page keeps the template values; Read or Dump again, and do not "
      + "Apply before that succeeds", "err");
    toast("reading the device config failed: Apply is not safe until Read or Dump succeeds", "err");
  } finally {
    endOp();
  }
}

async function dumpDevice() {
  if (busy) return;
  if (!device) { toast("connect the device first", "warn"); return; }

  const dev = device;
  stage();                       // compare with the write the page would send now
  const planned = writeImage(frames);
  const total = DUMP_LEN;
  const t0 = performance.now();

  beginOp("Dump", total);
  try {
    const chunks = await readRanges(dev,
      [[DUMP_ADDR, DUMP_ADDR + DUMP_LEN - 1]],
      (done, all) => showProgress("Dump", done, all), total);
    const memory = chunks[0].bytes;
    applyDeviceConfig((a) => memory[a - DUMP_ADDR]);
    configOk = true;
    setConfigOkUi();
    const sha = await sha256Hex(memory);
    const created = new Date();
    const stamp = created.toISOString().replace(/[:.]/g, "-");
    const name = `k815-memory-${stamp}.json`;

    const record = {
      version: 1,
      kind: "k815-memory-dump",
      created: created.toISOString(),
      vendorId: "0x" + hex16(dev.vendorId),
      productId: "0x" + hex16(dev.productId),
      product: dev.productName || "K815",
      address: DUMP_ADDR,
      length: DUMP_LEN,
      reportId: SESSION.reportId,
      configBase: SESSION_BASE,
      recordBase: SESSION_RECORD,
      sha256: sha,
      // address -> bytes, one key per 16-byte line
      memory: {},
    };
    for (let off = 0; off < memory.length; off += 16) {
      record.memory["0x" + hex16(DUMP_ADDR + off)] =
        [...memory.subarray(off, off + 16)];
    }
    downloadJson(record, name);

    const cmp = compareDump(memory, planned);
    dumpOk = true;
    lastDump = { memory, sha, created };
    const text = `Last dump ${created.toLocaleString()}: ${DUMP_LEN} bytes from `
      + `0x${hex16(DUMP_ADDR)}, sha256 ${sha.slice(0, 16)}…, saved as ${name}. `
      + `Against the planned write: ${cmp.diffs} of ${cmp.compared} written bytes `
      + `differ (${diffText(cmp)}).`;
    // The top bar only has room for a chip; the full story goes to the
    // tooltip and to the log below.
    const clock = created.toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
    $("dumpText").textContent = `Dump ${clock} · ${cmp.diffs} bytes differ from Apply`;
    $("dumpInfo").dataset.state = cmp.diffs ? "warn" : "ok";
    $("dumpInfo").title = text;
    $("btnDump").title = text;
    log(`dump: ${DUMP_LEN} bytes from 0x${hex16(DUMP_ADDR)}, sha256 ${sha}`
      + ` in ${Math.round(performance.now() - t0)} ms`, "ok");
    log(`dump saved as ${name}`);
    log(`dump vs planned write: ${cmp.diffs} of ${cmp.compared} written bytes differ `
      + `(${diffText(cmp)})`, cmp.diffs ? "warn" : "ok");
    setStatus(`Dump: ${DUMP_LEN} bytes, ${cmp.diffs} byte(s) differ from the planned write`,
      cmp.diffs ? "warn" : "ok");
  } catch (e) {
    const msg = `dump failed: ${e.message}. The read is read-only; nothing was written.`;
    toast(msg, "err");
    setStatus("Dump failed (read-only, nothing written)", "err");
  } finally {
    endOp();
  }
}

// Read back the ranges that a write leaves on the device and compare them with
// the bytes actually sent. Catches a silent truncation or a rejected write.
async function verifyWrite(dev, sent) {
  const expected = writeImage(sent);
  const total = VERIFY_RANGES.reduce((n, [a, b]) => n + (b - a + 1), 0);
  const t0 = performance.now();
  try {
    const chunks = await readRanges(dev, VERIFY_RANGES,
      (done, all) => showProgress("Verify", done, all), total, expected);
    const bad = [];
    let checked = 0;
    for (const { from, bytes } of chunks) {
      for (let i = 0; i < bytes.length; i++) {
        const addr = from + i;
        const want = expected.get(addr);
        if (want === undefined) continue;
        checked++;
        if (bytes[i] !== want) bad.push([addr, want, bytes[i]]);
      }
    }
    if (!bad.length) {
      log(`verified ${checked} bytes after write (switches, LED, keymap, encoder)`
        + ` in ${Math.round(performance.now() - t0)} ms`, "ok");
      setStatus(`Verified ${checked} bytes after write`, "ok");
      return true;
    }
    log(`verification: ${bad.length} of ${checked} bytes differ from what was sent`, "err");
    for (const [addr, want, got] of bad.slice(0, 10)) {
      log(`  0x${hex16(addr)}: sent 0x${hex(want)}, read 0x${hex(got)}`, "err");
    }
    if (bad.length > 10) log(`  ... ${bad.length - 10} more mismatches`, "err");
    const msg = `verification failed: ${bad.length} of ${checked} bytes differ; `
      + "the write may be partial or the device may transform these bytes. "
      + "Run Dump before retrying.";
    toast(msg, "err");
    setStatus(`Verification failed: ${bad.length} byte(s) differ`, "err");
    return false;
  } catch (e) {
    const msg = `write finished, but the verification read failed: ${e.message}. `
      + "The device may hold a partial or complete write; run Dump and inspect "
      + "it before retrying.";
    log(msg, "err");
    toast(msg, "err");
    setStatus("Write done; verification read failed", "err");
    return false;
  }
}

// --------------------------------------------------------------------------- //
// writing
// --------------------------------------------------------------------------- //

async function apply() {
  if (busy) return;
  if (!device) { toast("connect the device first", "warn"); return; }

  if (!configOk) {
    const proceed = confirm(
      "The page has not read the device configuration in this session, so it "
      + "still holds the template values (for example keymap slot 49, the "
      + "encoder press).\n\n"
      + "Applying now writes those template values over the current ones.\n\n"
      + "Press Read (or Dump) first. Continue anyway?");
    if (!proceed) {
      log("apply cancelled: read the device config first", "warn");
      return;
    }
  }

  if (!dumpOk) {
    const proceed = confirm(
      "No successful device memory dump has been taken in this page session.\n\n"
      + "Apply overwrites the keyboard memory. If the write fails at any frame, "
      + "the device may stay partially written and there will be no dump to "
      + "compare it with.\n\n"
      + "Take a Dump first. Continue without a dump?");
    if (!proceed) {
      log("apply cancelled: take a Dump first", "warn");
      return;
    }
  }

  const { changed } = stage();
  // One immutable copy for the whole operation: a second writer or a later
  // stage() must not change the bytes in flight.
  const out = frames.slice();
  const total = SESSION.frameCount;
  const dev = device;
  const delayAt = new Map((SESSION.delays || []).map(([i, ms]) => [i, ms]));
  const extra = Math.min(1000, Math.max(0, Number($("extraDelay").value) || 0));

  log(`write: ${changed} changed bytes in a stream of ${total} reports`
    + (extra ? `, extra delay ${extra} ms per frame` : ""));
  beginOp("Write", total);
  const t0 = performance.now();
  let i = 0;
  try {
    for (i = 0; i < total; i++) {
      const before = delayAt.get(i);
      if (before) await sleep(before);
      const frame = out.subarray(i * 16, i * 16 + 16);
      // WebHID takes the report id and the payload separately, while in a frame
      // the id sits in the first byte
      await sendFrame(dev, frame);
      if (extra) await sleep(extra);
      if (frame[1] === 0x18 && frame[2] === 0x05) {
        await sleep(SESSION.readPauseMs ?? 1);
        try {
          await dev.receiveFeatureReport(SESSION.reportId);
        } catch (e) {
          log(`receiveFeatureReport after frame ${i + 1} / ${total} failed: ${e.message}`,
            "warn");
        }
      }
      showProgress("Write", i + 1, total);
    }
    log(`sent ${total} reports in ${Math.round(performance.now() - t0)} ms`, "ok");
    const verified = await verifyWrite(dev, out);
    if (verified) {
      commitWrittenState();
      // This firmware only applies a new keymap after a real USB
      // re-enumeration.  WebHID cannot reset a device, so ask the local
      // server (tools/serve_webhid.py) for USBDEVFS_RESET.
      let resetOk = false;
      try {
        const r = await fetch("/reset", { method: "POST" });
        const j = await r.json();
        resetOk = !!j.ok;
        log(resetOk ? `USB reset: ${j.path}`
          : `USB reset failed: ${j.error || "unknown"}`, resetOk ? "ok" : "warn");
      } catch (e) {
        log(`USB reset request failed: ${e.message}`, "muted");
      }
      if (resetOk) {
        resetting = true;
        if (device === dev) {
          device = null;
          resetDeviceUi();
        }
        try {
          await dev.close();
        } catch (e) {
          // the USB reset already detached the interface
        }
        await sleep(1800);
        try {
          const devices = await navigator.hid.getDevices();
          const picked = chooseDevice(devices);
          if (picked.device) {
            await useDevice(picked.device, picked.usagePage);
            log("reconnected after USB reset", "ok");
          } else {
            log("device is re-enumerating; click Connect to reconnect", "muted");
          }
        } catch (e) {
          log(`reconnect after USB reset failed: ${e.message}`, "warn");
        }
        resetting = false;
        setStatus("Write applied. Keyboard reconnected after USB reset.", "ok");
        toast("Write applied \u2014 device reconnected", "ok");
      } else {
        // No reset service (offline server or missing udev access): keep the
        // HID handle open so Read/Dump keep working, and ask for a replug.
        log("write applied; if the keyboard does not react, replug it once "
          + "(install the udev rule for automatic reset)", "warn");
        setStatus("Write applied. Replug once, or install the udev rule.", "warn");
        toast("Write applied \u2014 replug once (udev rule needed for auto-reset)", "warn");
      }
    }
  } catch (e) {
    const at = i + 1;
    const msg = `write failed at frame ${at} / ${total}: ${e.message}. `
      + `The device may be partially written (${i} of ${total} reports accepted). `
      + "Do not retry blindly: run Dump, inspect the differences and restore "
      + "from a dump or backup.";
    log(`write aborted at frame ${at} / ${total}; ` +
      `${total - at} reports were not sent`, "err");
    toast(msg, "err");
    setStatus(`Write failed at frame ${at} / ${total}`, "err");
  } finally {
    endOp();
  }
}

// "Read" button: the startup info exchange (as the vendor application does)
// followed by the real config read, so the page always shows device state.
async function readConfig() {
  if (busy || !device) return;
  const dev = device;
  beginOp("Read", 2);
  try {
    const init = new Uint8Array(16); init[0] = 0x07; init[1] = 0x18; init[2] = 0x11;
    const commit = new Uint8Array(16); commit[0] = 0x07; commit[1] = 0x18; commit[2] = 0x00;
    await sendFrame(dev, init);
    await sendFrame(dev, commit);
    const dump = (r) => {
      const view = r instanceof DataView
        ? new Uint8Array(r.buffer, r.byteOffset, r.byteLength)
        : new Uint8Array(r);
      return [...view].map(hex).join(" ");
    };
    const a = await dev.receiveFeatureReport(SESSION.reportId);
    showProgress("Read", 1, 2);
    const b = await dev.receiveFeatureReport(SESSION.reportId);
    showProgress("Read", 2, 2);
    log(`info #1: ${dump(a)}`);
    log(`info #2: ${dump(b)}`);
  } catch (e) {
    toast(`read failed: ${e.message}`, "err");
  } finally {
    endOp();
  }
  if (!busy && device === dev) await readDeviceConfig(dev);
}

// --------------------------------------------------------------------------- //
// device
// --------------------------------------------------------------------------- //

async function connect() {
  if (!navigator.hid) {
    toast("WebHID is not available: Chromium and a page on localhost or https are required", "err");
    return;
  }
  if (busy || resetting) return;
  if (device) {
    log(`already connected: ${device.productName || "K815"}`, "muted");
    return;
  }
  try {
    const picked = await pickDevice();
    if (!picked.device) {
      log(`connection failed: ${picked.error}`, "err");
      for (const line of picked.details || []) log(`  ${line}`, "muted");
      toast(picked.error, "err");
      return;
    }
    await useDevice(picked.device, picked.usagePage);
    await readDeviceConfig(picked.device);
  } catch (e) {
    toast(`connection failed: ${e.message}`, "err");
  }
}

function resetDeviceUi() {
  $("connText").textContent = "Connect device";
  $("connVid").textContent = "—";
  $("conn").classList.remove("ok");
  $("conn").title = "Connect the device over WebHID";
  syncControls();
}

async function useDevice(dev, usagePage) {
  if (!dev) return;
  if (device === dev && dev.opened) return;
  if (device && device !== dev) {
    try {
      if (device.opened) await device.close();
    } catch (e) {
      log(`could not close the previous interface: ${e.message}`, "warn");
    }
  }
  if (!dev.opened) await dev.open();
  device = dev;

  const vid = dev.vendorId.toString(16).toUpperCase().padStart(4, "0");
  const pid = dev.productId.toString(16).toUpperCase().padStart(4, "0");
  $("connText").textContent = "Connected";
  $("connVid").textContent = `${vid}:${pid}`;
  $("conn").classList.add("ok");
  $("conn").title = `${dev.productName || "K815"} · ${vid}:${pid}`;
  syncControls();

  log(`connected: ${dev.productName || "K815"} (${vid}:${pid}), `
    + `interface usage page 0x${(usagePage >>> 0).toString(16).toUpperCase()}`, "ok");
  if (usagePage !== WRITE_USAGE_PAGE) {
    log("warning: only the 0xFF00 read collection is available; the device may reject writes", "warn");
  }
}

function handleDisconnect(dev) {
  if (!device || dev !== device) return;
  const wasBusy = busy;
  device = null;
  resetDeviceUi();
  log("device disconnected", "warn");
  if (wasBusy) {
    toast(`device disconnected during ${wasBusy.toLowerCase()}; `
      + "reconnect, run Dump and inspect the device before retrying", "err");
    setStatus(`Device disconnected during ${wasBusy.toLowerCase()}`, "err");
  }
}

async function autoConnect() {
  if (!navigator.hid || device || busy || resetting) return;
  try {
    const devices = await navigator.hid.getDevices();
    const picked = chooseDevice(devices);
    if (picked.device) {
      log("auto-connect: using an already permitted device", "muted");
      await useDevice(picked.device, picked.usagePage);
      await readDeviceConfig(picked.device);
    } else if (devices.length) {
      log(`auto-connect: ${picked.error}`, "warn");
      for (const line of picked.details || []) log(`  ${line}`, "muted");
    }
  } catch (e) {
    log(`auto-connect failed: ${e.message}`, "warn");
  }
}

// --------------------------------------------------------------------------- //
// configuration files
// --------------------------------------------------------------------------- //

function loadIni(file) {
  if (busy) { toast("another operation is running, try again when it finishes", "warn"); return; }
  const reader = new FileReader();
  reader.onload = () => {
    if (busy) { toast("another operation is running, KBconfig.ini was not loaded", "warn"); return; }
    const { keymap: km, seen } = keymapFromIni(reader.result, keymap);
    keymap.set(km);
    refreshSchematic();
    renderInspector();
    updateDirty();
    log(`loaded ${file.name}: ${seen} matrix values`, "ok");
  };
  reader.readAsText(file, "utf-8");
}

function exportJson() {
  const obj = {
    version: 3, keymap: [...keymap], led,
    switches: { ...switches },
    encoderMode,
    encoder: encoderMode === "media" ? { ...encoder } : {},
  };
  if (encoderMode === "keys") obj.encoderKeys = { ...encoderKeys };
  for (const s of SLOTS) obj[s.id] = keymap[s.idx];
  downloadJson(obj, "k815-config.json");
  log("configuration exported to k815-config.json (settings of this page, not device memory)", "ok");
}

function importJson(file) {
  if (busy) { toast("another operation is running, import is blocked", "warn"); return; }
  const reader = new FileReader();
  reader.onload = () => {
    if (busy) { toast("another operation is running, import is blocked", "warn"); return; }
    try {
      const obj = JSON.parse(reader.result);
      if (Array.isArray(obj.keymap) && obj.keymap.length === 160) {
        keymap.set(obj.keymap.map((v) => v & 0xff));
      } else {
        for (const s of SLOTS) if (typeof obj[s.id] === "number") keymap[s.idx] = obj[s.id] & 0xff;
      }
      if (obj.led && Array.isArray(obj.led.palette)) {
        Object.assign(led, obj.led);
        while (led.palette.length < 7) led.palette.push([0, 0, 0]);
        buildLedControls();
        buildSwatches();
        renderColorEditor();
      }
      if (obj.switches && typeof obj.switches === "object") {
        for (const [id, key] of SWITCH_INPUTS) {
          if (typeof obj.switches[key] === "boolean") switches[key] = obj.switches[key];
        }
        refreshSwitches();
      }
      if (obj.encoderKeys && typeof obj.encoderKeys === "object") {
        encoderMode = "keys";
        for (const [id, key] of ENC_KEY_INPUTS) {
          if (typeof obj.encoderKeys[key] === "number") {
            encoderKeys[key] = obj.encoderKeys[key] & 0xff;
          }
        }
        refreshEncoderControls();
      } else if (obj.encoder && typeof obj.encoder === "object") {
        encoderMode = "media";
        for (const [id, key] of ENC_INPUTS) {
          if (typeof obj.encoder[key] === "number") encoder[key] = obj.encoder[key] | 0;
        }
        refreshEncoderControls();
      } else if (obj.encoderMode === "keys" || obj.encoderMode === "media") {
        encoderMode = obj.encoderMode;
        refreshEncoderControls();
      }
      refreshSchematic();
      renderInspector();
      updateDirty();
      log(`imported from ${file.name}`, "ok");
    } catch (e) {
      toast(`import failed: ${e.message}`, "err");
    }
  };
  reader.readAsText(file, "utf-8");
}

// --------------------------------------------------------------------------- //
// wiring
// --------------------------------------------------------------------------- //

initSchematic();
initLighting();
initSwitches();
buildEncoderControls();

$("conn").addEventListener("click", connect);
$("btnRead").addEventListener("click", readConfig);
$("btnDump").addEventListener("click", dumpDevice);
$("btnCheck").addEventListener("click", validate);
$("btnApply").addEventListener("click", apply);
$("btnReset").addEventListener("click", () => assign(defaultKeymap[slot.idx]));
$("btnIni").addEventListener("click", () => $("fileIni").click());
$("fileIni").addEventListener("change", (e) => {
  if (e.target.files[0]) loadIni(e.target.files[0]);
  e.target.value = "";
});
$("btnExport").addEventListener("click", exportJson);
$("btnImport").addEventListener("click", () => $("fileJson").click());
$("fileJson").addEventListener("change", (e) => {
  if (e.target.files[0]) importJson(e.target.files[0]);
  e.target.value = "";
});

if (navigator.hid) {
  navigator.hid.addEventListener("disconnect", (e) => handleDisconnect(e.device));
  navigator.hid.addEventListener("connect", (e) => {
    log(`HID device connected: ${describeDevice(e.device)}`, "muted");
    if (!device && !resetting) autoConnect();
  });
}

$("search").addEventListener("input", () => {
  query = $("search").value;
  renderList();
});

$("chips").addEventListener("click", (e) => {
  const b = e.target.closest(".chip");
  if (!b) return;
  filter = b.dataset.filter;
  for (const c of $("chips").children) c.classList.toggle("active", c === b);
  renderList();
});

$("codeInput").addEventListener("change", () => {
  const raw = $("codeInput").value.trim().replace(/^0x/i, "");
  const v = parseInt(raw, 16);
  if (Number.isNaN(v) || v < 0 || v > 0xff || !/^[0-9a-f]{1,2}$/i.test(raw)) {
    toast(`invalid code “${$("codeInput").value}”`, "warn");
    $("codeInput").value = "0x" + hex(keymap[slot.idx]);
    return;
  }
  assign(v);
});

// --------------------------------------------------------------------------- //
// tabs, log overlay and the "⋯" menu
// --------------------------------------------------------------------------- //

const TAB_VIEWS = { keys: "viewKeys", lighting: "viewLight" };
const UI_STATE_KEY = "k815.webhid.ui.v1";

function readUiState() {
  try { return JSON.parse(localStorage.getItem(UI_STATE_KEY) || "{}"); }
  catch (e) { return {}; }
}

function writeUiState(patch) {
  try {
    localStorage.setItem(UI_STATE_KEY, JSON.stringify({ ...readUiState(), ...patch }));
  } catch (e) { /* file:// or private mode: the UI still works, it just forgets */ }
}

function showTab(name, updateHash) {
  if (!Object.prototype.hasOwnProperty.call(TAB_VIEWS, name)) name = "keys";
  for (const [tab, id] of Object.entries(TAB_VIEWS)) {
    $(id).hidden = tab !== name;
  }
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  if (updateHash) {
    try { history.replaceState(null, "", `#${name}`); } catch (e) { /* file:// */ }
  }
  if (name === "lighting") startLedPreview();
  else stopLedPreview();
  writeUiState({ tab: name });
}

document.querySelectorAll("[data-tab]").forEach((b) => {
  b.addEventListener("click", () => showTab(b.dataset.tab, true));
});

$("btnLog").addEventListener("click", toggleLog);
$("btnLogClose").addEventListener("click", closeLog);
$("btnShowLog").addEventListener("click", toggleLog);
$("dumpInfo").addEventListener("click", openLog);
$("fastRead").addEventListener("change", () => writeUiState({ fastRead: $("fastRead").checked }));
$("lpTemplate").addEventListener("click", () => {
  if (device) readConfig();
  else connect();
});

// A menu entry closes the menu; the delay field keeps it open so the value
// can be typed in.
$("appMenu").querySelector(".menu-pop").addEventListener("click", (e) => {
  if (e.target.closest(".menu-item")) $("appMenu").open = false;
});

document.addEventListener("click", (e) => {
  const menu = $("appMenu");
  if (menu.open && !menu.contains(e.target)) menu.open = false;

  const panel = $("logCard");
  const trigger = e.target.closest && e.target.closest("#btnLog, #btnShowLog, #dumpInfo");
  if (!panel.hidden && !panel.contains(e.target) && !trigger) closeLog();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const menu = $("appMenu");
    if (menu.open) { menu.open = false; return; }
    if (!$("logCard").hidden) { closeLog(); return; }
    return;
  }
  const typing = e.target.closest && e.target.closest("input, select, textarea");
  if (typing) return;
  if (e.key === "/") { e.preventDefault(); $("search").focus(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); apply(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "r" || e.key === "R") readConfig();
  else if (e.key === "d" || e.key === "D") dumpDevice();
});

window.addEventListener("beforeunload", (e) => {
  if (editCount() === 0) return;
  e.preventDefault();
  e.returnValue = "";
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopLedPreview();
  else if (!$("viewLight").hidden) startLedPreview();
});

// --------------------------------------------------------------------------- //
// start
// --------------------------------------------------------------------------- //

const savedUi = readUiState();
$("fastRead").checked = !!savedUi.fastRead;
const startTab = location.hash === "#keys" || location.hash === "#lighting"
  ? location.hash.slice(1)
  : (savedUi.tab === "lighting" ? "lighting" : "keys");
showTab(startTab, false);
const startSlot = SLOTS.some((s) => s.id === savedUi.slot) ? savedUi.slot : SLOTS[0].id;
selectSlot(startSlot);
refreshSchematic();
updateDirty();
syncControls();

log(`session template: ${SESSION.frameCount} reports (${SESSION.capture})`);
if (defaultKeymap.length) log("default keymap taken from KBconfig.ini");
else log("no default keymap is embedded in the template, load KBconfig.ini", "warn");
log("LED: the 7-colour palette and the effect parameters are loaded");
log(`timing: ${(SESSION.delays || []).length} measured frame pause(s), `
  + `${SESSION.readPauseMs ?? 1} ms before every read answer`);
log("apply: a successful Dump is recommended; without one the page asks for confirmation");
if (!navigator.hid) log("WebHID is not available in this browser", "warn");
else autoConnect();
