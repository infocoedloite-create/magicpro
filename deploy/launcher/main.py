"""Magic Pro launcher: an always-on page (Cloud Run) that starts the Vast.ai GPU on demand.

Flow: sign in -> Start camera -> the Vast instance is started -> this page polls until the app
on the instance answers -> the browser is sent to the app's current HTTPS address (the
Cloudflare quick tunnel, which changes on every restart) with ?autostart=1.
Stopping is the instance watchdog's job (deploy/vast/watchdog); this page only adds a
"Stop GPU now" button and a "Cancel start" when the stopped GPU was rented by someone else.

Environment:
  VAST_API_KEY        Vast.ai API key (from Secret Manager)              required
  LAUNCHER_PASSWORD   shared sign-in password (from Secret Manager)      required
  VAST_INSTANCE_ID    instance to control                                required
  APP_PORT            the app's Caddy (token-auth) container port        default 10100
  VAST_URL            default https://console.vast.ai
"""
import asyncio
import hashlib
import hmac
import html
import os
import time

import httpx
from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

VAST_URL = os.environ.get("VAST_URL", "https://console.vast.ai").rstrip("/")
API_KEY = os.environ.get("VAST_API_KEY", "")
PASSWORD = os.environ.get("LAUNCHER_PASSWORD", "")
INSTANCE_ID = os.environ.get("VAST_INSTANCE_ID", "")
APP_PORT = os.environ.get("APP_PORT", "10100")
SESSION_DAYS = 14
COOKIE = "mp_launcher"
UNAVAILABLE_AFTER = 75   # seconds a start may sit in "scheduling" before we say the GPU is taken

_key = hashlib.sha256(f"magicpro-launcher|{PASSWORD}|{API_KEY}".encode()).digest()
_started_at: dict[str, float] = {}
_fails: dict[str, list[float]] = {}
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


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    msg = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return PAGE.replace("{{BODY}}", f"""
<form method="post" action="/login" class="card">
  <h1>Magic Pro</h1><p class="muted">Sign in to start the GPU.</p>{msg}
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
  <button class="btn primary" type="submit">Sign in</button>
</form>""")


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 10:
        return RedirectResponse("/login?error=Too+many+attempts.+Try+again+in+10+minutes.", 303)
    if not PASSWORD or not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        _fails[ip] = recent + [time.time()]
        await asyncio.sleep(1)
        return RedirectResponse("/login?error=Wrong+password.", 303)
    r = RedirectResponse("/", 303)
    exp = int(time.time()) + SESSION_DAYS * 86400
    r.set_cookie(COOKIE, _sign(exp), max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="strict")
    return r


@app.post("/logout")
def logout():
    r = RedirectResponse("/login", 303)
    r.delete_cookie(COOKIE)
    return r


# ---------------- Vast.ai + the app on the instance ----------------
async def vast(method: str, path: str, json=None) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, f"{VAST_URL}/api/v0{path}", json=json,
                            headers={"Authorization": f"Bearer {API_KEY}"})
    try:
        body = r.json()
    except ValueError:
        body = {"msg": r.text[:300]}
    if r.status_code >= 400:
        raise HTTPException(502, f"Vast.ai said {r.status_code}: {body.get('msg') or body.get('error') or body}")
    return body


async def instance() -> dict:
    d = await vast("GET", f"/instances/{INSTANCE_ID}/")
    inst = d.get("instances", d)
    if isinstance(inst, list):
        inst = inst[0] if inst else {}
    if not inst:
        raise HTTPException(502, f"Vast.ai has no instance {INSTANCE_ID}.")
    return inst


def _direct(inst: dict) -> str | None:
    try:
        port = inst["ports"][f"{APP_PORT}/tcp"][0]["HostPort"]
        return f"http://{inst['public_ipaddr'].strip()}:{port}"
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


async def app_call(inst: dict, method: str, path: str, json=None, timeout=4.0):
    """Talk to the app through the instance's Caddy edge (token = the instance's jupyter_token)."""
    base = _direct(inst)
    if not base:
        return None
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, base + path, json=json, headers={
            "Authorization": f"Bearer {inst.get('jupyter_token', '')}", "X-Magic-Control": "1"})
    return r


