"""Magic Pro launcher: an always-on page (Cloud Run) in front of an autoscaled pool of Vast.ai GPUs.

Flow: sign in -> Start camera -> the launcher picks the ready GPU node with the most free
capacity (starting or creating one if none is free) -> the browser goes to that node's current
HTTPS address (its Cloudflare quick tunnel, reported by the node's heartbeat) with ?autostart=1.
If a node dies mid-stream, the app page comes back here with ?reconnect=1 and gets another node.
Nodes stop themselves when idle (deploy/vast/watchdog); see pool.py for the scaling rules.

Secrets (Secret Manager): VAST_API_KEY, LAUNCHER_PASSWORD, POOL_SECRET.
Settings (env): see pool.Config, plus LAUNCHER_URL (this service's https address).
"""
import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from urllib.parse import quote

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import pool as poolmod

API_KEY = os.environ.get("VAST_API_KEY", "")
PASSWORD = os.environ.get("LAUNCHER_PASSWORD", "")
SESSION_DAYS = 14
COOKIE = "mp_launcher"

_key = hashlib.sha256(f"magicpro-launcher|{PASSWORD}|{API_KEY}".encode()).digest()
_fails: dict[str, list[float]] = {}
pool = poolmod.Pool(poolmod.Vast(API_KEY, os.environ.get("VAST_URL", "https://console.vast.ai")))
app = FastAPI(title="Magic Pro launcher", docs_url=None, redoc_url=None, openapi_url=None)


# ---------------- auth: one shared password, signed cookie ----------------
def _sign(exp: int) -> str:
    return f"{exp}.{hmac.new(_key, str(exp).encode(), hashlib.sha256).hexdigest()}"


def _authed(request: Request) -> bool:
    try:
        exp, _sig = request.cookies.get(COOKIE, "").split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(_sign(int(exp)), request.cookies[COOKIE])
    except (ValueError, KeyError):
        return False


def _require(request: Request):
    if not _authed(request):
        raise HTTPException(401, "Sign in first.")
    if not pool.cfg.launcher_url:   # nodes we create need to know where to send heartbeats
        pool.cfg.launcher_url = f"https://{request.url.netloc}"


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = "", next: str = "/"):
    msg = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    nxt = html.escape(next if next.startswith("/") and not next.startswith("//") else "/")
    return PAGE.replace("{{BODY}}", f"""
<form method="post" action="/login" class="card">
  <h1>Magic Pro</h1><p class="muted">Sign in to start a GPU.</p>{msg}
  <input type="hidden" name="next" value="{nxt}">
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
  <button class="btn primary" type="submit">Sign in</button>
</form>""")


@app.post("/login")
async def login(request: Request, password: str = Form(""), next: str = Form("/")):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 10:
        return RedirectResponse("/login?error=Too+many+attempts.+Try+again+in+10+minutes.", 303)
    if not PASSWORD or not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        _fails[ip] = recent + [time.time()]
        await asyncio.sleep(1)
        return RedirectResponse("/login?error=Wrong+password.&next=" + quote(next), 303)
    r = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", 303)
    exp = int(time.time()) + SESSION_DAYS * 86400
    r.set_cookie(COOKIE, _sign(exp), max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="lax")
    return r


@app.post("/logout")
def logout():
    r = RedirectResponse("/login", 303)
    r.delete_cookie(COOKIE)
    return r


# ---------------- users ----------------
def _node_url(n: poolmod.Node) -> str:
    tunnel = n.beat["tunnel"].rstrip("/")
    token = n.inst.get("jupyter_token", "")
    return f"{tunnel}/?token={quote(token)}&autostart=1"


@app.post("/api/assign")
async def assign(request: Request, body: dict = Body(default={})):
    """Give this browser a GPU node. {cid, exclude?} -> {url} or {wait: true, text}."""
    _require(request)
    cid = str(body.get("cid", ""))[:64] or secrets.token_hex(8)
    exclude = {str(body["exclude"])} if body.get("exclude") else set()
    if exclude:
        for e in exclude:
            pool.report_unreachable(e)
    try:
        await pool.refresh()
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    n = pool.assign(cid, exclude)
    await pool.tick()                 # scale early when this user used up the last free capacity
    if n:
        return {"url": _node_url(n), "node": n.id}
    s = pool.summary()
    booting = [x for x in s["nodes"] if x["state"] == "booting"]
    text = ("Starting GPU…" if booting else
            pool.note if pool.note else "Looking for a free GPU…")
    return {"wait": True, "text": text, "booting": len(booting), "note": pool.note}


