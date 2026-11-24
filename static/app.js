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
  for (const m of items) sel.add(new Option(m.detail ? `${m.name} · ${m.detail}` : m.size_mb ? `${m.name} · ${m.size_mb} MB` : m.name, m.id));
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
// Pacing, so a distant GPU gets a full frame rate without a queue building up:
//  * frames in flight = base round trip x fps + 1, where the base is the LOWEST recent round trip
//    (queueing inflates the average, and steering by it would feed the queue it measures)
//  * nothing is sent while the socket still holds an unsent frame, so the upload paces itself
const bs = { running: false, stream: null, ws: null, sendId: 0, recvId: 0, sent: new Map(), rtt: 0, rtts: [], busy: false, cap: null, capCtx: null, outCtx: null, timer: null };
function baseRtt() { return bs.rtts.length ? Math.min(...bs.rtts) : 0; }
function maxInFlight() {
  const base = baseRtt();
  if (!base) return 2;
  return Math.max(2, Math.min(6, Math.ceil((base * (state?.settings.fps || 30)) / 1000) + 1));
}

async function listBrowserCams() {
  const sel = $("#sel-bcam");
  const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
  sel.innerHTML = "";
  devs.forEach((d, i) => sel.add(new Option(d.label || `Webcam ${i + 1}`, d.deviceId)));
  if (!devs.length) sel.add(new Option("Allow camera access to list webcams", ""));
  if (localStorage.bcam && devs.some((d) => d.deviceId === localStorage.bcam)) sel.value = localStorage.bcam;
}

const hasWebCodecs = () => "VideoEncoder" in window && "VideoDecoder" in window && "MediaStreamTrackProcessor" in window;

async function startBrowser() {
  if (!window.isSecureContext || !navigator.mediaDevices) {
    throw new Error("The browser only allows the webcam on https:// or localhost. Open this page through the SSH tunnel or the https link.");
  }
  const [w, h] = state.settings.resolution === "1080p" ? [1920, 1080] : [1280, 720];
  bs.stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { deviceId: localStorage.bcam ? { ideal: localStorage.bcam } : undefined, width: { ideal: w }, height: { ideal: h }, frameRate: { ideal: state.settings.fps } },
  });
  listBrowserCams();
  if (hasWebCodecs()) {
    try { await startH264(); return; }
    catch (e) { console.warn("H.264 mode unavailable, falling back to JPEG:", e); bs.ws?.close(); }
  }
  await startJpeg(w, h);
}

/* H.264 both ways (WebCodecs, hardware encode/decode): about a tenth of the bandwidth of JPEG frames */
async function pickEncoderConfig(width, height, framerate, bitrate) {
  for (const hw of ["prefer-hardware", "no-preference"]) {
    for (const codec of ["avc1.640033", "avc1.4D0033", "avc1.42E033"]) {
      const cfg = { codec, width, height, bitrate, framerate, latencyMode: "realtime", hardwareAcceleration: hw, avc: { format: "annexb" } };
      try { const r = await VideoEncoder.isConfigSupported(cfg); if (r.supported) return r.config; } catch { /* try next */ }
    }
  }
  return null;
}

