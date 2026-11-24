"use strict";

const $ = (sel) => document.querySelector(sel);
const EXTS = [".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a"];
const STATUS = { queued: "Wartet", running: "Läuft", done: "Fertig", failed: "Fehler", cancelled: "Abgebrochen" };
const PHASE = {
  downloading: "Modell wird einmalig heruntergeladen …",
  loading: "Modell wird geladen …",
  extracting: "Audiospur wird extrahiert …",
  transcribing: "Transkription läuft",
  writing: "Dateien werden geschrieben …",
};

let META = null;
let SETTINGS = null;
let STATE = null;
let uploading = false;

// ---------- helpers ----------
async function api(url, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(url, init);
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtClock(sec) {
  if (sec == null || !isFinite(sec)) return "–";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtEta(sec) {
  if (sec == null || !isFinite(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `ca. ${Math.max(5, Math.ceil(sec / 5) * 5)} s`;
  const m = Math.round(sec / 60);
  if (m < 60) return `ca. ${m} min`;
  return `ca. ${Math.floor(m / 60)} h ${m % 60} min`;
}

function fmtSize(b) {
  if (!b) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i >= 2 ? 1 : 0).replace(".", ",")} ${u[i]}`;
}

function langName(code) {
  if (!code || code === "auto") return "";
  const l = META?.languages.find((x) => x.code === code);
  return l ? l.name : code;
}

function modelShort(id) {
  const m = META?.models.find((x) => x.id === id);
  return m ? m.label.replace(/\s*\(.*\)$/, "") : (id || "").split("/").pop();
}

let toastTimer;
function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast("In die Zwischenablage kopiert");
}

function summaryText(s) {
  const parts = [];
  if (s.added) parts.push(`${s.added} hinzugefügt`);
  if (s.requeued) parts.push(`${s.requeued} erneut eingereiht`);
  if (s.skipped) parts.push(`${s.skipped} übersprungen (bereits transkribiert)`);
  if (s.queued) parts.push(`${s.queued} bereits in der Warteschlange`);
  if (s.running) parts.push(`${s.running} wird gerade verarbeitet`);
  if (s.ignored) parts.push(`${s.ignored} ignoriert`);
  return parts.join(" · ") || "Keine unterstützten Dateien gefunden";
}

// ---------- settings ----------
async function loadMeta() {
  META = await api("/api/meta");
  SETTINGS = await api("/api/settings");

  const ms = $("#set-model");
  ms.innerHTML = META.models.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
  const ls = $("#set-language");
  ls.innerHTML = `<option value="auto">Automatisch erkennen</option>` +
    META.languages.map((l) => `<option value="${esc(l.code)}">${esc(l.name)}</option>`).join("");

  ms.value = SETTINGS.model;
  ls.value = SETTINGS.language;
  $("#set-recursive").checked = SETTINGS.recursive;
  $("#set-merge").checked = SETTINGS.merge;
  $("#set-delete-uploads").checked = SETTINGS.delete_uploads;
  $("#set-output").value = SETTINGS.output_dir;
  $("#set-prompt").value = SETTINGS.initial_prompt;
  $("#set-timestamps").checked = SETTINGS.txt_timestamps;
  $("#set-replacements").value = SETTINGS.replacements;
  document.querySelectorAll("#set-formats input").forEach((cb) => { cb.checked = SETTINGS.formats.includes(cb.value); });
  updateModelInfo();

  const gpu = META.device.includes("gpu");
  $("#chip-device").textContent = gpu ? "Apple GPU (Metal)" : META.device;
  $("#chip-device").classList.toggle("on", gpu);
  $("#sys-info").textContent = `Version ${META.version} · ${gpu ? "MLX auf Apple GPU" : META.device} · FFmpeg: ${META.ffmpeg}`;
}

function updateModelInfo() {
  const m = META.models.find((x) => x.id === $("#set-model").value);
  if (!m) return;
  $("#model-info").textContent = m.downloaded
    ? `${m.size} · lokal vorhanden ✓`
    : `${m.size} · wird beim ersten Start einmalig heruntergeladen`;
}

let savedTimer;
async function saveSettings(patch) {
  try {
    SETTINGS = await api("/api/settings", { method: "PUT", body: patch });
    const h = $("#saved-hint");
    h.textContent = "Gespeichert ✓";
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => (h.innerHTML = "&nbsp;"), 1500);
  } catch (e) {
    toast(e.message);
    $("#set-output").value = SETTINGS.output_dir;
  }
}

function bindSettings() {
  $("#set-model").addEventListener("change", (e) => { saveSettings({ model: e.target.value }); updateModelInfo(); });
  $("#set-language").addEventListener("change", (e) => saveSettings({ language: e.target.value }));
  $("#set-recursive").addEventListener("change", (e) => saveSettings({ recursive: e.target.checked }));
  $("#set-merge").addEventListener("change", (e) => saveSettings({ merge: e.target.checked }));
  $("#set-delete-uploads").addEventListener("change", (e) => saveSettings({ delete_uploads: e.target.checked }));
  $("#set-timestamps").addEventListener("change", (e) => saveSettings({ txt_timestamps: e.target.checked }));
  $("#set-formats").addEventListener("change", () => {
    const formats = [...document.querySelectorAll("#set-formats input:checked")].map((cb) => cb.value);
    saveSettings({ formats });
  });
  const out = $("#set-output");
  out.addEventListener("change", () => saveSettings({ output_dir: out.value.trim() }));
  out.addEventListener("keydown", (e) => { if (e.key === "Enter") out.blur(); });
  let pt, rt;
  $("#set-prompt").addEventListener("input", (e) => {
    clearTimeout(pt);
    pt = setTimeout(() => saveSettings({ initial_prompt: e.target.value }), 600);
  });
  $("#set-replacements").addEventListener("input", (e) => {
    clearTimeout(rt);
    rt = setTimeout(() => saveSettings({ replacements: e.target.value }), 600);
  });
  $("#btn-pick-output").addEventListener("click", async () => {
    try {
      const r = await api("/api/pick", { method: "POST", body: { kind: "output" } });
      if (r.paths.length) { out.value = r.paths[0]; saveSettings({ output_dir: r.paths[0] }); }
    } catch (e) { toast(e.message); }
  });
}

// ---------- adding files ----------
async function addPaths(paths) {
  if (!paths.length) return;
  try {
    const s = await api("/api/jobs/add-paths", { method: "POST", body: { paths } });
    toast(summaryText(s));
    refresh();
  } catch (e) { toast(e.message); }
}

async function pickNative(kind, fallbackInput) {
  try {
    const r = await api("/api/pick", { method: "POST", body: { kind } });
    await addPaths(r.paths);
  } catch (e) {
    fallbackInput.click();
  }
}

function isMediaName(name) {
  const n = name.toLowerCase();
  return !n.startsWith(".") && EXTS.some((e) => n.endsWith(e));
}

function readEntries(reader) {
  return new Promise((res, rej) => reader.readEntries(res, rej));
}

async function collectDropped(dt, recursive) {
  const entries = [...dt.items]
    .filter((i) => i.kind === "file")
    .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null));
  const out = [];
  async function walk(entry, prefix, depth) {
    if (!entry) return;
    if (entry.isFile) {
      if (!isMediaName(entry.name)) return;
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file, path: prefix + file.name });
    } else if (entry.isDirectory) {
      if (depth > 0 && !recursive) return;
      const reader = entry.createReader();
      let batch;
      do {
        batch = await readEntries(reader);
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`, depth + 1);
      } while (batch.length);
    }
  }
  if (entries.some(Boolean)) {
    for (const e of entries) await walk(e, "", 0);
  } else {
    for (const f of dt.files) if (isMediaName(f.name)) out.push({ file: f, path: f.name });
  }
  return out;
}

