import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

const $ = (s) => document.querySelector(s);
// build-plate footprints (X x Y, mm) from the makers' published specs
const PRINTERS = {
  "Bambu Lab": [["A1 mini", 180, 180], ["A1", 256, 256], ["P1P", 256, 256], ["P1S", 256, 256],
    ["P2S", 256, 256], ["X1 Carbon", 256, 256], ["X1E", 256, 256], ["H2S", 340, 320], ["H2D (one nozzle)", 325, 320]],
  "Prusa": [["MINI+", 180, 180], ["MK3S+", 250, 210], ["MK4S", 250, 210], ["CORE One", 250, 220], ["XL", 360, 360]],
  "Creality": [["Ender-3 V3 SE", 220, 220], ["Ender-3 V3 KE", 220, 220], ["Ender-3 V3", 220, 220],
    ["K1 / K1C", 220, 220], ["K1 Max", 300, 300], ["K2 Plus", 350, 350]],
  "Elegoo": [["Neptune 4 / 4 Pro", 225, 225], ["Neptune 4 Plus", 320, 320], ["Neptune 4 Max", 420, 420],
    ["Centauri Carbon", 256, 256]],
  "Anycubic": [["Kobra 2 Neo", 220, 220], ["Kobra 3", 250, 250], ["Kobra S1", 250, 250]],
  "Sovol": [["SV06", 220, 220], ["SV08", 350, 350]],
  "Voron": [["2.4 (250)", 250, 250], ["2.4 (300)", 300, 300], ["2.4 (350)", 350, 350]],
};
const DEFAULT_PRINTER = "Bambu Lab|A1 mini";
const KEP = 10.5;     // mm2: smallest tile printed so far (Cambodia/Kep)
const FAINT = 0.7;    // mm: smallest engraved glyphs printed so far

const state = {
  text: null, nameProp: "shapeName", base: "puzzle",
  features: [], assign: [], quiz: [], notes: [],
  overrides: {}, prep: null, build: null,
};

// ------------------------------------------------------------------ worker RPC

let worker, seq = 0;
const pending = new Map();

function startWorker() {
  worker = new Worker("worker.js?v=3");
  worker.onmessage = (e) => {
    const m = e.data;
    if (m.type === "status") return setEngine(m.text);
    if (m.type === "log") return appendLog(m.text);
    const p = pending.get(m.id);
    if (!p) return;
    pending.delete(m.id);
    if (m.ok) p.resolve(m.result);
    else p.reject(Object.assign(new Error(m.error), { fatal: m.fatal }));
  };
  worker.onerror = (e) => setEngine(`Engine failed to start: ${e.message || "network error"}`, "bad");
}

function call(cmd, args) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, cmd, args });
  });
}

// a GEOS crash kills the interpreter; start a fresh one and reload the data into it
async function recover(err) {
  if (!err.fatal) return;
  worker.terminate();
  for (const p of pending.values()) p.reject(new Error("engine restarted"));
  pending.clear();
  startWorker();
  if (state.text) await call("load", { text: state.text, nameProp: state.nameProp });
}

function shortError(err) {
  const lines = String(err.message || err).trim().split("\n").filter(Boolean);
  return lines[lines.length - 1].replace(/^\w+Error: /, "");
}

function setEngine(text, kind) {
  const pill = $("#engine");
  const ready = text === "ready";
  pill.className = "pill " + (kind || (ready ? "ok" : "busy"));
  $("#engine-text").textContent = ready ? "Engine ready · runs locally" : text;
}

function appendLog(text) {
  const log = $("#log");
  log.textContent += text;
  log.scrollTop = log.scrollHeight;
}

// ------------------------------------------------------------------ helpers

function palette(i, n) {   // same colours as extrude_map._palette
  const h = i / Math.max(n, 1), s = 0.55, v = 0.85;
  const k = Math.floor(h * 6), f = h * 6 - k;
  const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  const [r, g, b] = [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][k % 6];
  return `rgb(${Math.round(r * 255)},${Math.round(g * 255)},${Math.round(b * 255)})`;
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (x, d = 1) => Number(x).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
const safeName = (n) => n.replace(/ /g, "_").replace(/\//g, "-");
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function show(el, on = true) { el.toggleAttribute("hidden", !on); }   // SVG elements have no .hidden property
function showError(el, msg) { el.textContent = msg || ""; show(el, !!msg); }

function selectTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".pane").forEach((p) => show(p, p.id === `pane-${name}`));
  if (name === "model") viewer.resize();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));

