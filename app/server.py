"""FastAPI control server: settings API, face library, live streams.

Local mode (no MAGIC_SESSION_SECRET): single user, as on a PC.
Hosted mode (MAGIC_SESSION_SECRET set): users arrive from the website with a signed session
(docs/handoff-contract.md), each with their own settings and face library; streaming time is
reported to the website, which can end it.
"""
import asyncio
import json
import re
import time
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config
from .pipeline import Engine, TrackState, list_models, list_swappers

engine = Engine()
HOSTED = auth.ENABLED


@asynccontextmanager
async def lifespan(_app):
    yield
    engine.stop()


app = FastAPI(title="Magic Pro Live Face Swap", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC), name="static")

NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
OPEN_PATHS = ("/session", "/healthz", "/static/", "/favicon")


def signed_out_page(msg="Please sign in to continue."):
    return HTMLResponse(
        f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Magic Pro</title><body style="font:16px system-ui;background:#0e0f11;color:#ecedee;display:grid;
place-items:center;height:100vh;margin:0"><div style="text-align:center;max-width:420px;padding:24px">
<h1 style="font-size:22px">{msg}</h1><p style="color:#9aa0a6">Your session link has expired or isn't valid.</p>
<a href="{auth.WEB_URL}" style="display:inline-block;margin-top:12px;padding:12px 20px;background:#f2a93b;
color:#1a1203;border-radius:4px;text-decoration:none;font-weight:600">Back to Magic Pro</a></div>""",
        status_code=401, headers=NO_CACHE)


@app.middleware("http")
async def require_session(request: Request, call_next):
    request.state.claims = None
    if HOSTED and not request.url.path.startswith(OPEN_PATHS):
        claims = auth.verify(request.cookies.get(auth.COOKIE))
        if claims is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "signed out", "web_url": auth.WEB_URL}, status_code=401)
            return signed_out_page()
        request.state.claims = claims
    return await call_next(request)


def space_of(request: Request):
    c = request.state.claims
    return engine.space_for(auth.user_key(c)) if c else engine.space


def local_only():
    if HOSTED:
        raise HTTPException(403, "Not available on the hosted service.")


@app.get("/healthz")
def healthz():
    return {"ok": True, "hosted": HOSTED}


@app.get("/session")
def session(t: str = ""):
    """The website hands a signed-in user over: check the token, keep it in a cookie, open the app."""
    claims = auth.verify(t)
    if claims is None:
        return signed_out_page("This link has expired.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, t, max_age=max(60, int(float(claims["exp"]) - time.time())),
                    httponly=True, secure=True, samesite="lax")
    return resp


@app.get("/")
def index():
    return FileResponse(config.STATIC / "index.html", headers=NO_CACHE)


@app.get("/api/me")
def me(request: Request):
    c = request.state.claims
    if not c:
        return {"hosted": False}
    return {"hosted": True, "name": c.get("name", ""), "email": c.get("email", ""), "role": c.get("role", "user"),
            "remaining": round(auth.sessions.remaining(c)), "exp": c["exp"], "web_url": c.get("web_url", auth.WEB_URL)}


@app.get("/api/state")
def state(request: Request):
    sp = space_of(request)
    sp.pick_default_models()
    return {
        "settings": engine.settings_dict(sp),
        "swappers": list_swappers(),
        "enhancers": list_models(config.ENHANCERS),
        "library": sp.library.items(),
        "running": engine.running,
        "stats": stats(request),
        "runtime": engine.runtime() if not HOSTED else {},
        "models_ready": config.DETECTOR.exists() and config.RECOGNIZER.exists(),
        "hosted": HOSTED,
    }


@app.get("/api/stats")
def stats(request: Request):
    s = engine.cluster_stats()
    c = request.state.claims
    if c:   # a hosted user sees performance numbers, not how many others are online
        s = {k: v for k, v in s.items() if k not in ("browser_clients", "workers", "camera", "vcam", "running")}
        s["remaining"] = round(auth.sessions.remaining(c))
        s["stop"] = auth.sessions.stop_reason(c)
    return s


@app.post("/api/settings")
def settings(request: Request, patch: dict = Body(...)):
    if HOSTED:
        # the AI label stays on for hosted users (terms of use); server-camera options don't apply
        patch = {k: v for k, v in patch.items()
                 if k not in ("input_source", "camera", "camera_backend", "virtual_cam", "disclosure_label")}
    return engine.update(patch, space_of(request))


@app.post("/api/start")
def start():
    local_only()
    engine.start()
    return {"running": True}


@app.post("/api/stop")
def stop():
    local_only()
    engine.stop()
    return {"running": False}


@app.get("/api/cameras")
def cameras():
    local_only()
    return engine.probe_cameras()


@app.get("/api/library")
def library(request: Request):
    return space_of(request).library.items()


