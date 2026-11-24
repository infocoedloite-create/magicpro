"""GPU on/off and pool bookkeeping for the node watchdog (deploy/vast/watchdog) and launcher.

The app is the ground truth for "is anyone streaming". A user counts as present only while
their WebSocket is open AND their page sent a heartbeat in the last HEARTBEAT_TIMEOUT seconds,
so a crashed tab or dropped Wi-Fi stops counting within 15 s even if TCP lingers. Heartbeats
are kept as small files so every worker process (MAGIC_WORKERS > 1) sees all of them.

Capacity is in cost units: this node has MAGIC_GPU_BUDGET units (default 4.0) and each stream
costs stream_cost(settings). Defaults come from load tests on an RTX PRO 6000 at 1080p:
GFPGAN 1080p30 = 1.0 (4 users), plain 1080p30 = 0.5 (8 users), 60 fps doubles. Override any
part with MAGIC_COST_MODEL='{"swap": 0.5, "enhancer": 0.5, ...}' after new load tests.
Nothing here stops the GPU: the watchdog reads /api/gpu/status and does that.
"""
import json
import os
import re
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

HEARTBEAT_TIMEOUT = float(os.environ.get("MAGIC_HEARTBEAT_TIMEOUT", "15"))
STATE_DIR = Path(os.environ.get("MAGIC_GPU_STATE_DIR", Path(tempfile.gettempdir()) / "magicpro-gpu"))
BEATS = STATE_DIR / "beats"
PORTAL = os.environ.get("MAGIC_PORTAL_URL", "http://localhost:11111")
EXTERNAL_PORT = os.environ.get("MAGIC_EXTERNAL_PORT", "10100")
BUDGET = float(os.environ.get("MAGIC_GPU_BUDGET", "4.0"))

COST_MODEL = {
    "swap": 0.5,          # detect + swap + video codec, one 1080p30 stream
    "enhancer": 0.5,      # GFPGAN on top
    "fps_ref": 30,        # cost scales with fps / fps_ref
    "resolution": {"1080p": 1.0, "720p": 0.5},          # 720p is an estimate, not load-tested yet
    "swapper": {"inswapper_512_live": 2.0, "inswapper_512": 5.0, "inswapper_studio": 8.0},  # x plain swap
}
try:
    COST_MODEL.update(json.loads(os.environ.get("MAGIC_COST_MODEL", "{}")))
except ValueError:
    pass

router = APIRouter(prefix="/api/gpu")
_engine = None


def attach(app, engine):
    global _engine
    _engine = engine
    app.include_router(router)


def stream_cost(s: dict) -> float:
    m = COST_MODEL
    swap = m["swap"] * float(m["swapper"].get(s.get("swapper") or "", 1.0))
    enh = m["enhancer"] if s.get("enhancer") else 0.0
    fps = float(s.get("fps") or m["fps_ref"]) / float(m["fps_ref"])
    res = float(m["resolution"].get(s.get("resolution") or "1080p", 1.0))
    return round((swap + enh) * fps * res, 3)


def _beats(exclude: str = "") -> int:
    """Fresh 'streaming' heartbeats from all worker processes."""
    now, n = time.time(), 0
    for f in BEATS.glob("*.json") if BEATS.exists() else []:
        try:
            age = now - f.stat().st_mtime
            if age > HEARTBEAT_TIMEOUT * 4:
                f.unlink(missing_ok=True)
                continue
            if age <= HEARTBEAT_TIMEOUT and f.stem != exclude and json.loads(f.read_text()).get("streaming"):
                n += 1
        except (OSError, ValueError):
            continue
    return n


def _sockets() -> int:
    if not _engine:
        return 0
    try:
        return int(_engine.cluster_stats().get("browser_clients", 0))
    except Exception:  # noqa: BLE001  older engine without cluster stats
        return int(getattr(_engine, "browser_clients", 0))


def _streaming(exclude: str = "") -> int:
    return min(_sockets(), _beats(exclude))


def _watchdog() -> dict:
    try:
        return json.loads((STATE_DIR / "watchdog.json").read_text())
    except (OSError, ValueError):
        return {}


def status() -> dict:
    users = _streaming()
    try:
        settings = _engine.settings_dict() if _engine else {}
        stats = _engine.cluster_stats() if _engine else {}
    except Exception:  # noqa: BLE001
        settings, stats = {}, {}
    cost = stream_cost(settings)
    return {
        "active_users": users,
        "sockets": _sockets(),
        "heartbeats": _beats(),
        "stream_cost": cost,
        "load": round(users * cost, 3),
        "budget": BUDGET,
        "healthy": stats.get("status") != "error",
        "error": stats.get("error", ""),
        "fps": stats.get("fps"),
        "ready": True,
        "watchdog": _watchdog(),
    }


@router.get("/status")
def gpu_status():
    return status()


@router.post("/heartbeat")
def heartbeat(body: dict = Body(...)):
    cid = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("cid", "")))[:64]
    if not cid:
        raise HTTPException(400, "cid required")
    BEATS.mkdir(parents=True, exist_ok=True)
    tmp = BEATS / f".{cid}.tmp"
    tmp.write_text(json.dumps({"streaming": bool(body.get("streaming"))}))
    tmp.replace(BEATS / f"{cid}.json")
    return status()


@router.post("/stop")
def stop_now(body: dict = Body(default={})):
    """'Stop GPU now'. Refused while someone else is streaming unless force is set."""
    cid = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("cid", "")))[:64]
    others = _streaming(exclude=cid)
    if others and not body.get("force"):
        raise HTTPException(409, f"{others} other {'person is' if others == 1 else 'people are'} still streaming.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "stop-request").write_text(json.dumps({"at": time.time(), "cid": cid}))
    return {"ok": True, "message": "Stopping this GPU. It will be off in about a minute."}


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