// ------------------------------------------------------------------ 1. source

// countries.json is built from geoBoundaries' own release list (releaseData/
// geoBoundariesOpen-meta.csv): [ISO3, name, {release: [levels]}]. Bundled with the site
// because the geoboundaries.org API is often down.
const countries = new Map();   // lower-case name or ISO -> ISO
const levels = new Map();      // ISO -> gbOpen levels, e.g. ["ADM0", "ADM1", "ADM2"]
fetch("countries.json")
  .then((r) => r.json())
  .then((list) => {
    const dl = $("#countries");
    for (const [iso, name, rel] of list) {
      countries.set(name.toLowerCase(), iso);
      countries.set(iso.toLowerCase(), iso);
      levels.set(iso, (rel.gbOpen || []).filter((l) => l !== "ADM0"));
      const o = document.createElement("option");
      o.value = name;
      o.label = iso;
      dl.append(o);
    }
  })
  .catch(() => {});   // the datalist is a nicety; typing an ISO3 code still works

function resolveIso(v) {
  v = v.trim();
  return countries.get(v.toLowerCase()) || (/^[a-z]{3}$/i.test(v) ? v.toUpperCase() : null);
}

// offer only the levels gbOpen actually has for the chosen country
$("#iso").addEventListener("input", () => {
  const iso = resolveIso($("#iso").value);
  const have = iso && levels.get(iso);
  if (!have || !have.length) return;
  const sel = $("#adm"), cur = sel.value;
  sel.innerHTML = have.map((l) => `<option>${l}</option>`).join("");
  sel.value = have.includes(cur) ? cur : have[0];
});

const GB = "https://media.githubusercontent.com/media/wmgeolab/geoBoundaries";

// gbOpen lives on GitHub's main branch, so it loads straight from the CDN. The other
// releases are only reachable through the geoBoundaries API, which names the commit
// the file sits at; media.githubusercontent.com serves that commit with CORS headers.
async function sourceUrls(rel, iso, adm) {
  if (rel === "gbOpen") {
    const base = `${GB}/main/releaseData/gbOpen/${iso}/${adm}/geoBoundaries-${iso}-${adm}`;
    return { geojson: base + ".geojson", meta: base + "-metaData.json" };
  }
  let info;
  try {
    const r = await fetch(`https://www.geoboundaries.org/api/current/${rel}/${iso}/${adm}/`, { signal: AbortSignal.timeout(20000) });
    if (r.status === 404) throw Object.assign(new Error("not found"), { status: 404 });
    info = await r.json();
  } catch (e) {
    if (e.status === 404) throw e;
    throw new Error(`the geoBoundaries server isn't responding, and ${rel} files are only available through it. Try gbOpen, or try again later.`);
  }
  if (!info || !info.gjDownloadURL) throw Object.assign(new Error("not found"), { status: 404 });
  const m = info.gjDownloadURL.match(/github\.com\/wmgeolab\/geoBoundaries\/raw\/([^/]+)\/(.+)$/);
  return { geojson: m ? `${GB}/${m[1]}/${m[2]}` : info.gjDownloadURL, meta: null, info };
}

async function download(url, onProgress) {
  const r = await fetch(url);
  if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
  const total = +r.headers.get("content-length") || 0;
  const reader = r.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    onProgress(got, total);
  }
  return new TextDecoder().decode(await new Blob(chunks).arrayBuffer());
}

