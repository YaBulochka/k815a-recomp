// MKESPN K815 — configuration interface.
// Protocol and WebHID: ./k815.js, session template: ./k815_session.js

import { SESSION, LED_EFFECTS } from "./k815_session.js";
import {
  SLOTS, USAGES, decodeBlob, patchKeymap, patchLed, patchSwitches, SWITCH_BITS,
  patchEncoder, ENCODER_FUNCTIONS, encoderNumberOf, keymapFromIni, pickDevice,
} from "./k815.js";

const $ = (id) => document.getElementById(id);
const make = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const hex = (v) => (v & 0xff).toString(16).padStart(2, "0").toUpperCase();
const hex16 = (v) => (v & 0xffff).toString(16).padStart(4, "0").toUpperCase();

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

const SLOT_META = {
  k1: { n: 1, pos: "Top row, first from the left", svg: "key-1" },
  k2: { n: 2, pos: "Top row, second from the left", svg: "key-2" },
  k3: { n: 3, pos: "Top row, third from the left", svg: "key-3" },
  k4: { n: 4, pos: "Top row, fourth from the left", svg: "key-4" },
  k5: { n: 5, pos: "Bottom row, first from the left", svg: "key-5" },
  k6: { n: 6, pos: "Bottom row, second from the left", svg: "key-6" },
  k7: { n: 7, pos: "Bottom row, third from the left", svg: "key-7" },
  k8: { n: 8, pos: "Bottom row, fourth from the left", svg: "key-8" },
  enc1: { n: 9, pos: "Encoder, turn left", svg: "enc-a" },
  enc2: { n: 10, pos: "Encoder, turn right", svg: "enc-b" },
};

// The effect list comes from the generated catalogue (tools/led_modes.json). Not
// every number is reachable through the vendor UI; those are still writable here.
const LED_MODES = (() => {
  const known = new Map((LED_EFFECTS || []).map((e) => [e.mode, e]));
  const out = [];
  for (let m = 0; m < 16; m++) {
    const e = known.get(m);
    if (e) {
      const hidden = e.in_ui_list === false ? " (not in the vendor list)" : "";
      out.push({ mode: m, label: `Mode ${m} · ${e.en}${hidden}` });
    } else {
      out.push({ mode: m, label: `Mode ${m} · undefined` });
    }
  }
  return out;
})();

let device = null;
let slot = SLOTS[0];
let colorSel = 0;
let category = "key";
let filter = "all";
let query = "";

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
  if (cls === "err" || cls === "warn") $("logCard").open = true;
}

// --------------------------------------------------------------------------- //
// device schematic
// --------------------------------------------------------------------------- //

const KEY_GROUPS = new Map();

function initSchematic() {
  for (const s of SLOTS) {
    const g = document.getElementById(SLOT_META[s.id].svg);
    if (!g) continue;
    KEY_GROUPS.set(s.id, g);
    g.addEventListener("click", () => selectSlot(s.id));
    g.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectSlot(s.id); }
    });
  }
}

