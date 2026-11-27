"""Magic Pro Desktop: subscription keys, one PC per key, signed licences and the update manifest.

How it fits together (docs/desktop.md has the full picture):

* An admin creates subscription keys (`MPRO-XXXXX-XXXXX-XXXXX-XXXXX`) for a number of days.
* The desktop app sends the key, an e-mail address and the PC's fingerprint
  (`POST /api/desktop/email/start`); the website e-mails a 6-digit code, and the app sends it back
  (`/api/desktop/email/verify`). That binds the key to that e-mail address (any provider) and to
  that PC, starts the subscription clock on first use, and returns a licence and a refresh
  credential. (The older browser flow, `/api/desktop/link` + Google sign-in on
  `/desktop/activate/<link>` + `/api/desktop/poll`, still works.)
* A licence is a small Ed25519-signed token. The app checks it offline with the public key built
  into it, so nobody can make one without the private key held here (secret `DESKTOP_LICENSE_KEY`).
  It lasts `DESKTOP_OFFLINE_HOURS` (72 h) at most and never past the subscription's end; the app
  renews it online (`/api/desktop/refresh`) at every start and every 30 minutes.
* One key works on one PC. Another PC is refused ("already active on another PC") until an admin
  moves the key. A PC is recognised by several hardware parts (Windows machine id, SMBIOS UUID,
  motherboard serial, network card MAC, system disk serial), hashed on the PC; most of them must
  match, so a Windows reinstall or a new network card doesn't lock anyone out but a copied disk
  in another PC doesn't pass.
* When the subscription ends, is revoked or the account is blocked, refresh answers 403 with a
  `code` and the app locks itself.
* Updates: `GET /api/desktop/latest` serves the release manifest an admin uploaded. Manifests are
  signed offline with a separate release key (tools/desktop_release.py), so the app installs only
  builds you signed, even if this website were compromised.

Collections: `licenses` (one doc per key), `desktop_links` (activation requests, 10 minutes),
`email_sends` (code rate limits).
"""
import base64
import hashlib
import json
import math
import os
import re
import secrets
import time

from fastapi import Body, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O, 1/I: easy to type from an e-mail
KEY_RE = re.compile(r"^MPRO(-[A-HJ-NP-Z2-9]{5}){4}$")
LINK_TTL = 600
PARTS = ("guid", "smbios", "board", "mac", "disk")      # hashed on the PC; see desktop/machine.py
CHANNELS = ("stable", "beta")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