$("#fetch").addEventListener("click", async () => {
  const err = $("#source-error");
  const iso = resolveIso($("#iso").value);
  if (!iso) return showError(err, "Enter a country name from the list or a 3-letter ISO code.");
  showError(err, "");
  const rel = $("#release").value, adm = $("#adm").value;
  const btn = $("#fetch");
  btn.disabled = true;
  try {
    btn.textContent = "Finding file…";
    const src = await sourceUrls(rel, iso, adm);
    btn.textContent = "Downloading…";
    const metaP = src.meta
      ? fetch(src.meta).then((r) => (r.ok ? r.json() : null)).catch(() => null)
      : Promise.resolve(src.info || null);
    const text = await download(src.geojson, (got, total) => {
      btn.textContent = total ? `Downloading ${Math.round((100 * got) / total)}%` : `Downloading ${fmt(got / 1e6)} MB`;
    });
    await loadText(text, "shapeName", `${iso.toLowerCase()}_${adm.toLowerCase()}`, await metaP);
  } catch (e) {
    showError(err, e.status === 404
      ? `geoBoundaries has no ${rel} ${adm} file for ${iso}. Try another release or level.`
      : `Couldn't load that: ${shortError(e)}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Load boundaries";
  }
});

$("#upload").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  showError($("#source-error"), "");
  try {
    await loadText(await f.text(), "shapeName", f.name.replace(/\.(geo)?json$/i, ""), null);
  } catch (err) {
    showError($("#source-error"), `Couldn't read that file: ${shortError(err)}`);
  }
  e.target.value = "";
});
document.querySelector(".link").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") $("#upload").click();
});

async function loadText(text, nameProp, base, meta) {
  setEngine("Reading boundaries…");
  let info;
  try {
    info = await call("load", { text, nameProp });
  } catch (e) {
    await recover(e);
    throw e;
  } finally {
    setEngine("ready");
  }
  Object.assign(state, { text, nameProp, base, features: info.features, overrides: {}, build: null });
  renderSourceInfo(info, meta);
  await runMatch();
  ["#step-names", "#step-layout", "#step-build"].forEach((s) => show($(s)));
  show($("#downloads"), false);
  ["model", "check"].forEach((t) => (document.querySelector(`[data-tab=${t}]`).disabled = true));
  selectTab("layout");
}

function renderSourceInfo(info, meta) {
  const el = $("#source-info");
  const verts = info.features.reduce((a, f) => a + f.verts, 0);
  const rows = [
    ["Regions", info.features.length],
    ["Detail", `${fmt(verts / info.features.length, 0)} points per region`],
  ];
  if (meta) rows.unshift(["Source", `${esc(meta.boundarySource)} (${esc(meta.boundaryYear)})`]);
  let html = rows.map(([k, v]) => `<div class="kv"><span>${k}</span><span>${v}</span></div>`).join("");
  const src = (meta && meta.boundarySource) || "";
  if (/natural earth/i.test(src) || verts / info.features.length < 150)
    html += `<div class="note warn">This file is coarse. For a small country, try gbHumanitarian.</div>`;
  if (/openstreetmap/i.test(src))
    html += `<div class="note">OpenStreetMap areas include lakes. If your quiz shows a big lake as water, it will print solid here.</div>`;
  if (info.wrapped) html += `<div class="note">This country crosses the 180° line; its eastern parts were shifted to sit next to the rest.</div>`;
  el.innerHTML = html;
  show(el);
}

// ------------------------------------------------------------------ 2. names

async function runMatch() {
  const m = await call("match", { quiz: $("#quiz").value });
  Object.assign(state, { assign: m.assign, quiz: m.quiz, notes: m.notes });
  $("#quiz-names").innerHTML = m.quiz.map((q) => `<option value="${esc(q)}">`).join("");
  renderFeatures();
  renderMatchSummary();
  schedulePrepare();
}

$("#match").addEventListener("click", () => runMatch().catch((e) => showError($("#source-error"), shortError(e))));

function renderFeatures() {
  const rows = state.features.map((f, i) => {
    const v = state.assign[i] || "";
    return `<tr><td class="src" title="${esc(f.name)}${f.parts > 1 ? ` · ${f.parts} parts` : ""}">${esc(f.name)}</td>
      <td><input data-i="${i}" value="${esc(v)}" list="quiz-names" placeholder="dropped" class="${v ? "" : "dropped"}" aria-label="Piece name for ${esc(f.name)}"></td>
      <td class="r">${fmt(f.km2, 0)}</td></tr>`;
  });
  $("#features tbody").innerHTML = rows.join("");
}