@app.get("/api/pool")
async def pool_view(request: Request):
    _require(request)
    try:
        await pool.tick()
    except RuntimeError:
        pass
    return pool.summary()


@app.post("/api/pool/stop-all")
async def stop_all(request: Request, body: dict = Body(default={})):
    _require(request)
    done = await pool.stop_all(force=bool(body.get("force")))
    return {"stopped": done}


@app.post("/api/pool/adopt")
async def adopt(request: Request, body: dict = Body(...)):
    """Make an existing instance a pool node: labels it and returns its watchdog settings."""
    _require(request)
    iid = str(body.get("instance_id", "")).strip()
    if not iid.isdigit():
        raise HTTPException(400, "instance_id must be a number")
    nonce = secrets.token_hex(8)
    await pool.vast.set_label(iid, f"{pool.cfg.label}:{nonce}")
    await pool.refresh(force=True)
    return {"instance_id": iid, "env": poolmod.env_lines(pool.cfg, nonce)}


# ---------------- nodes ----------------
@app.post("/api/node/heartbeat")
async def node_heartbeat(request: Request):
    raw = await request.body()
    node_id = request.headers.get("x-node-id", "")
    try:
        await pool.refresh()
    except RuntimeError:
        pass
    n = pool.verify(node_id, request.headers.get("x-node-time", ""), request.headers.get("x-node-sig", ""), raw)
    if n is None:
        await pool.refresh(force=True)   # a node we have not listed yet (just created / adopted)
        n = pool.verify(node_id, request.headers.get("x-node-time", ""), request.headers.get("x-node-sig", ""), raw)
    if n is None:
        raise HTTPException(403, "Unknown node or bad signature.")
    try:
        beat = json.loads(raw or b"{}")
    except ValueError as e:
        raise HTTPException(400, "bad json") from e
    reply = pool.heartbeat(n, beat)
    await pool.tick()                 # Cloud Run gives no CPU between requests: decide while we have it
    return reply


@app.post("/api/pool/tick")
async def tick(request: Request):
    """For an optional Cloud Scheduler job (only needed with POOL_MIN_WARM > 0)."""
    want = hmac.new(pool.cfg.secret.encode(), b"tick", hashlib.sha256).hexdigest() if pool.cfg.secret else ""
    if not want or not hmac.compare_digest(want, request.headers.get("x-tick-token", "")):
        raise HTTPException(403, "bad tick token")
    return {"actions": [list(a) for a in await pool.tick()]}


@app.get("/healthz")
def healthz():
    return {"ok": bool(API_KEY and PASSWORD and pool.cfg.secret)}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not _authed(request):
        return RedirectResponse("/login?next=" + quote(str(request.url.path) + ("?" + request.url.query if request.url.query else "")), 303)
    return PAGE.replace("{{BODY}}", HOME)