# ---------------- signing ----------------
def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_private(value: str):
    """DESKTOP_LICENSE_KEY: the 32-byte Ed25519 seed, base64url (tools/desktop_keys.py prints it)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = b64u_dec(value.strip())
    if len(raw) != 32:
        raise ValueError("DESKTOP_LICENSE_KEY must be a 32-byte Ed25519 seed (base64url).")
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_b64(priv) -> str:
    from cryptography.hazmat.primitives import serialization
    return b64u(priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def sign_token(priv, payload: dict) -> str:
    """'v1.<payload>.<sig>': payload = base64url(canonical JSON), sig = Ed25519 over 'v1.<payload>'."""
    body = "v1." + b64u(canonical(payload))
    return body + "." + b64u(priv.sign(body.encode()))


def verify_manifest(manifest: dict, pub_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    m = dict(manifest)
    sig = m.pop("sig", "")
    try:
        Ed25519PublicKey.from_public_bytes(b64u_dec(pub_b64)).verify(b64u_dec(sig), canonical(m))
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------- keys and machines ----------------
def new_key() -> str:
    groups = ["".join(secrets.choice(KEY_ALPHABET) for _ in range(5)) for _ in range(4)]
    return "MPRO-" + "-".join(groups)


def clean_key(s) -> str:
    """Accepts any case, spaces and missing dashes."""
    s = re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
    if s.startswith("MPRO"):
        s = s[4:]
    if len(s) != 20:
        return ""
    k = "MPRO-" + "-".join(s[i:i + 5] for i in range(0, 20, 5))
    return k if KEY_RE.match(k) else ""


def mask(key: str) -> str:
    return key[:11] + "-•••••-•••••-" + key[-5:] if key else ""


def clean_machine(m) -> dict:
    if not isinstance(m, dict):
        raise HTTPException(400, "Missing machine details.")
    parts = m.get("parts") if isinstance(m.get("parts"), dict) else {}
    parts = {p: str(parts[p])[:64] for p in PARTS if re.fullmatch(r"[0-9a-f]{16,64}", str(parts.get(p, "")))}
    mid = str(m.get("id", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", mid) or len(parts) < 2:
        raise HTTPException(400, "This PC could not be identified. Restart Magic Pro and try again.")
    name = re.sub(r"[^\w .\-()]", "", str(m.get("name", "")))[:60] or "PC"
    info = {k: str(m.get(k, ""))[:120] for k in ("os", "gpu", "cpu") if m.get(k)}
    return {"id": mid, "name": name, "parts": parts, **info}


def same_machine(bound: dict, now: dict) -> bool:
    """Most hardware parts both sides know must be equal, and at least two of them."""
    if not bound:
        return False
    a, b = bound.get("parts") or {}, now.get("parts") or {}
    common = [p for p in PARTS if a.get(p) and b.get(p)]
    same = sum(1 for p in common if a[p] == b[p])
    return same >= 2 and same * 2 > len(common)


def version_tuple(v) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "0"))[:4])


# ---------------- the rules ----------------
class Licenses:
    """All licence rules, on the website's Store backend (memory in tests, Firestore on Cloud Run)."""

    def __init__(self, store, private_key=None, offline_hours=72.0):
        self.db, self.b, self.clock = store, store.b, store.clock
        self.priv = private_key
        self.offline_s = float(offline_hours) * 3600

    @property
    def ready(self):
        return self.priv is not None

    # ----- admin -----
    def create_keys(self, count, days, plan="", note="", email="", admin=""):
        count, days = int(count), float(days)
        if not 1 <= count <= 200:
            raise ValueError("Make between 1 and 200 keys at a time.")
        if not 0 < days <= 3660:
            raise ValueError("Days must be between 1 and 3660.")
        email = email.strip().lower()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("That e-mail address doesn't look right.")
        t, out = self.clock(), []

        def fn(v):
            for _ in range(count):
                k = new_key()
                while v.get("licenses", k):
                    k = new_key()
                v.set("licenses", k, {"key": k, "days": days, "plan": plan.strip()[:40] or f"{days:g} days",
                                      "note": note.strip()[:200], "email_lock": email, "created": t, "created_by": admin,
                                      "sub": "", "email": "", "name": "", "activated": None, "expires": None,
                                      "revoked": False, "machine": None, "moves": 0, "last_seen": None, "events": []})
                out.append(k)
        self.b.run(fn)
        return out

    def get(self, key):
        return self.b.run(lambda v: v.get("licenses", key)) if key else None

    def list(self, search="", limit=500):
        rows = self.b.query("licenses", order="created", limit=None)
        s = search.strip().lower()
        if s:
            rows = [r for r in rows if s in r["key"].lower() or s in (r.get("email") or "").lower()
                    or s in (r.get("email_lock") or "").lower() or s in (r.get("note") or "").lower()
                    or s in ((r.get("machine") or {}).get("name") or "").lower()]
        return rows[:limit]

    def for_user(self, sub):
        return self.b.query("licenses", [("sub", "==", sub)], order="created")

    def admin_action(self, key, action, days=0.0):
        t = self.clock()

        def fn(v):
            lic = v.get("licenses", key)
            if not lic:
                raise ValueError("No such key.")
            if action == "revoke":
                lic["revoked"] = True
            elif action == "unrevoke":
                lic["revoked"] = False
            elif action == "move":          # unbind the PC: the next activation (same account) binds a new one
                lic["machine"] = None
                lic["moves"] = int(lic.get("moves", 0)) + 1
            elif action == "extend":
                d = float(days)
                if not -3660 <= d <= 3660 or d == 0:
                    raise ValueError("Days must be between -3660 and 3660.")
                if lic.get("expires"):
                    lic["expires"] = max(float(lic["expires"]), t) + d * 86400 if d > 0 else float(lic["expires"]) + d * 86400
                else:
                    lic["days"] = max(1.0, float(lic["days"]) + d)
            elif action == "release":       # free the key from its Google account too (reissue to someone else)
                lic.update(sub="", email="", name="", machine=None)
            else:
                raise ValueError("Unknown action.")
            _event(lic, t, action, f"{days:g} days" if action == "extend" else "")
            v.set("licenses", key, lic)
            return lic
        return self.b.run(fn)

    # ----- the PC asks to be activated -----
    def start_link(self, key, machine, app_version=""):
        t = self.clock()
        if key:
            self._precheck(self.get(key), t)
        link_id, poll = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        code = "".join(secrets.choice(KEY_ALPHABET) for _ in range(8))
        doc = {"id": link_id, "poll_h": _h(poll), "key": key, "machine": machine, "app_version": str(app_version)[:20],
               "created": t, "exp": t + LINK_TTL, "status": "pending", "code": code, "result": None, "reason": ""}
        self.b.run(lambda v: v.set("desktop_links", link_id, doc))
        return {"link_id": link_id, "poll_token": poll, "code": f"{code[:4]}-{code[4:]}", "expires_in": LINK_TTL, "interval": 2}

    def get_link(self, link_id):
        if not re.fullmatch(r"[\w-]{10,40}", link_id or ""):
            return None
        d = self.b.run(lambda v: v.get("desktop_links", link_id))
        return d if d and d["exp"] > self.clock() else None

    def approve(self, link_id, user, key):
        """The signed-in user pressed Activate on the website."""
        t = self.clock()
        if user.get("disabled"):
            raise ValueError("This account is blocked. Contact support.")
        rt = secrets.token_urlsafe(32)

        def fn(v):
            link = v.get("desktop_links", link_id)
            if not link or link["exp"] <= t or link["status"] != "pending":
                raise ValueError("This activation request has expired. Press Activate in Magic Pro again.")
            k = link.get("key") or key
            lic = v.get("licenses", k) if k else None
            self._precheck(lic, t)
            if lic.get("email_lock") and lic["email_lock"] != user["email"].lower():
                raise ValueError("This key was issued to a different e-mail address. Sign in with that Google account.")
            if lic.get("sub") and lic["sub"] != user["sub"]:
                raise ValueError("This key is already used by another Google account.")
            m = link["machine"]
            bound = lic.get("machine")
            if bound and not same_machine(bound, m):
                raise ValueError(f"This key is already active on another PC ({bound.get('name', 'PC')}). "
                                 "One key works on one PC. Contact support to move it to this PC.")
            if not lic.get("activated"):
                lic["activated"] = t
                lic["expires"] = t + float(lic["days"]) * 86400
            if float(lic["expires"]) <= t:
                raise ValueError("This subscription has ended. Renew it to keep using Magic Pro.")
            lic.update(sub=user["sub"], email=user["email"].lower(), name=user.get("name", ""), last_seen=t,
                       app_version=link.get("app_version", ""))
            lic["machine"] = {**m, "bound": (bound or {}).get("bound", t), "rt_h": _h(rt), "parts": m["parts"]}
            _event(lic, t, "activated" if not bound else "reactivated", m["name"])
            v.set("licenses", k, lic)
            link.update(status="approved", key=k, result={"license": self._token(lic, t), "refresh": rt, "key": k})
            v.set("desktop_links", link_id, link)
            return lic
        return self.b.run(fn)

    def deny(self, link_id, reason="Cancelled on the website."):
        def fn(v):
            link = v.get("desktop_links", link_id)
            if link and link["status"] == "pending":
                link.update(status="denied", reason=reason)
                v.set("desktop_links", link_id, link)
        self.b.run(fn)

    def poll(self, link_id, poll_token):
        t = self.clock()

        def fn(v):
            link = v.get("desktop_links", link_id) if re.fullmatch(r"[\w-]{10,40}", link_id or "") else None
            if not link or not secrets.compare_digest(link["poll_h"], _h(poll_token or "")):
                return {"status": "unknown"}
            if link["status"] == "approved":
                v.delete("desktop_links", link_id)          # the licence is handed over once
                return {"status": "approved", **link["result"]}
            if link["status"] == "denied":
                v.delete("desktop_links", link_id)
                return {"status": "denied", "reason": link.get("reason", "")}
            if link["exp"] <= t:
                v.delete("desktop_links", link_id)
                return {"status": "expired"}
            return {"status": "pending"}
        return self.b.run(fn)

    # ----- sign-in with an e-mail code (any address: Gmail, Outlook, Yahoo, work) -----
    def start_email(self, key, email, machine, app_version, mailer):
        """Sends a 6-digit code to `email`. Only for a valid key, or an e-mail that already owns a key
        (activating again without typing it), so this can't be used to send mail to anyone."""
        t = self.clock()
        email = email.strip().lower()
        if not EMAIL_RE.fullmatch(email) or len(email) > 120:
            raise ValueError("Enter a valid e-mail address.")
        if key:
            lic = self.get(key)
            self._precheck(lic, t)
            if lic.get("email_lock") and lic["email_lock"] != email:
                raise ValueError("This key was issued to a different e-mail address. Use that address.")
            if lic.get("email") and lic["email"] != email:
                raise ValueError("This key is already used by another e-mail address.")
        elif not self.b.query("licenses", [("email", "==", email)], limit=1):
            raise ValueError("Enter your subscription key (no key is linked to this e-mail yet).")
        self._count_send(email, t)
        out = self.start_link(key, machine, app_version)
        code = f"{secrets.randbelow(10**6):06d}"

        def fn(v):
            link = v.get("desktop_links", out["link_id"])
            link.update(email=email, otp_h=_h(f"{out['link_id']}|{code}"), tries=0)
            v.set("desktop_links", out["link_id"], link)
        self.b.run(fn)
        mailer.send(email, f"Your Magic Pro code: {code}",
                    f"Your Magic Pro code is {code}\n\nType it in Magic Pro on {machine['name']} to activate it. "
                    "It works for 10 minutes.\n\nIf you didn't ask for this code, ignore this e-mail: nothing "
                    "happens without it.\n")
        return {"link_id": out["link_id"], "poll_token": out["poll_token"], "expires_in": LINK_TTL, "email": email}

    def verify_email(self, link_id, poll_token, code):
        """-> the licence for the app once the code is right (5 tries per code)."""
        t = self.clock()
        code = re.sub(r"\D", "", str(code or ""))

        def check(v):
            link = v.get("desktop_links", link_id) if re.fullmatch(r"[\w-]{10,40}", link_id or "") else None
            if not link or not link.get("otp_h") or not secrets.compare_digest(link["poll_h"], _h(poll_token or "")):
                raise ValueError("This code request is gone. Ask for a new code.")
            if link["exp"] <= t or link["status"] != "pending":
                raise ValueError("The code expired. Ask for a new code.")
            if int(link.get("tries", 0)) >= 5:
                raise ValueError("Too many wrong codes. Ask for a new code.")
            if not secrets.compare_digest(link["otp_h"], _h(f"{link_id}|{code}")):
                link["tries"] = int(link.get("tries", 0)) + 1
                v.set("desktop_links", link_id, link)
                return None, 5 - link["tries"]
            return link, 0
        link, left = self.b.run(check)
        if link is None:
            raise ValueError(f"That code isn't right. {left} tr{'y' if left == 1 else 'ies'} left." if left
                             else "Too many wrong codes. Ask for a new code.")
        email = link["email"]
        # the same account as on the website if they signed in there with Google, else a new e-mail account
        user = self.db.find_user_by_email(email)
        if not user:
            user = self.db.sign_in("email:" + _h(email)[:24], email, email.split("@")[0], allow_new=True)
        self.approve(link_id, user, link.get("key") or self._own_key(email, link["machine"], t))
        r = self.poll(link_id, poll_token)
        if r.get("status") != "approved":
            raise ValueError("Activation failed. Ask for a new code.")
        return r

    def _own_key(self, email, machine, t):
        """No key typed: the e-mail's key for this PC, else one of its keys that has no PC yet."""
        mine = [x for x in self.b.query("licenses", [("email", "==", email)], order="created")
                if not x.get("revoked") and not (x.get("expires") and float(x["expires"]) <= t)]
        for x in mine:
            if x.get("machine") and same_machine(x["machine"], machine):
                return x["key"]
        free = [x for x in mine if not x.get("machine")]
        return (free or mine or [{"key": ""}])[0]["key"]

    def _count_send(self, email, t):
        """At most 5 codes per e-mail address per hour, and one per 30 s."""
        doc_id = _h(email)[:32]

        def fn(v):
            d = v.get("email_sends", doc_id) or {"t": []}
            recent = [x for x in d["t"] if x > t - 3600]
            if recent and t - max(recent) < 30:
                raise ValueError("A code was just sent. Wait 30 seconds before asking again.")
            if len(recent) >= 5:
                raise ValueError("Too many codes for this e-mail address. Try again in an hour.")
            v.set("email_sends", doc_id, {"t": recent + [t]})
        self.b.run(fn)

    # ----- the app renews its licence -----
    def refresh(self, key, refresh_token, machine, app_version=""):
        t = self.clock()

        def fn(v):
            lic = v.get("licenses", key) if key else None
            bound = (lic or {}).get("machine") or {}
            if not lic or not bound.get("rt_h") or not secrets.compare_digest(bound["rt_h"], _h(refresh_token or "")):
                raise LicenseRefused("signed_out", "This PC is no longer activated. Sign in again.")
            if lic.get("revoked"):
                raise LicenseRefused("revoked", "This subscription key was cancelled. Contact support.")
            if not same_machine(bound, machine):
                raise LicenseRefused("machine", "This licence belongs to another PC.")
            if float(lic.get("expires") or 0) <= t:
                raise LicenseRefused("expired", "Your subscription has ended. Renew it to keep using Magic Pro.")
            user = v.get("users", lic["sub"]) or {}
            if user.get("disabled"):
                raise LicenseRefused("blocked", "Your account is paused. Contact support.")
            # hardware drifts a little (new network card, Windows reinstalled): keep what this PC says now
            lic["machine"] = {**bound, **{k: machine[k] for k in ("id", "name", "parts", "os", "gpu", "cpu") if k in machine}}
            lic.update(last_seen=t, app_version=str(app_version)[:20])
            v.set("licenses", key, lic)
            return {"license": self._token(lic, t), "expires": lic["expires"], "server_time": t}
        return self.b.run(fn)

    # ----- internals -----
    def _precheck(self, lic, t):
        if not lic:
            raise ValueError("That subscription key doesn't exist. Check it and try again.")
        if lic.get("revoked"):
            raise ValueError("This subscription key was cancelled. Contact support.")
        if lic.get("expires") and float(lic["expires"]) <= t:
            raise ValueError("This subscription has ended. Renew it to keep using Magic Pro.")

    def _token(self, lic, t):
        if not self.priv:
            raise ValueError("Desktop licences are not set up on this website yet.")
        m = lic["machine"]
        return sign_token(self.priv, {
            "typ": "magicpro-desktop", "key": lic["key"], "sub": lic["sub"], "email": lic["email"], "name": lic.get("name", ""),
            "plan": lic.get("plan", ""), "mid": m["id"], "iat": int(t), "exp": int(min(t + self.offline_s, float(lic["expires"]))),
            "sub_exp": int(float(lic["expires"]))})

    # ----- releases -----
    def get_release(self, channel="stable"):
        cfg = self.b.run(lambda v: v.get("config", f"desktop_release_{channel}"))
        return (cfg or {}).get("manifest")

    def set_release(self, channel, manifest):
        self.b.run(lambda v: v.set("config", f"desktop_release_{channel}", {"manifest": manifest, "t": self.clock()}))


