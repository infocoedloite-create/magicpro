"""Website tests, fully offline: fake Google token endpoint, fake Razorpay API, simulated Vast pool.
Run: python deploy/launcher/test_web.py"""
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import payments as paymod  # noqa: E402
import pool as P  # noqa: E402
import store as storemod  # noqa: E402
import webauth  # noqa: E402
from test_pool import FakeVast  # noqa: E402

SECRET = "test-session-secret-0123456789abcdef"
SITE = "https://testserver"
CLIENT_ID = "cid.apps.googleusercontent.com"


# ---------------- fakes ----------------
class FakeGoogle:
    """Answers the token endpoint with an ID token for whoever `who` is set to."""

    def __init__(self):
        self.who = None
        self.nonce_override = None
        self.aud = CLIENT_ID

    def handler(self, request: httpx.Request):
        form = parse_qs(request.content.decode())
        assert form["client_id"] == [CLIENT_ID] and form["grant_type"] == ["authorization_code"]
        claims = {"iss": "https://accounts.google.com", "aud": self.aud, "exp": time.time() + 600, "email_verified": True,
                  "nonce": self.nonce_override or self.pending_nonce, **self.who}
        body = webauth.b64u(json.dumps(claims).encode())
        return httpx.Response(200, json={"id_token": f"e30.{body}.sig", "access_token": "x"})


class FakeRazorpay:
    def __init__(self):
        self.orders = []

    def handler(self, request: httpx.Request):
        assert request.url.path.endswith("/orders") and request.headers["authorization"].startswith("Basic ")
        body = json.loads(request.content)
        oid = f"order_{len(self.orders) + 1:04d}"
        self.orders.append({"id": oid, **body})
        return httpx.Response(200, json={"id": oid, "amount": body["amount"], "currency": "INR", "status": "created"})


fg, fr = FakeGoogle(), FakeRazorpay()


def build(key_id="rzp_test_abc", mode="test", confirm=""):
    google = webauth.Google(CLIENT_ID, "gsecret", f"{SITE}/auth/callback", transport=httpx.MockTransport(fg.handler))
    rp = paymod.Razorpay(key_id, "rsecret", "whsecret", mode=mode, confirm=confirm, transport=httpx.MockTransport(fr.handler))
    vast = FakeVast(time.time)
    pool = P.Pool(vast, P.Config(secret="poolsecret", image="", launcher_url=SITE), log=lambda *_: None)
    settings = main.Settings(site_url=SITE, session_secret=SECRET, google_client_id=CLIENT_ID, google_client_secret="gsecret",
                             admin_emails={"admin@example.com"})
    app = main.create_app(settings, storemod.MemoryStore(), pool, google, rp)
    return app, vast, pool


