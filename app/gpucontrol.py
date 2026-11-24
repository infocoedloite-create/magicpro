"""GPU on/off bookkeeping for the instance watchdog and the always-on launcher.

The app is the ground truth for "is anyone streaming". A user counts as present only while
their WebSocket is open AND their page has sent a heartbeat in the last HEARTBEAT_TIMEOUT
seconds, so a crashed tab or dropped Wi-Fi stops counting within 15 s even if TCP lingers.
Nothing here stops the GPU itself: the watchdog (deploy/vast/watchdog) reads /api/gpu/status
and does that, and picks up "Stop GPU now" requests from a file in STATE_DIR.
"""
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request

HEARTBEAT_TIMEOUT = float(os.environ.get("MAGIC_HEARTBEAT_TIMEOUT", "15"))
STATE_DIR = Path(os.environ.get("MAGIC_GPU_STATE_DIR", "/tmp/magicpro-gpu"))
PORTAL = os.environ.get("MAGIC_PORTAL_URL", "http://localhost:11111")
EXTERNAL_PORT = os.environ.get("MAGIC_EXTERNAL_PORT", "10100")

router = APIRouter(prefix="/api/gpu")
_engine = None
_beats: dict[str, tuple[float, bool]] = {}   # client id -> (last heartbeat, streaming)
_control = {"last": 0.0}                     # last contact from the launcher


def attach(app, engine):
    global _engine
    _engine = engine
    app.include_router(router)


def _streaming(exclude: str = "") -> int:
    now = time.time()
    for cid in [c for c, (t, _) in _beats.items() if now - t > HEARTBEAT_TIMEOUT * 4]:
        _beats.pop(cid, None)
    beating = sum(1 for c, (t, s) in _beats.items() if s and c != exclude and now - t <= HEARTBEAT_TIMEOUT)
    sockets = _engine.browser_clients if _engine else 0
    return min(sockets, beating)


def _watchdog() -> dict:
    try:
        return json.loads((STATE_DIR / "watchdog.json").read_text())
    except (OSError, ValueError):
        return {}


def status(request: Request | None = None) -> dict:
    if request is not None and request.headers.get("x-magic-control"):
        _control["last"] = time.time()
    last = _control["last"]
    return {
        "active_users": _streaming(),
        "sockets": _engine.browser_clients if _engine else 0,
        "heartbeats": sum(1 for t, s in _beats.values() if s and time.time() - t <= HEARTBEAT_TIMEOUT),
        "control_contact_age_s": round(time.time() - last, 1) if last else None,
        "ready": True,
        "watchdog": _watchdog(),
    }


@router.get("/status")
def gpu_status(request: Request):
    return status(request)


@router.post("/heartbeat")
def heartbeat(body: dict = Body(...)):
    cid = str(body.get("cid", ""))[:64]
    if not cid:
        raise HTTPException(400, "cid required")
    _beats[cid] = (time.time(), bool(body.get("streaming")))
    return status()


@router.post("/stop")
def stop_now(request: Request, body: dict = Body(default={})):
    """'Stop GPU now'. Refused while someone else is streaming unless force is set."""
    if request.headers.get("x-magic-control"):
        _control["last"] = time.time()
    cid = str(body.get("cid", ""))[:64]
    others = _streaming(exclude=cid)
    if others and not body.get("force"):
        raise HTTPException(409, f"{others} other {'person is' if others == 1 else 'people are'} still streaming.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "stop-request").write_text(json.dumps({"at": time.time(), "cid": cid}))
    return {"ok": True, "message": "Stopping the GPU. It will be off in about a minute."}


@router.get("/tunnel")
def tunnel():
    """Current Cloudflare quick-tunnel URL for this app (it changes on every restart)."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{PORTAL}/get-all-quick-tunnels", timeout=5) as r:
            tunnels = json.load(r)
    except (OSError, ValueError) as e:
        raise HTTPException(503, f"Instance portal unavailable: {e}") from e
    for t in tunnels:
        if str(t.get("targetUrl", "")).rstrip("/").endswith(f":{EXTERNAL_PORT}"):
            return {"url": t.get("tunnelUrl")}
    raise HTTPException(404, "No quick tunnel for the app yet.")
