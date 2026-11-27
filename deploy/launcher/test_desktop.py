"""Magic Pro Desktop licensing, end to end and offline: the website (licensing.py) with fake Google
sign-in, the desktop app's licence client and updater (desktop/), and the desktop gate in front of
a stand-in studio. Run: python deploy/launcher/test_desktop.py"""
import hashlib
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="mp-desktop-test-"))
os.environ["MAGIC_DESKTOP_DATA"], os.environ["MAGIC_DESKTOP_CACHE"] = str(TMP / "data"), str(TMP / "cache")

import httpx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import licensing as L  # noqa: E402
import main  # noqa: E402
import payments as paymod  # noqa: E402
import pool as P  # noqa: E402
import store as storemod  # noqa: E402
import webauth  # noqa: E402
from test_pool import FakeVast  # noqa: E402
from test_web import CLIENT_ID, SECRET, SITE, fg, fr, sign_in  # noqa: E402

from desktop import license as lc  # noqa: E402
from desktop import updater as up  # noqa: E402
from desktop.gate import Gate  # noqa: E402
from tools.desktop_release import sign_manifest  # noqa: E402

T = [1_800_000_000.0]
clock = lambda: T[0]  # noqa: E731
LIC_KEY = Ed25519PrivateKey.generate()
REL_SEED = L.b64u(Ed25519PrivateKey.generate().private_bytes_raw())
REL_PUB = L.public_b64(Ed25519PrivateKey.from_private_bytes(L.b64u_dec(REL_SEED)))
LIC_PUB = L.public_b64(LIC_KEY)


def hw(**over):
    parts = {p: hashlib.sha256(f"A-{p}".encode()).hexdigest() for p in L.PARTS}
    parts.update(over)
    mid = hashlib.sha256("|".join(f"{p}={parts.get(p, '')}" for p in L.PARTS).encode()).hexdigest()[:32]
    return {"id": mid, "name": "STUDIO-PC", "parts": parts, "gpu": "NVIDIA GeForce RTX 5070"}


PC_A = hw()
PC_B = {**hw(**{p: hashlib.sha256(f"B-{p}".encode()).hexdigest() for p in L.PARTS}), "name": "OTHER-PC"}


def build():
    google = webauth.Google(CLIENT_ID, "gsecret", f"{SITE}/auth/callback", transport=httpx.MockTransport(fg.handler))
    rp = paymod.Razorpay("rzp_test_abc", "rsecret", "whsecret", transport=httpx.MockTransport(fr.handler))
    pool = P.Pool(FakeVast(time.time), P.Config(secret="poolsecret", image="", launcher_url=SITE), log=lambda *_: None)
    settings = main.Settings(site_url=SITE, session_secret=SECRET, google_client_id=CLIENT_ID,
                             google_client_secret="gsecret", admin_emails={"admin@example.com"})
    db = storemod.Store(storemod.MemoryBackend(), clock=clock)
    return main.create_app(settings, db, pool, google, rp,
                           desktop={"private_key": LIC_KEY, "offline_hours": 72, "release_pubkey": REL_PUB, "mailer": MAIL})


class FakeMail:
    """Keeps the sign-in e-mails instead of sending them."""

    def __init__(self):
        self.sent = []
        self.ready = True

    def send(self, to, subject, text):
        self.sent.append({"to": to, "subject": subject, "text": text})

    def code(self, to):
        return re.search(r"code is (\d{6})", [m for m in self.sent if m["to"] == to][-1]["text"]).group(1)


MAIL = FakeMail()


class Net:
    """The desktop app's HTTP, routed into the website app; `down` simulates no internet."""

    def __init__(self, app):
        self.c = TestClient(app, base_url=SITE)
        self.down = False

    def __call__(self, method, url, body=None, timeout=20):
        if self.down:
            raise lc.Offline("no internet")
        r = self.c.request(method, url.replace(SITE, ""), json=body)
        b = r.json() if r.content else {}
        if r.status_code >= 500:
            raise lc.Offline(str(r.status_code))
        if r.status_code >= 400:
            raise lc.Refused(r.status_code, b.get("detail", ""), b.get("code", ""))
        return b


