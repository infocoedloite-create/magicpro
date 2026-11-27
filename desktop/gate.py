"""The ASGI app the desktop runs: the studio (app.server, unchanged) behind the licence.

    /desktop/...   activation, first-run setup, updates (always open)
    everything else  the studio, only while the licence is valid, the runtime and models are in
                     place and no required update is pending

The studio is imported only once the GPU runtime is installed (it needs ONNX Runtime and
OpenCV). When the licence lapses while the studio is open (subscription ended, key cancelled),
the camera engine stops, live streams are closed with code 4401 (the studio page then reloads
and lands on /desktop/) and API calls answer 401 with web_url=/desktop/, which the studio page
already follows. Requests must come from this PC's own address (no DNS rebinding), and the
desktop's own POSTs need an X-Magic-Desktop header, which other websites can't send.
"""
import asyncio
import threading
import time
import webbrowser

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import paths
from .license import Offline, Refused
from .version import VERSION

WEB = paths.ROOT / "desktop" / "web"
NO_CACHE = {"Cache-Control": "no-store"}
STREAMS = ("/video.mjpg",)
CLOUD_ONLY = ("/api/gpu/stop", "/api/gpu/tunnel")      # GPU-pool controls of the hosted service
INJECT = ('<link rel="stylesheet" href="/desktop/static/desktop.css">'
          '<script src="/desktop/static/badge.js" defer></script>')


class Life:
    """When the app window is gone, the app quits (see __main__)."""

    def __init__(self):
        self.last = time.time()
        self.bye = 0.0
        self.sockets = 0

    def touch(self):
        self.last = time.time()
        self.bye = 0.0


