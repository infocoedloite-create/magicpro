"""Magic Pro website (Cloud Run, https://magicpro.nacgenai.in) in front of the Vast.ai GPU pool.

Public pages, Google sign-in, hour balances, Razorpay hour packages, invite codes, an admin area
(users, packages, limits, overview), and the GPU handoff of docs/handoff-contract.md: Start mints
an HS256 session token and sends the browser to {gpu_base}/session?t=...; GPU nodes report usage to
/api/gpu/usage and the balance is charged by the increase of total_seconds per session. The website
itself runs on Cloud Run all the time (scaled to zero between visits); a GPU runs only while someone
streams. The pool autoscaler is pool.py.

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
ONLINE_WINDOW = 90          # a session that reported streaming within this many seconds is "online"


@dataclass
class Settings:
    site_url: str = os.environ.get("SITE_URL", "https://magicpro.nacgenai.in").rstrip("/")
    session_secret: str = os.environ.get("MAGIC_SESSION_SECRET", "")
    google_client_id: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    # owners: always admins, can't be demoted from the admin page
    admin_emails: set = field(default_factory=lambda: {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()})
    support_email: str = os.environ.get("SUPPORT_EMAIL", "")
    brand: str = os.environ.get("BRAND_NAME", "Magic Pro")

    @property
    def secure_cookies(self):
        return self.site_url.startswith("https://")


def make_store():
    if os.environ.get("STORE", "memory") == "firestore":
        return storemod.Store(storemod.FirestoreBackend(project=os.environ.get("FIRESTORE_PROJECT") or None,
                                                        database=os.environ.get("FIRESTORE_DATABASE", "(default)")))
    return storemod.MemoryStore()


def hms(seconds) -> str:
    s = max(0, int(seconds or 0))
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def pack_view(p):
    return {"id": p["id"], "name": p["name"], "hours": p["hours"], "price_inr": p["price_inr"],
            "popular": bool(p.get("popular")), "validity_days": p.get("validity_days")}


def create_app(settings=None, store=None, pool=None, google=None, razorpay=None) -> FastAPI:
    S = settings or Settings()
    db = store or make_store()
    if pool is None:
        pool = poolmod.Pool(poolmod.Vast(os.environ.get("VAST_API_KEY", ""), os.environ.get("VAST_URL", "https://console.vast.ai")))
    pool.cfg.launcher_url = pool.cfg.launcher_url or S.site_url
    pool.cfg.session_secret = pool.cfg.session_secret or S.session_secret
    pool.cost_cb = lambda usd, secs: db.add_gpu_cost(usd, secs)
    gg = google or webauth.Google(S.google_client_id, S.google_client_secret, f"{S.site_url}/auth/callback")
    rp = razorpay or paymod.Razorpay(os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", ""),
                                     os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""))
    db.seed_packages(paymod.load_packs())          # first run only: PACKS env (or the placeholders) become packages
    cookies = webauth.Signer(S.session_secret or "dev-only-secret", "cookie")
    oauth_state = webauth.Signer(S.session_secret or "dev-only-secret", "oauth")
    jenv = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    jenv.filters["hms"] = hms
    jenv.filters["inr"] = lambda p: f"₹{float(p):,.0f}"
    jenv.filters["hours"] = lambda h: f"{float(h):g} hour{'' if float(h) == 1 else 's'}"

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
        packs = db.list_packages(enabled_only=True)
        html = jenv.get_template(name).render(S=S, user=user, is_admin=is_admin(user), packs=packs,
                                              min_price=min((p["price_inr"] for p in packs), default=0),
                                              test_mode=not rp.live, **ctx)
        return HTMLResponse(html)

    def apply_pool_limits(lim):
        pool.cfg.max_nodes = int(lim["max_gpus"])
        pool.cfg.max_instances = max(int(lim["max_gpus"]), 0)
        pool.grace_s = float(lim["idle_grace_minutes"]) * 60
        pool.paused = bool(lim.get("maintenance"))

    apply_pool_limits(db.get_limits())

    # ---------------- who is signed in ----------------
    def current(request: Request):
        c = cookies.loads(request.cookies.get(COOKIE))
        return db.get_user(c["sub"]) if c else None

    def is_owner(user) -> bool:
        return bool(user and user.get("email", "").lower() in S.admin_emails)

    def is_admin(user) -> bool:
        return bool(user and (is_owner(user) or user.get("admin")))

    def need_user(request: Request, allow_blocked=False):
        u = current(request)
        if not u:
            raise HTTPException(401, "Sign in first.")
        if u.get("disabled") and not allow_blocked:
            raise HTTPException(403, "This account is blocked. Contact support.")
        return u

    def need_admin(request: Request):
        u = need_user(request)
        if not is_admin(u):
            raise HTTPException(403, "Admins only.")
        return u

    def safe_next(n: str) -> str:
        return n if n.startswith("/") and not n.startswith("//") else "/app"

    def day_left(u, lim):
        cap = float(lim.get("daily_hours") or 0) * 3600
        return None if cap <= 0 else max(0.0, cap - float(u.get("day_used_s", 0)))

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
        nxt = st.get("next") or "/app"
        lim = await run_in_threadpool(db.get_limits)
        # signups closed: only existing users, owners, and people arriving with a usable invite code
        invite = nxt[len("/invite/"):] if nxt.startswith("/invite/") else ""
        allow_new = (lim["signups_open"] or who["email"].lower() in S.admin_emails
                     or bool(invite and await run_in_threadpool(db.invite_usable, invite)))
        try:
            await run_in_threadpool(db.sign_in, who["sub"], who["email"], who["name"], allow_new,
                                    float(lim["trial_minutes"]) * 60)
        except storemod.StoreError:
            return RedirectResponse("/?signin=closed", 303)
        r = RedirectResponse(nxt, 303)
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
        u = need_user(request, allow_blocked=True)
        lim = db.user_limits(u)
        left = day_left(u, lim)
        sessions = db.list_sessions(u["sub"], limit=20)
        ledger = db.list_ledger(u["sub"], 50)
        return {"email": u["email"], "name": u["name"], "balance_s": int(u["balance_s"]), "balance": hms(u["balance_s"]),
                "blocked": bool(u.get("disabled")), "admin": is_admin(u),
                "daily_left": None if left is None else hms(left),
                "expiring": [{"hours": round(x["left"] / 3600, 2), "expires": x["exp"]} for x in u.get("lots", [])],
                "packs": [pack_view(p) for p in db.list_packages(enabled_only=True)],
                "sessions": [{"started": s["started"], "used_s": int(s.get("charged_s", 0)), "used": hms(s.get("charged_s", 0))} for s in sessions],
                "ledger": [{"t": r["t"], "kind": r["kind"], "hours": round(r["seconds"] / 3600, 2), "note": r.get("note", "")}
                           for r in ledger],
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
        u = await run_in_threadpool(need_user, request)          # blocked users get 403 here
        lim = await run_in_threadpool(db.user_limits, u)
        apply_pool_limits(await run_in_threadpool(db.get_limits))
        if lim.get("maintenance"):
            raise HTTPException(503, "Service is paused, try again later.")
        if body.get("reconnect") and u.get("active_sid"):
            # the page lost its GPU: if an admin turned that GPU off, don't start one again by itself
            last = await run_in_threadpool(db.get_session, u["active_sid"])
            if last and last.get("ended_by") == "admin":
                raise HTTPException(409, "Your session was ended because the GPU was turned off. Press Start the studio to begin again.")
        bal = float(u.get("balance_s", 0))
        if bal <= 0:
            raise HTTPException(402, "You have no hours left. Buy a package to continue.")
        left_today = day_left(u, lim)
        if left_today is not None and left_today <= 0:
            raise HTTPException(429, f"You've reached today's limit of {float(lim['daily_hours']):g} hours. It resets at midnight (India time).")
        exclude = {str(body["exclude"])} if body.get("exclude") else set()
        for e in exclude:
            pool.report_unreachable(e)
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
        caps = [bal, float(lim["max_session_hours"]) * 3600] + ([left_today] if left_today is not None else [])
        exp = now + max(1, int(min(caps)))
        gpu_base = n.beat["tunnel"].rstrip("/")
        await run_in_threadpool(db.create_session, {"sid": sid, "sub": u["sub"], "node": n.id, "gpu_base": gpu_base,
                                                    "started": now, "exp": exp}, bool(lim["single_session"]))
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
            _, u, sess = await run_in_threadpool(db.charge_usage, sid, sub, total, bool(rep.get("streaming", True)))
        except storemod.StoreError:
            return JSONResponse({"ok": False, "remaining": 0, "stop": True}, 404)
        lim = await run_in_threadpool(db.user_limits, u)
        remaining = float(u["balance_s"])
        left = day_left(u, lim)
        if left is not None:
            remaining = min(remaining, left)
        superseded = bool(lim["single_session"]) and u.get("active_sid") not in (None, sid)
        # reason is informational for the GPU page: ended (GPU turned off by an admin), blocked,
        # replaced (a newer Start), daily_limit, balance
        reason = ("ended" if sess.get("ended") else "blocked" if u.get("disabled") else "replaced" if superseded
                  else "balance" if float(u["balance_s"]) <= 0 else "daily_limit" if remaining <= 0 else "")
        return {"ok": True, "remaining": int(remaining), "stop": bool(reason), **({"reason": reason} if reason else {})}

    # ---------------- payments ----------------
    @app.post("/api/pay/order")
    async def pay_order(request: Request, body: dict = Body(...)):
        u = await run_in_threadpool(need_user, request)
        packs = await run_in_threadpool(db.list_packages, True)
        pack = next((p for p in packs if p["id"] == body.get("pack")), None)
        if not pack:
            raise HTTPException(400, "That package is not on sale.")
        if rp.problem():
            raise HTTPException(503, rp.problem())
        amount = int(round(float(pack["price_inr"]) * 100))
        receipt = f"mp-{uuid.uuid4().hex[:16]}"
        try:
            o = await rp.create_order(amount, receipt, {"sub": u["sub"], "pack": pack["id"]})
        except RuntimeError as e:
            raise HTTPException(502, str(e)) from e
        # the order keeps its own copy of what was bought: later package edits never change it
        await run_in_threadpool(db.create_order, {"order_id": o["id"], "sub": u["sub"], "email": u["email"], "pack": pack["id"],
                                                  "pack_label": pack["name"], "seconds": float(pack["hours"]) * 3600,
                                                  "validity_days": pack.get("validity_days"), "amount": amount,
                                                  "receipt": receipt, "live": rp.live})
        return {"order_id": o["id"], "amount": amount, "currency": "INR", "key": rp.key_id,
                "name": S.brand, "description": f"{pack['name']} of live face swap", "email": u["email"], "user_name": u["name"]}

    @app.post("/api/pay/verify")
    def pay_verify(request: Request, body: dict = Body(...)):
        u = need_user(request, allow_blocked=True)
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

    def user_row(x):
        return {"sub": x["sub"], "email": x["email"], "name": x["name"], "balance": hms(x["balance_s"]),
                "balance_s": int(x["balance_s"]), "used": hms(x.get("used_total_s", 0)), "created": x.get("created"),
                "last_seen": x.get("last_seen"), "blocked": bool(x.get("disabled")), "admin": is_admin(x), "owner": is_owner(x)}

    @app.get("/api/admin/overview")
    async def admin_overview(request: Request):
        await run_in_threadpool(need_admin, request)
        now = time.time()
        today = storemod.day_key(now)
        month = [storemod.day_key(t) for t in range(int(storemod.day_start(now)), int(now) - 32 * 86400, -86400)
                 if storemod.day_key(t)[:7] == today[:7]]
        stats = await run_in_threadpool(db.stats_days, month)
        t_stat, m = stats[0], {}
        for st in stats:
            for k, val in st.items():
                if k != "day":
                    m[k] = m.get(k, 0) + val
        online = [s for s in await run_in_threadpool(db.list_sessions, None, now - ONLINE_WINDOW, 500) if s.get("streaming")]
        try:
            await pool.tick()
        except RuntimeError:
            pass
        ps = pool.summary()
        h = lambda s: round(float(s or 0) / 3600, 2)  # noqa: E731
        return {
            "online_users": len({s["sub"] for s in online}),
            "gpus_running": len([x for x in ps["nodes"] if x["state"] in ("ready", "booting", "unhealthy", "stopping")]),
            "gpu_usd_per_hour": ps["billing_per_hour"],
            "gpu_usd_today": round(t_stat.get("gpu_usd", 0), 2), "gpu_usd_month": round(m.get("gpu_usd", 0), 2),
            "hours_sold_today": h(t_stat.get("sold_s")), "hours_sold_month": h(m.get("sold_s")),
            "hours_used_today": h(t_stat.get("used_s")), "hours_used_month": h(m.get("used_s")),
            "revenue_test_today": t_stat.get("revenue_test_paise", 0) / 100, "revenue_test_month": m.get("revenue_test_paise", 0) / 100,
            "revenue_live_today": t_stat.get("revenue_live_paise", 0) / 100, "revenue_live_month": m.get("revenue_live_paise", 0) / 100,
            "test_mode": not rp.live, "pay_problem": rp.problem(), "pool_note": ps["note"],
        }

    @app.get("/api/admin/users")
    def admin_users(request: Request, search: str = ""):
        need_admin(request)
        return {"users": [user_row(x) for x in db.list_users(search)]}

    @app.get("/api/admin/user/{sub}")
    def admin_user_profile(request: Request, sub: str):
        need_admin(request)
        u = db.get_user(sub)
        if not u:
            raise HTTPException(404, "No such user.")
        return {**user_row(u), "limits": db.user_limits(u), "overrides": u.get("limits") or {},
                "day_used": hms(u.get("day_used_s", 0)),
                "expiring": [{"hours": round(x["left"] / 3600, 2), "expires": x["exp"]} for x in u.get("lots", [])],
                "ledger": db.list_ledger(sub, 100), "orders": db.list_orders(sub, 50),
                "sessions": [{"started": s["started"], "used": hms(s.get("charged_s", 0)), "node": s.get("node"),
                              "live": s.get("streaming") and (s.get("last_report") or 0) > time.time() - ONLINE_WINDOW}
                             for s in db.list_sessions(sub, limit=50)],
                "audit": db.list_audit(sub, 50)}

    @app.post("/api/admin/grant")
    def admin_grant(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        try:
            hours = float(body.get("hours", 0))
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "Hours must be a number.") from e
        note = str(body.get("note", "")).strip()[:200]
        if not 0 < abs(hours) <= 1000:
            raise HTTPException(400, "Hours must be between -1000 and 1000, not 0.")
        if not note:
            raise HTTPException(400, "Write a note: it goes into the audit log.")
        sub = str(body.get("sub"))
        try:
            db.credit(sub, hours * 3600, "grant", f"grant:{uuid.uuid4().hex}", f"{'Added' if hours > 0 else 'Removed'} by {a['email']}: {note}")
        except storemod.StoreError as e:
            raise HTTPException(404, str(e)) from e
        db.audit(a["email"], "grant" if hours > 0 else "revoke_hours", sub, f"{hours:+g} h: {note}")
        return {"ok": True}

    @app.post("/api/admin/user")
    def admin_user(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        sub = str(body.get("sub", ""))
        target = db.get_user(sub)
        if not target:
            raise HTTPException(404, "No such user.")
        if body.get("delete"):
            if is_owner(target) or sub == a["sub"]:
                raise HTTPException(400, "Owners and your own account can't be deleted here.")
            db.delete_user(sub)
            db.audit(a["email"], "delete_user", sub, target["email"])
            return {"ok": True}
        patch = {k: body[k] for k in ("disabled", "admin", "limits") if k in body}
        if sub == a["sub"] and ("disabled" in patch or patch.get("admin") is False):
            raise HTTPException(400, "You can't block yourself or remove your own admin role.")
        if is_owner(target) and ("disabled" in patch or patch.get("admin") is False):
            raise HTTPException(400, "Owners (ADMIN_EMAILS) can't be blocked or demoted here.")
        try:
            db.set_user(sub, patch)
        except storemod.StoreError as e:
            raise HTTPException(400, str(e)) from e
        db.audit(a["email"], "update_user", sub, json.dumps(patch))
        return {"ok": True}

    @app.get("/api/admin/packages")
    def admin_packages(request: Request):
        need_admin(request)
        return {"packages": db.list_packages()}

    @app.post("/api/admin/packages")
    def admin_save_package(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        try:
            p = db.save_package(body)
        except storemod.StoreError as e:
            raise HTTPException(400, str(e)) from e
        db.audit(a["email"], "save_package", p["id"], json.dumps({k: p.get(k) for k in ("name", "hours", "price_inr", "enabled", "popular", "validity_days")}))
        return p

    @app.post("/api/admin/packages/order")
    def admin_order_packages(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        db.reorder_packages([str(i) for i in body.get("ids", [])])
        db.audit(a["email"], "reorder_packages", "", ",".join(map(str, body.get("ids", []))))
        return {"packages": db.list_packages()}

    @app.get("/api/admin/limits")
    def admin_limits(request: Request):
        need_admin(request)
        return {"limits": db.get_limits(), "defaults": storemod.DEFAULT_LIMITS}

    @app.post("/api/admin/limits")
    def admin_set_limits(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        try:
            lim = db.set_limits(body)
        except storemod.StoreError as e:
            raise HTTPException(400, str(e)) from e
        apply_pool_limits(lim)
        db.audit(a["email"], "set_limits", "", json.dumps(body))
        return {"limits": lim}

    @app.get("/api/admin/audit")
    def admin_audit(request: Request):
        need_admin(request)
        return {"audit": db.list_audit(limit=200)}

    @app.get("/api/admin/invites")
    def admin_invites(request: Request):
        need_admin(request)
        now = time.time()
        return {"invites": [{**{k: i[k] for k in ("code", "hours", "max_uses", "uses", "revoked", "expires")},
                             "active": not i["revoked"] and i["expires"] > now and i["uses"] < i["max_uses"],
                             "link": f"{S.site_url}/invite/{i['code']}"} for i in db.list_invites()],
                "orders": [{k: o.get(k) for k in ("order_id", "email", "pack_label", "amount", "status", "t", "live")}
                           for o in db.list_orders(limit=50)]}

    @app.post("/api/admin/invite")
    def admin_invite(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        hours, valid, uses = float(body.get("hours", 3)), float(body.get("valid_hours", 72)), int(body.get("max_uses", 1))
        if not (0 < hours <= 100 and 0 < valid <= 24 * 365 and 0 < uses <= 1000):
            raise HTTPException(400, "Hours 0-100, valid for up to a year, 1-1000 uses.")
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        db.create_invite({"code": code, "hours": hours, "expires": time.time() + valid * 3600, "max_uses": uses, "created_by": a["email"]})
        db.audit(a["email"], "create_invite", code, f"{hours:g} h, {uses} uses, {valid:g} h to redeem")
        return {"code": code, "link": f"{S.site_url}/invite/{code}"}

    @app.post("/api/admin/invite/revoke")
    def admin_revoke(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        try:
            db.revoke_invite(str(body.get("code", "")))
        except storemod.StoreError as e:
            raise HTTPException(404, str(e)) from e
        db.audit(a["email"], "revoke_invite", str(body.get("code", "")))
        return {"ok": True}

    # ---------------- GPU pool (admin) and nodes ----------------
    def streaming_by_node():
        out = {}
        for x in db.list_sessions(None, time.time() - ONLINE_WINDOW, 1000):
            if x.get("streaming") and not x.get("ended"):
                out.setdefault(str(x.get("node")), set()).add(x["sub"])
        return out

    @app.get("/api/pool")
    async def pool_view(request: Request):
        await run_in_threadpool(need_admin, request)
        try:
            await pool.tick()
        except RuntimeError:
            pass
        s = pool.summary()
        live = await run_in_threadpool(streaming_by_node)
        for n in s["nodes"]:
            n["users"] = max(int(n["users"] or 0), len(live.get(n["id"], ())))
        return s

    @app.post("/api/admin/gpu/off")
    async def gpu_off(request: Request, body: dict = Body(...)):
        """Turn one GPU (id) or all GPUs (all: true) off now. With people streaming, the first call
        answers 409 with how many; resend with confirm: true. Their sessions end and stop being charged."""
        a = await run_in_threadpool(need_admin, request)
        try:
            await pool.refresh(force=True)
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from e
        ids = [i for i, n in pool.nodes.items() if n.inst.get("intended_status") != "stopped"] if body.get("all") else [str(body.get("id", ""))]
        if not body.get("all") and ids[0] not in pool.nodes:
            raise HTTPException(404, "No such GPU.")
        live = await run_in_threadpool(streaming_by_node)
        affected = sorted({sub for i in ids for sub in live.get(i, ())})
        if affected and not body.get("confirm"):
            return JSONResponse({"detail": f"{len(affected)} {'person is' if len(affected) == 1 else 'people are'} streaming and will be cut off.",
                                 "users": len(affected), "needs_confirm": True}, 409)
        done = []
        for i in ids:
            try:
                await pool.turn_off(i)
            except RuntimeError as e:
                raise HTTPException(502, str(e)) from e
            await run_in_threadpool(db.end_sessions, i, "admin", ONLINE_WINDOW)
            done.append(i)
        await run_in_threadpool(db.audit, a["email"], "gpu_off_all" if body.get("all") else "gpu_off", ",".join(done),
                                f"{len(affected)} streaming user(s) cut off")
        return {"ok": True, "stopped": done, "users_cut_off": len(affected)}

    @app.post("/api/admin/gpu/on")
    async def gpu_on(request: Request, body: dict = Body(...)):
        a = await run_in_threadpool(need_admin, request)
        try:
            await pool.refresh(force=True)
            await pool.turn_on(str(body.get("id", "")))
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e
        await run_in_threadpool(db.audit, a["email"], "gpu_on", str(body.get("id", "")), "turned on by hand")
        return {"ok": True}

    @app.post("/api/pool/stop-all")
    async def stop_all(request: Request, body: dict = Body(default={})):
        a = await run_in_threadpool(need_admin, request)
        done = await pool.stop_all(force=bool(body.get("force")))
        await run_in_threadpool(db.audit, a["email"], "stop_gpus", ",".join(done))
        return {"stopped": done}

    @app.post("/api/pool/adopt")
    async def adopt(request: Request, body: dict = Body(...)):
        a = await run_in_threadpool(need_admin, request)
        iid = str(body.get("instance_id", "")).strip()
        if not iid.isdigit():
            raise HTTPException(400, "instance_id must be a number")
        nonce = secrets.token_hex(8)
        await pool.vast.set_label(iid, f"{pool.cfg.label}:{nonce}")
        await pool.refresh(force=True)
        await run_in_threadpool(db.audit, a["email"], "adopt_gpu", iid)
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
        apply_pool_limits(await run_in_threadpool(db.get_limits))
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
