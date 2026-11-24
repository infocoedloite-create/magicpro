"""Admin: users, packages, limits and overview, fully offline (fake Google, fake Razorpay, simulated Vast).
Run: python deploy/launcher/test_admin.py"""
import hashlib
import hmac
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import payments as paymod  # noqa: E402
import pool as P  # noqa: E402
import store as storemod  # noqa: E402
import webauth  # noqa: E402
from test_pool import FakeVast  # noqa: E402
from test_web import CLIENT_ID, SECRET, SITE, fg, fr, node_beat, sign_in, usage  # noqa: E402


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def build(clock=None):
    google = webauth.Google(CLIENT_ID, "gsecret", f"{SITE}/auth/callback", transport=httpx.MockTransport(fg.handler))
    rp = paymod.Razorpay("rzp_test_abc", "rsecret", "whsecret", mode="test", confirm="", transport=httpx.MockTransport(fr.handler))
    vast = FakeVast(time.time)
    vast.add(7, "running")
    pool = P.Pool(vast, P.Config(secret="poolsecret", image="", launcher_url=SITE), log=lambda *_: None)
    db = storemod.Store(storemod.MemoryBackend(), clock=clock or time.time)
    settings = main.Settings(site_url=SITE, session_secret=SECRET, google_client_id=CLIENT_ID, google_client_secret="gsecret",
                             admin_emails={"owner@example.com"})
    app = main.create_app(settings, db, pool, google, rp)
    c = TestClient(app, base_url=SITE)
    assert node_beat(c, 7, "n1", "https://n7.trycloudflare.com").status_code == 200
    return app, db, pool, vast


def clients(app):
    owner = sign_in(TestClient(app, base_url=SITE), "o1", "owner@example.com", "Owner")
    user = sign_in(TestClient(app, base_url=SITE), "u1", "user@example.com", "User")
    return owner, user


def start_claims(client):
    r = client.post("/api/start")
    assert r.status_code == 200 and "url" in r.json(), r.text
    return webauth.jwt_decode(r.json()["url"].split("t=", 1)[1], SECRET, aud="magicpro-gpu")


def free_node(pool):
    for n in pool.nodes.values():
        n.reserved.clear()
        n.suspect_until = 0


def test_blocked_user():
    app, db, pool, _ = build()
    owner, user = clients(app)
    db.credit("u1", 3600, "grant", "g1")
    sid = start_claims(user)["sid"]
    assert usage(user, sid, "u1", 30).json()["stop"] is False
    assert owner.post("/api/admin/user", json={"sub": "u1", "disabled": True}).status_code == 200
    assert usage(user, sid, "u1", 60).json()["stop"] is True          # live session ends at the next report
    assert user.post("/api/start").status_code == 403                  # and no new one
    assert user.get("/api/me").json()["blocked"] is True               # the account page still loads
    owner.post("/api/admin/user", json={"sub": "u1", "disabled": False})
    free_node(pool)
    start_claims(user)
    assert owner.post("/api/admin/user", json={"sub": "o1", "disabled": True}).status_code == 400   # not yourself
    actions = [a["action"] for a in owner.get("/api/admin/audit").json()["audit"]]
    assert actions.count("update_user") == 2


def test_daily_cap_and_session_cap():
    app, db, pool, _ = build()
    owner, user = clients(app)
    db.credit("u1", 10 * 3600, "grant", "g1")
    assert start_claims(user)["exp"] - int(time.time()) <= 4 * 3600   # default 4 h session cap
    free_node(pool)
    owner.post("/api/admin/limits", json={"max_session_hours": 1.5, "daily_hours": 2})
    c = start_claims(user)
    assert c["exp"] - c["iat"] == int(1.5 * 3600)                       # session cap
    r = usage(user, c["sid"], "u1", 1.5 * 3600).json()
    assert r["stop"] is False and r["remaining"] == 1800                # daily cap leaves 30 min
    free_node(pool)
    c2 = start_claims(user)
    assert c2["exp"] - c2["iat"] == 1800                                # token capped by what's left today
    r = usage(user, c2["sid"], "u1", 1800).json()
    assert r == {"ok": True, "remaining": 0, "stop": True}              # daily limit hit -> stop
    r = user.post("/api/start")
    assert r.status_code == 429 and "today" in r.json()["detail"]
    assert user.get("/api/me").json()["balance_s"] == 8 * 3600          # balance itself untouched beyond use
    # per-user override lifts the cap for this user only
    owner.post("/api/admin/user", json={"sub": "u1", "limits": {"daily_hours": 0, "max_session_hours": 3}})
    free_node(pool)
    c3 = start_claims(user)
    assert c3["exp"] - c3["iat"] == 3 * 3600
    prof = owner.get("/api/admin/user/u1").json()
    assert prof["limits"]["daily_hours"] == 0 and prof["overrides"]["max_session_hours"] == 3.0
    owner.post("/api/admin/user", json={"sub": "u1", "limits": {"daily_hours": None}})
    assert "daily_hours" not in owner.get("/api/admin/user/u1").json()["overrides"]


