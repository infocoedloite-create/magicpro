"""The desktop app's side of the licence (the website's side is deploy/launcher/licensing.py, at magicpro-desk.nacgenai.in).

* Activation (what the app shows): `email_start(key, email)` has the website e-mail a 6-digit code
  to any address, `email_verify(code)` returns the licence and a refresh credential. (`activate` +
  `poll` is the older browser sign-in with Google, still served by the website.)
* A licence is an Ed25519-signed token naming this PC (`mid`), the account (e-mail) and two times:
  `exp` (offline limit, at most 72 h ahead) and `sub_exp` (end of the subscription). It is checked
  with the public key in desktop/desktop.json, so it can't be made or edited without the website's
  private key.
* The saved state (%APPDATA%\\MagicPro\\license.dat) is encrypted with Windows DPAPI for the
  signed-in Windows user, so a copy is useless on another PC or account.
* Renewal: at start and every 30 minutes. A definite "no" from the website (subscription ended,
  key cancelled, account blocked, another PC) locks the app at once; no internet only matters
  once `exp` passes. Setting the clock back doesn't help: the newest time ever seen is kept.
"""
import base64
import ctypes
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.request

from . import machine as machinemod
from .version import VERSION

RENEW_EVERY = 30 * 60
RETRY_EVERY = 5 * 60
CLOCK_SLACK = 2 * 3600          # the clock may go back this much (time zone / sync fixes) before it counts as set back
LOCKING_CODES = {"expired", "revoked", "blocked", "machine", "signed_out"}


class Offline(Exception):
    pass


