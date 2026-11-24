"""Hosted mode: sessions handed over by the website (docs/handoff-contract.md).

Enabled when MAGIC_SESSION_SECRET is set. Then every route except /session, /healthz and
/static/* needs the `mp_session` cookie holding a valid HS256 JWT from the website, each user
works in their own space, and streaming time is reported to the website, which can end it.
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request

SECRET = os.environ.get("MAGIC_SESSION_SECRET", "")
ENABLED = bool(SECRET)
WEB_URL = os.environ.get("MAGIC_WEB_URL", "https://magicpro.nacgenai.in/app")
COOKIE = "mp_session"
REPORT_EVERY = float(os.environ.get("MAGIC_USAGE_EVERY", "30"))


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign_jwt(claims, secret=None):
    """For tests and tools; the website mints the real tokens."""
    key = (secret or SECRET).encode()
    head = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64e(json.dumps(claims, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def verify(token, now=None):
    """-> claims dict, or None if the token is missing, forged, for another audience or expired."""
    if not ENABLED or not token or token.count(".") != 2:
        return None
    head, body, sig = token.split(".")
    try:
        if json.loads(_b64d(head)).get("alg") != "HS256":
            return None
        good = _b64e(hmac.new(SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        c = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if c.get("aud") != "magicpro-gpu" or c.get("iss") != "magicpro-web" or not c.get("sub") or not c.get("sid"):
        return None
    if float(c.get("exp", 0)) <= (now or time.time()):
        return None
    return c


def user_key(claims):
    return hashlib.sha256(str(claims["sub"]).encode()).hexdigest()[:16]


class Sessions:
    """Streaming time per session id, reported to the website (cumulative, signed)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_sid = {}
        threading.Thread(target=self._loop, name="usage", daemon=True).start()

    def open(self, claims):
        with self.lock:
            s = self.by_sid.get(claims["sid"])
            if s is None:
                s = self.by_sid[claims["sid"]] = {
                    "claims": claims, "streams": 0, "total": 0.0, "last_tick": time.time(),
                    "last_frame": 0.0, "last_report": 0.0, "stop": None, "remaining": None}
            s["claims"] = claims
            s["streams"] += 1
            s["last_tick"] = time.time()
            return s

    def close(self, claims):
        report = None
        with self.lock:
            s = self.by_sid.get(claims["sid"])
            if s:
                self._tick(s, time.time())
                s["streams"] = max(0, s["streams"] - 1)
                if s["streams"] == 0:
                    report = s
        if report:
            self._report(report, streaming=False)

    def stop_reason(self, claims):
        """Why this session must end now, or None."""
        s = self.by_sid.get(claims["sid"])
        if s and s["stop"]:
            return s["stop"]
        if time.time() >= float(claims["exp"]):
            return "Your session time is over."
        return None

    def remaining(self, claims):
        """Seconds left: the website's last answer, else the token's balance minus what was used."""
        s = self.by_sid.get(claims["sid"])
        used = s["total"] if s else 0.0
        left_exp = float(claims["exp"]) - time.time()
        left_bal = float(s["remaining"]) if s and s["remaining"] is not None else float(claims.get("bal", left_exp)) - used
        return max(0.0, min(left_exp, left_bal))

    def touch(self, claims):
        """A video frame arrived for this session (called per frame: a dict lookup and a store)."""
        s = self.by_sid.get(claims["sid"])
        if s:
            s["last_frame"] = time.time()

    @staticmethod
    def _tick(s, now):
        # charge only while video is actually flowing: a connected but idle or stalled stream
        # (camera paused, tab in the background) costs nothing
        if s["streams"] > 0 and now - s["last_frame"] < 2.0:
            s["total"] += now - s["last_tick"]
        s["last_tick"] = now

    def _loop(self):
        while True:
            time.sleep(1.0)
            now = time.time()
            due = []
            with self.lock:
                for sid, s in list(self.by_sid.items()):
                    self._tick(s, now)
                    if s["streams"] > 0 and now - s["last_report"] >= REPORT_EVERY:
                        due.append(s)
                    if s["streams"] == 0 and now - s["last_tick"] > 6 * 3600:
                        del self.by_sid[sid]
            for s in due:
                self._report(s, streaming=True)

    def _report(self, s, streaming):
        c = s["claims"]
        s["last_report"] = time.time()
        url = c.get("usage_url")
        if not url:
            return
        body = json.dumps({"sid": c["sid"], "sub": c["sub"], "total_seconds": round(s["total"], 1),
                           "streaming": streaming, "ts": int(time.time())}, separators=(",", ":")).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "X-Magic-Signature": sig})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                ans = json.loads(r.read() or b"{}")
            if "remaining" in ans:
                s["remaining"] = float(ans["remaining"])
            if ans.get("stop") or (s["remaining"] is not None and s["remaining"] <= 0):
                s["stop"] = "Your hours are used up."
        except Exception as e:  # noqa: BLE001  website unreachable: keep going until the token's exp
            print(f"[usage] report failed for {c['sid'][:8]}: {e}", flush=True)


sessions = Sessions() if ENABLED else None
