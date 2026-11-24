const $ = (s) => document.querySelector(s);
let state = null;

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data ?? {}) });
const source = () => state?.settings.input_source || "browser";

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
  document.body.dataset.source = s.input_source;
}

/* ---------------- browser webcam -> GPU -> browser ---------------- */
// Frames allowed in flight scale with the measured round trip, so a far-away GPU still gets a
// full frame rate: in-flight = round trip x fps + 1 (e.g. 200 ms at 30 fps -> 7).
const bs = { running: false, stream: null, ws: null, sendId: 0, recvId: 0, sent: new Map(), rtt: 0, rttAvg: 0, busy: false, cap: null, capCtx: null, outCtx: null, timer: null };
function maxInFlight() {
  if (!bs.rttAvg) return 3;
  return Math.max(2, Math.min(8, Math.ceil((bs.rttAvg * (state?.settings.fps || 30)) / 1000) + 1));
}

async function listBrowserCams() {
  const sel = $("#sel-bcam");
  const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
  sel.innerHTML = "";
  devs.forEach((d, i) => sel.add(new Option(d.label || `Webcam ${i + 1}`, d.deviceId)));
  if (!devs.length) sel.add(new Option("Allow camera access to list webcams", ""));
  if (localStorage.bcam && devs.some((d) => d.deviceId === localStorage.bcam)) sel.value = localStorage.bcam;
}