function selectSlot(id) {
  slot = SLOTS.find((s) => s.id === id) || SLOTS[0];
  for (const [sid, g] of KEY_GROUPS) g.classList.toggle("sel", sid === slot.id);
  renderInspector();
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
    g.classList.toggle("mod", v !== defaultKeymap[s.idx]);
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

function renderInspector() {
  const m = SLOT_META[slot.id];
  const v = keymap[slot.idx];

  $("inspTitle").textContent = `Key ${m.n}`;
  $("inspSub").textContent = m.pos;
  $("inspBadge").textContent = `#${m.n}`;
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

  if (category === "media" || category === "macro") {
    list.hidden = true;
    $("chips").hidden = true;
    note.hidden = false;
    note.textContent = category === "media"
      ? "A media code is 16 bits wide (Consumer page), while a key sends a single byte of the keyboard page, so such a code does not fit the matrix. Media functions are assigned to encoder rotation, see the Encoder card below."
      : "Macros live in ShortcutMacro.dat and are played by the application itself: they are not written to the device memory. A dedicated editor will come later.";
    return;
  }

  note.hidden = true;
  list.hidden = false;
  $("chips").hidden = category !== "key";

  if (category === "code") {
    $("codeInput").value = "0x" + hex(keymap[slot.idx]);
    $("codeInput").focus();
    $("codeInput").select();
  }

  const current = keymap[slot.idx];
  const q = query.trim().toLowerCase();
  let shown = 0;
  let active = null;

  for (const [name, code] of USAGES) {
    if (code === 0xff) continue;
    if (category === "key" && filter !== "all" && chipOf(code) !== filter) continue;
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
  updateDirty();
}

// --------------------------------------------------------------------------- //
// lighting
// --------------------------------------------------------------------------- //

const rgbCss = (rgb) => `rgb(${rgb[0] * 17}, ${rgb[1] * 17}, ${rgb[2] * 17})`;
const activeColor = (i) => ((led.rgbEn >> i) & 1) === 1;

function buildSwatches() {
  const wrap = $("swatches");
  wrap.textContent = "";
  led.palette.forEach((rgb, i) => {
    const b = make("button", "swatch");
    b.type = "button";
    b.title = `Colour ${i + 1}`;
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

function renderColorEditor() {
  const rgb = led.palette[colorSel] || [0, 0, 0];
  $("ceTitle").textContent = `Colour ${colorSel + 1}`;
  $("ceRgb").textContent = `R ${rgb[0]} · G ${rgb[1]} · B ${rgb[2]}`;
  [["ceR", 0], ["ceG", 1], ["ceB", 2]].forEach(([id, i]) => {
    $(id).value = rgb[i];
    $(id + "v").textContent = rgb[i];
  });
  $("ceUse").checked = activeColor(colorSel);
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

function initLighting() {
  buildLedControls();
  buildSwatches();
  renderColorEditor();

  [["ceR", 0], ["ceG", 1], ["ceB", 2]].forEach(([id, i]) => {
    $(id).addEventListener("input", () => {
      led.palette[colorSel][i] = Number($(id).value) | 0;
      $(id + "v").textContent = $(id).value;
      const rgb = led.palette[colorSel];
      $("ceRgb").textContent = `R ${rgb[0]} · G ${rgb[1]} · B ${rgb[2]}`;
      const sw = $("swatches").children[colorSel];
      if (sw) sw.style.setProperty("--c", rgbCss(rgb));
      updateDirty();
    });
  });

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

function fillEncoderSelect(sel, key) {
  sel.textContent = "";
  for (const [label, num, usage] of ENCODER_FUNCTIONS) {
    const o = document.createElement("option");
    o.value = String(num);
    o.textContent = `${label} · 0x${hex16(usage)}`;
    sel.appendChild(o);
  }
  if (!ENCODER_FUNCTIONS.some((f) => f[1] === encoder[key])) {
    const o = document.createElement("option");
    o.value = String(encoder[key]);
    o.textContent = `unknown · 0x${hex16(DENC[key] || 0)}`;
    sel.appendChild(o);
  }
  sel.value = String(encoder[key]);
}

function buildEncoderControls() {
  for (const [id, key] of ENC_INPUTS) {
    const sel = $(id);
    sel.addEventListener("change", () => {
      encoder[key] = Number(sel.value);
      updateDirty();
    });
    fillEncoderSelect(sel, key);
  }
  refreshEncoderNote();
}

function refreshEncoderNote() {
  for (const [id, key] of ENC_INPUTS) fillEncoderSelect($(id), key);
  const unknown = ENC_INPUTS.filter(([, k]) => encoder[k] < 0).map(([, k]) => k);
  $("encNote").innerHTML = unknown.length
    ? `The template code (${unknown.join(", ")}) is not in the measured table, so the field is left untouched.`
    : `The second number of <code>USCROLL0</code>/<code>USCROLL1</code> from <code>[Config1]</code>: `
      + `0…16 is a Consumer-page function. Larger numbers go to internal functions of the application `
      + `and never reach the device as a usage. The encoder keys are assigned separately, by cells `
      + `<code>5_1</code>/<code>6_1</code> of <code>KBconfig.ini</code> (the “Encoder A/B” slots on the schematic). `
      + `Which encoder sits on which rotation direction has not been verified on hardware.`;
}

// --------------------------------------------------------------------------- //
// edits and writing
// --------------------------------------------------------------------------- //

function editCount() {
  let n = 0;
  for (const s of SLOTS) if (keymap[s.idx] !== defaultKeymap[s.idx]) n++;
  const dp = D.palette || [];
  for (let i = 0; i < led.palette.length; i++) {
    const a = led.palette[i];
    const b = dp[i] || [0, 0, 0];
    if (a[0] !== b[0] || a[1] !== b[1] || a[2] !== b[2]) { n++; break; }
  }
  for (const k of LED_KEYS) if (led[k] !== defaultLed[k]) n++;
  if (switchesByte() !== defaultSwitches) n++;
  if (encoder.enc0 !== defaultEncoder.enc0 || encoder.enc1 !== defaultEncoder.enc1) n++;
  return n;
}

function updateDirty() {
  const n = editCount();
  const box = $("dirty");
  box.hidden = n === 0;
  if (n === 1) $("dirtyText").textContent = "1 edit not written";
  else if (n > 1) $("dirtyText").textContent = `${n} edits not written`;
}

function stage() {
  frames.set(baseline);
  const nk = patchKeymap(frames, keymap, SESSION);
  const nl = patchLed(frames, led, SESSION);
  const ns = patchSwitches(frames, switches, SESSION);
  const ne = patchEncoder(frames, encoder, SESSION);
  let changed = 0;
  for (let i = 0; i < frames.length; i++) if (frames[i] !== baseline[i]) changed++;
  return { nk, nl, ns, ne, changed };
}

function validate() {
  const { nk, nl, ns, ne, changed } = stage();
  const keys = SLOTS.map((s) => `${s.label}=0x${hex(keymap[s.idx])}`).join("  ");
  log(`check: ${SESSION.frameCount} frames, ${nk} keymap bytes, ${nl} LED bytes, `
    + `${ns} switch bytes, ${ne} encoder bytes, ${changed} bytes changed`, "ok");
  log(`keys: ${keys}`);
  log(`switches: byte 0x${hex(switchesByte())} `
    + `(Alt+F4 ${switches.altF4 ? "on" : "off"}, Win ${switches.winLock ? "on" : "off"}, `
    + `all keys ${switches.allKeysLock ? "on" : "off"}, KEYSMODE ${switches.keyMode ? "on" : "off"})`);
  const fn = (n) => (ENCODER_FUNCTIONS.find((f) => f[1] === n) || ["?", n, 0]);
  log(`encoder: A = ${fn(encoder.enc0)[0]} (number ${encoder.enc0}), `
    + `B = ${fn(encoder.enc1)[0]} (number ${encoder.enc1})`);
}

async function apply() {
  if (!device) { toast("connect the device first", "warn"); return; }
  const { changed } = stage();
  log(`write: ${changed} changed bytes in a stream of ${SESSION.frameCount} reports`);
  const t0 = performance.now();
  try {
    for (let i = 0; i < SESSION.frameCount; i++) {
      const frame = frames.slice(i * 16, i * 16 + 16);
      // WebHID takes the report id and the payload separately, while in a frame
      // the id sits in the first byte
      await device.sendFeatureReport(frame[0], frame.subarray(1));
      if (frame[1] === 0x18 && frame[2] === 0x05) {
        try { await device.receiveFeatureReport(SESSION.reportId); } catch (_) { /* ok */ }
      }
    }
    log(`sent ${SESSION.frameCount} reports in ${Math.round(performance.now() - t0)} ms`, "ok");
  } catch (e) {
    toast(`send failed: ${e.message}`, "err");
  }
}

async function readInfo() {
  if (!device) return;
  try {
    const init = new Uint8Array(16); init[0] = 0x07; init[1] = 0x18; init[2] = 0x11;
    const commit = new Uint8Array(16); commit[0] = 0x07; commit[1] = 0x18; commit[2] = 0x00;
    await device.sendFeatureReport(init[0], init.subarray(1));
    await device.sendFeatureReport(commit[0], commit.subarray(1));
    const a = await device.receiveFeatureReport(SESSION.reportId);
    const b = await device.receiveFeatureReport(SESSION.reportId);
    const dump = (r) => [...new Uint8Array(r.buffer, r.byteOffset, r.byteLength)].map(hex).join(" ");
    log(`info #1: ${dump(a)}`);
    log(`info #2: ${dump(b)}`);
  } catch (e) {
    toast(`read failed: ${e.message}`, "err");
  }
}

// --------------------------------------------------------------------------- //
// device
// --------------------------------------------------------------------------- //

async function connect() {
  if (!navigator.hid) {
    toast("WebHID is not available: Chromium and a page on localhost or https are required", "err");
    return;
  }
  try {
    const dev = await pickDevice();
    if (!dev) { log("no device selected", "warn"); return; }
    await dev.open();
    device = dev;

    const vid = dev.vendorId.toString(16).toUpperCase().padStart(4, "0");
    const pid = dev.productId.toString(16).toUpperCase().padStart(4, "0");
    $("connText").textContent = `Connected · ${dev.productName || "K815"}`;
    $("connVid").textContent = `${vid}:${pid}`;
    $("conn").classList.add("ok");
    $("btnRead").disabled = false;
    $("btnApply").disabled = false;

    log(`connected: ${dev.productName || "K815"} (${vid}:${pid})`, "ok");
    if (!(dev.collections || []).some((c) => c.usagePage === 0xff01)) {
      log("warning: interface 0xFF01 is not visible, the wrong interface may be selected", "warn");
    }
  } catch (e) {
    toast(`connection failed: ${e.message}`, "err");
  }
}

// --------------------------------------------------------------------------- //
// configuration files
// --------------------------------------------------------------------------- //

function loadIni(file) {
  const reader = new FileReader();
  reader.onload = () => {
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
  const obj = { version: 3, keymap: [...keymap], led, switches: { ...switches }, encoder: { ...encoder } };
  for (const s of SLOTS) obj[s.id] = keymap[s.idx];
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "k815-config.json";
  a.click();
  URL.revokeObjectURL(a.href);
  log("configuration exported to k815-config.json", "ok");
}

function importJson(file) {
  const reader = new FileReader();
  reader.onload = () => {
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
      if (obj.encoder && typeof obj.encoder === "object") {
        for (const [id, key] of ENC_INPUTS) {
          if (typeof obj.encoder[key] === "number") encoder[key] = obj.encoder[key] | 0;
        }
        refreshEncoderNote();
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
$("btnRead").addEventListener("click", readInfo);
$("btnCheck").addEventListener("click", validate);
$("btnApply").addEventListener("click", apply);
$("btnReset").addEventListener("click", () => assign(defaultKeymap[slot.idx]));
$("btnIni").addEventListener("click", () => $("fileIni").click());
$("fileIni").addEventListener("change", (e) => e.target.files[0] && loadIni(e.target.files[0]));
$("btnExport").addEventListener("click", exportJson);
$("btnImport").addEventListener("click", () => $("fileJson").click());
$("fileJson").addEventListener("change", (e) => e.target.files[0] && importJson(e.target.files[0]));

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

$("catSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  category = b.dataset.cat;
  for (const c of $("catSeg").children) c.classList.toggle("active", c === b);
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

document.querySelectorAll(".nav-item").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((x) => x.classList.toggle("active", x === b));
    const view = b.dataset.view;
    if (view === "keys") $("layoutCard").scrollIntoView({ behavior: "smooth", block: "start" });
    else if (view === "light") $("lighting").scrollIntoView({ behavior: "smooth", block: "start" });
    else if (view === "log") {
      $("logCard").open = true;
      $("logCard").scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (view === "macro") {
      log("Macros: no macro address was found in the device memory, ShortcutMacro.dat is read by the application itself", "warn");
    } else {
      log("Profiles: the device holds a single profile, Config1", "muted");
    }
  });
});

// --------------------------------------------------------------------------- //
// start
// --------------------------------------------------------------------------- //

selectSlot(SLOTS[0].id);
refreshSchematic();
updateDirty();

log(`session template: ${SESSION.frameCount} reports (${SESSION.capture})`);
if (defaultKeymap.length) log("default keymap taken from KBconfig.ini");
else log("no default keymap is embedded in the template, load KBconfig.ini", "warn");
log("LED: the 7-colour palette and the effect parameters are loaded");
if (!navigator.hid) log("WebHID is not available in this browser", "warn");
