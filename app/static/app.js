"use strict";

const $ = (sel) => document.querySelector(sel);
const EXTS = [".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a"];
const FORMATS = ["txt", "srt", "vtt", "json"];

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
    throw new Error(translateMessage(msg));
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
  if (sec < 60) return t("eta.s", { n: Math.max(5, Math.ceil(sec / 5) * 5) });
  const m = Math.round(sec / 60);
  if (m < 60) return t("eta.m", { n: m });
  return t("eta.h", { h: Math.floor(m / 60), m: m % 60 });
}

function fmtNumber(n, digits = 0) {
  return n.toLocaleString(LANG, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtSize(b) {
  if (!b) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${fmtNumber(b, i >= 2 ? 1 : 0)} ${u[i]}`;
}

function fmtModelSize(mb) {
  return mb >= 1000 ? `${fmtNumber(mb / 1000, 1)} GB` : `${mb} MB`;
}

let displayNames = null;
function langName(code) {
  if (!code || code === "auto") return "";
  try {
    displayNames ??= new Intl.DisplayNames([LANG], { type: "language" });
    const name = displayNames.of(code);
    if (name && name !== code) return name.charAt(0).toUpperCase() + name.slice(1);
  } catch (_) {}
  return META?.languages.find((x) => x.code === code)?.name || code;
}

function modelName(id) {
  return META?.models.find((x) => x.id === id)?.name || (id || "").split("/").pop();
}

function modelLabel(m) {
  return m.tag ? `${m.name} (${t(`model.tag.${m.tag}`)})` : m.name;
}

let toastTimer;
function toast(msg, ms = 3500) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), ms);
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
  toast(t("toast.copied"));
}

function summaryText(s) {
  const parts = ["added", "requeued", "skipped", "queued", "running", "ignored"]
    .filter((k) => s[k])
    .map((k) => t(`summary.${k}`, { n: s[k] }));
  return parts.join(" · ") || t("summary.none");
}

// ---------- language ----------
function setLang(lang) {
  LANG = lang;
  localStorage.setItem("ui-lang", lang);
  displayNames = null;
  document.querySelectorAll("#lang-switch button").forEach((b) => b.classList.toggle("active", b.dataset.lang === lang));
  applyStaticI18n();
  if (META) {
    populateSelects();
    updateModelInfo();
    updateSysInfo();
  }
  rows.forEach((li) => { li._html = null; });
  if (STATE) render(STATE);
  if (preview && !$("#modal").hidden) updatePreviewHeader();
}

// ---------- settings ----------
function populateSelects() {
  const ms = $("#set-model"), ls = $("#set-language");
  const model = ms.value || SETTINGS.model, language = ls.value || SETTINGS.language;
  ms.innerHTML = META.models.map((m) => `<option value="${esc(m.id)}">${esc(modelLabel(m))}</option>`).join("");
  const langs = META.languages
    .map((l) => ({ code: l.code, name: langName(l.code) }))
    .sort((a, b) => {
      const pa = ["en", "de"].indexOf(a.code), pb = ["en", "de"].indexOf(b.code);
      if (pa !== pb) return (pa === -1 ? 9 : pa) - (pb === -1 ? 9 : pb);
      return a.name.localeCompare(b.name, LANG);
    });
  ls.innerHTML = `<option value="auto">${esc(t("settings.language_auto"))}</option>` +
    langs.map((l) => `<option value="${esc(l.code)}">${esc(l.name)}</option>`).join("");
  ms.value = model;
  ls.value = language;
}

async function loadMeta() {
  META = await api("/api/meta");
  SETTINGS = await api("/api/settings");
  populateSelects();
  $("#set-model").value = SETTINGS.model;
  $("#set-language").value = SETTINGS.language;
  $("#set-recursive").checked = SETTINGS.recursive;
  $("#set-merge").checked = SETTINGS.merge;
  $("#set-delete-uploads").checked = SETTINGS.delete_uploads;
  $("#set-output").value = SETTINGS.output_dir;
  $("#set-prompt").value = SETTINGS.initial_prompt;
  $("#set-timestamps").checked = SETTINGS.txt_timestamps;
  $("#set-replacements").value = SETTINGS.replacements;
  document.querySelectorAll("#set-formats input").forEach((cb) => { cb.checked = SETTINGS.formats.includes(cb.value); });
  updateModelInfo();
  updateSysInfo();
}

function updateSysInfo() {
  const gpu = META.device.includes("gpu");
  $("#chip-device").textContent = gpu ? "Apple GPU (Metal)" : META.device;
  $("#chip-device").classList.toggle("on", gpu);
  $("#sys-info").textContent = t("sys.info", { v: META.version, device: gpu ? t("sys.gpu") : META.device, ffmpeg: META.ffmpeg });
}

function updateModelInfo() {
  const m = META.models.find((x) => x.id === $("#set-model").value);
  if (!m) return;
  $("#model-info").textContent = t(m.downloaded ? "settings.model_local" : "settings.model_download", { size: fmtModelSize(m.size_mb) });
}

let savedTimer;
async function saveSettings(patch) {
  try {
    SETTINGS = await api("/api/settings", { method: "PUT", body: patch });
    const h = $("#saved-hint");
    h.textContent = t("settings.saved");
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
      const r = await api("/api/pick", { method: "POST", body: { kind: "output", lang: LANG } });
      if (r.paths.length) { out.value = r.paths[0]; saveSettings({ output_dir: r.paths[0] }); }
    } catch (e) { toast(e.message); }
  });
  document.querySelectorAll("#lang-switch button").forEach((b) => b.addEventListener("click", () => setLang(b.dataset.lang)));
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
    const r = await api("/api/pick", { method: "POST", body: { kind, lang: LANG } });
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
    xhr.onload = () => {
      if (xhr.status < 300) return resolve();
      let msg = xhr.statusText;
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
      reject(new Error(translateMessage(msg)));
    };
    xhr.onerror = () => reject(new Error(t("upload.failed")));
    xhr.send(item.file);
  });
}

async function uploadItems(items) {
  if (!items.length) { toast(t("summary.none")); return; }
  if (uploading) { toast(t("upload.busy")); return; }
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
      label.textContent = t("upload.copying", { i: i + 1, n: todo.length, name: it.path });
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
    toast(t("upload.error", { msg: e.message }), 6000);
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
const FOLDER_ICON = `<svg viewBox="0 0 24 24" width="15" height="15"><path fill="currentColor" d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2Z"/></svg>`;

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
    const label = phase === "transcribing"
      ? `${t("phase.transcribing")} · ${Math.floor(p * 100)} %`
      : t(phase ? `phase.${phase}` : "phase.preparing");
    meta.push(`<span>${esc(label)}</span>`);
    meta.push(`<span>${esc(t("job.elapsed", { t: fmtClock(j.elapsed) }))}</span>`);
    if (j.eta != null && phase === "transcribing") meta.push(`<span>${esc(t("job.remaining", { eta: fmtEta(j.eta) }))}</span>`);
    const indet = phase !== "transcribing" || p === 0;
    bar = `<div class="bar"><div class="bar-fill ${indet ? "indet" : ""}" style="width:${p * 100}%"></div></div>`;
  } else if (j.status === "done") {
    if (j.detected_language) meta.push(`<span>${esc(langName(j.detected_language))}</span>`);
    if (j.model) meta.push(`<span>${esc(modelName(j.model))}</span>`);
    if (j.elapsed) meta.push(`<span>${esc(t("job.took", { t: fmtClock(j.elapsed) }))}</span>`);
  } else if (j.status === "failed") {
    meta.push(`<span class="err">${esc(j.error ? translateMessage(j.error) : t("job.unknown_error"))}</span>`);
  }

  side.push(`<span class="pill ${j.status}">${esc(t(`status.${j.status}`))}</span>`);
  if (j.status === "done") {
    side.push(`<button class="btn sm" data-act="preview">${esc(t("job.preview"))}</button>`);
    side.push(`<button class="btn sm" data-act="copy">${esc(t("job.copy"))}</button>`);
    side.push(`<span class="dl">${FORMATS.map((f) => `<a href="/api/jobs/${j.id}/download/${f}" download>${f.toUpperCase()}</a>`).join("")}</span>`);
    side.push(`<button class="x" data-act="reveal" title="${esc(t("job.reveal"))}" aria-label="${esc(t("job.reveal"))}">${FOLDER_ICON}</button>`);
    side.push(`<button class="x" data-act="retry" title="${esc(t("job.redo"))}" aria-label="${esc(t("job.redo"))}">↻</button>`);
  }
  if (j.status === "failed" || j.status === "cancelled") side.push(`<button class="btn sm" data-act="retry">${esc(t("job.retry"))}</button>`);
  if (j.status !== "running") side.push(`<button class="x" data-act="remove" title="${esc(t("job.remove"))}" aria-label="${esc(t("job.remove"))}">✕</button>`);

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
  const order = [];
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

  $("#queue-counts").textContent = ["queued", "done", "failed", "cancelled"]
    .filter((k) => counts[k])
    .map((k) => t(`counts.${k}`, { n: counts[k] }))
    .join(" · ");

  const pending = (counts.queued || 0) + (counts.cancelled || 0);
  const startBtn = $("#btn-start");
  startBtn.hidden = worker.active;
  startBtn.disabled = !pending;
  startBtn.textContent = t(counts.cancelled || (counts.queued && (counts.done || counts.failed)) ? "queue.resume" : "queue.start");
  $("#btn-cancel").hidden = !worker.active;
  $("#btn-retry-failed").hidden = !counts.failed;

  const ov = $("#overall");
  ov.hidden = jobs.length === 0;
  if (jobs.length) {
    $("#overall-bar").style.width = `${overall.progress * 100}%`;
    $("#overall-label").textContent = t("overall.label", { done: overall.done, total: overall.total, pct: Math.floor(overall.progress * 100) });
    $("#overall-eta").textContent = worker.active && overall.eta != null ? t("overall.eta", { eta: fmtEta(overall.eta) }) : "";
    let msg = "";
    const cur = jobs.find((j) => j.status === "running");
    if (worker.active && cur) {
      const phase = cur.phase || worker.phase;
      msg = `${cur.name} · ${t(phase ? `phase.${phase}` : "phase.preparing")}`;
    } else if (!worker.active && pending) {
      msg = t("overall.paused", { action: startBtn.textContent });
    } else if (!worker.active && !pending && !counts.running) {
      msg = t(counts.failed ? "overall.done_errors" : "overall.all_done");
    }
    $("#overall-msg").textContent = msg;
  }

  const chip = $("#chip-model");
  chip.textContent = worker.loaded_model ? t("chip.model_loaded", { name: modelName(worker.loaded_model) }) : t("chip.model_none");
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
    if (++failCount === 3) toast(t("toast.server_down"), 6000);
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
        toast(t("merge.done", { n: r.count, path: r.path }), 6000);
      } else if (act === "merged-dl") {
        window.location.href = "/api/merged";
      } else if (act === "open-output") {
        await api("/api/open", { method: "POST", body: { target: "output" } });
      } else if (act.startsWith("clear-")) {
        const which = { "clear-done": "done", "clear-finished": "finished", "clear-all": "all" }[act];
        if (which === "all" && !confirm(t("confirm.clear_all"))) return;
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
  const data = await api(`/api/jobs/${id}/transcript`);
  preview = { id, ...data, view: "text" };
  $("#modal-title").textContent = data.name;
  $("#modal-dl").innerHTML = FORMATS.map((f) => `<a href="/api/jobs/${id}/download/${f}" download>${f.toUpperCase()}</a>`).join("");
  updatePreviewHeader();
  setView("text");
  $("#modal").hidden = false;
}

function updatePreviewHeader() {
  const words = preview.text.trim().split(/\s+/).filter(Boolean).length;
  $("#modal-sub").textContent = t("modal.sub", {
    lang: langName(preview.language), segments: fmtNumber(preview.segments.length), words: fmtNumber(words),
  });
}

function setView(view) {
  preview.view = view;
  document.querySelectorAll("#view-switch button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  const body = $("#modal-body");
  if (view === "text") body.textContent = preview.text;
  else body.innerHTML = preview.segments.map((s) => `<div class="seg-row"><time>${fmtClock(s.start)}</time><span>${esc(s.text)}</span></div>`).join("");
  body.scrollTop = 0;
}

function bindModal() {
  $("#modal").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) $("#modal").hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").hidden = true; });
  document.querySelectorAll("#view-switch button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  $("#modal-copy").addEventListener("click", () => {
    if (!preview) return;
    const text = preview.view === "text" ? preview.text : preview.segments.map((s) => `[${fmtClock(s.start)}] ${s.text}`).join("\n");
    copyText(text);
  });
}

// ---------- init ----------
(async function init() {
  setLang(LANG);
  bindSettings();
  bindAdding();
  bindQueue();
  bindModal();
  try { await loadMeta(); } catch (e) { toast(t("toast.load_error", { msg: e.message }), 8000); }
  poll();
})();