async function startBrowser() {
  if (!window.isSecureContext || !navigator.mediaDevices) {
    throw new Error("The browser only allows the webcam on https:// or localhost. Open this page through the SSH tunnel or the https link.");
  }
  const [w, h] = state.settings.resolution === "1080p" ? [1920, 1080] : [1280, 720];
  const video = $("#cam");
  bs.stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { deviceId: localStorage.bcam ? { ideal: localStorage.bcam } : undefined, width: { ideal: w }, height: { ideal: h }, frameRate: { ideal: state.settings.fps } },
  });
  video.srcObject = bs.stream;
  await video.play();
  listBrowserCams();

  bs.cap = Object.assign(document.createElement("canvas"), { width: w, height: h });
  bs.capCtx = bs.cap.getContext("2d");
  const out = $("#out");
  out.width = w; out.height = h;
  bs.outCtx = out.getContext("2d");
  bs.sendId = bs.recvId = 0; bs.sent.clear();

  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/stream`);
  ws.binaryType = "arraybuffer";
  bs.ws = ws;
  ws.onmessage = async (e) => {
    const id = new DataView(e.data).getUint32(0, true);
    const t = bs.sent.get(id);
    if (t) {
      bs.rtt = performance.now() - t;
      bs.rttAvg = bs.rttAvg ? bs.rttAvg * 0.9 + bs.rtt * 0.1 : bs.rtt;
    }
    for (const k of bs.sent.keys()) if (k <= id) bs.sent.delete(k);
    if (id <= bs.recvId) return;   // an older frame finished after a newer one: never step back
    bs.recvId = id;
    const bmp = await createImageBitmap(new Blob([new Uint8Array(e.data, 4)], { type: "image/jpeg" }));
    bs.outCtx.drawImage(bmp, 0, 0, bs.cap.width, bs.cap.height);
    bmp.close();
    pump();
  };
  ws.onclose = () => { if (bs.running) { stopBrowser(); showError("The connection to the GPU server closed. Press Start camera to reconnect."); } };
  await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = () => fail(new Error("Could not connect to the GPU server.")); });

  bs.running = true;
  if ("requestVideoFrameCallback" in HTMLVideoElement.prototype) {
    const onFrame = () => { if (!bs.running) return; pump(); video.requestVideoFrameCallback(onFrame); };
    video.requestVideoFrameCallback(onFrame);
  } else {
    bs.timer = setInterval(pump, 1000 / state.settings.fps);
  }
}

async function pump() {
  const ws = bs.ws;
  if (!bs.running || bs.busy || !ws || ws.readyState !== 1 || ws.bufferedAmount > 4e6) return;
  if (bs.sendId - bs.recvId >= maxInFlight()) {
    const oldest = bs.sent.values().next().value;
    if (!oldest || performance.now() - oldest < 1500) return;
    bs.recvId = bs.sendId; bs.sent.clear();   // a reply went missing: don't stall
  }
  bs.busy = true;
  try {
    bs.capCtx.drawImage($("#cam"), 0, 0, bs.cap.width, bs.cap.height);
    const blob = await new Promise((r) => bs.cap.toBlob(r, "image/jpeg", parseInt($("#send-q").value, 10) / 100));
    if (!blob || !bs.running) return;
    const id = ++bs.sendId;
    const head = new ArrayBuffer(8);
    new DataView(head).setUint32(0, id, true);
    bs.sent.set(id, performance.now());
    ws.send(new Blob([head, blob]));
  } finally {
    bs.busy = false;
  }
}

function stopBrowser() {
  bs.running = false;
  clearInterval(bs.timer);
  bs.ws?.close();
  bs.ws = null;
  bs.stream?.getTracks().forEach((t) => t.stop());
  bs.stream = null;
  bs.rtt = 0;
  bs.rttAvg = 0;
}

/* ---------------- library ---------------- */
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
      setSetting(el.dataset.key, v).then(() => { if (el.dataset.key === "input_source") switchSource(); }).catch((e) => showError(e.message));
    };
    el.addEventListener("input", () => {
      if (out) out.textContent = fmt(el);
      if (el.type === "range") { clearTimeout(t); t = setTimeout(send, 60); }
    });
    if (el.type !== "range") el.addEventListener("change", send);
  });
  const sq = $("#send-q");
  sq.value = localStorage.sendQ || 70;
  sq.closest(".range").querySelector("output").textContent = sq.value;
  sq.addEventListener("input", () => { localStorage.sendQ = sq.value; sq.closest(".range").querySelector("output").textContent = sq.value; });
  $("#sel-bcam").addEventListener("change", async (e) => {
    localStorage.bcam = e.target.value;
    if (bs.running) { stopBrowser(); await start(); }
  });
}

function showError(msg) {
  const e = $("#error");
  e.hidden = !msg;
  e.textContent = msg || "";
}

function isRunning(s) { return source() === "browser" ? bs.running : !!s?.running; }

function renderRunning(stats) {
  const running = isRunning(stats ?? state);
  const btn = $("#toggle");
  btn.textContent = running ? "Stop camera" : "Start camera";
  btn.classList.toggle("stop", running);
  const status = running ? (stats?.status === "error" ? "error" : stats?.status === "idle" ? "connecting" : stats?.status || "live") : "idle";
  $("#status").className = "status " + (status === "live" ? "live" : status === "error" ? "error" : "");
  $("#status-text").textContent = status.charAt(0).toUpperCase() + status.slice(1);
}

function renderStats(s) {
  $("#m-fps").textContent = s.fps ?? 0;
  $("#m-rtt").textContent = bs.running ? `${Math.round(bs.rtt)} ms` : "–";
  $("#m-lat").textContent = `${s.latency_ms ?? 0} ms`;
  $("#m-det").textContent = `${s.detect_ms ?? 0} ms`;
  $("#m-swap").textContent = `${s.swap_ms ?? 0} ms`;
  $("#m-enh").textContent = `${s.enhance_ms ?? 0} ms`;
  const facts = [`faces ${s.faces ?? 0}`, source() === "server" && s.camera && `camera ${s.camera}`, s.output && `output ${s.output}`,
    s.swap_provider && s.swap_provider.replace("ExecutionProvider", ""), source() === "server" && s.vcam && `virtual cam: ${s.vcam}`].filter(Boolean);
  $("#facts").textContent = facts.join("  ·  ");
  if (s.error) showError(s.error);
  renderRunning(s);
}

function switchSource() {
  document.body.dataset.source = source();
  const browser = source() === "browser";
  if (!browser && bs.running) stopBrowser();
  $("#out").hidden = !browser;
  $("#preview").hidden = browser;
  if (browser) $("#preview").removeAttribute("src");
  else $("#preview").src = "/video.mjpg?t=" + Date.now();
  if (browser && navigator.mediaDevices) listBrowserCams().catch(() => {});
  renderRunning(state);
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
    let ok = 0;
    const errors = [];
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

async function start() {
  showError("");
  if (source() === "browser") await startBrowser();
  else { const r = await post("/api/start"); state.running = r.running; $("#preview").src = "/video.mjpg?t=" + Date.now(); }
}

async function stop() {
  if (source() === "browser") stopBrowser();
  else { const r = await post("/api/stop"); state.running = r.running; }
}

$("#toggle").addEventListener("click", async () => {
  const btn = $("#toggle");
  btn.disabled = true;
  try { isRunning(state) ? await stop() : await start(); }
  catch (e) { stopBrowser(); showError(e.message); }
  finally { btn.disabled = false; renderRunning(state); }
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
load().then(switchSource).catch((e) => showError(e.message));
