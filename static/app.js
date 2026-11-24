const $ = (s) => document.querySelector(s);
let state = null;
let me = null;          // hosted mode: the signed-in user's session ({hosted, name, remaining, web_url})

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (r.status === 401 && body.web_url) { location.href = body.web_url; throw new Error("signed out"); }
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data ?? {}) });
const source = () => (me?.hosted ? "browser" : state?.settings.input_source || "browser");

function fmtLeft(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} min left` : `${m}:${String(s).padStart(2, "0")} left`;
}

function renderAccount(remaining) {
  if (!me?.hosted) return;
  const el = $("#time-left");
  el.textContent = fmtLeft(remaining);
  el.classList.toggle("low", remaining < 600);
}

function endSession(reason, webUrl) {
  stopBrowser();
  renderRunning(state);
  const note = $("#error");
  note.hidden = false;
  note.className = "end-note";
  note.innerHTML = "";
  note.append(document.createTextNode(`${reason || "Your session has ended."} `));
  const a = Object.assign(document.createElement("a"), { href: webUrl || me?.web_url || "/", textContent: "Add hours or start a new session" });
  note.append(a);
}

async function loadMe() {
  me = await api("/api/me").catch(() => ({ hosted: false }));
  document.body.dataset.hosted = me.hosted ? "1" : "0";
  document.body.dataset.admin = me.role === "admin" ? "1" : "0";
  if (me.hosted) {
    $("#account").hidden = false;
    $("#acct-name").textContent = me.name || me.email || "";
    $("#acct-back").href = me.web_url;
    renderAccount(me.remaining);
  }
}

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
// (measured: a cap from the single LOWEST round trip dropped 1 frame in 10 whenever the internet
// jittered; the low quartile plus 2 spare slots keeps 30 fps without letting a queue grow)
function baseRtt() {
  if (!bs.rtts.length) return 0;
  const s = [...bs.rtts].sort((a, b) => a - b);
  return s[Math.floor(s.length / 4)];
}
function maxInFlight() {
  const base = baseRtt();
  if (!base) return 3;
  return Math.max(3, Math.min(8, Math.ceil((base * (state?.settings.fps || 30)) / 1000) + 2));
}

async function listBrowserCams() {
  const sel = $("#sel-bcam");
  const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
  sel.innerHTML = "";
  devs.forEach((d, i) => sel.add(new Option(d.label || `Webcam ${i + 1}`, d.deviceId)));
  if (!devs.length) sel.add(new Option("Allow camera access to list webcams", ""));
  if (localStorage.bcam && devs.some((d) => d.deviceId === localStorage.bcam)) sel.value = localStorage.bcam;
}

// H.264 needs an encoder, a decoder and a way to read camera frames: MediaStreamTrackProcessor
// (Chrome, Edge, Android) or a playing <video> with requestVideoFrameCallback (Safari, Firefox).
const hasWebCodecs = () => "VideoEncoder" in window && "VideoDecoder" in window && "VideoFrame" in window
  && ("MediaStreamTrackProcessor" in window || "requestVideoFrameCallback" in HTMLVideoElement.prototype);
const touch = matchMedia("(pointer: coarse)").matches;
if (touch) $("#lib-hint").textContent = "Tap to use · press and hold to blend photos of one person";

// The visible screen takes the video's own shape (portrait phones, 4:3 webcams) instead of 16:9.
function setScreenShape(w, h) {
  if (!w || !h || (bs.shape && bs.shape[0] === w && bs.shape[1] === h)) return;
  bs.shape = [w, h];
  const sc = $("#screen");
  sc.style.setProperty("--arw", w);
  sc.style.setProperty("--arh", h);
}

// Camera frames as VideoFrames, one at a time: the newest frame when the reader asks for one.
async function cameraFrames(track) {
  if ("MediaStreamTrackProcessor" in window) {
    const reader = new MediaStreamTrackProcessor({ track }).readable.getReader();
    return { read: () => reader.read(), cancel: () => reader.cancel().catch(() => {}) };
  }
  // Safari, Firefox: a playing <video>. It must be rendered (not display:none) for frame callbacks.
  const video = $("#cam");
  video.srcObject = bs.stream;
  video.muted = true;
  video.playsInline = true;
  video.classList.add("capture");
  video.hidden = false;
  await video.play();
  let waiter = null, closed = false, ts = 0;
  const give = () => {
    if (!waiter || closed || video.readyState < 2) return;
    const w = waiter;
    waiter = null;
    try { w({ value: new VideoFrame(video, { timestamp: (ts += 33333) }), done: false }); } catch { waiter = w; }
  };
  const onFrame = () => { if (closed) return; give(); video.requestVideoFrameCallback(onFrame); };
  video.requestVideoFrameCallback(onFrame);
  const poll = setInterval(give, 1000 / (state.settings.fps || 30));   // in case frame callbacks stall
  return {
    read: () => new Promise((r) => { waiter = r; }),
    cancel: () => { closed = true; clearInterval(poll); video.classList.remove("capture"); waiter?.({ done: true }); },
  };
}

async function startBrowser() {
  if (!window.isSecureContext || !navigator.mediaDevices) {
    throw new Error("The browser only allows the webcam on https:// or localhost. Open this page through the SSH tunnel or the https link.");
  }
  let [w, h] = state.settings.resolution === "1080p" ? [1920, 1080] : [1280, 720];
  if (touch && matchMedia("(orientation: portrait)").matches) [w, h] = [h, w];   // phone held upright
  const cam = localStorage.bcam ? { deviceId: { ideal: localStorage.bcam } } : touch ? { facingMode: "user" } : {};
  bs.stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { ...cam, width: { ideal: w }, height: { ideal: h }, frameRate: { ideal: state.settings.fps } },
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
  bs.diag = newDiag();
  bs.bitrate = bitrate;
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
        setScreenShape(f.displayWidth, f.displayHeight);
        outCtx.drawImage(f, 0, 0);
        f.close();
        bs.diag.shown++;
        bs.shownTotal = (bs.shownTotal || 0) + 1;
      },
      error: () => { bs.diag.decErr++; decReady = false; bs.decoder = makeDecoder(); if (ws.readyState === 1) ws.send('{"t":"kf"}'); },
    });
    d.configure({ codec: "avc1.640033", optimizeForLatency: true, hardwareAcceleration: "prefer-hardware" });
    return d;
  };
  bs.decoder = makeDecoder();
  bs.encoder = encoder;

  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      try {
        const m = JSON.parse(e.data);
        if (m.t === "kf") needKey = true;
        if (m.t === "end") endSession(m.reason, m.web_url);
      } catch { /* ignore */ }
      return;
    }
    const dv = new DataView(e.data);
    const key = dv.getUint8(1) === 1, id = dv.getUint32(2, true);
    const t = bs.sent.get(id);
    if (t) { bs.rtt = performance.now() - t; bs.rtts.push(bs.rtt); if (bs.rtts.length > 90) bs.rtts.shift(); }
    for (const k of bs.sent.keys()) if (k <= id) bs.sent.delete(k);
    bs.recvId = Math.max(bs.recvId, id);
    bs.diag.rx++;
    toOutputTab({ k: key, d: e.data.slice(6) });
    if (!decReady && !key) return;
    decReady = true;
    try { bs.decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: id, data: new Uint8Array(e.data, 6) })); }
    catch { decReady = false; ws.send('{"t":"kf"}'); }
  };
  ws.onclose = (e) => {
    if (e.code === 4401) { location.href = me?.web_url || "/"; return; }       // session gone: sign in again
    // 4402 = session ended (hours used up / expired): endSession() already stopped and explained it
    if (bs.running) { stopBrowser(); showError("The connection to the server closed. Press Start camera to reconnect."); streamLost(); }
  };
  await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = () => fail(new Error("Could not connect to the server.")); });

  bs.running = true;
  const reader = await cameraFrames(track);
  bs.reader = reader;
  // at most ~120 ms of video waiting in the socket, but never less than one keyframe's worth
  const budget = () => Math.max(60_000, bs.bitrate / 8 * 0.12);
  // Adaptive upload bitrate (AIMD): when the link starts to queue, cut it by 30 % at once so frames
  // keep flowing instead of being dropped; after 3 clean seconds creep back up towards the setting.
  let clean = 0, lastChange = 0;
  bs.abr = setInterval(() => {
    const d = bs.diag, now = performance.now();
    const congested = d.dropSock > d.abrDrops || d.peakBuf > budget() * 0.5;
    d.abrDrops = d.dropSock;
    d.peakBuf = 0;
    clean = congested ? 0 : clean + 1;
    let next = bs.bitrate;
    if (congested && now - lastChange > 1500) next = Math.max(800_000, Math.round(bs.bitrate * 0.7));
    else if (clean >= 3 && bs.bitrate < bitrate) next = Math.min(bitrate, Math.round(bs.bitrate * 1.15));
    if (next !== bs.bitrate && encoder.state === "configured") {
      bs.bitrate = next;
      lastChange = now;
      clean = 0;
      try { encoder.configure({ ...cfg, bitrate: next }); needKey = true; } catch { /* keep the old rate */ }
    }
  }, 1000);
  (async () => {
    while (bs.running) {
      const { value: frame, done } = await reader.read();
      if (done || !frame) break;
      bs.diag.cam++;
      bs.diag.peakBuf = Math.max(bs.diag.peakBuf, ws.bufferedAmount);
      // Drop before encoding (never after): keeps the H.264 stream valid while the link catches up.
      if (encoder.encodeQueueSize > 1) { bs.diag.dropEnc++; frame.close(); continue; }
      if (ws.bufferedAmount > budget() || ws.readyState !== 1) { bs.diag.dropSock++; frame.close(); continue; }
      const id = ++bs.sendId;
      bs.sent.set(id, performance.now());
      const vf = new VideoFrame(frame, { timestamp: id });
      frame.close();
      encoder.encode(vf, { keyFrame: needKey || id % (fps * 2) === 0 });
      needKey = false;
      vf.close();
      bs.diag.sent++;
    }
  })().catch((e) => console.warn(e));
}

/* ---- stream diagnostics: where frames go (sent to the server with the page heartbeat) ---- */
function newDiag() {
  return { t: performance.now(), cam: 0, sent: 0, dropEnc: 0, dropSock: 0, rx: 0, shown: 0, decErr: 0, peakBuf: 0, abrDrops: 0 };
}
function takeDiag() {
  const d = bs.diag;
  if (!d || !bs.running) return null;
  const sec = Math.max(0.5, (performance.now() - d.t) / 1000), r = (n) => Math.round((n / sec) * 10) / 10;
  const out = { mode: bs.mode, cam_fps: r(d.cam), sent_fps: r(d.sent), shown_fps: r(d.shown), rx_fps: r(d.rx),
    drop_enc: d.dropEnc, drop_sock: d.dropSock, dec_err: d.decErr, rtt_ms: Math.round(bs.rtt), kbps: Math.round((bs.bitrate || 0) / 1000),
    size: bs.shape ? bs.shape.join("x") : "", hidden: document.hidden, touch };
  bs.diag = Object.assign(newDiag(), { abrDrops: 0 });
  return out;
}
// What the user sees: frames shown per second, measured on this page (the server's number only
// counts frames it received).
let shownLast = 0;
setInterval(() => {
  const n = bs.shownTotal || 0;
  bs.viewFps = bs.running ? n - shownLast : 0;
  shownLast = n;
}, 1000);

/* ---- picture in picture: the output tab decodes the same video itself ---- */
// A hidden tab produces no canvas frames, so the output tab can't borrow this page's canvas. It gets
// the encoded video over a BroadcastChannel (a few KB per frame) and decodes it on its own.
const outChannel = "BroadcastChannel" in window ? new BroadcastChannel("magicpro-output") : null;
let outTabSeen = 0;
outChannel?.addEventListener("message", (e) => {
  if (!e.data || typeof e.data !== "object") return;
  if (e.data.t === "alive" || e.data.t === "need-key") outTabSeen = Date.now();
  if (e.data.t === "need-key" && bs.ws?.readyState === 1 && bs.mode === "H.264") bs.ws.send('{"t":"kf"}');
});
function toOutputTab(msg) {
  if (outChannel && Date.now() - outTabSeen < 5000) outChannel.postMessage(msg);
}

async function startJpeg(w, h) {
  const video = $("#cam");
  video.srcObject = bs.stream;
  video.muted = true;
  video.playsInline = true;
  video.classList.add("capture");      // rendered (tiny), or frame callbacks never fire
  video.hidden = false;
  await video.play();
  bs.diag = newDiag();
  setScreenShape(w, h);
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
    if (typeof e.data === "string") {
      try { const m = JSON.parse(e.data); if (m.t === "end") endSession(m.reason, m.web_url); } catch { /* ignore */ }
      return;
    }
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
    bs.diag.rx++;
    toOutputTab({ jpeg: e.data.slice(4) });
    const bmp = await createImageBitmap(new Blob([new Uint8Array(e.data, 4)], { type: "image/jpeg" }));
    bs.outCtx.drawImage(bmp, 0, 0, bs.cap.width, bs.cap.height);
    bmp.close();
    bs.diag.shown++;
    bs.shownTotal = (bs.shownTotal || 0) + 1;
    pump();
  };
  ws.onclose = (e) => {
    if (e.code === 4401) { location.href = me?.web_url || "/"; return; }       // session gone: sign in again
    if (bs.running) { stopBrowser(); showError("The connection to the server closed. Press Start camera to reconnect."); streamLost(); }
  };
  await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = () => fail(new Error("Could not connect to the server.")); });

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
    bs.diag.sent++;
  } finally {
    bs.busy = false;
  }
}

function stopBrowser() {
  bs.running = false;
  clearInterval(bs.timer);
  clearInterval(bs.abr);
  const r = bs.reader?.cancel();
  if (r && r.catch) r.catch(() => {});
  $("#cam").classList.remove("capture");
  $("#cam").hidden = true;
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
    // touch screens have no Ctrl key: a long press (half a second) adds the photo to the blend
    let pressT = null, longPressed = false;
    b.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" || e.target.closest(".del")) return;
      longPressed = false;
      pressT = setTimeout(() => { longPressed = true; navigator.vibrate?.(15); pick(true); }, 500);
    });
    ["pointerup", "pointercancel", "pointerleave"].forEach((ev) => b.addEventListener(ev, () => clearTimeout(pressT)));
    b.addEventListener("contextmenu", (e) => { if (touch) e.preventDefault(); });
    b.addEventListener("click", (e) => {
      if (longPressed) { longPressed = false; return; }
      if (!e.target.closest(".del")) pick(e.ctrlKey || e.metaKey || e.shiftKey);
    });
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
  if (e.className === "end-note" && !msg) return;      // keep the "session ended" note visible
  e.className = "error";
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
  $("#m-fps").textContent = bs.running ? (bs.viewFps ?? 0) : (s.fps ?? 0);   // frames you actually see
  $("#m-rtt").textContent = bs.running ? `${Math.round(bs.rtt)} ms` : "–";
  $("#m-lat").textContent = `${s.latency_ms ?? 0} ms`;
  $("#m-det").textContent = `${s.detect_ms ?? 0} ms`;
  $("#m-swap").textContent = `${s.swap_ms ?? 0} ms`;
  $("#m-enh").textContent = `${s.enhance_ms ?? 0} ms`;
  const facts = me?.hosted ? [s.output && `output ${s.output}`].filter(Boolean)
    : [`faces ${s.faces ?? 0}`, bs.running && bs.mode && `stream ${bs.mode}${s.encoder ? " · " + s.encoder : ""}`, source() === "server" && s.camera && `camera ${s.camera}`, s.output && `output ${s.output}`,
      s.swap_provider && s.swap_provider.replace("ExecutionProvider", ""), source() === "server" && s.vcam && `virtual cam: ${s.vcam}`].filter(Boolean);
  $("#facts").textContent = facts.join("  ·  ");
  if (s.error) showError(s.error);
  if (me?.hosted && s.remaining !== undefined) {
    renderAccount(s.remaining);
    if (s.stop && bs.running) endSession(s.stop, me.web_url);
  }
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
/* Picture in picture: the swapped video alone in a new tab (static/output.html), fitted to the
   tab, no controls. The tab takes the live canvas stream straight from this page: no second
   connection, no extra delay. */
window.magicOutputSource = () => (source() === "server"
  ? { kind: "url", url: "/video.mjpg?full=1" }
  : { kind: "channel" });          // the output tab decodes the stream itself (see toOutputTab)
$("#pip").addEventListener("click", () => {
  const w = window.open("/static/output.html", "magicpro-output");
  if (!w) showError("Your browser blocked the new tab. Allow pop-ups for this site and try again.");
});

// Full screen: the real thing where the browser allows it on an element (not iPhone Safari);
// otherwise the output fills the window, and a tap on the same button brings the page back.
$("#fullscreen").addEventListener("click", () => {
  const sc = $("#screen");
  if (document.fullscreenEnabled) {
    if (document.fullscreenElement) document.exitFullscreen();
    else sc.requestFullscreen().catch(() => sc.classList.toggle("fill"));
  } else {
    sc.classList.toggle("fill");
  }
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
loadMe().then(load).then(switchSource).catch((e) => showError(e.message));

/* ---------------- GPU on/off (deploy/vast/watchdog + deploy/launcher) ----------------
   Every 5 s the page tells the server whether it is streaming; the server counts a user only
   while their socket is open AND heartbeats are fresh (15 s). The watchdog stops the GPU after
   the grace period with nobody streaming. ?autostart=1 (set by the launcher) starts the camera. */
const gpuCid = sessionStorage.gpuCid || (sessionStorage.gpuCid = Math.random().toString(36).slice(2) + Date.now().toString(36));
async function gpuBeat() {
  try {
    const s = await post("/api/gpu/heartbeat", { cid: gpuCid, streaming: isRunning(state), diag: takeDiag() });
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
