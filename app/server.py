"""FastAPI control server: settings API, face library, MJPEG preview."""
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
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
    return {"running": engine.running, **engine.stats}


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


@app.get("/video.mjpg")
def video(full: int = 0):
    return StreamingResponse(engine.mjpeg(bool(full)),
                             media_type="multipart/x-mixed-replace; boundary=frame", headers=NO_CACHE)


@app.get("/snapshot.jpg")
def snapshot(full: int = 1):
    return Response(engine.jpeg(bool(full)), media_type="image/jpeg", headers=NO_CACHE)