@app.post("/api/library")
async def upload(request: Request, file: UploadFile = File(...), name: str = Form(""), consent: bool = Form(False)):
    if not consent:
        raise HTTPException(400, "Confirm you have the person's consent to use this face.")
    data = await file.read()
    if len(data) > 25 * 2**20:
        raise HTTPException(413, "Image is larger than 25 MB.")
    lib = space_of(request).library
    try:
        return await run_in_threadpool(lib.add, name or file.filename.rsplit(".", 1)[0], data)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(422, str(e)) from e


@app.delete("/api/library/{item_id}")
def delete(request: Request, item_id: str):
    sp = space_of(request)
    if not sp.library.delete(item_id):
        raise HTTPException(404, "Not found")
    if item_id in sp.s.source_ids:
        engine.update({"source_ids": [i for i in sp.s.source_ids if i != item_id]}, sp)
    return {"ok": True}


LIB_FILE = re.compile(r"^[0-9a-f]{12}(_thumb)?\.jpg$")


@app.get("/library-files/{name}")
def library_file(request: Request, name: str):
    """Thumbnails from the requesting user's own library only."""
    if not LIB_FILE.match(name):
        raise HTTPException(404)
    path = space_of(request).library.root / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


async def ws_claims(ws: WebSocket):
    """Hosted mode: the stream needs the same signed session as the page."""
    if not HOSTED:
        return None, True
    c = auth.verify(ws.cookies.get(auth.COOKIE))
    return c, c is not None


async def end_when_due(ws: WebSocket, claims):
    """Close the stream when the website says the hours are used up or the session expires."""
    while True:
        await asyncio.sleep(1.0)
        reason = auth.sessions.stop_reason(claims)
        if reason:
            try:
                await ws.send_text(json.dumps({"t": "end", "reason": reason,
                                               "web_url": claims.get("web_url", auth.WEB_URL)}))
            finally:
                await ws.close(code=4402)
            return


@app.websocket("/ws/stream")
async def stream(ws: WebSocket):
    """Browser webcam mode (JPEG fallback). Client frame = 4-byte id + 4 reserved bytes + JPEG.
    Reply = the same 4-byte id + swapped JPEG. Only the newest frame is processed."""
    claims, ok = await ws_claims(ws)
    if not ok:
        await ws.close(code=4401)
        return
    await ws.accept()
    engine.browser_clients += 1
    space = engine.space_for(auth.user_key(claims)) if claims else None
    if claims:
        auth.sessions.open(claims)
    track = TrackState(space)
    slot = {"data": None}
    ready = asyncio.Event()

    async def receive():
        while True:
            slot["data"] = await ws.receive_bytes()
            if claims:
                auth.sessions.touch(claims)
            ready.set()

    async def work():
        while True:
            await ready.wait()
            ready.clear()
            data, slot["data"] = slot["data"], None
            if not data or len(data) < 9:
                continue
            out = await run_in_threadpool(engine.process_jpeg, data[8:], track)
            if out is not None:
                await ws.send_bytes(data[:4] + out)

    tasks = [asyncio.create_task(receive()), asyncio.create_task(work()), asyncio.create_task(work())]
    if claims:
        tasks.append(asyncio.create_task(end_when_due(ws, claims)))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        engine.browser_clients -= 1
        if claims:
            await run_in_threadpool(auth.sessions.close, claims)
        if engine.browser_clients == 0 and not engine.running:
            engine.stats.update(status="idle", fps=0.0)


@app.websocket("/ws/h264")
async def stream_h264(ws: WebSocket):
    """Browser WebCodecs mode: H.264 in, swapped H.264 out (see app/h264stream.py)."""
    from .h264stream import H264Session

    claims, ok = await ws_claims(ws)
    if not ok:
        await ws.close(code=4401)
        return
    await ws.accept()
    engine.browser_clients += 1
    space = engine.space_for(auth.user_key(claims)) if claims else None
    if claims:
        auth.sessions.open(claims)
    session = H264Session(engine, asyncio.get_running_loop(), ws.send_bytes, ws.send_text, space=space)
    watcher = asyncio.create_task(end_when_due(ws, claims)) if claims else None
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                session.feed(msg["bytes"])
                if claims:
                    auth.sessions.touch(claims)
            elif msg.get("text"):
                session.control(msg["text"])
    except Exception:  # noqa: BLE001  client went away, or the stream was ended
        pass
    finally:
        if watcher:
            watcher.cancel()
        await run_in_threadpool(session.close)
        engine.browser_clients -= 1
        if claims:
            await run_in_threadpool(auth.sessions.close, claims)
        if engine.browser_clients == 0 and not engine.running:
            engine.stats.update(status="idle", fps=0.0)


@app.get("/video.mjpg")
def video(full: int = 0):
    local_only()     # the shared preview would show other users' video
    return StreamingResponse(engine.mjpeg(bool(full)),
                             media_type="multipart/x-mixed-replace; boundary=frame", headers=NO_CACHE)


@app.get("/snapshot.jpg")
def snapshot(full: int = 1):
    local_only()
    return Response(engine.jpeg(bool(full)), media_type="image/jpeg", headers=NO_CACHE)