$("#features").addEventListener("input", (e) => {
  const i = e.target.dataset.i;
  if (i === undefined) return;
  state.assign[i] = e.target.value.trim();
  e.target.classList.toggle("dropped", !state.assign[i]);
  state.notes = [];
  renderMatchSummary();
  schedulePrepare();
});

function renderMatchSummary() {
  const pieces = [...new Set(state.assign.filter(Boolean))];
  let html = "";
  if (state.quiz.length) {
    const missing = state.quiz.filter((q) => !pieces.includes(q));
    const extra = pieces.filter((p) => !state.quiz.includes(p));
    const ok = !missing.length && !extra.length;
    html += `<div class="note ${ok ? "good" : "warn"}">${pieces.length} pieces · quiz has ${state.quiz.length}${ok ? " · every name matches" : ""}</div>`;
    if (missing.length) html += `<div class="note warn">Not matched yet: ${missing.map(esc).join(", ")}</div>`;
    if (extra.length) html += `<div class="note warn">Not in the quiz: ${extra.map(esc).join(", ")}</div>`;
  } else {
    html += `<div class="kv"><span>Pieces</span><span>${pieces.length}</span></div>`;
  }
  if (state.notes.length)
    html += `<details><summary>${state.notes.length} automatic change${state.notes.length > 1 ? "s" : ""}</summary><ul>${state.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></details>`;
  $("#match-summary").innerHTML = html;
}

// ------------------------------------------------------------------ 3. layout

let prepToken = 0;
const schedulePrepare = debounce(runPrepare, 250);
["#printer", "#bed-w", "#bed-h", "#margin", "#island", "#move", "#fill"].forEach((s) =>
  $(s).addEventListener("input", () => { state.overrides = {}; schedulePrepare(); }));

// printer picker: presets grouped by maker, plus a custom size; remembered per browser
(function initPrinters() {
  const sel = $("#printer");
  sel.innerHTML = Object.entries(PRINTERS).map(([maker, models]) =>
    `<optgroup label="${esc(maker)}">${models.map(([m, w, h]) =>
      `<option value="${esc(maker)}|${esc(m)}">${esc(maker)} ${esc(m)} · ${w} × ${h} mm</option>`).join("")}</optgroup>`,
  ).join("") + `<option value="custom">Custom size…</option>`;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("printer") || "{}"); } catch {}
  sel.value = saved.id && [...sel.options].some((o) => o.value === saved.id) ? saved.id : DEFAULT_PRINTER;
  if (saved.w) $("#bed-w").value = saved.w;
  if (saved.h) $("#bed-h").value = saved.h;
  if (saved.margin != null) $("#margin").value = saved.margin;
  const sync = () => {
    show($("#custom-bed"), sel.value === "custom");
    try {
      localStorage.setItem("printer", JSON.stringify({ id: sel.value, w: +$("#bed-w").value, h: +$("#bed-h").value, margin: +$("#margin").value }));
    } catch {}
  };
  ["#printer", "#bed-w", "#bed-h", "#margin"].forEach((s) => $(s).addEventListener("input", sync));
  sync();
})();

function bedSize() {
  const v = $("#printer").value;
  if (v === "custom") return [Math.max(+$("#bed-w").value || 200, 40), Math.max(+$("#bed-h").value || 200, 40)];
  const [maker, model] = v.split("|");
  const hit = (PRINTERS[maker] || []).find(([m]) => m === model);
  return hit ? [hit[1], hit[2]] : [180, 180];
}

function printerName() {
  const v = $("#printer").value;
  return v === "custom" ? "custom" : v.replace("|", " ");
}

