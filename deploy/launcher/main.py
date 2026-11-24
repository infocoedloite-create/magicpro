"""Magic Pro website (Cloud Run, https://magicpro.nacgenai.in) in front of the Vast.ai GPU pool.

Public pages, Google sign-in, hour balances, Razorpay hour packs, invite codes, an admin page, and
the GPU handoff of docs/handoff-contract.md: Start mints an HS256 session token and sends the
browser to {gpu_base}/session?t=...; GPU nodes report usage to /api/gpu/usage and the balance is
charged by the increase of total_seconds per session. The pool autoscaler is pool.py.

Configuration (env; secrets from Secret Manager): see Settings below and the README.
"""
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

import payments as paymod
import pool as poolmod
import store as storemod
import webauth

HERE = Path(__file__).resolve().parent
COOKIE, OAUTH_COOKIE = "mp_web", "mp_oauth"
COOKIE_DAYS = 30


@dataclass
class Settings:
    site_url: str = os.environ.get("SITE_URL", "https://magicpro.nacgenai.in").rstrip("/")
    session_secret: str = os.environ.get("MAGIC_SESSION_SECRET", "")
    google_client_id: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    admin_emails: set = field(default_factory=lambda: {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()})
    max_session_hours: float = float(os.environ.get("MAX_SESSION_HOURS", "4"))
    support_email: str = os.environ.get("SUPPORT_EMAIL", "")
    brand: str = os.environ.get("BRAND_NAME", "Magic Pro")

    @property
    def secure_cookies(self):
        return self.site_url.startswith("https://")


def make_store():
    if os.environ.get("STORE", "memory") == "firestore":
        return storemod.FirestoreStore(project=os.environ.get("FIRESTORE_PROJECT") or None,
                                       database=os.environ.get("FIRESTORE_DATABASE", "(default)"))
    return storemod.MemoryStore()