function putFile(item, onProgress) {
  return new Promise((resolve, reject) => {
    const q = new URLSearchParams({ path: item.path, size: item.file.size, mtime: item.file.lastModified / 1000 });
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/uploads?${q}`);
    xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => (xhr.status < 300 ? resolve() : reject(new Error(xhr.responseText || xhr.statusText)));
    xhr.onerror = () => reject(new Error("Upload fehlgeschlagen"));
    xhr.send(item.file);
  });
}

async function uploadItems(items) {
  if (!items.length) { toast("Keine unterstützten Dateien gefunden"); return; }
  if (uploading) { toast("Bitte warten, es wird noch kopiert …"); return; }
  uploading = true;
  const box = $("#upload-status"), label = $("#upload-label"), pct = $("#upload-pct"), bar = $("#upload-bar");
  try {
    const files = items.map((x) => ({ path: x.path, size: x.file.size, mtime: x.file.lastModified / 1000 }));
    const check = await api("/api/uploads/check", { method: "POST", body: { files } });
    const needed = new Set(check.files.filter((c) => c.state === "needed").map((c) => c.path));
    const todo = items.filter((x) => needed.has(x.path));
    const total = todo.reduce((a, x) => a + x.file.size, 0);
    let doneBytes = 0;
    if (todo.length) box.hidden = false;
    for (let i = 0; i < todo.length; i++) {
      const it = todo[i];
      label.textContent = `Kopiere ${i + 1}/${todo.length}: ${it.path}`;
      await putFile(it, (loaded) => {
        const p = total ? ((doneBytes + loaded) / total) * 100 : 100;
        bar.style.width = `${p}%`;
        pct.textContent = `${Math.floor(p)} %`;
      });
      doneBytes += it.file.size;
    }
    const valid = files.filter((f) => check.files.find((c) => c.path === f.path)?.state !== "invalid");
    const s = await api("/api/jobs/add-uploaded", { method: "POST", body: { files: valid } });
    toast(summaryText(s));
    refresh();
  } catch (e) {
    toast(`Fehler beim Kopieren: ${e.message}`, 6000);
  } finally {
    uploading = false;
    box.hidden = true;
    bar.style.width = "0";
  }
}

function bindAdding() {
  const dz = $("#dropzone");
  let depth = 0;
  dz.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; dz.classList.add("over"); });
  dz.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
  dz.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; dz.classList.remove("over"); } });
  dz.addEventListener("drop", async (e) => {
    e.preventDefault();
    depth = 0;
    dz.classList.remove("over");
    const items = await collectDropped(e.dataTransfer, $("#set-recursive").checked);
    uploadItems(items);
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  const inFiles = $("#input-files"), inDir = $("#input-dir");
  inFiles.addEventListener("change", () => {
    uploadItems([...inFiles.files].filter((f) => isMediaName(f.name)).map((f) => ({ file: f, path: f.name })));
    inFiles.value = "";
  });
  inDir.addEventListener("change", () => {
    const recursive = $("#set-recursive").checked;
    const items = [...inDir.files]
      .filter((f) => isMediaName(f.name))
      .filter((f) => recursive || (f.webkitRelativePath || f.name).split("/").length <= 2)
      .map((f) => ({ file: f, path: f.webkitRelativePath || f.name }));
    uploadItems(items);
    inDir.value = "";
  });

  $("#btn-pick-files").addEventListener("click", () => pickNative("files", inFiles));
  $("#btn-pick-folder").addEventListener("click", () => pickNative("folder", inDir));
  $("#path-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("#path-input").value.trim().replace(/^["']|["']$/g, "");
    if (v) { addPaths([v]); $("#path-input").value = ""; }
  });
}

// ---------- queue rendering ----------
function jobHtml(j, worker) {
  const ext = (j.name.split(".").pop() || "").slice(0, 4).toUpperCase();
  const meta = [];
  const side = [];
  let bar = "";

  if (j.duration) meta.push(`<span>${fmtClock(j.duration)}</span>`);
  if (j.size) meta.push(`<span>${fmtSize(j.size)}</span>`);

  if (j.status === "running") {
    const phase = j.phase || worker.phase;
    const p = j.progress || 0;
    const label = phase === "transcribing" ? `${PHASE.transcribing} · ${Math.floor(p * 100)} %` : (PHASE[phase] || "Wird vorbereitet …");
    meta.push(`<span>${esc(label)}</span>`);
    meta.push(`<span>Laufzeit ${fmtClock(j.elapsed)}</span>`);
    if (j.eta != null && phase === "transcribing") meta.push(`<span>Rest ${fmtEta(j.eta)}</span>`);
    const indet = phase !== "transcribing" || p === 0;
    bar = `<div class="bar"><div class="bar-fill ${indet ? "indet" : ""}" style="width:${p * 100}%"></div></div>`;
  } else if (j.status === "done") {
    if (j.detected_language) meta.push(`<span>${esc(langName(j.detected_language))}</span>`);
    if (j.model) meta.push(`<span>${esc(modelShort(j.model))}</span>`);
    if (j.elapsed) meta.push(`<span>in ${fmtClock(j.elapsed)}</span>`);
  } else if (j.status === "failed") {
    meta.push(`<span class="err">${esc(j.error || "Unbekannter Fehler")}</span>`);
  }

  side.push(`<span class="pill ${j.status}">${STATUS[j.status] || j.status}</span>`);
  if (j.status === "done") {
    side.push(`<button class="btn sm" data-act="preview">Vorschau</button>`);
    side.push(`<button class="btn sm" data-act="copy">Kopieren</button>`);
    side.push(`<span class="dl">${["txt", "srt", "vtt", "json"].map((f) => `<a href="/api/jobs/${j.id}/download/${f}" download>${f.toUpperCase()}</a>`).join("")}</span>`);
    side.push(`<button class="x" data-act="reveal" title="Im Finder zeigen" aria-label="Im Finder zeigen"><svg viewBox="0 0 24 24" width="15" height="15"><path fill="currentColor" d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2Z"/></svg></button>`);
  }
  if (j.status === "failed" || j.status === "cancelled") side.push(`<button class="btn sm" data-act="retry">Wiederholen</button>`);
  if (j.status === "done") side.push(`<button class="x" data-act="retry" title="Neu transkribieren" aria-label="Neu transkribieren">↻</button>`);
  if (j.status !== "running") side.push(`<button class="x" data-act="remove" title="Aus Liste entfernen">✕</button>`);

  return `<div class="job-icon">${esc(ext)}</div>
    <div class="job-main">
      <div class="job-name" title="${esc(j.source_path)}">${esc(j.name)}</div>
      <div class="job-meta">${meta.join("")}</div>
      ${bar}
    </div>
    <div class="job-side">${side.join("")}</div>`;
}

const rows = new Map();

function render(state) {
  STATE = state;
  const { jobs, counts, overall, worker } = state;
  const list = $("#jobs");
  $("#empty").hidden = jobs.length > 0;

  const seen = new Set();
  let order = [];
  for (const j of jobs) {
    seen.add(j.id);
    let li = rows.get(j.id);
    if (!li) {
      li = document.createElement("li");
      li.className = "job";
      li.dataset.id = j.id;
      rows.set(j.id, li);
    }
    const html = jobHtml(j, worker);
    if (li._html !== html) { li.innerHTML = html; li._html = html; }
    order.push(li);
  }
  for (const [id, li] of rows) if (!seen.has(id)) { li.remove(); rows.delete(id); }
  const current = [...list.children];
  if (current.length !== order.length || current.some((el, i) => el !== order[i])) {
    order.forEach((li) => list.appendChild(li));
  }

  const parts = [];
  if (counts.queued) parts.push(`${counts.queued} wartend`);
  if (counts.done) parts.push(`${counts.done} fertig`);
  if (counts.failed) parts.push(`${counts.failed} fehlgeschlagen`);
  if (counts.cancelled) parts.push(`${counts.cancelled} abgebrochen`);
  $("#queue-counts").textContent = parts.join(" · ");

  const pending = (counts.queued || 0) + (counts.cancelled || 0);
  const startBtn = $("#btn-start");
  startBtn.hidden = worker.active;
  startBtn.disabled = !pending;
  startBtn.textContent = counts.cancelled || (counts.queued && (counts.done || counts.failed)) ? "Fortsetzen" : "Starten";
  $("#btn-cancel").hidden = !worker.active;
  $("#btn-retry-failed").hidden = !counts.failed;

  const ov = $("#overall");
  ov.hidden = jobs.length === 0;
  if (jobs.length) {
    $("#overall-bar").style.width = `${overall.progress * 100}%`;
    $("#overall-label").textContent = `${overall.done} von ${overall.total} Dateien fertig · ${Math.floor(overall.progress * 100)} %`;
    $("#overall-eta").textContent = worker.active && overall.eta != null ? `Restzeit gesamt ${fmtEta(overall.eta)}` : "";
    let msg = "";
    const cur = jobs.find((j) => j.status === "running");
    if (worker.active && cur) msg = `${worker.message || "Verarbeite"} ${cur.name}`;
    else if (!worker.active && pending) msg = "Warteschlange pausiert – zum Verarbeiten auf „" + startBtn.textContent + "“ klicken.";
    else if (!worker.active && jobs.length && !pending && !counts.running) msg = counts.failed ? "Fertig – einige Dateien sind fehlgeschlagen." : "Alle Dateien sind transkribiert.";
    $("#overall-msg").textContent = msg;
  }

  const chip = $("#chip-model");
  chip.textContent = worker.loaded_model ? `Modell geladen: ${modelShort(worker.loaded_model)}` : "Modell nicht geladen";
  chip.classList.toggle("on", !!worker.loaded_model);

  if (worker.loaded_model && META) {
    const m = META.models.find((x) => x.id === worker.loaded_model);
    if (m && !m.downloaded) { m.downloaded = true; updateModelInfo(); }
  }
}

let pollTimer, failCount = 0;
async function refresh() {
  try {
    render(await api("/api/state"));
    failCount = 0;
  } catch (e) {
    if (++failCount === 3) toast("Server nicht erreichbar – läuft start.sh noch?", 6000);
  }
}
function poll() {
  clearTimeout(pollTimer);
  refresh().finally(() => { pollTimer = setTimeout(poll, STATE?.worker.active ? 700 : 1500); });
}

// ---------- queue actions ----------
function bindQueue() {
  $("#btn-start").addEventListener("click", async () => { await api("/api/queue/start", { method: "POST" }); refresh(); });
  $("#btn-cancel").addEventListener("click", async () => { await api("/api/queue/cancel", { method: "POST" }); refresh(); });
  $("#btn-retry-failed").addEventListener("click", async () => { await api("/api/jobs/retry-failed", { method: "POST" }); refresh(); });

  const pop = $("#menu-pop");
  $("#btn-menu").addEventListener("click", (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; });
  document.addEventListener("click", () => (pop.hidden = true));
  pop.addEventListener("click", async (e) => {
    const act = e.target.dataset.act;
    if (!act) return;
    pop.hidden = true;
    try {
      if (act === "merge") {
        const r = await api("/api/merge", { method: "POST" });
        toast(`${r.count} Transkripte zusammengeführt: ${r.path}`, 6000);
      } else if (act === "merged-dl") {
        window.location.href = "/api/merged";
      } else if (act === "open-output") {
        await api("/api/open", { method: "POST", body: { target: "output" } });
      } else if (act.startsWith("clear-")) {
        const which = { "clear-done": "done", "clear-finished": "finished", "clear-all": "all" }[act];
        if (which === "all" && !confirm("Alle nicht laufenden Einträge aus der Liste entfernen? Bereits erstellte Transkripte bleiben erhalten.")) return;
        await api(`/api/jobs/clear?which=${which}`, { method: "POST" });
        refresh();
      }
    } catch (err) { toast(err.message); }
  });

  $("#jobs").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = Number(btn.closest(".job").dataset.id);
    const act = btn.dataset.act;
    try {
      if (act === "preview") openPreview(id);
      else if (act === "copy") copyText((await api(`/api/jobs/${id}/transcript`)).text);
      else if (act === "retry") { await api(`/api/jobs/${id}/retry`, { method: "POST" }); refresh(); }
      else if (act === "remove") { await api(`/api/jobs/${id}`, { method: "DELETE" }); refresh(); }
      else if (act === "reveal") await api("/api/open", { method: "POST", body: { job_id: id, target: "output" } });
    } catch (err) { toast(err.message); }
  });
}