async function runPrepare() {
  if (!state.features.length) return;
  if (!state.assign.some(Boolean)) {
    $("#layout-info").innerHTML = `<div class="note warn">Every region is dropped. Give at least one a piece name.</div>`;
    return;
  }
  const token = ++prepToken;
  setEngine("Laying out…");
  try {
    const prep = await call("prepare", {
      assign: state.assign, bed_w: bedSize()[0], bed_h: bedSize()[1],
      margin: Math.max(+$("#margin").value || 0, 0), island_mm2: +$("#island").value || 0,
      move_far: $("#move").checked, fill_holes: $("#fill").checked, overrides: state.overrides,
    });
    if (token !== prepToken) return;
    state.prep = prep;
    renderMap(prep);
    renderLayoutInfo(prep);
  } catch (e) {
    $("#layout-info").innerHTML = `<div class="note bad">Layout failed: ${esc(shortError(e))}</div>`;
    await recover(e);
  } finally {
    if (token === prepToken) setEngine("ready");
  }
}

function renderMap(prep) {
  const svg = $("#map");
  const n = prep.units.length;
  const [w, h] = prep.plate;
  const [bw, bh] = prep.bed;
  // the bed fills the view; the plate sits centred on it, as the slicer would place it
  const pad = Math.max(bw, bh) * 0.03;
  svg.setAttribute("viewBox", `${-pad} ${-pad} ${bw + 2 * pad} ${bh + 2 * pad}`);
  let html = `<rect class="bed" x="0" y="0" width="${bw}" height="${bh}" rx="${Math.max(bw, bh) * 0.01}" vector-effect="non-scaling-stroke"/>`;
  html += `<g transform="translate(${(bw - w) / 2} ${bh - (bh - h) / 2}) scale(1 -1)">`;
  prep.units.forEach((u, i) => {
    const col = palette(i, n);
    for (const p of u.parts) {
      const cls = p.kept ? (p.moved ? "moved" : "kept") : "dropped";
      html += `<path d="${p.d}" class="${cls}" fill="${col}" data-u="${i}" data-id="${esc(p.id)}"/>`;
    }
  });
  svg.innerHTML = html + "</g>";
  $("#bed-label").textContent = `${printerName()} bed, ${bw} × ${bh} mm`;
  show($("#empty"), false);
  show(svg);
  show($("#legend"));
}

function partOf(target) {
  const id = target.dataset && target.dataset.id;
  if (!id || !state.prep) return null;
  const u = state.prep.units[target.dataset.u];
  return { u, p: u.parts.find((p) => p.id === id) };
}

$("#map").addEventListener("pointermove", (e) => {
  const tip = $("#tip");
  const hit = partOf(e.target);
  if (!hit) return show(tip, false);
  const { u, p } = hit;
  const what = !p.kept ? "dropped · click to keep" : p.main ? "main part" : p.moved ? "moved closer · click to drop" : "click to drop";
  tip.innerHTML = `<b>${esc(u.name)}</b><br>${fmt(p.mm2)} mm² · ${fmt(p.km2, 0)} km²<br>${what}`;
  const box = $("#pane-layout").getBoundingClientRect();
  tip.style.left = Math.min(e.clientX - box.left + 14, box.width - 250) + "px";
  tip.style.top = e.clientY - box.top + 14 + "px";
  show(tip);
});
$("#map").addEventListener("pointerleave", () => show($("#tip"), false));
$("#map").addEventListener("click", (e) => {
  const hit = partOf(e.target);
  if (!hit) return;
  if (hit.p.main) {
    $("#layout-info").insertAdjacentHTML("afterbegin", `<div class="note">The largest part of each piece is always kept.</div>`);
    return;
  }
  state.overrides[hit.p.id] = !hit.p.kept;
  show($("#tip"), false);
  schedulePrepare();
});