class Mailer:
    """Sends the sign-in codes over SMTP: works with Brevo, SendGrid, Resend, Zoho, Amazon SES or a
    Google Workspace / Gmail app password. Env: SMTP_HOST, SMTP_PORT (587 STARTTLS or 465 SSL),
    SMTP_USER, SMTP_PASSWORD (secret), MAIL_FROM ("Magic Pro <no-reply@your-domain>")."""

    def __init__(self, host="", port=587, user="", password="", sender=""):
        self.host, self.port, self.user, self.sender = host.strip(), int(port or 587), user.strip(), sender.strip()
        # tolerate a secret saved with a UTF-8 BOM or trailing newline, and the spaces Gmail shows in
        # its 16-character app passwords (SMTP app passwords never contain spaces)
        self.password = password.replace("﻿", "").strip().replace(" ", "")

    @classmethod
    def from_env(cls):
        e = os.environ.get
        return cls(e("SMTP_HOST", ""), e("SMTP_PORT", "587"), e("SMTP_USER", ""), e("SMTP_PASSWORD", ""), e("MAIL_FROM", ""))

    @property
    def ready(self):
        return bool(self.host and self.sender)

    def send(self, to, subject, text):
        import smtplib
        import ssl
        from email.message import EmailMessage
        if not self.ready:
            raise ValueError("Sign-in e-mails are not set up on this website yet (SMTP_HOST, MAIL_FROM).")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(text)
        ctx = ssl.create_default_context()
        try:
            if self.port == 465:
                with smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=20) as s:
                    if self.user:
                        s.login(self.user, self.password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                    s.starttls(context=ctx)
                    if self.user:
                        s.login(self.user, self.password)
                    s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            print(f"[mail] sending to @{to.split('@')[-1]} failed: {e}", flush=True)
            raise ValueError("The code couldn't be sent. Try again in a minute.") from e


class LicenseRefused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _event(lic, t, what, detail=""):
    lic["events"] = ([{"t": t, "what": what, "detail": str(detail)[:80]}] + list(lic.get("events") or []))[:20]


def check_manifest(m) -> dict:
    """Shape of a release manifest (tools/desktop_release.py writes it)."""
    if not isinstance(m, dict) or not m.get("sig"):
        raise ValueError("The manifest is not signed. Make it with tools/desktop_release.py.")
    if m.get("channel") not in CHANNELS or not re.fullmatch(r"\d+\.\d+\.\d+", str(m.get("version", ""))):
        raise ValueError("The manifest needs a channel (stable or beta) and a version like 1.2.0.")
    f = m.get("full")
    if not isinstance(f, dict) or not str(f.get("url", "")).startswith("https://") \
            or not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))) or int(f.get("size", 0)) <= 0:
        raise ValueError("The manifest's installer (full) needs an https url, a sha256 and a size.")
    return m