HOME = """
<main class="card" aria-live="polite">
  <h1>Magic Pro</h1>
  <p id="state" class="state"><span class="dot"></span><span id="text">Ready when you are.</span></p>
  <p id="detail" class="muted"></p>
  <p id="err" class="err" role="alert" hidden></p>
  <div class="row"><button class="btn primary" id="go" type="button">Start camera</button>
    <button class="btn" id="cancel" type="button" hidden>Cancel</button></div>
  <details id="gpus"><summary>GPUs</summary>
    <table><thead><tr><th>GPU</th><th>State</th><th>Users</th><th>Load</th><th>$/h</th></tr></thead><tbody id="rows"></tbody></table>
    <p id="pool-note" class="muted"></p>
    <div class="row"><button class="btn" id="stop-all" type="button">Stop idle GPUs now</button></div>
  </details>
  <form method="post" action="/logout"><button class="link" type="submit">Sign out</button></form>
</main>
<script>
const $ = (s) => document.querySelector(s);
const cid = sessionStorage.cid || (sessionStorage.cid = Math.random().toString(36).slice(2) + Date.now().toString(36));
const qs = new URLSearchParams(location.search);
let want = false, exclude = qs.get("exclude") || "", timer = null;
async function call(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  const b = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(b.message || b.detail || r.statusText); e.body = b; throw e; }
  return b;
}
const post = (p, d) => call(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(d || {}) });
function err(m) { $("#err").hidden = !m; $("#err").textContent = m || ""; }
function phase(p, text) { $("#state").dataset.phase = p; $("#text").textContent = text; }
async function tryAssign() {
  if (!want) return;
  try {
    const r = await post("/api/assign", { cid, exclude });
    if (r.url) { phase("ready", "Connecting…"); location.href = r.url; return; }
    phase("starting", r.text); $("#detail").textContent = r.note || "";
  } catch (e) { err(e.message); }
  timer = setTimeout(tryAssign, 3000);
}
function go() { want = true; err(""); $("#go").disabled = true; $("#cancel").hidden = false; phase("starting", "Finding a GPU…"); tryAssign(); }
$("#go").addEventListener("click", go);
$("#cancel").addEventListener("click", () => { want = false; clearTimeout(timer); $("#go").disabled = false; $("#cancel").hidden = true; phase("", "Cancelled. A GPU that was starting turns itself off again if nobody uses it."); });
async function refreshPool() {
  try {
    const s = await call("/api/pool");
    $("#rows").innerHTML = "";
    for (const n of s.nodes) {
      const tr = document.createElement("tr");
      for (const v of [`${n.id}${n.gpu ? " · " + n.gpu : ""}`, n.state + (n.dry_run ? " (test)" : ""), n.users, `${n.load}/${n.budget}`, n.price_per_hour]) {
        const td = document.createElement("td"); td.textContent = v; tr.appendChild(td);
      }
      $("#rows").appendChild(tr);
    }
    $("#pool-note").textContent = `Billing now: $${s.billing_per_hour}/h · limit ${s.limits.max_nodes} GPUs` + (s.note ? ` · ${s.note}` : "");
  } catch { /* shown by the main flow */ }
}
$("#gpus").addEventListener("toggle", () => { if ($("#gpus").open) refreshPool(); });
setInterval(() => { if ($("#gpus").open) refreshPool(); }, 5000);
$("#stop-all").addEventListener("click", async () => {
  if (!confirm("Stop every GPU nobody is streaming on?")) return;
  try { const r = await post("/api/pool/stop-all"); $("#pool-note").textContent = `Stopping: ${r.stopped.join(", ") || "none"}`; } catch (e) { err(e.message); }
});
if (qs.has("reconnect")) { history.replaceState(null, "", "/"); phase("starting", "Your GPU stopped answering. Moving you to another one…"); go(); }
</script>"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Magic Pro</title>
<style>
:root { --bg:#0f1113; --surface:#171a1d; --line:#2a2e33; --ink:#eceef0; --muted:#9aa1a9; --accent:#f2a93b; --live:#e5484d; --ok:#3fb950; color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; padding:16px; }
.card { width:100%; max-width:480px; background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:28px; display:grid; gap:14px; }
h1 { margin:0; font-size:22px; }
.muted { color:var(--muted); margin:0; font-size:14px; }
.err { color:#ff8a8d; margin:0; font-size:14px; }
.state { margin:0; display:flex; align-items:center; gap:10px; font-weight:600; }
.dot { width:10px; height:10px; border-radius:50%; background:var(--muted); flex:none; }
[data-phase=ready] .dot { background:var(--ok); }
[data-phase=starting] .dot { background:var(--accent); animation:pulse 1s infinite alternate; }
@keyframes pulse { to { opacity:.3; } }
.row { display:flex; gap:10px; flex-wrap:wrap; }
.btn { min-height:44px; padding:0 18px; border-radius:10px; border:1px solid var(--line); background:#20242a; color:var(--ink); font:600 14px inherit; font-family:inherit; cursor:pointer; }
.btn.primary { background:var(--accent); border-color:var(--accent); color:#1a1206; }
.btn:disabled { opacity:.6; cursor:default; }
label { font-size:13px; color:var(--muted); }
input { min-height:44px; border-radius:10px; border:1px solid var(--line); background:#0f1113; color:var(--ink); padding:0 12px; font:inherit; }
details { border-top:1px solid var(--line); padding-top:10px; display:grid; gap:10px; }
summary { cursor:pointer; color:var(--muted); font-size:14px; }
table { width:100%; border-collapse:collapse; font-size:13px; margin:8px 0; }
th, td { text-align:left; padding:4px 6px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; }
.link { background:none; border:0; color:var(--muted); padding:0; cursor:pointer; font:inherit; font-size:13px; text-decoration:underline; }
</style></head><body>{{BODY}}</body></html>"""