function renderLayoutInfo(prep) {
  const rows = [
    ["Plate", `${fmt(prep.plate[0])} × ${fmt(prep.plate[1])} mm on a ${prep.bed[0]} × ${prep.bed[1]} bed`],
    ["Scale", `${fmt(prep.scale)} mm per degree`],
    ["Pieces", prep.units.length],
  ];
  let html = rows.map(([k, v]) => `<div class="kv"><span>${k}</span><span>${v}</span></div>`).join("");
  if (prep.turned) html += `<div class="note">The map fits bigger with the bed turned sideways. Rotate the plate 90° in your slicer.</div>`;
  for (const m of prep.moved)
    html += `<div class="note good">Moved ${esc(m.units.join(", "))} ${fmt(m.km, 0)} km closer</div>`;
  if (prep.gain_pct > 0.05) html += `<div class="note good">Moving parts makes the whole print ${fmt(prep.gain_pct, 0)}% bigger</div>`;
  if (prep.dropped) html += `<div class="kv"><span>Small islands dropped</span><span>${prep.dropped}</span></div>`;
  if (prep.filled_holes) html += `<div class="kv"><span>Holes filled</span><span>${prep.filled_holes}</span></div>`;
  const tiny = prep.units.filter((u) => u.mm2 < KEP).sort((a, b) => a.mm2 - b.mm2);
  if (tiny.length)
    html += `<div class="note warn">${tiny.map((u) => `${esc(u.name)} (${fmt(u.mm2)} mm²)`).join(", ")} ${tiny.length > 1 ? "are" : "is"} smaller than the smallest tile printed so far (${KEP} mm²).</div>`;
  $("#layout-info").innerHTML = html;
}

// ------------------------------------------------------------------ 4. build

$("#build").addEventListener("click", async () => {
  if (!state.prep) return;
  const btn = $("#build");
  btn.disabled = true;
  btn.textContent = "Building…";
  showError($("#build-error"), "");
  $("#log").textContent = "";
  selectTab("log");
  setEngine("Building plate…");
  try {
    const rep = await call("build", {
      base: state.base, target: state.prep.target,
      thickness: +$("#thickness").value || 1.6, label_depth: +$("#depth").value || 0.6,
      gap: +$("#gap").value, labels: $("#labels").checked, min_glyph: FAINT,
    });
    state.build = rep;
    renderDownloads(rep);
    renderCheck(rep);
    ["model", "check"].forEach((t) => (document.querySelector(`[data-tab=${t}]`).disabled = false));
    selectTab("model");
    viewer.load(rep, state.prep.units.map((u) => u.name));
  } catch (e) {
    showError($("#build-error"), `Build failed: ${shortError(e)}`);
    await recover(e);
  } finally {
    btn.disabled = false;
    btn.textContent = "Build plate";
    setEngine("ready");
  }
});

const urls = [];
function blobLink(data, type, name, label, cls = "") {
  const url = URL.createObjectURL(new Blob([data], { type }));
  urls.push(url);
  return `<a href="${url}" download="${esc(name)}" class="${cls}">${label}</a>`;
}

function renderDownloads(rep) {
  urls.splice(0).forEach((u) => URL.revokeObjectURL(u));
  const b = state.base;
  $("#downloads").innerHTML =
    blobLink(rep.data.plate, "model/3mf", `${b}_plate.3mf`, "Download plate (.3mf)", "main") +
    blobLink(rep.data.zip, "application/zip", `${b}_stl.zip`, "STL per tile (.zip)") +
    blobLink(rep.data.geojson, "application/geo+json", `${b}_prepared.geojson`, "Prepared GeoJSON");
  show($("#downloads"));
  $("#img-back").src = URL.createObjectURL(new Blob([rep.data.back], { type: "image/png" }));
}