def _phase(inst: dict, ready: bool) -> tuple[str, str]:
    actual, intended = inst.get("actual_status") or "", inst.get("intended_status") or ""
    msg = (inst.get("status_msg") or "").strip()
    if ready:
        return "ready", "GPU is on."
    if intended == "stopped":
        return ("stopping", "Stopping the GPU…") if actual == "running" else ("off", "GPU is off.")
    if actual == "running":
        return "loading", "GPU is on, loading the face swap models…"
    started = _started_at.get(INSTANCE_ID)
    waited = time.time() - started if started else 0
    if (actual == "scheduling" or "schedul" in msg.lower()) and (not started or waited > UNAVAILABLE_AFTER):
        return "unavailable", ("Our GPU was rented by someone else while it was stopped, so it cannot start "
                               "right now. Vast will start it as soon as that machine frees up (the watchdog "
                               "turns it off again if nobody streams), or press Cancel start.")
    return "starting", "Starting GPU…"


@app.get("/api/status")
async def status(request: Request):
    _require(request)
    inst = await instance()
    ready, url, users, stop_in = False, None, None, None
    if inst.get("actual_status") == "running" and inst.get("intended_status") != "stopped":
        try:
            r = await app_call(inst, "GET", "/api/gpu/status")
            if r is not None and r.status_code == 200:
                s = r.json()
                users, stop_in = s.get("active_users"), (s.get("watchdog") or {}).get("stop_in_s")
                t = await app_call(inst, "GET", "/api/gpu/tunnel")
                if t is not None and t.status_code == 200 and t.json().get("url"):
                    url = f"{t.json()['url'].rstrip('/')}/?token={inst.get('jupyter_token', '')}&autostart=1"
                    ready = True
        except (httpx.HTTPError, ValueError):
            pass
    phase, text = _phase(inst, ready)
    if phase in ("ready", "off"):
        _started_at.pop(INSTANCE_ID, None)
    return {"phase": phase, "text": text, "url": url, "users": users, "stop_in_s": stop_in,
            "gpu": inst.get("gpu_name"), "price_per_hour": round(float(inst.get("dph_total") or 0), 3),
            "vast": {k: inst.get(k) for k in ("actual_status", "intended_status", "cur_state", "status_msg")}}


@app.post("/api/start")
async def start(request: Request):
    _require(request)
    body = await vast("PUT", f"/instances/{INSTANCE_ID}/", {"state": "running"})
    if body.get("success") is False:
        msg = body.get("msg") or body.get("error") or "Vast.ai refused to start the instance."
        return JSONResponse({"ok": False, "message": f"Could not start the GPU: {msg}"}, 409)
    _started_at.setdefault(INSTANCE_ID, time.time())
    return {"ok": True}


@app.post("/api/stop")
async def stop(request: Request, body: dict = Body(default={})):
    _require(request)
    inst = await instance()
    if not body.get("force") and inst.get("actual_status") == "running":
        try:
            r = await app_call(inst, "GET", "/api/gpu/status")
            users = r.json().get("active_users", 0) if r is not None and r.status_code == 200 else 0
        except (httpx.HTTPError, ValueError):
            users = 0
        if users:
            return JSONResponse({"ok": False, "users": users,
                                 "message": f"{users} {'person is' if users == 1 else 'people are'} still streaming."}, 409)
    res = await vast("PUT", f"/instances/{INSTANCE_ID}/", {"state": "stopped"})
    _started_at.pop(INSTANCE_ID, None)
    if res.get("success") is False:
        return JSONResponse({"ok": False, "message": res.get("msg") or "Vast.ai refused to stop it."}, 502)
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": bool(API_KEY and PASSWORD and INSTANCE_ID)}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return PAGE.replace("{{BODY}}", HOME)