def sign_in(client, sub, email, name="Test User"):
    r = client.get("/login?next=/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == [f"{SITE}/auth/callback"] and "openid" in q["scope"][0]
    fg.pending_nonce, fg.who = q["nonce"][0], {"sub": sub, "email": email, "name": name}
    r = client.get(f"/auth/callback?code=abc&state={q['state'][0]}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app", (r.status_code, r.headers.get("location"))
    return client


def usage(client, sid, sub, total, secret=SECRET):
    raw = json.dumps({"sid": sid, "sub": sub, "total_seconds": total, "streaming": True, "ts": int(time.time())}).encode()
    return client.post("/api/gpu/usage", content=raw, headers={"Content-Type": "application/json",
                                                               "X-Magic-Signature": webauth.sign_body(secret, raw)})


def node_beat(client, iid, nonce, tunnel):
    body = json.dumps({"tunnel": tunnel, "users": 0, "load": 0, "budget": 4, "stream_cost": 1.0, "healthy": True}).encode()
    ts = str(int(time.time()))
    sig = P.sign(P.node_key("poolsecret", nonce), str(iid), ts, body)
    return client.post("/api/node/heartbeat", content=body, headers={"X-Node-Id": str(iid), "X-Node-Time": ts, "X-Node-Sig": sig})


# ---------------- tests ----------------
def test_public_pages():
    app, _, _ = build()
    c = TestClient(app, base_url=SITE)
    home = c.get("/")
    assert home.status_code == 200 and "Live face swap for streamers and video calls" in home.text
    assert "₹499" in home.text and "Hour packs" in home.text
    text = home.text.lower()
    for word in ("gpu", "vast", "onnx", "inswapper", "gfpgan", "nvidia", "cuda", "model"):
        assert word not in text, f"home page mentions {word}"
    for path in ("/terms", "/privacy", "/acceptable-use"):
        r = c.get(path)
        assert r.status_code == 200 and "Draft for review by a lawyer" in r.text, path
    assert "written consent" in c.get("/acceptable-use").text
    assert c.get("/app", follow_redirects=False).headers["location"] == "/login?next=/app"
    assert c.get("/api/me").status_code == 401
    assert c.get("/static/site.css").status_code == 200


def test_signin_rejects_bad_tokens():
    app, _, _ = build()
    c = TestClient(app, base_url=SITE)
    r = c.get("/login", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    fg.pending_nonce, fg.who = q["nonce"][0], {"sub": "1", "email": "a@example.com", "name": "A"}
    assert c.get("/auth/callback?code=x&state=WRONG", follow_redirects=False).headers["location"] == "/?signin=failed"
    fg.nonce_override = "replayed"
    assert c.get(f"/auth/callback?code=x&state={q['state'][0]}", follow_redirects=False).headers["location"] == "/?signin=failed"
    fg.nonce_override, fg.aud = None, "someone-else"
    assert c.get(f"/auth/callback?code=x&state={q['state'][0]}", follow_redirects=False).headers["location"] == "/?signin=failed"
    fg.aud = CLIENT_ID
    assert c.get("/api/me").status_code == 401
    assert c.get("/login?next=//evil.example", follow_redirects=False).status_code == 303   # next is forced local
    r = c.get("/login?next=//evil.example", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    fg.pending_nonce = q["nonce"][0]
    assert c.get(f"/auth/callback?code=x&state={q['state'][0]}", follow_redirects=False).headers["location"] == "/app"


def test_payments_and_webhook():
    app, _, _ = build()
    c = sign_in(TestClient(app, base_url=SITE), "u1", "buyer@example.com")
    assert c.get("/app").status_code == 200 and "Test mode" in c.get("/app").text
    me = c.get("/api/me").json()
    assert me["balance_s"] == 0 and me["test_mode"] is True
    assert c.post("/api/start").status_code == 402
    o = c.post("/api/pay/order", json={"pack": "h3"}).json()
    assert o["amount"] == 49900 and o["key"] == "rzp_test_abc" and fr.orders[-1]["notes"]["sub"] == "u1"
    bad = c.post("/api/pay/verify", json={"razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_1", "razorpay_signature": "00"})
    assert bad.status_code == 400 and c.get("/api/me").json()["balance_s"] == 0
    sig = hmac.new(b"rsecret", f"{o['order_id']}|pay_1".encode(), hashlib.sha256).hexdigest()
    ok = c.post("/api/pay/verify", json={"razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_1", "razorpay_signature": sig})
    assert ok.status_code == 200 and c.get("/api/me").json()["balance_s"] == 3 * 3600
    c.post("/api/pay/verify", json={"razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_1", "razorpay_signature": sig})
    assert c.get("/api/me").json()["balance_s"] == 3 * 3600                       # verify twice: credited once

    def hook(event, order_id, pid, amount, secret=b"whsecret"):
        raw = json.dumps({"event": event, "payload": {"payment": {"entity": {"id": pid, "order_id": order_id, "amount": amount}}}}).encode()
        return c.post("/api/pay/webhook", content=raw, headers={"X-Razorpay-Signature": hmac.new(secret, raw, hashlib.sha256).hexdigest()})
    r = hook("payment.captured", o["order_id"], "pay_1", 49900)
    assert r.json() == {"ok": True, "credited": False}                            # webhook after verify: no double credit
    assert hook("payment.captured", o["order_id"], "pay_1", 49900, secret=b"nope").status_code == 400
    o2 = c.post("/api/pay/order", json={"pack": "h5"}).json()                      # browser closed before verify
    assert hook("payment.captured", o2["order_id"], "pay_2", 1).status_code == 400  # wrong amount: refused
    assert hook("payment.captured", o2["order_id"], "pay_2", 79900).json()["credited"] is True
    assert hook("payment.captured", o2["order_id"], "pay_2", 79900).json()["credited"] is False   # Razorpay retries
    assert hook("payment.failed", o2["order_id"], "pay_3", 79900).json()["ignored"] == "payment.failed"
    assert hook("payment.captured", "order_unknown", "pay_9", 100).json()["ignored"] == "unknown order"
    assert c.get("/api/me").json()["balance_s"] == 8 * 3600
    other = sign_in(TestClient(app, base_url=SITE), "u2", "other@example.com")
    assert other.post("/api/pay/verify", json={"razorpay_order_id": o["order_id"], "razorpay_payment_id": "pay_1",
                                               "razorpay_signature": sig}).status_code == 404   # not their order
    assert c.post("/api/pay/order", json={"pack": "nope"}).status_code == 400


def test_live_mode_switch():
    app, _, _ = build(key_id="rzp_live_abc")                                        # live key while in test mode
    c = sign_in(TestClient(app, base_url=SITE), "u1", "a@example.com")
    r = c.post("/api/pay/order", json={"pack": "h3"})
    assert r.status_code == 503 and "test mode" in r.json()["detail"]
    app, _, _ = build(key_id="rzp_live_abc", mode="live", confirm="yes")            # live without the exact sentence
    c = sign_in(TestClient(app, base_url=SITE), "u1", "a@example.com")
    assert c.post("/api/pay/order", json={"pack": "h3"}).status_code == 503
    assert paymod.Razorpay("rzp_live_abc", "s", "w", mode="live", confirm=paymod.LIVE_CONFIRM).problem() == ""


def test_invites_admin_and_origin():
    app, _, _ = build()
    admin = sign_in(TestClient(app, base_url=SITE), "a1", "admin@example.com", "Admin")
    user = sign_in(TestClient(app, base_url=SITE), "u1", "user@example.com")
    user2 = sign_in(TestClient(app, base_url=SITE), "u2", "user2@example.com")
    assert user.get("/api/admin/overview").status_code == 403 and user.get("/admin").status_code == 403
    assert admin.get("/admin").status_code == 200
    inv = admin.post("/api/admin/invite", json={"hours": 3, "valid_hours": 24, "max_uses": 1}).json()
    assert inv["link"] == f"{SITE}/invite/{inv['code']}"
    assert user.get(f"/invite/{inv['code']}", follow_redirects=False).headers["location"] == f"/app?code={inv['code']}"
    assert user.post("/api/redeem", json={"code": inv["code"].lower()}).status_code == 200
    assert user.get("/api/me").json()["balance_s"] == 3 * 3600
    assert "already used" in user.post("/api/redeem", json={"code": inv["code"]}).json()["detail"]
    assert "used up" in user2.post("/api/redeem", json={"code": inv["code"]}).json()["detail"]
    inv2 = admin.post("/api/admin/invite", json={"hours": 4, "valid_hours": 1, "max_uses": 5}).json()
    admin.post("/api/admin/invite/revoke", json={"code": inv2["code"]})
    assert "withdrawn" in user2.post("/api/redeem", json={"code": inv2["code"]}).json()["detail"]
    inv3 = admin.post("/api/admin/invite", json={"hours": 4, "valid_hours": 1, "max_uses": 5}).json()
    app.state.store.b.data["invites"][inv3["code"]]["expires"] = time.time() - 1
    assert "expired" in user2.post("/api/redeem", json={"code": inv3["code"]}).json()["detail"]
    assert admin.post("/api/admin/grant", json={"sub": "u2", "hours": 1.5, "note": "welcome"}).status_code == 200
    assert user2.get("/api/me").json()["balance_s"] == 5400
    users = admin.get("/api/admin/users").json()["users"]
    assert {u["email"] for u in users} == {"admin@example.com", "user@example.com", "user2@example.com"}
    # cross-site POSTs are refused even with the cookie
    assert user.post("/api/redeem", json={"code": "X"}, headers={"Origin": "https://evil.example"}).status_code == 403
    # pause, then data deletion
    admin.post("/api/admin/user", json={"sub": "u2", "disabled": True})
    assert user2.post("/api/start").status_code == 403
    admin.post("/api/admin/user", json={"sub": "u2", "delete": True})
    assert user2.get("/api/me").status_code == 401


def test_start_token_and_usage():
    app, vast, pool = build()
    c = sign_in(TestClient(app, base_url=SITE), "u1", "user@example.com", "User One")
    app.state.store.credit("u1", 5 * 3600, "grant", "g1")
    vast.add(7)                                                                    # one stopped pool node
    r = c.post("/api/start").json()
    assert r["wait"] is True and ("state", "7", "running") in vast.calls, (r, vast.calls)   # starts it, user waits
    vast.boot(7)
    assert node_beat(c, 7, "n1", "https://n7.trycloudflare.com").status_code == 200
    r = c.post("/api/start").json()
    assert r["url"].startswith("https://n7.trycloudflare.com/session?t="), r
    token = r["url"].split("t=", 1)[1]
    claims = webauth.jwt_decode(token, SECRET, aud="magicpro-gpu")
    assert claims["iss"] == "magicpro-web" and claims["sub"] == "u1" and claims["role"] == "user"
    assert claims["exp"] - claims["iat"] == 4 * 3600 and claims["bal"] == 5 * 3600        # min(balance, 4 h)
    assert claims["usage_url"] == f"{SITE}/api/gpu/usage" and claims["web_url"] == f"{SITE}/app"
    assert claims["email"] == "user@example.com" and claims["name"] == "User One" and len(claims["sid"]) == 32
    sid = claims["sid"]
    # usage: charge only the increase of the cumulative total
    assert usage(c, sid, "u1", 100).json() == {"ok": True, "remaining": 5 * 3600 - 100, "stop": False}
    assert usage(c, sid, "u1", 100).json()["remaining"] == 5 * 3600 - 100           # repeated report
    assert usage(c, sid, "u1", 40).json()["remaining"] == 5 * 3600 - 100            # out-of-order older report
    assert usage(c, sid, "u1", 160.5).json()["remaining"] == int(5 * 3600 - 160.5)
    assert usage(c, sid, "u1", 200, secret="wrong").status_code == 401
    assert usage(c, "nosuchsid", "u1", 10).json() == {"ok": False, "remaining": 0, "stop": True}
    assert usage(c, sid, "someone-else", 10).json()["stop"] is True
    me = c.get("/api/me").json()
    assert me["sessions"][0]["used_s"] == 160
    r = usage(c, sid, "u1", 99999).json()
    assert r == {"ok": True, "remaining": 0, "stop": True}
    assert c.post("/api/start").status_code == 402
    # short balance -> short token
    app.state.store.credit("u1", 600, "grant", "g2")
    claims = webauth.jwt_decode(c.post("/api/start").json()["url"].split("t=", 1)[1], SECRET, aud="magicpro-gpu")
    assert claims["exp"] - claims["iat"] == 600
    # admin role claim
    a = sign_in(TestClient(app, base_url=SITE), "a1", "admin@example.com")
    app.state.store.credit("a1", 60, "grant", "g3")
    pool.nodes["7"].reserved.clear()
    assert webauth.jwt_decode(a.post("/api/start").json()["url"].split("t=", 1)[1], SECRET)["role"] == "admin"
    assert c.get("/?reconnect=1&exclude=7", follow_redirects=False).headers["location"] == "/app?reconnect=1&exclude=7"
    r = a.post("/api/start", json={"exclude": "7"}).json()
    assert r.get("wait"), r                                                    # the only node was reported dead


def test_node_endpoints():
    app, vast, _ = build()
    c = TestClient(app, base_url=SITE)
    vast.add(5, "running", label="magicpro-pool:abc")
    assert node_beat(c, 5, "wrong", "https://x.trycloudflare.com").status_code == 403
    assert node_beat(c, 5, "abc", "https://x.trycloudflare.com").json()["keep"] is False
    assert c.get("/api/pool").status_code == 401
    admin = sign_in(TestClient(app, base_url=SITE), "a1", "admin@example.com")
    s = admin.get("/api/pool").json()
    assert s["nodes"][0]["state"] == "ready"
    env = admin.post("/api/pool/adopt", json={"instance_id": "5"}).json()["env"]
    assert "MAGIC_NODE_KEY=" in env and vast.inst["5"]["label"].startswith("magicpro-pool:")
    assert c.post("/api/pool/tick").status_code == 403


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("web: all ok")