def test_daily_cap_resets_next_india_day():
    clock = Clock()
    clock.t = storemod.day_start() + 20 * 3600                          # 20:00 India time
    app, db, pool, _ = build(clock)
    owner, user = clients(app)
    db.credit("u1", 10 * 3600, "grant", "g1")
    owner.post("/api/admin/limits", json={"daily_hours": 1})
    sid = start_claims(user)["sid"]
    assert usage(user, sid, "u1", 3600).json()["stop"] is True
    clock.t += 5 * 3600                                                 # past midnight India time
    assert db.get_user("u1")["day_used_s"] == 0.0
    free_node(pool)
    start_claims(user)


def test_single_session():
    app, db, pool, _ = build()
    owner, user = clients(app)
    db.credit("u1", 3 * 3600, "grant", "g1")
    old = start_claims(user)["sid"]
    free_node(pool)
    new = start_claims(user)["sid"]
    assert usage(user, old, "u1", 10).json()["stop"] is True            # a new Start ends the old sid
    assert usage(user, new, "u1", 10).json()["stop"] is False
    owner.post("/api/admin/limits", json={"single_session": False})
    free_node(pool)
    third = start_claims(user)["sid"]
    assert usage(user, new, "u1", 20).json()["stop"] is False and usage(user, third, "u1", 5).json()["stop"] is False


def test_signups_closed_trial_and_invites():
    app, db, pool, _ = build()
    owner, _user = clients(app)
    owner.post("/api/admin/limits", json={"trial_minutes": 15, "signups_open": False})
    c = TestClient(app, base_url=SITE)
    r = c.get("/login?next=/app", follow_redirects=False)
    q = dict(x.split("=", 1) for x in r.headers["location"].split("?", 1)[1].split("&"))
    fg.pending_nonce, fg.who = q["nonce"], {"sub": "new1", "email": "new@example.com", "name": "New"}
    r = c.get(f"/auth/callback?code=x&state={q['state']}", follow_redirects=False)
    assert r.headers["location"] == "/?signin=closed" and db.get_user("new1") is None
    sign_in(TestClient(app, base_url=SITE), "u1", "user@example.com")   # existing users still get in
    inv = owner.post("/api/admin/invite", json={"hours": 2, "valid_hours": 24, "max_uses": 1}).json()
    c = TestClient(app, base_url=SITE)
    r = c.get(f"/invite/{inv['code']}", follow_redirects=False)
    r = c.get(r.headers["location"], follow_redirects=False)
    q = dict(x.split("=", 1) for x in r.headers["location"].split("?", 1)[1].split("&"))
    fg.pending_nonce, fg.who = q["nonce"], {"sub": "new2", "email": "invited@example.com", "name": "Invited"}
    r = c.get(f"/auth/callback?code=x&state={q['state']}", follow_redirects=False)
    assert r.headers["location"] == f"/invite/{inv['code']}"            # account made: they came with a code
    assert db.get_user("new2")["balance_s"] == 15 * 60                  # free trial
    c.post("/api/redeem", json={"code": inv["code"]})
    assert db.get_user("new2")["balance_s"] == 15 * 60 + 2 * 3600
    owner.post("/api/admin/limits", json={"signups_open": True, "trial_minutes": 0})
    sign_in(TestClient(app, base_url=SITE), "new3", "open@example.com")
    assert db.get_user("new3")["balance_s"] == 0


def test_packages_and_paid_orders():
    app, db, pool, _ = build()
    owner, user = clients(app)
    pk = {p["id"]: p for p in owner.get("/api/admin/packages").json()["packages"]}
    assert set(pk) == {"h3", "h5", "h10"}                               # seeded once from the defaults
    o = user.post("/api/pay/order", json={"pack": "h3"}).json()
    assert o["amount"] == 49900
    # admin changes the package after the order was made: hours, price, validity
    r = owner.post("/api/admin/packages", json={"id": "h3", "name": "Starter", "hours": 2, "price_inr": 999, "validity_days": 30, "popular": True})
    assert r.status_code == 200 and r.json()["name"] == "Starter"
    sig = hmac.new(b"rsecret", f"{o['order_id']}|pay_1".encode(), hashlib.sha256).hexdigest()
    user.post("/api/pay/verify", json={"razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_1", "razorpay_signature": sig})
    assert db.get_user("u1")["balance_s"] == 3 * 3600 and not db.get_user("u1")["lots"]   # as bought: 3 h, no expiry
    assert db.get_order(o["order_id"])["amount"] == 49900
    o2 = user.post("/api/pay/order", json={"pack": "h3"}).json()
    assert o2["amount"] == 99900                                        # new orders use the new price
    home = TestClient(app, base_url=SITE).get("/").text
    assert "Starter" in home and "₹999" in home and "Most bought" in home and "use within 30 days" in home
    # disable: off the home page and not buyable
    owner.post("/api/admin/packages", json={"id": "h10", "enabled": False})
    assert "10 hours" not in TestClient(app, base_url=SITE).get("/").text
    assert user.post("/api/pay/order", json={"pack": "h10"}).status_code == 400
    # create and reorder
    new = owner.post("/api/admin/packages", json={"name": "Day pass", "hours": 1, "price_inr": 199, "enabled": True}).json()
    ids = [new["id"], "h5", "h3", "h10"]
    owner.post("/api/admin/packages/order", json={"ids": ids})
    assert [p["id"] for p in owner.get("/api/admin/packages").json()["packages"]] == ids
    assert user.get("/api/me").json()["packs"][0]["name"] == "Day pass"
    assert owner.post("/api/admin/packages", json={"name": "", "hours": 1, "price_inr": 1}).status_code == 400
    assert owner.post("/api/admin/packages", json={"name": "x"}).status_code == 400          # missing hours/price
    assert user.post("/api/admin/packages", json={"name": "hack", "hours": 9, "price_inr": 1}).status_code == 403