function renderCheck(rep) {
  const t = rep.tiles;
  const minG = t.filter((r) => r.glyph > 0).reduce((a, r) => (r.glyph < a.glyph ? r : a), { glyph: 99 });
  const minW = t.reduce((a, r) => (r.waist < a.waist ? r : a), { waist: 99 });
  const extras = t.filter((r) => r.extra.length);
  const pretty = (s) => s.replace(/_/g, " ");
  let html = `<div class="kv"><span>Tiles</span><span>${t.length}</span></div>`;
  html += `<div class="kv"><span>Smallest label</span><span>${fmt(minG.glyph, 2)} mm · ${esc(pretty(minG.tile || ""))}</span></div>`;
  html += `<div class="kv"><span>Narrowest point</span><span>${fmt(minW.waist)} mm · ${esc(pretty(minW.tile))}</span></div>`;
  if (rep.gap) html += `<div class="kv"><span>Puzzle gaps</span><span>${rep.gap[0]}–${rep.gap[1]} mm across ${rep.pairs} pairs</span></div>`;
  html += rep.overlaps.length
    ? `<div class="note bad">Overlapping tiles: ${rep.overlaps.map(esc).join(", ")}</div>`
    : `<div class="note good">No tiles overlap</div>`;
  if (rep.faint.length) html += `<div class="note warn">Labels under ${FAINT} mm may print blank: ${rep.faint.map(pretty).map(esc).join(", ")}</div>`;
  if (extras.length) html += `<div class="note">${extras.length} tile${extras.length > 1 ? "s have" : " has"} extra loose pieces (islands or exclaves). They print unlabelled.</div>`;
  $("#check-summary").innerHTML = html;
  $("#check tbody").innerHTML = [...t].sort((a, b) => a.glyph - b.glyph).map((r) => `<tr>
    <td>${esc(pretty(r.tile))}</td>
    <td class="r ${r.glyph > 0 && r.glyph < FAINT ? "flag" : ""}">${r.glyph ? fmt(r.glyph, 2) : "none"}</td>
    <td class="r ${r.waist < 1 ? "flag-warn" : ""}">${fmt(r.waist)}</td>
    <td class="r">${fmt(r.area)}</td>
    <td>${r.extra.length ? r.extra.map((a) => fmt(a) + " mm²").join(", ") : ""}</td></tr>`).join("");
}

// ------------------------------------------------------------------ 3D viewer

const viewer = (() => {
  let renderer, scene, camera, controls, group, flipped = false;
  const el = $("#viewer");

  function init() {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    el.append(renderer.domElement);
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(30, 1, 1, 5000);
    camera.up.set(0, 0, 1);
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8a80, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(-120, -160, 260);
    scene.add(key);
    const rake = new THREE.DirectionalLight(0xffffff, 0.8);   // low light shows the engraving
    rake.position.set(200, 80, 60);
    scene.add(rake);
    new ResizeObserver(resize).observe(el);
    (function loop() {
      requestAnimationFrame(loop);
      controls.update();
      renderer.render(scene, camera);
    })();
  }

  function resize() {
    if (!renderer || !el.clientWidth) return;
    renderer.setSize(el.clientWidth, el.clientHeight);
    camera.aspect = el.clientWidth / el.clientHeight;
    camera.updateProjectionMatrix();
  }

  function load(rep, unitNames) {
    if (!renderer) init();
    if (group) {
      scene.remove(group);
      group.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
    }
    group = new THREE.Group();
    const loader = new STLLoader();
    const index = new Map(unitNames.map((n, i) => [safeName(n) + ".stl", i]));
    const box = new THREE.Box3();
    for (const s of rep.stlData) {
      const geo = loader.parse(s.data.buffer.slice(s.data.byteOffset, s.data.byteOffset + s.data.byteLength));
      const i = index.get(s.file) ?? 0;
      const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: palette(i, unitNames.length), roughness: 0.75 }));
      group.add(mesh);
      geo.computeBoundingBox();
      box.union(geo.boundingBox);
    }
    const c = box.getCenter(new THREE.Vector3());
    group.children.forEach((m) => m.position.sub(c));
    const pivot = new THREE.Group();
    pivot.add(group);
    scene.add(pivot);
    group = pivot;
    const w = box.max.x - box.min.x, h = box.max.y - box.min.y;
    flipped = false;
    $("#flip").textContent = "Show back";
    resize();
    // back off until the whole plate fits the view, then tilt ~30 degrees
    const half = Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2);
    const d = 1.2 * Math.max(h / 2 / half, w / 2 / half / camera.aspect);
    camera.position.set(0, -d * 0.5, d * 0.87);
    controls.target.set(0, 0, 0);
    controls.update();
  }

  $("#flip").addEventListener("click", () => {
    if (!group) return;
    flipped = !flipped;
    group.rotation.y = flipped ? Math.PI : 0;   // flip left-right, like turning the tiles over
    $("#flip").textContent = flipped ? "Show front" : "Show back";
  });

  return { load, resize };
})();

// ------------------------------------------------------------------ go

startWorker();