def client(net, machine, name):
    c = lc.LicenseClient(SITE, LIC_PUB, TMP / f"{name}.dat", machine_fn=lambda: machine, http=net, clock=clock)
    c.ensure_machine()
    return c


def browser(app, sub, email):
    return sign_in(TestClient(app, base_url=SITE), sub, email)


def link_of(url):
    return url.rsplit("/", 1)[1]


def make_keys(admin, n=1, days=30, **kw):
    r = admin.post("/api/admin/desktop/keys", json={"count": n, "days": days, **kw})
    assert r.status_code == 200, r.text
    return r.json()["keys"]


def activate(c, user, key=""):
    r = c.activate(key)
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", r["code"])
    page = user.get(f"/desktop/activate/{link_of(r['url'])}")
    assert page.status_code == 200 and r["code"] in page.text, page.text[:300]
    return user.post("/api/desktop/approve", json={"link_id": link_of(r["url"]), "key": key})


# ---------------- tests ----------------
def test_key_format():
    k = L.new_key()
    assert L.KEY_RE.match(k) and L.clean_key(k.lower().replace("-", " ")) == k and L.clean_key("MPRO-123") == ""
    assert L.mask(k).startswith(k[:11]) and k[12:17] not in L.mask(k)


def test_same_machine_rule():
    a = PC_A
    assert L.same_machine(a, a)
    assert L.same_machine(a, hw(mac="1" * 64))                       # new network card
    assert L.same_machine(a, hw(guid="2" * 64))                      # Windows reinstalled
    assert not L.same_machine(a, PC_B)
    moved_disk = hw(smbios="3" * 64, board="4" * 64, mac="5" * 64)  # disk (and Windows) moved to another PC
    assert not L.same_machine(a, moved_disk)
    two = {"parts": {"guid": a["parts"]["guid"], "mac": a["parts"]["mac"]}}
    assert L.same_machine(two, two) and not L.same_machine(two, {"parts": {"guid": a["parts"]["guid"], "mac": "6" * 64}})


def test_activate_one_pc_one_account():
    app = build()
    net = Net(app)
    admin = browser(app, "adm", "admin@example.com")
    key = make_keys(admin, days=30, plan="Monthly")[0]
    alice = browser(app, "g-alice", "alice@gmail.com")

    a = client(net, PC_A, "a1")
    assert a.status()["state"] == "none" and not a.is_active()
    r = activate(a, alice, key)
    assert r.status_code == 200 and "activated on STUDIO-PC" in r.json()["message"], r.text
    st = a.poll()
    assert st["active"] and st["email"] == "alice@gmail.com" and st["days_left"] == 30 and st["plan"] == "Monthly"
    assert a.poll()["active"]                                          # nothing pending any more: unchanged

    # the same key on another PC: refused, also for the same account
    b = client(net, PC_B, "b1")
    r = activate(b, alice, key)
    assert r.status_code == 400 and "already active on another PC (STUDIO-PC)" in r.json()["detail"]
    assert b.poll()["state"] == "pending" and not b.is_active()

    # another Google account on the first PC: refused
    bob = browser(app, "g-bob", "bob@gmail.com")
    a2 = client(net, PC_A, "a2")
    r = activate(a2, bob, key)
    assert r.status_code == 400 and "another Google account" in r.json()["detail"]

    # reactivation of the same PC with the same account, without typing the key (picked on the page)
    a3 = client(net, PC_A, "a3")
    r3 = a3.activate("")
    page = alice.get(f"/desktop/activate/{link_of(r3['url'])}")
    assert key in page.text
    assert alice.post("/api/desktop/approve", json={"link_id": link_of(r3["url"]), "key": key}).status_code == 200
    assert a3.poll()["active"]
    assert not a.refresh()["active"] and a.status()["code"] == "signed_out"   # the older copy's refresh credential is replaced

    # the activation link only works for the signed-in user, once, for 10 minutes
    r4 = a3.activate(key)
    lid = link_of(r4["url"])
    anon = TestClient(app, base_url=SITE)
    assert anon.get(f"/desktop/activate/{lid}", follow_redirects=False).headers["location"] == f"/login?next=/desktop/activate/{lid}"
    assert anon.post("/api/desktop/approve", json={"link_id": lid}).status_code == 401
    T[0] += 601
    assert alice.post("/api/desktop/approve", json={"link_id": lid}).status_code == 400
    assert a3.poll()["state"] != "pending"