def lic_view(lic, now, admin=False):
    m = lic.get("machine") or {}
    exp = lic.get("expires")
    state = ("revoked" if lic.get("revoked") else "expired" if exp and float(exp) <= now
             else "active" if m else "not activated" if not lic.get("activated") else "no PC")
    out = {"key": lic["key"] if admin else mask(lic["key"]), "plan": lic.get("plan", ""), "state": state,
           "days": lic.get("days"), "activated": lic.get("activated"), "expires": exp,
           "days_left": None if not exp else max(0, math.ceil((float(exp) - now) / 86400)),
           "pc": m.get("name", ""), "pc_gpu": m.get("gpu", ""), "last_seen": lic.get("last_seen"),
           "app_version": lic.get("app_version", "")}
    if admin:
        out.update(email=lic.get("email", ""), email_lock=lic.get("email_lock", ""), note=lic.get("note", ""),
                   created=lic.get("created"), moves=lic.get("moves", 0), events=lic.get("events", [])[:10])
    return out


# ---------------- routes ----------------
def attach(app, *, db, settings, current, need_user, need_admin, page, private_key=None, offline_hours=None,
           release_pubkey=None, mailer=None):
    """Adds the desktop routes to the website. Keys come from the environment unless given (tests)."""
    if private_key is None and os.environ.get("DESKTOP_LICENSE_KEY", "").strip():
        private_key = load_private(os.environ["DESKTOP_LICENSE_KEY"])
    if offline_hours is None:
        offline_hours = float(os.environ.get("DESKTOP_OFFLINE_HOURS", "72"))
    if release_pubkey is None:
        release_pubkey = os.environ.get("DESKTOP_RELEASE_PUBKEY", "").strip()
    mail = mailer or Mailer.from_env()
    L = Licenses(db, private_key, offline_hours)
    app.state.licenses = L

    def need_ready():
        if not L.ready:
            raise HTTPException(503, "Desktop licences are not set up on this website yet.")

    # ----- called by the desktop app (no cookies; JSON in, JSON out) -----
    @app.post("/api/desktop/link")
    async def link(body: dict = Body(...)):
        need_ready()
        raw = str(body.get("key", "")).strip()
        key = clean_key(raw)
        if raw and not key:
            raise HTTPException(400, "That doesn't look like a subscription key (MPRO-XXXXX-XXXXX-XXXXX-XXXXX).")
        machine = clean_machine(body.get("machine"))
        try:
            out = await run_in_threadpool(L.start_link, key, machine, body.get("app_version", ""))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {**out, "url": f"{settings.site_url}/desktop/activate/{out['link_id']}"}

    @app.post("/api/desktop/email/start")
    async def email_start(body: dict = Body(...)):
        need_ready()
        raw = str(body.get("key", "")).strip()
        key = clean_key(raw)
        if raw and not key:
            raise HTTPException(400, "That doesn't look like a subscription key (MPRO-XXXXX-XXXXX-XXXXX-XXXXX).")
        machine = clean_machine(body.get("machine"))
        try:
            return await run_in_threadpool(L.start_email, key, str(body.get("email", "")), machine,
                                           body.get("app_version", ""), mail)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/desktop/email/verify")
    async def email_verify(body: dict = Body(...)):
        need_ready()
        try:
            return await run_in_threadpool(L.verify_email, str(body.get("link_id", "")), str(body.get("poll_token", "")),
                                           str(body.get("code", "")))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/desktop/poll")
    async def poll(body: dict = Body(...)):
        return await run_in_threadpool(L.poll, str(body.get("link_id", "")), str(body.get("poll_token", "")))

    @app.post("/api/desktop/refresh")
    async def refresh(body: dict = Body(...)):
        need_ready()
        key = clean_key(body.get("key", ""))
        machine = clean_machine(body.get("machine"))
        try:
            return await run_in_threadpool(L.refresh, key, str(body.get("refresh", "")), machine, body.get("app_version", ""))
        except LicenseRefused as e:
            return JSONResponse({"detail": e.message, "code": e.code}, 403)

    @app.get("/api/desktop/latest")
    def latest(channel: str = "stable"):
        m = L.get_release(channel if channel in CHANNELS else "stable")
        return m or {}

    # ----- the activation page the app opens in the browser -----
    @app.get("/desktop/activate/{link_id}", response_class=HTMLResponse)
    def activate_page(request: Request, link_id: str):
        u = current(request)
        if not u:
            return RedirectResponse(f"/login?next=/desktop/activate/{link_id}", 303)
        link = L.get_link(link_id)
        lic = L.get(link["key"]) if link and link.get("key") else None
        own = [lic_view(x, L.clock()) | {"id": x["key"]} for x in L.for_user(u["sub"])] if link and not link.get("key") else []
        return page("desktop_activate.html", request, link=link, lic=lic_view(lic, L.clock()) if lic else None, own=own,
                    link_id=link_id)

    @app.post("/api/desktop/approve")
    async def approve(request: Request, body: dict = Body(...)):
        u = await run_in_threadpool(need_user, request)
        need_ready()
        raw = str(body.get("key", ""))
        key = clean_key(raw)
        try:
            lic = await run_in_threadpool(L.approve, str(body.get("link_id", "")), u, key)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "message": f"Magic Pro is activated on {lic['machine']['name']}. You can go back to the app.",
                "license": lic_view(lic, L.clock())}

    @app.post("/api/desktop/deny")
    async def deny(request: Request, body: dict = Body(...)):
        await run_in_threadpool(need_user, request)
        await run_in_threadpool(L.deny, str(body.get("link_id", "")))
        return {"ok": True}

    @app.get("/api/desktop/mine")
    def mine(request: Request):
        u = need_user(request, allow_blocked=True)
        return {"licenses": [lic_view(x, L.clock()) for x in L.for_user(u["sub"])]}

    @app.get("/download", response_class=HTMLResponse)
    def download(request: Request):
        u = current(request)
        mine_ = [lic_view(x, L.clock()) for x in L.for_user(u["sub"])] if u else []
        return page("download.html", request, release=L.get_release("stable"), mine=mine_)

    # ----- admin -----
    @app.get("/api/admin/desktop/keys")
    def admin_keys(request: Request, search: str = ""):
        need_admin(request)
        t = L.clock()
        return {"keys": [lic_view(x, t, admin=True) for x in L.list(search)], "ready": L.ready, "mail_ready": mail.ready,
                "offline_hours": L.offline_s / 3600}

    @app.post("/api/admin/desktop/keys")
    def admin_make_keys(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        try:
            keys = L.create_keys(body.get("count", 1), body.get("days", 30), str(body.get("plan", "")),
                                 str(body.get("note", "")), str(body.get("email", "")), a["email"])
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        db.audit(a["email"], "desktop_keys_created", "", f"{len(keys)} × {body.get('days')} days {body.get('email', '')}".strip())
        return {"keys": keys}

    @app.post("/api/admin/desktop/key")
    def admin_key(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        key, action = clean_key(body.get("key", "")), str(body.get("action", ""))
        try:
            lic = L.admin_action(key, action, float(body.get("days", 0) or 0))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        db.audit(a["email"], f"desktop_key_{action}", key, body.get("days", ""))
        return {"license": lic_view(lic, L.clock(), admin=True)}

    @app.get("/api/admin/desktop/release")
    def admin_release(request: Request):
        need_admin(request)
        return {c: L.get_release(c) for c in CHANNELS} | {"pubkey_set": bool(release_pubkey)}

    @app.post("/api/admin/desktop/release")
    def admin_set_release(request: Request, body: dict = Body(...)):
        a = need_admin(request)
        m = body.get("manifest", body)
        try:
            check_manifest(m)
            if release_pubkey and not verify_manifest(m, release_pubkey):
                raise ValueError("The manifest's signature doesn't match DESKTOP_RELEASE_PUBKEY.")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        L.set_release(m["channel"], m)
        db.audit(a["email"], "desktop_release", m["channel"], m["version"])
        return {"ok": True, "channel": m["channel"], "version": m["version"]}

    return L
