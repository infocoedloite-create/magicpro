"""Sign-in with Google (OIDC authorization code flow), the site's session cookie, and the GPU
session token (HS256 JWT, docs/handoff-contract.md).

Key note for both sides of the contract: MAGIC_SESSION_SECRET is used as the HMAC key exactly as
the string is stored (its UTF-8 bytes), not base64-decoded, both for the JWT and for the usage
report signature. That is what PyJWT does with a str key.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------- HS256 JWT (contract section 1) ----------------
def jwt_encode(claims: dict, secret: str) -> str:
    head = b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64u(sig)}"


def jwt_decode(token: str, secret: str, aud: str | None = None, now: float | None = None) -> dict:
    """For tests and tools; the GPU node has its own verifier."""
    head, body, sig = token.split(".")
    want = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(want, b64u_dec(sig)):
        raise ValueError("bad signature")
    if json.loads(b64u_dec(head)).get("alg") != "HS256":
        raise ValueError("bad alg")
    claims = json.loads(b64u_dec(body))
    if aud and claims.get("aud") != aud:
        raise ValueError("bad aud")
    if claims.get("exp", 0) < (now or time.time()):
        raise ValueError("expired")
    return claims


def sign_body(secret: str, raw: bytes) -> str:
    """X-Magic-Signature of a usage report (contract section 3)."""
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


# ---------------- signed cookies ----------------
class Signer:
    def __init__(self, secret: str, purpose: str):
        self.key = hmac.new(secret.encode(), f"magicpro-web|{purpose}".encode(), hashlib.sha256).digest()

    def dumps(self, data: dict, max_age: int) -> str:
        body = b64u(json.dumps({**data, "_exp": int(time.time()) + max_age}, separators=(",", ":")).encode())
        return f"{body}.{b64u(hmac.new(self.key, body.encode(), hashlib.sha256).digest())}"

    def loads(self, value: str | None) -> dict | None:
        try:
            body, sig = (value or "").split(".", 1)
            if not hmac.compare_digest(b64u(hmac.new(self.key, body.encode(), hashlib.sha256).digest()), sig):
                return None
            data = json.loads(b64u_dec(body))
            return data if data.get("_exp", 0) > time.time() else None
        except (ValueError, TypeError):
            return None


# ---------------- Google OIDC ----------------
class Google:
    """Two modes, chosen by whether a client secret is configured:
    * code flow (secret set): the code is exchanged at Google's token endpoint;
    * secret-free (no secret, or the 'not-configured' placeholder): Google posts the ID token back
      (response_type=id_token, response_mode=form_post) and its signature is checked by Google's
      tokeninfo endpoint. Needs only the public client id, so no secret is ever handled."""
    AUTH = os.environ.get("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
    TOKEN = os.environ.get("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
    TOKENINFO = os.environ.get("GOOGLE_TOKENINFO_URL", "https://oauth2.googleapis.com/tokeninfo")
    ISSUERS = ("https://accounts.google.com", "accounts.google.com")

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, transport=None):
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.transport = transport          # tests pass an httpx.MockTransport
        self.secretless = not client_secret or client_secret.strip() in ("not-configured", "")

    def start(self) -> tuple[str, dict]:
        """-> (URL to send the browser to, state to keep in a short signed cookie)"""
        st = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24)}
        q = {"client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
             "scope": "openid email profile", "state": st["state"], "nonce": st["nonce"],
             "prompt": "select_account"}
        if self.secretless:
            q.update(response_type="id_token", response_mode="form_post")
        return f"{self.AUTH}?{urlencode(q)}", st

    async def finish_id_token(self, id_token: str, nonce: str) -> dict:
        """Secret-free mode: Google's tokeninfo endpoint verifies the ID token's signature and
        expiry; issuer, audience, nonce and e-mail verification are checked here."""
        if not id_token:
            raise ValueError("Google sent no ID token.")
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as c:
            r = await c.get(self.TOKENINFO, params={"id_token": id_token})
        if r.status_code != 200:
            raise ValueError("Google did not accept the ID token.")
        return self._check(r.json(), nonce)

    def _check(self, claims: dict, nonce: str) -> dict:
        if claims.get("iss") not in self.ISSUERS or claims.get("aud") != self.client_id:
            raise ValueError("ID token is not for this site.")
        if float(claims.get("exp", 0)) < time.time() or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError("ID token expired or replayed.")
        if not claims.get("email") or claims.get("email_verified") not in (True, "true"):
            raise ValueError("Your Google account's e-mail address is not verified.")
        return {"sub": str(claims["sub"]), "email": claims["email"].lower(), "name": claims.get("name") or claims["email"]}

    async def finish(self, code: str, nonce: str) -> dict:
        """Exchange the code; check the ID token's claims. -> {sub, email, name}.
        The ID token comes straight from Google's token endpoint over TLS, which OIDC Core 3.1.3.7
        accepts in place of checking its signature; issuer, audience, expiry and nonce are checked."""
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as c:
            r = await c.post(self.TOKEN, data={"code": code, "client_id": self.client_id,
                                               "client_secret": self.client_secret,
                                               "redirect_uri": self.redirect_uri, "grant_type": "authorization_code"})
        if r.status_code != 200:
            raise ValueError(f"Google sign-in failed ({r.status_code}).")
        idt = r.json().get("id_token", "")
        try:
            claims = json.loads(b64u_dec(idt.split(".")[1]))
        except (IndexError, ValueError) as e:
            raise ValueError("Google sent no usable ID token.") from e
        return self._check(claims, nonce)