class Refused(Exception):
    def __init__(self, status, detail, code=""):
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_token(token: str, pub_b64: str):
    """-> payload dict when the signature is good, else None."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        v, body, sig = token.split(".")
        if v != "v1" or not pub_b64:
            return None
        Ed25519PublicKey.from_public_bytes(b64u_dec(pub_b64)).verify(b64u_dec(sig), f"v1.{body}".encode())
        p = json.loads(b64u_dec(body))
        return p if p.get("typ") == "magicpro-desktop" else None
    except (ValueError, InvalidSignature, AttributeError, TypeError):
        return None


# ---------------- DPAPI (Windows) ----------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        return data
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    entropy_raw = b"magicpro-desktop-license"
    ebuf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    ent = _Blob(len(entropy_raw), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(dst))   # 0x1: no UI
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)


# ---------------- HTTP ----------------
def http_json(method, url, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"MagicProDesktop/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            b = json.loads(e.read() or b"{}")
        except ValueError:
            b = {}
        if e.code >= 500 and not b.get("code"):
            raise Offline(f"The licence server answered {e.code}.") from e
        raise Refused(e.code, b.get("detail") or f"HTTP {e.code}", b.get("code", "")) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Offline(str(getattr(e, "reason", e))) from e


# ---------------- the licence ----------------
class LicenseClient:
    def __init__(self, server, pubkey, state_file, machine_fn=machinemod.collect, http=http_json, clock=time.time):
        self.server, self.pubkey, self.file = server.rstrip("/"), pubkey, state_file
        self.http, self.clock, self.machine_fn = http, clock, machine_fn
        self.lock = threading.RLock()
        self.listeners = []
        self.pending = None             # activation in progress: {link_id, poll_token, code, url, exp}
        self.message, self.offline = "", False
        self.st = self._load()
        self.machine = self.st.get("machine")           # last known fingerprint: instant start, checked again below
        self.machine_fresh = False
        self._active = None
        self._wake = threading.Event()

    # ----- state file -----
    def _load(self):
        try:
            return json.loads(_dpapi(self.file.read_bytes(), protect=False))
        except (OSError, ValueError):
            return {}

    def _save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_bytes(_dpapi(json.dumps(self.st).encode(), protect=True))
        tmp.replace(self.file)

    # ----- what the app asks -----
    def payload(self):
        tok = self.st.get("license")
        return verify_token(tok, self.pubkey) if tok else None

    def _seen(self, t):
        self.st["max_time"] = max(float(self.st.get("max_time", 0)), float(t))

    def is_active(self) -> bool:
        with self.lock:
            now = self.clock()
            p = self.payload()
            ok = bool(p and self.machine and p.get("mid") == self.machine["id"] and now < float(p["exp"])
                      and now < float(p["sub_exp"]) and now > float(self.st.get("max_time", 0)) - CLOCK_SLACK
                      and not self.st.get("locked"))
            if ok:
                self.st["max_time"] = max(float(self.st.get("max_time", 0)), now)
            if ok != self._active:
                self._active = ok
                for fn in list(self.listeners):
                    threading.Thread(target=fn, args=(ok,), daemon=True).start()
            return ok

    def status(self) -> dict:
        with self.lock:
            active = self.is_active()
            p = self.payload() or {}
            now = self.clock()
            if not self.pubkey:
                state = "unconfigured"
            elif active:
                state = "active"
            elif self.pending and self.pending["exp"] > now:
                state = "pending"
            elif self.st.get("locked"):
                state = "locked"
            elif p and now < float(self.st.get("max_time", 0)) - CLOCK_SLACK:
                state = "clock"
            elif p and now >= float(p.get("exp", 0)) and now < float(p.get("sub_exp", 0)):
                state = "offline"
            elif p and now >= float(p.get("sub_exp", 0)):
                state = "locked"
            elif p and self.machine and p.get("mid") != self.machine["id"]:
                state = "checking" if not self.machine_fresh else "locked"
            elif self.st.get("key") and not p:
                state = "checking"
            else:
                state = "none"
            sub_exp = p.get("sub_exp")
            key = self.st.get("key", "")
            m = self.machine or {}
            return {"state": state, "active": active, "email": p.get("email", ""), "name": p.get("name", ""),
                    "plan": p.get("plan", ""), "key": key[:11] + "-•••••-•••••-" + key[-5:] if key else "",
                    "expires": sub_exp, "days_left": max(0, math.ceil((sub_exp - now) / 86400)) if sub_exp else None,
                    "offline_until": p.get("exp"), "message": self.message or self.st.get("lock_reason", ""),
                    "code": self.st.get("lock_code", ""), "offline": self.offline,
                    "pending": {k: self.pending.get(k) for k in ("kind", "email", "code", "url")} if state == "pending" else None,
                    "machine": {"name": m.get("name", ""), "gpu": m.get("gpu", "")}}

    # ----- fingerprint -----
    def ensure_machine(self):
        """Reads the hardware (a few seconds). Called once in the background at start."""
        m = self.machine_fn()
        with self.lock:
            changed = (self.machine or {}).get("id") != m["id"]
            self.machine, self.machine_fresh = m, True
            self.st["machine"] = m
            if self.st:
                self._save()
        return changed

    def _need_machine(self):
        if not self.machine_fresh:
            self.ensure_machine()
        return self.machine

    # ----- activation -----
    def activate(self, key=""):
        m = self._need_machine()
        r = self.http("POST", f"{self.server}/api/desktop/link", {"key": key, "machine": m, "app_version": VERSION})
        with self.lock:
            self.pending = {**r, "exp": self.clock() + float(r.get("expires_in", 600))}
            self.message = ""
            if key:
                self.st["key_typed"] = key
        return {"code": r["code"], "url": r["url"]}

    def cancel(self):
        with self.lock:
            self.pending = None

    def poll(self):
        with self.lock:
            p = self.pending
        if not p:
            return self.status()
        r = self.http("POST", f"{self.server}/api/desktop/poll", {"link_id": p["link_id"], "poll_token": p["poll_token"]})
        with self.lock:
            s = r.get("status")
            if s == "approved":
                self._accept(r)
            elif s in ("denied", "expired", "unknown"):
                self.pending = None
                self.message = r.get("reason") or {"expired": "The activation request expired. Press Activate again.",
                                                   "unknown": "The activation request is gone. Press Activate again."}.get(s, "Cancelled.")
        return self.status()

    def _accept(self, r):
        """The website handed over a licence (lock held)."""
        if not verify_token(r["license"], self.pubkey):
            self.message = "The licence from the server has a bad signature. Is this the official Magic Pro?"
        else:
            self.st.update(key=r["key"], refresh=r["refresh"], license=r["license"], machine=self.machine)
            for k in ("locked", "lock_reason", "lock_code", "key_typed"):
                self.st.pop(k, None)
            self._seen(self.clock())
            self._save()
            self.message, self.offline = "", False
        self.pending = None

    # ----- activation with an e-mail code (what the app uses) -----
    def email_start(self, key, email):
        """Asks the website to e-mail a 6-digit code; `email_verify` finishes the activation."""
        with self.lock:
            p = self.pending
        if not email and p and p.get("kind") == "email":       # "Send a new code" after a page reload
            key, email = p.get("key", ""), p.get("email", "")
        m = self._need_machine()
        r = self.http("POST", f"{self.server}/api/desktop/email/start",
                      {"key": key, "email": email, "machine": m, "app_version": VERSION})
        with self.lock:
            self.pending = {**r, "kind": "email", "key": key, "exp": self.clock() + float(r.get("expires_in", 600))}
            self.message = ""
        return {"email": r["email"]}

    def email_verify(self, code):
        with self.lock:
            p = self.pending
        if not p or p.get("kind") != "email":
            raise Refused(400, "Ask for a code first.")
        r = self.http("POST", f"{self.server}/api/desktop/email/verify",
                      {"link_id": p["link_id"], "poll_token": p["poll_token"], "code": code})
        with self.lock:
            self._accept(r)
        self.is_active()
        return self.status()

    # ----- renewal -----
    def refresh(self):
        with self.lock:
            key, rt = self.st.get("key"), self.st.get("refresh")
        if not key or not rt:
            return self.status()
        m = self._need_machine()
        try:
            r = self.http("POST", f"{self.server}/api/desktop/refresh",
                          {"key": key, "refresh": rt, "machine": m, "app_version": VERSION})
        except Offline as e:
            with self.lock:
                self.offline = True
                self.message = f"Can't reach the licence server ({e}). Magic Pro keeps working offline until the time shown."
            return self.status()
        except Refused as e:
            with self.lock:
                self.offline = False
                if e.code in LOCKING_CODES:
                    self.st.update(locked=True, lock_reason=e.detail, lock_code=e.code)
                    self.st.pop("license", None)
                    if e.code == "signed_out":
                        self.st.pop("refresh", None)
                    self._save()
                self.message = e.detail
            self.is_active()
            return self.status()
        with self.lock:
            if not verify_token(r.get("license", ""), self.pubkey):
                self.message = "The licence from the server has a bad signature."
                return self.status()
            self.st["license"] = r["license"]
            for k in ("locked", "lock_reason", "lock_code"):
                self.st.pop(k, None)
            self._seen(r.get("server_time", self.clock()))
            self._seen(self.clock())
            self._save()
            self.message, self.offline = "", False
        self.is_active()
        return self.status()

    def sign_out(self):
        """Forget the licence on this PC. The key stays bound to this PC on the website."""
        with self.lock:
            m = self.st.get("machine")
            self.st = {"machine": m} if m else {}
            self.pending, self.message = None, ""
            try:
                self.file.unlink()
            except OSError:
                pass
        self.is_active()

    def wake(self):
        self._wake.set()

    def run(self):
        """Background loop: fingerprint once, then renew at start and every 30 minutes."""
        try:
            self.ensure_machine()
        except Exception as e:  # noqa: BLE001
            print(f"[license] reading this PC failed: {e}", flush=True)
        while True:
            try:
                st = self.refresh()
                wait = RETRY_EVERY if st["offline"] else RENEW_EVERY
            except Exception as e:  # noqa: BLE001
                print(f"[license] renewal failed: {e}", flush=True)
                wait = RETRY_EVERY
            self.is_active()
            self._wake.wait(wait)
            self._wake.clear()