def test_unknown_and_locked_keys():
    app = build()
    net = Net(app)
    admin = browser(app, "adm", "admin@example.com")
    a = client(net, PC_A, "u1")
    try:
        a.activate("MPRO-AAAAA-BBBBB-CCCCC-DDDDD")
        raise AssertionError("unknown key accepted")
    except lc.Refused as e:
        assert "doesn't exist" in e.detail
    key = make_keys(admin, email="carol@gmail.com")[0]
    dave = browser(app, "g-dave", "dave@gmail.com")
    r = activate(a, dave, key)
    assert r.status_code == 400 and "different e-mail" in r.json()["detail"]
    carol = browser(app, "g-carol", "carol@gmail.com")
    assert activate(a, carol, key).status_code == 200 and a.poll()["active"]


def test_renewal_expiry_revoke_block_and_offline():
    app = build()
    net = Net(app)
    admin = browser(app, "adm", "admin@example.com")
    key = make_keys(admin, days=10)[0]
    eve = browser(app, "g-eve", "eve@gmail.com")
    a = client(net, PC_A, "r1")
    activate(a, eve, key)
    assert a.poll()["active"]
    start = T[0]

    # offline: keeps working for 72 h from the last renewal, then asks to go online
    net.down = True
    T[0] = start + 71 * 3600
    assert a.refresh()["offline"] and a.is_active()
    T[0] = start + 73 * 3600
    assert not a.is_active() and a.status()["state"] == "offline"
    net.down = False
    assert a.refresh()["active"]

    # clock set back a day: not accepted offline
    T[0] -= 86400
    assert not a.is_active() and a.status()["state"] == "clock"
    T[0] += 86400

    # hardware drift: new network card -> renewed for the new fingerprint
    a.machine_fn = lambda: hw(mac="7" * 64)
    a.ensure_machine()
    assert not a.is_active()
    assert a.refresh()["active"]

    # the state file copied to another PC is useless there
    b = lc.LicenseClient(SITE, LIC_PUB, TMP / "r1.dat", machine_fn=lambda: PC_B, http=net, clock=clock)
    b.ensure_machine()
    assert not b.is_active()
    st = b.refresh()
    assert not st["active"] and st["code"] == "machine"

    # subscription end: locked, also offline (sub_exp is in the licence)
    a = lc.LicenseClient(SITE, LIC_PUB, TMP / "r1b.dat", machine_fn=lambda: hw(mac="7" * 64), http=net, clock=clock)
    a.st = dict(json.loads(lc._dpapi((TMP / "r1.dat").read_bytes(), False)))
    a.ensure_machine()
    a.st.pop("locked", None)
    T[0] = start + 10 * 86400 - 60
    assert a.refresh()["active"]
    net.down = True
    T[0] = start + 10 * 86400 + 1
    assert not a.is_active() and a.status()["state"] == "locked"
    net.down = False
    st = a.refresh()
    assert st["state"] == "locked" and st["code"] == "expired" and "ended" in st["message"]

    # admin adds days: the same PC works again after "check again"
    lic = admin.post("/api/admin/desktop/key", json={"key": key, "action": "extend", "days": 30}).json()["license"]
    assert lic["state"] == "active" and lic["days_left"] == 30
    a.st.pop("locked", None)
    assert a.refresh()["active"]

    # cancelled key, blocked account
    admin.post("/api/admin/desktop/key", json={"key": key, "action": "revoke"})
    assert a.refresh()["code"] == "revoked" and not a.is_active()
    admin.post("/api/admin/desktop/key", json={"key": key, "action": "unrevoke"})
    a.st.pop("locked", None)
    assert a.refresh()["active"]
    assert admin.post("/api/admin/user", json={"sub": "g-eve", "disabled": True}).status_code == 200
    assert a.refresh()["code"] == "blocked"