// ---------- preview modal ----------
let preview = null;
async function openPreview(id) {
  const t = await api(`/api/jobs/${id}/transcript`);
  preview = { id, ...t, view: "text" };
  $("#modal-title").textContent = t.name;
  const words = t.text.trim().split(/\s+/).filter(Boolean).length;
  $("#modal-sub").textContent = `${langName(t.language)} · ${t.segments.length} Segmente · ${words.toLocaleString("de-DE")} Wörter`;
  $("#modal-dl").innerHTML = ["txt", "srt", "vtt", "json"].map((f) => `<a href="/api/jobs/${id}/download/${f}" download>${f.toUpperCase()}</a>`).join("");
  setView("text");
  $("#modal").hidden = false;
}

function setView(view) {
  preview.view = view;
  document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  const body = $("#modal-body");
  if (view === "text") body.textContent = preview.text;
  else body.innerHTML = preview.segments.map((s) => `<div class="seg-row"><time>${fmtClock(s.start)}</time><span>${esc(s.text)}</span></div>`).join("");
  body.scrollTop = 0;
}

function bindModal() {
  $("#modal").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) $("#modal").hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").hidden = true; });
  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  $("#modal-copy").addEventListener("click", () => {
    if (!preview) return;
    const text = preview.view === "text" ? preview.text : preview.segments.map((s) => `[${fmtClock(s.start)}] ${s.text}`).join("\n");
    copyText(text);
  });
}

// ---------- init ----------
(async function init() {
  bindSettings();
  bindAdding();
  bindQueue();
  bindModal();
  try { await loadMeta(); } catch (e) { toast(`Fehler beim Laden: ${e.message}`, 8000); }
  poll();
})();