async function startH264() {
  const track = bs.stream.getVideoTracks()[0];
  const st = track.getSettings();
  const width = st.width & ~1, height = st.height & ~1;
  const fps = state.settings.fps;
  const bitrate = Math.round(state.settings.stream_bitrate_mbps * 1e6);
  const cfg = await pickEncoderConfig(width, height, fps, bitrate);
  if (!cfg) throw new Error("No H.264 encoder in this browser");

  const out = $("#out");
  const outCtx = out.getContext("2d");
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/h264`);
  ws.binaryType = "arraybuffer";
  bs.ws = ws;
  bs.sendId = bs.recvId = 0; bs.sent.clear(); bs.mode = "H.264";
  let needKey = true, decReady = false;

  const encoder = new VideoEncoder({
    output: (chunk) => {
      if (ws.readyState !== 1) return;
      const buf = new Uint8Array(6 + chunk.byteLength);
      const dv = new DataView(buf.buffer);
      dv.setUint8(0, 1);
      dv.setUint8(1, chunk.type === "key" ? 1 : 0);
      dv.setUint32(2, chunk.timestamp, true);          // the frame id rides in the timestamp
      chunk.copyTo(buf.subarray(6));
      ws.send(buf);
    },
    error: (e) => showError("Video encoder: " + e.message),
  });
  encoder.configure(cfg);

  const makeDecoder = () => {
    const d = new VideoDecoder({
      output: (f) => {
        if (out.width !== f.displayWidth || out.height !== f.displayHeight) { out.width = f.displayWidth; out.height = f.displayHeight; }
        outCtx.drawImage(f, 0, 0);
        f.close();
      },
      error: () => { decReady = false; bs.decoder = makeDecoder(); if (ws.readyState === 1) ws.send('{"t":"kf"}'); },
    });
    d.configure({ codec: "avc1.640033", optimizeForLatency: true, hardwareAcceleration: "prefer-hardware" });
    return d;
  };
  bs.decoder = makeDecoder();
  bs.encoder = encoder;

  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      try { if (JSON.parse(e.data).t === "kf") needKey = true; } catch { /* ignore */ }
      return;
    }
    const dv = new DataView(e.data);
    const key = dv.getUint8(1) === 1, id = dv.getUint32(2, true);
    const t = bs.sent.get(id);
    if (t) { bs.rtt = performance.now() - t; bs.rtts.push(bs.rtt); if (bs.rtts.length > 90) bs.rtts.shift(); }
    for (const k of bs.sent.keys()) if (k <= id) bs.sent.delete(k);
    bs.recvId = Math.max(bs.recvId, id);
    if (!decReady && !key) return;
    decReady = true;
    try { bs.decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: id, data: new Uint8Array(e.data, 6) })); }
    catch { decReady = false; ws.send('{"t":"kf"}'); }
  };
  ws.onclose = () => { if (bs.running) { stopBrowser(); showError("The connection to the GPU server closed. Press Start camera to reconnect."); streamLost(); } };
  await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = () => fail(new Error("Could not connect to the GPU server.")); });

  bs.running = true;
  const reader = new MediaStreamTrackProcessor({ track }).readable.getReader();
  bs.reader = reader;
  const sendBudget = bitrate / 8 * 0.12;              // at most ~120 ms of video waiting in the socket
  (async () => {
    while (bs.running) {
      const { value: frame, done } = await reader.read();
      if (done || !frame) break;
      // Drop before encoding (never after): keeps the H.264 stream valid while the link catches up.
      if (encoder.encodeQueueSize > 1 || ws.bufferedAmount > sendBudget || ws.readyState !== 1) { frame.close(); continue; }
      const id = ++bs.sendId;
      bs.sent.set(id, performance.now());
      const vf = new VideoFrame(frame, { timestamp: id });
      frame.close();
      encoder.encode(vf, { keyFrame: needKey || id % (fps * 2) === 0 });
      needKey = false;
      vf.close();
    }
  })().catch((e) => console.warn(e));
}

async function startJpeg(w, h) {
  const video = $("#cam");
  video.srcObject = bs.stream;
  await video.play();
  bs.mode = "JPEG";

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
      bs.rtts.push(bs.rtt);
      if (bs.rtts.length > 90) bs.rtts.shift();   // about the last 3 seconds
    }
    for (const k of bs.sent.keys()) if (k <= id) bs.sent.delete(k);
    if (id <= bs.recvId) return;   // an older frame finished after a newer one: never step back
    bs.recvId = id;
    const bmp = await createImageBitmap(new Blob([new Uint8Array(e.data, 4)], { type: "image/jpeg" }));
    bs.outCtx.drawImage(bmp, 0, 0, bs.cap.width, bs.cap.height);
    bmp.close();
    pump();
  };
  ws.onclose = () => { if (bs.running) { stopBrowser(); showError("The connection to the GPU server closed. Press Start camera to reconnect."); streamLost(); } };
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
  if (!bs.running || bs.busy || !ws || ws.readyState !== 1 || ws.bufferedAmount > 0) return;
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
  bs.reader?.cancel().catch(() => {});
  for (const c of [bs.encoder, bs.decoder]) { try { if (c && c.state !== "closed") c.close(); } catch { /* closed */ } }
  bs.reader = bs.encoder = bs.decoder = null;
  bs.ws?.close();
  bs.ws = null;
  bs.stream?.getTracks().forEach((t) => t.stop());
  bs.stream = null;
  bs.rtt = 0;
  bs.rtts = [];
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
  const facts = [`faces ${s.faces ?? 0}`, bs.running && bs.mode && `stream ${bs.mode}${s.encoder ? " · " + s.encoder : ""}`, source() === "server" && s.camera && `camera ${s.camera}`, s.output && `output ${s.output}`,
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

// Measured best likeness and full jaw/chin coverage (see README, "Swap quality")
const BEST_QUALITY = {
  mask_mode: "face", face_mask_expand: 0.08, chin_extend: 0.04, swap_zoom: 0.9, swap_passes: 1, mask_blur: 0.3,
  mask_top: 0, mask_bottom: 0, mask_left: 0, mask_right: 0, mouth_mask: false, mouth_keep: "inner", mouth_size: 1.0,
  edge_detail: 0.4, color_match: 0, enhancer: "gfpgan_1.4", enhancer_blend: 0.5, smoothing: 0.5,
  det_thresh: 0.5, track_thresh: 0.3, hold_ms: 600,
};
$("#preset-best").addEventListener("click", async () => {
  state.settings = await post("/api/settings", BEST_QUALITY);
  applySettings(state.settings);
});
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

/* ---------------- GPU on/off (deploy/vast/watchdog + deploy/launcher) ----------------
   Every 5 s the page tells the server whether it is streaming; the server counts a user only
   while their socket is open AND heartbeats are fresh (15 s). The watchdog stops the GPU after
   the grace period with nobody streaming. ?autostart=1 (set by the launcher) starts the camera. */
const gpuCid = sessionStorage.gpuCid || (sessionStorage.gpuCid = Math.random().toString(36).slice(2) + Date.now().toString(36));
async function gpuBeat() {
  try {
    const s = await post("/api/gpu/heartbeat", { cid: gpuCid, streaming: isRunning(state) });
    const w = s.watchdog || {};
    if (w.launcher) gpuPool = { launcher: w.launcher, node: w.node };
    const fresh = w.updated && Date.now() / 1000 - w.updated < 30;
    $("#gpu-stop").hidden = !fresh;
    const note = $("#gpu-note");
    note.hidden = !(fresh && w.stop_in_s != null);
    if (!note.hidden) note.textContent = `GPU stops in ${Math.ceil(w.stop_in_s / 60)} min unless someone streams${w.dry_run ? " (test mode)" : ""}`;
  } catch { /* older server or restarting */ }
}
setInterval(gpuBeat, 5000);
gpuBeat();
$("#gpu-stop").addEventListener("click", async () => {
  if (!confirm("Stop the GPU now? Streaming ends for this page, and starting again takes 1–2 minutes.")) return;
  if (isRunning(state)) { await stop().catch(() => {}); renderRunning(state); }
  await gpuBeat();
  try {
    const r = await post("/api/gpu/stop", { cid: gpuCid });
    showError(r.message);
  } catch (e) {
    if (!confirm(`${e.message} Stop the GPU for everyone anyway?`)) return;
    try { showError((await post("/api/gpu/stop", { cid: gpuCid, force: true })).message); } catch (e2) { showError(e2.message); }
  }
});
// Pool: the node's watchdog tells the page its launcher (cached from heartbeats, since the node
// may be gone when we need it). If the stream drops and this node no longer answers (GPU died,
// stopped, tunnel gone), go back to the launcher, which hands out another node.
const qp = new URLSearchParams(location.search);
let gpuPool = null;   // {launcher, node}
async function streamLost() {
  if (!gpuPool || !/^https:\/\//.test(gpuPool.launcher || "")) return;
  for (let i = 0; i < 2; i++) {
    try {
      const r = await fetch("/api/gpu/status", { signal: AbortSignal.timeout(3000), cache: "no-store" });
      const s = await r.json();
      if (r.ok && !(s.watchdog && s.watchdog.stopping)) return;     // node is fine: a network blip
    } catch { /* unreachable */ }
    await new Promise((ok) => setTimeout(ok, 1500));
  }
  showError("This GPU stopped answering. Moving you to another one…");
  location.href = `${gpuPool.launcher.replace(/\/$/, "")}/?reconnect=1&exclude=${encodeURIComponent(gpuPool.node || "")}`;
}
if (qp.has("autostart")) {
  history.replaceState(null, "", location.pathname);
  const go = () => (state ? $("#toggle").click() : setTimeout(go, 300));
  setTimeout(go, 300);
}