class Gate:
    def __init__(self, lic, upd, setup, port, life=None):
        self.lic, self.upd, self.setup, self.port = lic, upd, setup, port
        self.life = life or Life()
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.studio = self.engine = None
        self.studio_error = ""
        self._loading = threading.Lock()
        self.panel = self._panel()
        lic.listeners.append(self._licence_changed)

    # ----- the studio -----
    def load_studio(self):
        """Import app.server (unchanged studio) once the runtime is there."""
        with self._loading:
            if self.studio is not None:
                return True
            try:
                from app import server
                self.studio, self.engine = server.app, server.engine
                self.studio_error = ""
                print(f"[desktop] studio loaded, Magic Pro {VERSION}", flush=True)
                return True
            except Exception as e:  # noqa: BLE001
                self.studio_error = f"The studio couldn't start: {e}"
                print(f"[desktop] {self.studio_error}", flush=True)
                return False

    def _licence_changed(self, active):
        if not active and self.engine is not None:
            print("[desktop] licence not valid any more: stopping the camera", flush=True)
            try:
                self.engine.stop()
            except Exception as e:  # noqa: BLE001
                print(f"[desktop] stopping the engine failed: {e}", flush=True)

    def blocker(self):
        """-> None when the studio may run, else (what, where to send the user)."""
        if not self.lic.is_active():
            return "licence", "/desktop/"
        if self.upd.status().get("mandatory"):
            return "update", "/desktop/"
        if self.setup.needed():
            return "setup", "/desktop/"
        if self.studio is None and not self.load_studio():
            return "studio", "/desktop/"
        return None

    # ----- ASGI -----
    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            return await self._lifespan(receive, send)
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1")
        if host not in self.hosts:
            return await self._refuse(scope, receive, send, 421, "Wrong host.")
        path = scope.get("path", "")
        self.life.touch()
        if path.startswith("/desktop/") or path == "/desktop" or path == "/favicon.ico":
            return await self.panel(scope, receive, send)
        if path in CLOUD_ONLY:     # the desktop app never uses rented/server GPUs
            return await JSONResponse({"detail": "Not available in the desktop app."}, 404)(scope, receive, send)
        block = self.blocker()
        if kind == "websocket":
            if block:
                return await self._ws_close(receive, send)
            self.life.sockets += 1
            try:
                return await self.studio(scope, self._guard(receive, send, "websocket.disconnect"), send)
            finally:
                self.life.sockets -= 1
        if block:
            if path.startswith("/api/"):
                return await JSONResponse({"detail": {"licence": "Magic Pro isn't activated on this PC.",
                                                      "update": "Magic Pro needs an update.",
                                                      "setup": "Magic Pro is finishing its setup.",
                                                      "studio": self.studio_error}[block[0]],
                                           "web_url": block[1]}, 401)(scope, receive, send)
            if path.startswith("/static/") and self.studio is not None:
                return await self.studio(scope, receive, send)
            return await _redirect(block[1])(scope, receive, send)
        if path in ("/", "/index.html") and scope.get("method") == "GET":
            return await self._index()(scope, receive, send)
        if path in STREAMS:
            return await self.studio(scope, self._guard(receive, send, "http.disconnect"), send)
        return await self.studio(scope, receive, send)

    def _index(self):
        html = (paths.ROOT / "static" / "index.html").read_text(encoding="utf-8")   # = app.config.STATIC
        return HTMLResponse(html.replace("</head>", INJECT + "</head>", 1), headers=NO_CACHE)

    def _guard(self, receive, send, gone):
        """receive() that reports a disconnect (and closes a socket with 4401) once the licence lapses."""
        lic = self.lic

        async def guarded():
            task = asyncio.ensure_future(receive())
            while True:
                done, _ = await asyncio.wait({task}, timeout=1.0)
                if done:
                    return task.result()
                if not lic.is_active():
                    task.cancel()
                    if gone == "websocket.disconnect":
                        try:
                            await send({"type": "websocket.close", "code": 4401, "reason": "licence"})
                        except Exception:  # noqa: BLE001
                            pass
                        return {"type": gone, "code": 4401}
                    return {"type": gone}
        return guarded

    async def _ws_close(self, receive, send):
        msg = await receive()
        if msg["type"] == "websocket.connect":
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 4401, "reason": "licence"})

    async def _refuse(self, scope, receive, send, code, text):
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse({"detail": text}, code)(scope, receive, send)

    async def _lifespan(self, receive, send):
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                if self.engine is not None:
                    try:
                        self.engine.stop()
                    except Exception:  # noqa: BLE001
                        pass
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ----- /desktop -----
    def _panel(self):
        lic, upd, setup = self.lic, self.upd, self.setup
        p = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        p.mount("/desktop/static", StaticFiles(directory=WEB), name="desktop-static")

        @p.middleware("http")
        async def own_posts(request: Request, call_next):
            if request.method == "POST" and request.url.path != "/desktop/api/bye" \
                    and request.headers.get("x-magic-desktop") != "1":
                return JSONResponse({"detail": "Refused."}, 403)
            resp = await call_next(request)
            if request.url.path.startswith("/desktop/static/"):
                resp.headers["Cache-Control"] = "no-cache"      # after an update the window gets the new files
            return resp

        @p.get("/desktop")
        @p.get("/desktop/")
        def index():
            return FileResponse(WEB / "index.html", headers=NO_CACHE)

        @p.get("/favicon.ico")
        def favicon():
            return FileResponse(WEB / "icon.svg", media_type="image/svg+xml")

        @p.get("/desktop/api/status")
        def status():
            block = self.blocker() if lic.is_active() else ("licence", "/desktop/")
            return {"version": VERSION, "license": lic.status(), "update": upd.status(),
                    "setup": setup.status() if lic.is_active() else None,
                    "blocked": block[0] if block else None, "studio_error": self.studio_error,
                    "server": paths.CONFIG["server"]}

        def call(fn, *a):
            try:
                return fn(*a)
            except Offline as e:
                raise HTTPException(503, f"Can't reach {paths.CONFIG['server']} ({e}). Check your internet connection.") from e
            except Refused as e:
                raise HTTPException(400, e.detail) from e

        @p.post("/desktop/api/email/start")
        def email_start(body: dict = Body(default={})):
            if not lic.pubkey:
                raise HTTPException(400, "This copy of Magic Pro has no licence key built in. Rebuild it (docs/desktop.md).")
            return call(lic.email_start, str(body.get("key", "")).strip(), str(body.get("email", "")).strip())

        @p.post("/desktop/api/email/verify")
        def email_verify(body: dict = Body(default={})):
            st = call(lic.email_verify, str(body.get("code", "")))
            if st["active"]:
                lic.wake()
                if setup.needed():
                    setup.start()
            return st

        @p.post("/desktop/api/poll")
        def poll():
            st = call(lic.poll)
            if st["active"]:
                lic.wake()
                if setup.needed():
                    setup.start()
            return st

        @p.post("/desktop/api/cancel")
        def cancel():
            lic.cancel()
            return lic.status()

        @p.post("/desktop/api/check")
        def check():
            return lic.refresh()

        @p.post("/desktop/api/signout")
        def signout():
            lic.sign_out()
            return lic.status()

        @p.post("/desktop/api/renew")
        def renew():
            # buying / renewing a subscription goes through the payment site (magicpro.nacgenai.in)
            webbrowser.open(paths.CONFIG.get("buy_url") or paths.CONFIG["server"])
            return {"ok": True}

        @p.post("/desktop/api/setup/start")
        def setup_start():
            if not lic.is_active():
                raise HTTPException(403, "Activate Magic Pro first.")
            setup.start()
            return setup.status()

        @p.post("/desktop/api/update/check")
        def update_check():
            return upd.check()

        @p.post("/desktop/api/update/install")
        def update_install():
            return upd.install()

        @p.post("/desktop/api/heartbeat")
        def heartbeat():
            return {"ok": True}

        @p.post("/desktop/api/bye")
        async def bye(request: Request):
            await request.body()
            self.life.bye = time.time()
            return {"ok": True}

        return p


def _redirect(to):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(to, 303, headers=NO_CACHE)
