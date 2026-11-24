"""FastAPI control server: settings API, face library, MJPEG preview."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .pipeline import Engine, list_models

engine = Engine()


@asynccontextmanager
async def lifespan(_app):
    yield
    engine.stop()


app = FastAPI(title="Magic Pro Live Face Swap", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC), name="static")
app.mount("/library-files", StaticFiles(directory=config.LIBRARY), name="library")

NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


@app.get("/")
def index():
    return FileResponse(config.STATIC / "index.html", headers=NO_CACHE)


@app.get("/api/state")
def state():
    engine._pick_default_models()
    return {
        "settings": engine.settings_dict(),
        "swappers": list_models(config.SWAPPERS),
        "enhancers": list_models(config.ENHANCERS),
        "library": engine.library.items(),
        "running": engine.running,
        "stats": engine.stats,
        "runtime": engine.runtime(),
        "models_ready": config.DETECTOR.exists() and config.RECOGNIZER.exists(),
    }


@app.get("/api/stats")
def stats():
    return {"running": engine.running, "browser_clients": engine.browser_clients, **engine.stats}


@app.post("/api/settings")
def settings(patch: dict = Body(...)):
    return engine.update(patch)


@app.post("/api/start")
def start():
    engine.start()
    return {"running": True}


@app.post("/api/stop")
def stop():
    engine.stop()
    return {"running": False}


@app.get("/api/cameras")
def cameras():
    return engine.probe_cameras()


@app.get("/api/library")
def library():
    return engine.library.items()


@app.post("/api/library")
async def upload(file: UploadFile = File(...), name: str = Form(""), consent: bool = Form(False)):
    if not consent:
        raise HTTPException(400, "Confirm you have the person's consent to use this face.")
    data = await file.read()
    if len(data) > 25 * 2**20:
        raise HTTPException(413, "Image is larger than 25 MB.")
    try:
        return await run_in_threadpool(engine.library.add, name or file.filename.rsplit(".", 1)[0], data)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(422, str(e)) from e


@app.delete("/api/library/{item_id}")
def delete(item_id: str):
    if not engine.library.delete(item_id):
        raise HTTPException(404, "Not found")
    if item_id in engine.s.source_ids:
        engine.update({"source_ids": [i for i in engine.s.source_ids if i != item_id]})
    return {"ok": True}


@app.websocket("/ws/stream")
async def stream(ws: WebSocket):
    """Browser webcam mode. Client frame = 4-byte id + 4 reserved bytes + JPEG.
    Reply = the same 4-byte id + swapped JPEG. Only the newest frame is processed, so a slow
    link drops frames instead of building up delay; the client keeps a few frames in flight."""
    await ws.accept()
    engine.browser_clients += 1
    slot = {"data": None}
    ready = asyncio.Event()

    async def receive():
        while True:
            slot["data"] = await ws.receive_bytes()
            ready.set()

    async def work():
        while True:
            await ready.wait()
            ready.clear()
            data, slot["data"] = slot["data"], None
            if not data or len(data) < 9:
                continue
            out = await run_in_threadpool(engine.process_jpeg, data[8:])
            if out is not None:
                await ws.send_bytes(data[:4] + out)

    # two workers: while one frame is on the GPU, the other is being JPEG-decoded/encoded on the CPU
    tasks = [asyncio.create_task(receive()), asyncio.create_task(work()), asyncio.create_task(work())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for t in tasks:
            t.cancel()
        engine.browser_clients -= 1
        if engine.browser_clients == 0 and not engine.running:
            engine.stats.update(status="idle", fps=0.0)


@app.websocket("/ws/h264")
async def stream_h264(ws: WebSocket):
    """Browser WebCodecs mode: H.264 in, swapped H.264 out (see app/h264stream.py)."""
    from .h264stream import H264Session

    await ws.accept()
    engine.browser_clients += 1
    session = H264Session(engine, asyncio.get_running_loop(), ws.send_bytes, ws.send_text)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                session.feed(msg["bytes"])
            elif msg.get("text"):
                session.control(msg["text"])
    except Exception:  # noqa: BLE001  client went away
        pass
    finally:
        await run_in_threadpool(session.close)
        engine.browser_clients -= 1
        if engine.browser_clients == 0 and not engine.running:
            engine.stats.update(status="idle", fps=0.0)


@app.get("/video.mjpg")
def video(full: int = 0):
    return StreamingResponse(engine.mjpeg(bool(full)),
                             media_type="multipart/x-mixed-replace; boundary=frame", headers=NO_CACHE)


@app.get("/snapshot.jpg")
def snapshot(full: int = 1):
    return Response(engine.jpeg(bool(full)), media_type="image/jpeg", headers=NO_CACHE)