def test_forged_licence_and_move():
    app = build()
    net = Net(app)
    admin = browser(app, "adm", "admin@example.com")
    key = make_keys(admin)[0]
    fay = browser(app, "g-fay", "fay@gmail.com")
    a = client(net, PC_A, "f1")
    activate(a, fay, key)
    assert a.poll()["active"]
    p = a.payload()
    a.st["license"] = L.sign_token(Ed25519PrivateKey.generate(), {**p, "sub_exp": p["sub_exp"] + 10**8})
    assert not a.is_active()
    body = a.st["license"].split(".")
    a.st["license"] = "v1." + L.b64u(L.canonical({**p, "exp": p["exp"] + 10**8})) + "." + body[2]
    assert not a.is_active()

    # moving to a new PC is an admin action; afterwards the old PC can't renew
    assert admin.post("/api/admin/desktop/key", json={"key": key, "action": "move"}).status_code == 200
    b = client(net, PC_B, "f2")
    assert activate(b, fay, key).status_code == 200 and b.poll()["active"]
    a2 = lc.LicenseClient(SITE, LIC_PUB, TMP / "f1.dat", machine_fn=lambda: PC_A, http=net, clock=clock)
    a2.ensure_machine()
    assert not a2.refresh()["active"]
    keys = admin.get("/api/admin/desktop/keys?search=fay").json()["keys"]
    assert keys[0]["pc"] == "OTHER-PC" and keys[0]["moves"] == 1
    assert fay.get("/api/desktop/mine").json()["licenses"][0]["pc"] == "OTHER-PC"
    assert "OTHER-PC" in fay.get("/download").text
    # only admins manage keys
    assert fay.post("/api/admin/desktop/keys", json={"count": 1, "days": 30}).status_code == 403