HOME = """
<main class="card" aria-live="polite">
  <h1>Magic Pro</h1>
  <p id="state" class="state"><span class="dot"></span><span id="text">Checking the GPU…</span></p>
  <p id="detail" class="muted"></p>
  <p id="err" class="err" role="alert" hidden></p>
  <div class="row">
    <button class="btn primary" id="go" type="button" disabled>Start camera</button>
    <button class="btn" id="stop" type="button" hidden>Stop GPU now</button>
  </div>
  <form method="post" action="/logout"><button class="link" type="submit">Sign out</button></form>
</main>
<script>
const $ = (s) => document.querySelector(s);
let want = false, last = null;
async function call(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  const b = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(b.message || b.detail || r.statusText); e.body = b; throw e; }
  return b;
}
const post = (p, d) => call(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(d || {}) });
function err(m) { $("#err").hidden = !m; $("#err").textContent = m || ""; }
function render(s) {
  last = s;
  $("#text").textContent = s.text;
  $("#state").dataset.phase = s.phase;
  const bits = [s.gpu, s.price_per_hour && `$${s.price_per_hour}/h while on`];
  if (s.phase === "ready" && s.users != null) bits.push(`${s.users} streaming now`);
  if (s.phase === "ready" && s.stop_in_s != null) bits.push(`turns off in ${Math.ceil(s.stop_in_s / 60)} min if nobody streams`);
  $("#detail").textContent = bits.filter(Boolean).join(" · ");
  const busy = ["starting", "loading", "stopping"].includes(s.phase);
  const go = $("#go");
  go.disabled = busy || s.phase === "stopping";
  go.textContent = s.phase === "ready" ? "Open camera" : s.phase === "unavailable" ? "Keep waiting" : busy ? "Starting GPU…" : "Start camera";
  $("#stop").hidden = !["ready", "loading", "starting", "unavailable"].includes(s.phase);
  $("#stop").textContent = s.phase === "unavailable" || s.phase === "starting" ? "Cancel start" : "Stop GPU now";
  if (want && s.phase === "ready" && s.url) location.href = s.url;
}
async function poll() {
  try { render(await call("/api/status")); err(""); } catch (e) { if (e.message !== "signed out") err(e.message); }
  setTimeout(poll, last && ["starting", "loading", "stopping"].includes(last.phase) || want ? 3000 : 10000);
}
$("#go").addEventListener("click", async () => {
  want = true; err("");
  if (last && last.phase === "ready" && last.url) { location.href = last.url; return; }
  try { await post("/api/start"); render({ ...last, phase: "starting", text: "Starting GPU…" }); }
  catch (e) { want = false; err(e.message); }
});
$("#stop").addEventListener("click", async () => {
  want = false; err("");
  try { await post("/api/stop"); }
  catch (e) {
    if (!(e.body && e.body.users) || !confirm(e.message + " Stop the GPU for everyone anyway?")) { err(e.message); return; }
    try { await post("/api/stop", { force: true }); } catch (e2) { err(e2.message); return; }
  }
  render({ ...last, phase: "stopping", text: "Stopping the GPU…" });
});
poll();
</script>"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Magic Pro</title>
<style>
:root { --bg:#0f1113; --surface:#171a1d; --line:#2a2e33; --ink:#eceef0; --muted:#9aa1a9; --accent:#f2a93b; --live:#e5484d; --ok:#3fb950; color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; padding:16px; }
.card { width:100%; max-width:420px; background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:28px; display:grid; gap:14px; }
h1 { margin:0; font-size:22px; }
.muted { color:var(--muted); margin:0; font-size:14px; }
.err { color:#ff8a8d; margin:0; font-size:14px; }
.state { margin:0; display:flex; align-items:center; gap:10px; font-weight:600; }
.dot { width:10px; height:10px; border-radius:50%; background:var(--muted); flex:none; }
[data-phase=ready] .dot { background:var(--ok); }
[data-phase=starting] .dot, [data-phase=loading] .dot, [data-phase=stopping] .dot { background:var(--accent); animation:pulse 1s infinite alternate; }
[data-phase=unavailable] .dot { background:var(--live); }
@keyframes pulse { to { opacity:.3; } }
.row { display:flex; gap:10px; flex-wrap:wrap; }
.btn { min-height:44px; padding:0 18px; border-radius:10px; border:1px solid var(--line); background:#20242a; color:var(--ink); font:600 14px inherit; font-family:inherit; cursor:pointer; }
.btn.primary { background:var(--accent); border-color:var(--accent); color:#1a1206; }
.btn:disabled { opacity:.6; cursor:default; }
label { font-size:13px; color:var(--muted); }
input { min-height:44px; border-radius:10px; border:1px solid var(--line); background:#0f1113; color:var(--ink); padding:0 12px; font:inherit; }
.link { background:none; border:0; color:var(--muted); padding:0; cursor:pointer; font:inherit; font-size:13px; text-decoration:underline; }
</style></head><body>{{BODY}}</body></html>"""