def test_package_validity_expires():
    clock = Clock()
    app, db, pool, _ = build(clock)
    owner, user = clients(app)
    owner.post("/api/admin/packages", json={"id": "h3", "validity_days": 7})
    o = user.post("/api/pay/order", json={"pack": "h3"}).json()
    sig = hmac.new(b"rsecret", f"{o['order_id']}|p".encode(), hashlib.sha256).hexdigest()
    user.post("/api/pay/verify", json={"razorpay_order_id": o["order_id"], "razorpay_payment_id": "p", "razorpay_signature": sig})
    db.credit("u1", 3600, "grant", "g-forever")                         # 1 h that never expires
    assert db.get_user("u1")["balance_s"] == 4 * 3600
    sid = start_claims(user)["sid"]
    usage(user, sid, "u1", 1800)                                         # usage spends expiring hours first
    clock.t += 8 * 86400
    assert db.get_user("u1")["balance_s"] == 3600                       # 2.5 h expired, the grant stays


def test_admin_roles_and_overview():
    app, db, pool, vast = build()
    owner, user = clients(app)
    assert user.get("/api/admin/users").status_code == 403
    owner.post("/api/admin/user", json={"sub": "u1", "admin": True})
    assert user.get("/api/admin/users").status_code == 200
    db.credit("o1", 600, "grant", "g0")
    free_node(pool)
    assert start_claims(user if db.get_user("u1")["balance_s"] else owner)["role"] == "admin"
    assert user.post("/api/admin/user", json={"sub": "o1", "admin": False}).status_code == 400    # owners stay
    assert user.post("/api/admin/user", json={"sub": "u1", "admin": False}).status_code == 400    # not yourself
    owner.post("/api/admin/user", json={"sub": "u1", "admin": False})
    assert user.get("/api/admin/users").status_code == 403
    found = owner.get("/api/admin/users?search=USER@").json()["users"]
    assert [u["email"] for u in found] == ["user@example.com"]
    # grants need a note; negative revokes
    assert owner.post("/api/admin/grant", json={"sub": "u1", "hours": 2}).status_code == 400
    owner.post("/api/admin/grant", json={"sub": "u1", "hours": 2, "note": "welcome"})
    owner.post("/api/admin/grant", json={"sub": "u1", "hours": -0.5, "note": "correction"})
    assert db.get_user("u1")["balance_s"] == 1.5 * 3600
    prof = owner.get("/api/admin/user/u1").json()
    assert [a["action"] for a in prof["audit"]][:2] == ["revoke_hours", "grant"]
    # overview: sold, used, online, GPU cost
    o = user.post("/api/pay/order", json={"pack": "h5"}).json()
    sig = hmac.new(b"rsecret", f"{o['order_id']}|p5".encode(), hashlib.sha256).hexdigest()
    user.post("/api/pay/verify", json={"razorpay_order_id": o["order_id"], "razorpay_payment_id": "p5", "razorpay_signature": sig})
    free_node(pool)
    sid = start_claims(user)["sid"]
    usage(user, sid, "u1", 900)
    pool.account_cost(); pool._cost_t -= 100; pool.account_cost()      # 100 s of one running GPU at $1/h
    ov = owner.get("/api/admin/overview").json()
    assert ov["online_users"] == 1 and ov["gpus_running"] == 1 and ov["test_mode"] is True
    assert ov["hours_sold_today"] == 5 and ov["hours_used_today"] == 0.25 and ov["revenue_test_today"] == 799
    assert ov["revenue_live_today"] == 0 and abs(ov["gpu_usd_today"] - round(100 / 3600, 2)) < 0.011


def test_pool_limits_reach_pool_and_nodes():
    app, db, pool, vast = build()
    owner, _ = clients(app)
    owner.post("/api/admin/limits", json={"max_gpus": 2, "idle_grace_minutes": 3})
    assert pool.cfg.max_nodes == 2
    r = node_beat(TestClient(app, base_url=SITE), 7, "n1", "https://n7.trycloudflare.com").json()
    assert r["grace_s"] == 180                                          # handed to the node's watchdog
    assert owner.post("/api/admin/limits", json={"max_gpus": 999}).status_code == 400
    assert owner.post("/api/admin/limits", json={"daily_hours": -1}).status_code == 400
    assert owner.get("/api/admin/limits").json()["limits"]["max_gpus"] == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("admin: all ok")