class _Files(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def test_updates():
    app = build()
    net = Net(app)
    admin = browser(app, "adm", "admin@example.com")
    inst = TMP / "www" / "MagicPro-Setup-9.0.0.exe"
    inst.parent.mkdir(exist_ok=True)
    inst.write_bytes(os.urandom(300_000))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: _Files(*a, directory=str(inst.parent)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/{inst.name}"
    m = {"channel": "stable", "version": "9.0.0", "min_version": "0.0.0", "notes": "Better", "published": 1,
         "full": {"url": "https://example.com/x.exe", "sha256": hashlib.sha256(inst.read_bytes()).hexdigest(), "size": 300_000}}

    u = up.Updater(SITE, "stable", REL_PUB, TMP / "updates", http=net, allow_http=True)
    assert u.check()["state"] == "latest"
    assert admin.post("/api/admin/desktop/release", json={"manifest": m}).status_code == 400          # unsigned
    bad = sign_manifest(m, L.b64u(Ed25519PrivateKey.generate().private_bytes_raw()))
    assert "doesn't match" in admin.post("/api/admin/desktop/release", json={"manifest": bad}).json()["detail"]
    good = sign_manifest(m, REL_SEED)
    assert admin.post("/api/admin/desktop/release", json={"manifest": good}).status_code == 200
    s = u.check()
    assert s["state"] == "available" and s["latest"] == "9.0.0" and not s["mandatory"]
    assert "Download version 9.0.0" in TestClient(app, base_url=SITE).get("/download").text

    # a manifest changed after signing (e.g. a compromised website) is ignored by the app
    app.state.licenses.set_release("stable", {**good, "full": {**good["full"], "url": "https://evil.example/x.exe"}})
    assert u.check()["state"] == "error"
    # required update
    app.state.licenses.set_release("stable", sign_manifest({**m, "min_version": "8.0.0"}, REL_SEED))
    assert u.check()["mandatory"]

    # download + checksum: a wrong file is rejected, the right one is started (simulated off Windows)
    u.manifest = {**m, "full": {**m["full"], "url": url, "sha256": "0" * 64}}
    u._install()
    assert u.status()["state"] == "error" and "checksum" in u.status()["message"]
    quit_called = threading.Event()
    u.on_exit = quit_called.set
    u.manifest = {**m, "full": {**m["full"], "url": url}}
    if sys.platform == "win32":     # don't really start the random "installer"
        up.subprocess.Popen = lambda *a, **k: None
    u._install()
    assert u.status()["state"] == "installing", u.status()
    assert quit_called.wait(3) and (TMP / "updates" / "MagicPro-Setup-9.0.0.exe").exists()
    srv.shutdown()


class StubSetup:
    def __init__(self):
        self.need = False

    def needed(self):
        return self.need

    def status(self):
        return {"state": "idle"}

    def start(self):
        pass


def test_email_code_login():
    app = build()
    net = Net(app)
    admin = browser(app, "adm", "admin@example.com")
    key = make_keys(admin, days=30, plan="Monthly")[0]
    a = client(net, PC_A, "e1")

    # anyone with a valid key and any e-mail provider
    assert a.email_start(key, "Hari@Outlook.com")["email"] == "hari@outlook.com"
    assert a.status()["pending"]["kind"] == "email" and a.status()["pending"]["email"] == "hari@outlook.com"
    code = MAIL.code("hari@outlook.com")
    assert "STUDIO-PC" in MAIL.sent[-1]["text"]
    wrong = "000000" if code != "000000" else "111111"
    try:
        a.email_verify(wrong)
        raise AssertionError("wrong code accepted")
    except lc.Refused as e:
        assert "4 tries left" in e.detail
    st = a.email_verify(code[:3] + " " + code[3:])       # spaces are fine
    assert st["active"] and st["email"] == "hari@outlook.com" and st["days_left"] == 30 and st["plan"] == "Monthly"
    keys = admin.get("/api/admin/desktop/keys?search=outlook").json()["keys"]
    assert keys[0]["email"] == "hari@outlook.com" and keys[0]["pc"] == "STUDIO-PC"

    def refused(fn, *a_, want=""):
        try:
            fn(*a_)
        except lc.Refused as e:
            assert want in e.detail, e.detail
            return e.detail
        raise AssertionError(f"not refused: {want}")

    # the same key: another e-mail is refused before any mail is sent; another PC after the code
    n = len(MAIL.sent)
    b = client(net, PC_B, "e2")
    refused(b.email_start, key, "someone@yahoo.com", want="another e-mail address")
    assert len(MAIL.sent) == n
    T[0] += 31
    b.email_start(key, "hari@outlook.com")
    refused(b.email_verify, MAIL.code("hari@outlook.com"), want="already active on another PC (STUDIO-PC)")
    assert not b.is_active()

    # no key: only an e-mail that already owns one gets a code (so this can't spam anyone)
    refused(b.email_start, "", "stranger@gmail.com", want="Enter your subscription key")
    T[0] += 31
    a2 = client(net, PC_A, "e3")
    a2.email_start("", "hari@outlook.com")
    assert a2.email_verify(MAIL.code("hari@outlook.com"))["active"]          # same PC, activated again

    # one code per 30 s, five per hour; five wrong tries end a code
    refused(a2.email_start, key, "hari@outlook.com", want="Wait 30 seconds")
    for _ in range(2):
        T[0] += 31
        a2.email_start(key, "hari@outlook.com")
    for i in range(5):
        refused(a2.email_verify, "999999" if MAIL.code("hari@outlook.com") != "999999" else "888888")
    refused(a2.email_verify, MAIL.code("hari@outlook.com"), want="Too many wrong codes")
    T[0] += 31
    refused(a2.email_start, key, "hari@outlook.com", want="Too many codes")
    T[0] += 3600
    a2.email_start("", "")                                 # "Send a new code" reuses the last key and e-mail
    assert a2.email_verify(MAIL.code("hari@outlook.com"))["active"]

    # a key locked to an e-mail; a Google account with the same e-mail is the same account
    key2 = make_keys(admin, email="priya@gmail.com")[0]
    c = client(net, PC_B, "e4")
    refused(c.email_start, key2, "other@gmail.com", want="different e-mail address")
    browser(app, "g-priya", "priya@gmail.com")
    c.email_start(key2, "priya@gmail.com")
    assert c.email_verify(MAIL.code("priya@gmail.com"))["active"]
    assert app.state.licenses.get(key2)["sub"] == "g-priya"


def test_gate():
    app = build()
    net = Net(app)
    admin = browser(app, "adm", "admin@example.com")
    key = make_keys(admin)[0]
    gus = browser(app, "g-gus", "gus@gmail.com")
    lic = client(net, PC_A, "g1")
    upd = up.Updater(SITE, "stable", REL_PUB, TMP / "u2", http=net)
    setup = StubSetup()
    gate = Gate(lic, upd, setup, 7860)

    studio = FastAPI()
    stopped = []

    @studio.get("/api/state")
    def state():
        return {"ok": True}

    @studio.websocket("/ws/h264")
    async def ws(w: WebSocket):
        await w.accept()
        while True:
            msg = await w.receive()
            if msg["type"] == "websocket.disconnect":
                break
            await w.send_bytes(msg.get("bytes") or b"")

    class Engine:
        def stop(self):
            stopped.append(1)
    gate.studio, gate.engine = studio, Engine()
    gate.hosts.add("testserver")      # TestClient sends this Host for websockets

    c = TestClient(gate, base_url="http://127.0.0.1:7860")
    assert TestClient(gate, base_url="http://evil.example:7860").get("/desktop/").status_code == 421
    assert c.get("/", follow_redirects=False).headers["location"] == "/desktop/"
    r = c.get("/api/state")
    assert r.status_code == 401 and r.json()["web_url"] == "/desktop/"
    assert c.get("/desktop/").status_code == 200 and "Activate Magic Pro" in c.get("/desktop/").text
    assert c.get("/desktop/api/status").json()["license"]["state"] == "none"
    assert c.post("/desktop/api/signout").status_code == 403                 # other websites can't post here
    try:
        with c.websocket_connect("/ws/h264") as w:
            w.receive_bytes()
        raise AssertionError("socket stayed open")
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "code", None) == 4401, e

    H = {"X-Magic-Desktop": "1"}
    assert c.post("/desktop/api/email/start", json={"key": key, "email": "gus@gmail.com"}).status_code == 403   # no header
    assert c.post("/desktop/api/email/start", json={"key": key, "email": "gus@gmail.com"}, headers=H).json()["email"] == "gus@gmail.com"
    assert c.get("/desktop/api/status").json()["license"]["state"] == "pending"
    assert c.post("/desktop/api/email/verify", json={"code": "12"}, headers=H).status_code == 400
    r = c.post("/desktop/api/email/verify", json={"code": MAIL.code("gus@gmail.com")}, headers=H)
    assert r.status_code == 200 and r.json()["active"], r.text
    assert c.get("/api/state").json() == {"ok": True}
    for cloud in ("/api/gpu/stop", "/api/gpu/tunnel"):           # local GPU only: no rented-GPU controls
        assert c.request("POST" if cloud.endswith("stop") else "GET", cloud).status_code == 404
    home = c.get("/")
    assert home.status_code == 200 and "/desktop/static/badge.js" in home.text and 'id="toggle"' in home.text
    setup.need = True
    assert c.get("/", follow_redirects=False).headers["location"] == "/desktop/"
    assert c.get("/desktop/api/status").json()["blocked"] == "setup"
    setup.need = False

    # a live stream ends with 4401 when the licence goes (key cancelled), and the camera stops
    with c.websocket_connect("/ws/h264") as w:
        w.send_bytes(b"x")
        assert w.receive_bytes() == b"x"
        admin.post("/api/admin/desktop/key", json={"key": key, "action": "revoke"})
        assert not lic.refresh()["active"]
        try:
            w.receive_bytes()
            raise AssertionError("socket stayed open")
        except Exception as e:  # noqa: BLE001
            assert getattr(e, "code", None) == 4401, e
    time.sleep(0.3)
    assert stopped and c.get("/api/state").status_code == 401


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("desktop: all ok")