def hms(seconds) -> str:
    s = max(0, int(seconds or 0))
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def create_app(settings=None, store=None, pool=None, google=None, razorpay=None) -> FastAPI:
    S = settings or Settings()
    db = store or make_store()
    if pool is None:
        pool = poolmod.Pool(poolmod.Vast(os.environ.get("VAST_API_KEY", ""), os.environ.get("VAST_URL", "https://console.vast.ai")))
    pool.cfg.launcher_url = pool.cfg.launcher_url or S.site_url
    pool.cfg.session_secret = pool.cfg.session_secret or S.session_secret
    gg = google or webauth.Google(S.google_client_id, S.google_client_secret, f"{S.site_url}/auth/callback")
    rp = razorpay or paymod.Razorpay(os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", ""),
                                     os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""))
    packs = paymod.load_packs()
    cookies = webauth.Signer(S.session_secret or "dev-only-secret", "cookie")
    oauth_state = webauth.Signer(S.session_secret or "dev-only-secret", "oauth")
    jenv = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    jenv.filters["hms"] = hms
    jenv.filters["date"] = lambda t: time.strftime("%d %b %Y, %H:%M", time.localtime(t)) if t else ""
    jenv.filters["inr"] = lambda p: f"₹{float(p):,.0f}"

    app = FastAPI(title="Magic Pro", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.store, app.state.pool, app.state.settings = db, pool, S

    machine = ("/api/gpu/usage", "/api/pay/webhook", "/api/node/heartbeat", "/api/pool/tick")

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        """Signed-in POSTs must come from our own pages (the cookie is SameSite=Lax as well)."""
        if request.method == "POST" and request.url.path not in machine:
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if origin and urlparse(origin).netloc != urlparse(S.site_url).netloc and urlparse(origin).netloc != request.url.netloc:
                return JSONResponse({"detail": "Cross-site request refused."}, 403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    def page(name, request, **ctx):
        user = current(request)
        html = jenv.get_template(name).render(S=S, user=user, is_admin=is_admin(user), packs=packs,
                                              min_price=min(p["price_inr"] for p in packs), test_mode=not rp.live, **ctx)
        return HTMLResponse(html)

    # ---------------- who is signed in ----------------
    def current(request: Request):
        c = cookies.loads(request.cookies.get(COOKIE))
        return db.get_user(c["sub"]) if c else None

    def is_admin(user) -> bool:
        return bool(user and user.get("email", "").lower() in S.admin_emails)

    def need_user(request: Request):
        u = current(request)
        if not u:
            raise HTTPException(401, "Sign in first.")
        if u.get("disabled"):
            raise HTTPException(403, "This account is paused. Contact support.")
        return u

    def need_admin(request: Request):
        u = need_user(request)
        if not is_admin(u):
            raise HTTPException(403, "Admins only.")
        return u

    def safe_next(n: str) -> str:
        return n if n.startswith("/") and not n.startswith("//") else "/app"

    # ---------------- public pages ----------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, reconnect: str = "", exclude: str = ""):
        if reconnect:   # a GPU node's page lost its node: start again on another one
            return RedirectResponse(f"/app?reconnect=1&exclude={quote(re.sub(r'[^0-9]', '', exclude))}", 303)
        return page("home.html", request)

    def static_page(tpl):
        def view(request: Request):
            return page(tpl, request)
        return view

    for path, tpl in (("/terms", "terms.html"), ("/privacy", "privacy.html"), ("/acceptable-use", "aup.html")):
        app.add_api_route(path, static_page(tpl), methods=["GET"], response_class=HTMLResponse)

    @app.get("/healthz")
    def healthz():
        return {"ok": bool(S.session_secret and S.google_client_id), "payments": rp.problem() or "ok",
                "test_mode": not rp.live}

    # ---------------- Google sign-in ----------------
    @app.get("/login")
    def login(next: str = "/app"):
        if not S.google_client_id:
            raise HTTPException(503, "Sign-in is not set up yet.")
        url, st = gg.start()
        r = RedirectResponse(url, 303)
        r.set_cookie(OAUTH_COOKIE, oauth_state.dumps({**st, "next": safe_next(next)}, 600), max_age=600,
                     httponly=True, secure=S.secure_cookies, samesite="lax", path="/auth")
        return r

    @app.get("/auth/callback")
    async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
        st = oauth_state.loads(request.cookies.get(OAUTH_COOKIE))
        if error or not st or not code or not secrets.compare_digest(st.get("state", ""), state):
            return RedirectResponse("/?signin=failed", 303)
        try:
            who = await gg.finish(code, st["nonce"])
        except ValueError:
            return RedirectResponse("/?signin=failed", 303)
        await run_in_threadpool(db.upsert_user, who["sub"], who["email"], who["name"])
        r = RedirectResponse(st.get("next") or "/app", 303)
        r.delete_cookie(OAUTH_COOKIE, path="/auth")
        r.set_cookie(COOKIE, cookies.dumps({"sub": who["sub"]}, COOKIE_DAYS * 86400), max_age=COOKIE_DAYS * 86400,
                     httponly=True, secure=S.secure_cookies, samesite="lax")
        return r

    @app.post("/logout")
    def logout():
        r = RedirectResponse("/", 303)
        r.delete_cookie(COOKIE)
        return r

    # ---------------- account ----------------
    @app.get("/app", response_class=HTMLResponse)
    def account(request: Request):
        u = current(request)
        if not u:
            return RedirectResponse("/login?next=/app", 303)
        return page("app.html", request, razorpay_key=rp.key_id, pay_problem=rp.problem())

    @app.get("/invite/{code}")
    def invite_link(request: Request, code: str):
        code = re.sub(r"[^A-Z0-9-]", "", code.upper())[:32]
        if not current(request):
            return RedirectResponse(f"/login?next={quote('/invite/' + code)}", 303)
        return RedirectResponse(f"/app?code={quote(code)}", 303)

    @app.get("/api/me")
    def me(request: Request):
        u = need_user(request)
        sessions = db.list_sessions(u["sub"], 20)
        ledger = db.list_ledger(u["sub"], 50)
        return {"email": u["email"], "name": u["name"], "balance_s": int(u["balance_s"]), "balance": hms(u["balance_s"]),
                "admin": is_admin(u), "packs": [{k: p[k] for k in ("id", "label", "hours", "price_inr")} for p in packs],
                "sessions": [{"started": s["started"], "used_s": int(s.get("charged_s", 0)), "used": hms(s.get("charged_s", 0))} for s in sessions],
                "ledger": [{"t": r["t"], "kind": r["kind"], "hours": round(r["seconds"] / 3600, 2), "note": r.get("note", "")}
                           for r in ledger if r["kind"] != "usage"],
                "pay_problem": rp.problem(), "test_mode": not rp.live}

    @app.post("/api/redeem")
    def redeem(request: Request, body: dict = Body(...)):
        u = need_user(request)
        code = re.sub(r"[^A-Z0-9-]", "", str(body.get("code", "")).upper())[:32]
        try:
            inv = db.redeem_invite(code, u["sub"])
        except storemod.StoreError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "message": f"Added {inv['hours']:g} hours.", "balance": hms(db.get_user(u["sub"])["balance_s"])}

    # ---------------- start a GPU session ----------------
    @app.post("/api/start")
    async def start(request: Request, body: dict = Body(default={})):
        u = await run_in_threadpool(need_user, request)
        exclude = {str(body["exclude"])} if body.get("exclude") else set()
        for e in exclude:
            pool.report_unreachable(e)
        bal = float(u.get("balance_s", 0))
        if bal <= 0:
            raise HTTPException(402, "You have no hours left. Buy a pack to continue.")
        try:
            await pool.refresh()
        except RuntimeError as e:
            raise HTTPException(503, "The video service is not reachable right now. Try again in a minute.") from e
        n = pool.assign(u["sub"], exclude)
        await pool.tick()
        if not n:
            booting = any(pool.state(x) == "booting" for x in pool.nodes.values())
            return {"wait": True, "text": "Starting GPU…" if booting else "Getting a GPU ready for you…"}
        now = int(time.time())
        sid = uuid.uuid4().hex
        exp = now + int(min(bal, S.max_session_hours * 3600))
        gpu_base = n.beat["tunnel"].rstrip("/")
        await run_in_threadpool(db.create_session, {"sid": sid, "sub": u["sub"], "node": n.id, "gpu_base": gpu_base,
                                                    "started": now, "exp": exp})
        token = webauth.jwt_encode({
            "iss": "magicpro-web", "aud": "magicpro-gpu", "sub": u["sub"], "email": u["email"], "name": u["name"],
            "role": "admin" if is_admin(u) else "user", "sid": sid, "iat": now, "exp": exp, "bal": int(bal),
            "usage_url": f"{S.site_url}/api/gpu/usage", "web_url": f"{S.site_url}/app"}, S.session_secret)
        return {"url": f"{gpu_base}/session?t={token}"}

    @app.post("/api/gpu/usage")
    async def usage(request: Request):
        raw = await request.body()
        if not S.session_secret or not secrets.compare_digest(webauth.sign_body(S.session_secret, raw),
                                                               request.headers.get("x-magic-signature", "")):
            raise HTTPException(401, "bad signature")
        try:
            rep = json.loads(raw)
            sid, sub, total = str(rep["sid"]), str(rep["sub"]), float(rep["total_seconds"])
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, "bad report") from e
        try:
            _, bal, u = await run_in_threadpool(db.charge_usage, sid, sub, total)
        except storemod.StoreError:
            return JSONResponse({"ok": False, "remaining": 0, "stop": True}, 404)
        stop = bal <= 0 or bool(u.get("disabled"))
        return {"ok": True, "remaining": int(bal), "stop": stop}

    # ---------------- payments ----------------
    @app.post("/api/pay/order")
    async def pay_order(request: Request, body: dict = Body(...)):
        u = await run_in_threadpool(need_user, request)
        pack = next((p for p in packs if p["id"] == body.get("pack")), None)
        if not pack:
            raise HTTPException(400, "Unknown pack.")
        if rp.problem():
            raise HTTPException(503, rp.problem())
        receipt = f"mp-{uuid.uuid4().hex[:16]}"
        try:
            o = await rp.create_order(pack["amount"], receipt, {"sub": u["sub"], "pack": pack["id"]})
        except RuntimeError as e:
            raise HTTPException(502, str(e)) from e
        await run_in_threadpool(db.create_order, {"order_id": o["id"], "sub": u["sub"], "email": u["email"], "pack": pack["id"],
                                                  "pack_label": pack["label"], "seconds": pack["hours"] * 3600,
                                                  "amount": pack["amount"], "receipt": receipt, "live": rp.live})
        return {"order_id": o["id"], "amount": pack["amount"], "currency": "INR", "key": rp.key_id,
                "name": S.brand, "description": f"{pack['label']} of live face swap", "email": u["email"], "user_name": u["name"]}

    @app.post("/api/pay/verify")
    def pay_verify(request: Request, body: dict = Body(...)):
        u = need_user(request)
        oid, pid, sig = (str(body.get(k, "")) for k in ("razorpay_order_id", "razorpay_payment_id", "razorpay_signature"))
        o = db.get_order(oid)
        if not o or o["sub"] != u["sub"]:
            raise HTTPException(404, "Unknown order.")
        if not rp.checkout_ok(oid, pid, sig):
            raise HTTPException(400, "The payment could not be verified. If money left your account, it will be credited once Razorpay confirms it.")
        db.pay_order(oid, pid)
        return {"ok": True, "balance": hms(db.get_user(u["sub"])["balance_s"])}

    @app.post("/api/pay/webhook")
    async def pay_webhook(request: Request):
        raw = await request.body()
        if not rp.webhook_ok(raw, request.headers.get("x-razorpay-signature", "")):
            raise HTTPException(400, "bad signature")
        ev = json.loads(raw or b"{}")
        if ev.get("event") not in ("payment.captured", "order.paid"):
            return {"ok": True, "ignored": ev.get("event")}
        pay = ((ev.get("payload") or {}).get("payment") or {}).get("entity") or {}
        oid = pay.get("order_id") or (((ev.get("payload") or {}).get("order") or {}).get("entity") or {}).get("id")
        if not oid or not await run_in_threadpool(db.get_order, oid):
            return {"ok": True, "ignored": "unknown order"}          # not ours: acknowledge so Razorpay stops retrying
        try:
            applied, _ = await run_in_threadpool(db.pay_order, oid, pay.get("id", ""), pay.get("amount"))
        except storemod.StoreError as e:
            return JSONResponse({"ok": False, "error": str(e)}, 400)
        return {"ok": True, "credited": applied}

    # ---------------- admin ----------------
    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        u = current(request)
        if not u:
            return RedirectResponse("/login?next=/admin", 303)
        if not is_admin(u):
            raise HTTPException(403, "Admins only.")
        return page("admin.html", request)

    @app.get("/api/admin/overview")
    def admin_overview(request: Request):
        need_admin(request)
        now = time.time()
        return {
            "users": [{"sub": x["sub"], "email": x["email"], "name": x["name"], "balance": hms(x["balance_s"]),
                       "balance_s": int(x["balance_s"]), "last_seen": x.get("last_seen"), "disabled": x.get("disabled", False)}
                      for x in db.list_users()],
            "invites": [{**{k: i[k] for k in ("code", "hours", "max_uses", "uses", "revoked", "expires")},
                         "active": not i["revoked"] and i["expires"] > now and i["uses"] < i["max_uses"],
                         "link": f"{S.site_url}/invite/{i['code']}"} for i in db.list_invites()],
            "orders": [{k: o.get(k) for k in ("order_id", "email", "pack_label", "amount", "status", "t", "live")} for o in db.list_orders(limit=50)],
            "test_mode": not rp.live, "pay_problem": rp.problem(),
        }

    @app.post("/api/admin/grant")
    def admin_grant(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        hours = float(body.get("hours", 0))
        if not 0 < abs(hours) <= 1000:
            raise HTTPException(400, "Hours must be between -1000 and 1000, not 0.")
        try:
            db.credit(str(body.get("sub")), hours * 3600, "grant", f"grant:{uuid.uuid4().hex}",
                      f"{'Granted' if hours > 0 else 'Removed'} by {a['email']}: {str(body.get('note', ''))[:120]}")
        except storemod.StoreError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    @app.post("/api/admin/user")
    def admin_user(request: Request, body: dict = Body(...)):
        need_admin(request)
        try:
            if body.get("delete"):
                db.delete_user(str(body["sub"]))
            else:
                db.set_disabled(str(body["sub"]), bool(body.get("disabled")))
        except (storemod.StoreError, KeyError) as e:
            raise HTTPException(404, "No such user.") from e
        return {"ok": True}

    @app.post("/api/admin/invite")
    def admin_invite(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        hours, valid, uses = float(body.get("hours", 3)), float(body.get("valid_hours", 72)), int(body.get("max_uses", 1))
        if not (0 < hours <= 100 and 0 < valid <= 24 * 365 and 0 < uses <= 1000):
            raise HTTPException(400, "Hours 0-100, valid for up to a year, 1-1000 uses.")
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        db.create_invite({"code": code, "hours": hours, "expires": time.time() + valid * 3600, "max_uses": uses, "created_by": a["email"]})
        return {"code": code, "link": f"{S.site_url}/invite/{code}"}

    @app.post("/api/admin/invite/revoke")
    def admin_revoke(request: Request, body: dict = Body(...)):
        need_admin(request)
        try:
            db.revoke_invite(str(body.get("code", "")))
        except storemod.StoreError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    # ---------------- GPU pool (admin) and nodes ----------------
    @app.get("/api/pool")
    async def pool_view(request: Request):
        await run_in_threadpool(need_admin, request)
        try:
            await pool.tick()
        except RuntimeError:
            pass
        return pool.summary()

    @app.post("/api/pool/stop-all")
    async def stop_all(request: Request, body: dict = Body(default={})):
        await run_in_threadpool(need_admin, request)
        return {"stopped": await pool.stop_all(force=bool(body.get("force")))}

    @app.post("/api/pool/adopt")
    async def adopt(request: Request, body: dict = Body(...)):
        await run_in_threadpool(need_admin, request)
        iid = str(body.get("instance_id", "")).strip()
        if not iid.isdigit():
            raise HTTPException(400, "instance_id must be a number")
        nonce = secrets.token_hex(8)
        await pool.vast.set_label(iid, f"{pool.cfg.label}:{nonce}")
        await pool.refresh(force=True)
        return {"instance_id": iid, "env": poolmod.env_lines(pool.cfg, nonce)}

    @app.post("/api/node/heartbeat")
    async def node_heartbeat(request: Request):
        raw = await request.body()
        h = request.headers
        try:
            await pool.refresh()
        except RuntimeError:
            pass
        n = pool.verify(h.get("x-node-id", ""), h.get("x-node-time", ""), h.get("x-node-sig", ""), raw)
        if n is None:
            await pool.refresh(force=True)   # a node we have not listed yet (just created / adopted)
            n = pool.verify(h.get("x-node-id", ""), h.get("x-node-time", ""), h.get("x-node-sig", ""), raw)
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
        import hashlib
        import hmac
        want = hmac.new(pool.cfg.secret.encode(), b"tick", hashlib.sha256).hexdigest() if pool.cfg.secret else ""
        if not want or not secrets.compare_digest(want, request.headers.get("x-tick-token", "")):
            raise HTTPException(403, "bad tick token")
        return {"actions": [list(a) for a in await pool.tick()]}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"detail": exc.detail}, exc.status_code)
        return HTMLResponse(jenv.get_template("error.html").render(S=S, user=None, code=exc.status_code, message=exc.detail),
                            exc.status_code)

    return app


app = create_app()
