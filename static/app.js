const $ = (s) => document.querySelector(s);
let state = null;

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data ?? {}) });

function fmt(el) {
  const v = parseFloat(el.value);
  return el.step && el.step.includes(".") ? v.toFixed(2) : String(v);
}

function fillSelect(sel, items, current, noneLabel) {
  sel.innerHTML = "";
  if (noneLabel) sel.add(new Option(noneLabel, ""));
  for (const m of items) sel.add(new Option(m.size_mb ? `${m.name} · ${m.size_mb} MB` : m.name, m.id));
  if (!items.length && !noneLabel) sel.add(new Option("No models in models/swappers", ""));
  sel.value = current ?? "";
}

function applySettings(s) {
  document.querySelectorAll("[data-key]").forEach((el) => {
    const v = s[el.dataset.key];
    if (v === undefined) return;
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = String(v);
    const out = el.closest(".range")?.querySelector("output");
    if (out) out.textContent = fmt(el);
  });
}

function renderLibrary() {
  const grid = $("#library");
  grid.innerHTML = "";
  const selected = new Set(state.settings.source_ids);
  for (const it of state.library) {
    const b = document.createElement("div");
    b.className = "face" + (selected.has(it.id) ? " on" : "");
    b.tabIndex = 0;
    b.setAttribute("role", "button");
    b.setAttribute("aria-pressed", selected.has(it.id));
    b.setAttribute("aria-label", `Use ${it.name}`);
    b.innerHTML = `<img alt="" src="${it.thumb}"><span class="name"></span><button class="del" type="button" aria-label="Delete ${it.name}">×</button>`;
    b.querySelector(".name").textContent = it.name;
    const pick = (multi) => {
      let ids = multi ? [...selected] : [];
      if (multi && selected.has(it.id)) ids = ids.filter((i) => i !== it.id);
      else ids.push(it.id);
      setSetting("source_ids", [...new Set(ids)]).then(renderLibrary);
    };
    b.addEventListener("click", (e) => { if (!e.target.closest(".del")) pick(e.ctrlKey || e.metaKey || e.shiftKey); });
    b.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(e.ctrlKey); } });
    b.querySelector(".del").addEventListener("click", async () => {
      if (!confirm(`Delete "${it.name}" from the library?`)) return;
      await api(`/api/library/${it.id}`, { method: "DELETE" });
      await load();
    });
    grid.appendChild(b);
  }
}

async function setSetting(key, value) {
  state.settings = await post("/api/settings", { [key]: value });
  return state.settings;
}

function bindControls() {
  document.querySelectorAll("[data-key]").forEach((el) => {
    const out = el.closest(".range")?.querySelector("output");
    let t;
    const send = () => {
      const v = el.type === "checkbox" ? el.checked : el.type === "range" ? parseFloat(el.value) : el.value;
      setSetting(el.dataset.key, v).catch((e) => showError(e.message));
    };
    el.addEventListener("input", () => {
      if (out) out.textContent = fmt(el);
      if (el.type === "range") { clearTimeout(t); t = setTimeout(send, 60); }
    });
    if (el.type !== "range") el.addEventListener("change", send);
  });
}

function showError(msg) {
  const e = $("#error");
  e.hidden = !msg;
  e.textContent = msg || "";
}

function renderRunning(running, stats) {
  const btn = $("#toggle");
  btn.textContent = running ? "Stop camera" : "Start camera";
  btn.classList.toggle("stop", running);
  const st = $("#status");
  const status = stats?.status || (running ? "live" : "idle");
  st.className = "status " + (status === "live" ? "live" : status === "error" ? "error" : "");
  $("#status-text").textContent = status === "live" ? "Live" : status.charAt(0).toUpperCase() + status.slice(1);
}

function renderStats(s) {
  $("#m-fps").textContent = s.fps ?? 0;
  $("#m-lat").textContent = `${s.latency_ms ?? 0} ms`;
  $("#m-det").textContent = `${s.detect_ms ?? 0} ms`;
  $("#m-swap").textContent = `${s.swap_ms ?? 0} ms`;
  $("#m-enh").textContent = `${s.enhance_ms ?? 0} ms`;
  $("#m-faces").textContent = s.faces ?? 0;
  const facts = [s.camera && `camera ${s.camera}`, s.output && `output ${s.output}`,
    s.swap_provider && s.swap_provider.replace("ExecutionProvider", ""), s.vcam && `virtual cam: ${s.vcam}`].filter(Boolean);
  $("#facts").textContent = facts.join("  ·  ");
  showError(s.error);
  renderRunning(s.running, s);
}

async function load() {
  state = await api("/api/state");
  fillSelect($("#sel-swapper"), state.swappers, state.settings.swapper);
  fillSelect($("#sel-enhancer"), state.enhancers, state.settings.enhancer, "Off");
  const cam = $("#sel-camera");
  if (!cam.options.length) cam.add(new Option(`Camera ${state.settings.camera}`, state.settings.camera));
  applySettings(state.settings);
  renderLibrary();
  renderStats({ running: state.running, ...state.stats });
  $("#setup-banner").hidden = state.models_ready;
}

async function scanCameras() {
  const btn = $("#scan");
  btn.disabled = true;
  btn.textContent = "Scanning…";
  try {
    const cams = await api("/api/cameras");
    const sel = $("#sel-camera");
    sel.innerHTML = "";
    for (const c of cams) sel.add(new Option(c.label, c.index));
    if (!cams.length) sel.add(new Option("No camera found", state.settings.camera));
    sel.value = String(state.settings.camera);
  } finally {
    btn.disabled = false;
    btn.textContent = "Find cameras";
  }
}

function bindUpload() {
  const form = $("#upload"), file = $("#file"), msg = $("#up-msg"), drop = $("#drop");
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); file.files = e.dataTransfer.files; });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.className = "msg";
    if (!file.files.length) { msg.textContent = "Choose at least one photo."; msg.classList.add("bad"); return; }
    if (!$("#consent").checked) { msg.textContent = "Tick the consent box first."; msg.classList.add("bad"); return; }
    $("#up-btn").disabled = true;
    const files = [...file.files];
    const base = $("#up-name").value.trim();
    let ok = 0, errors = [];
    for (const [i, f] of files.entries()) {
      msg.textContent = `Finding the face in ${f.name}…`;
      const fd = new FormData();
      fd.append("file", f);
      fd.append("name", base ? (files.length > 1 ? `${base} ${i + 1}` : base) : "");
      fd.append("consent", "true");
      try { await api("/api/library", { method: "POST", body: fd }); ok++; }
      catch (err) { errors.push(`${f.name}: ${err.message}`); }
    }
    $("#up-btn").disabled = false;
    msg.textContent = errors.length ? errors.join(" ") : `Added ${ok} face${ok === 1 ? "" : "s"}.`;
    msg.classList.add(errors.length ? "bad" : "good");
    form.reset();
    await load();
  });
}

$("#toggle").addEventListener("click", async () => {
  const running = state?.running;
  const r = await post(running ? "/api/stop" : "/api/start");
  state.running = r.running;
  renderRunning(r.running);
  $("#preview").src = "/video.mjpg?t=" + Date.now();
});
$("#scan").addEventListener("click", scanCameras);
$("#fullscreen").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else $("#screen").requestFullscreen();
});

setInterval(async () => {
  try {
    const s = await api("/api/stats");
    if (state) state.running = s.running;
    renderStats(s);
  } catch { /* server restarting */ }
}, 1000);

bindControls();
bindUpload();
load().catch((e) => showError(e.message));
