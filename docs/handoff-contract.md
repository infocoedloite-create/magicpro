# Website ↔ GPU handoff contract

The website (Cloud Run, `https://magicpro.nacgenai.in`) owns accounts, Google sign-in, Razorpay
payments and hour balances. GPU nodes (Vast.ai) only run the live swap. This is the whole
interface between the two. Both sides share one secret, `MAGIC_SESSION_SECRET` (at least 32
random bytes, base64url): on Cloud Run from Secret Manager (`magicpro-session-secret`), on a GPU
node as an environment variable of the `magicpro` service.

## 1. Session token (website → browser → GPU)

A standard JWT, **HS256**, signed with `MAGIC_SESSION_SECRET`.

| Claim | Value |
|---|---|
| `iss` | `"magicpro-web"` |
| `aud` | `"magicpro-gpu"` |
| `sub` | stable user id (Google account `sub`) |
| `email`, `name` | for display only |
| `role` | `"user"` or `"admin"` |
| `sid` | id of this GPU session (uuid4 hex), one per "Start" |
| `iat` | issue time (unix seconds) |
| `exp` | session end: `iat + min(balance_seconds, 4 h)` |
| `bal` | balance in seconds when issued (for the countdown) |
| `usage_url` | absolute URL for usage reports, e.g. `https://magicpro.nacgenai.in/api/gpu/usage` |
| `web_url` | where to send the user back, e.g. `https://magicpro.nacgenai.in/app` |

## 2. Handoff

1. User presses **Start** on the website. The website checks the balance (> 0), makes sure a GPU
   node is ready (launcher/pool), mints the token and redirects the browser to
   `{gpu_base}/session?t={jwt}`.
2. The GPU node verifies signature, `aud`, `exp`, sets an `HttpOnly; Secure; SameSite=Lax` cookie
   `mp_session` = the JWT and redirects to `/`.
3. Every GPU route except `/session`, `/healthz` and `/static/*` requires a valid `mp_session`
   cookie, WebSockets included. Invalid or expired → HTTP 401 with a link to `web_url`.
4. Each user's face library and settings live in a separate folder on the node
   (`data/users/{sha256(sub)[:16]}/`); users never see each other's data.
5. The GPU node is reached directly on the app port (`127.0.0.1:17860`) through its HTTPS tunnel,
   not through the Vast Caddy token: the app enforces the session itself.

## 3. Usage reports (GPU → website)

While a session has at least one live stream, the GPU node sends every 30 s, and once when the
stream ends:

```
POST {usage_url}
Content-Type: application/json
X-Magic-Signature: hex(hmac_sha256(MAGIC_SESSION_SECRET, raw_request_body))

{"sid": "...", "sub": "...", "total_seconds": 1234.5, "streaming": true, "ts": 1764000000}
```

`total_seconds` is the **cumulative** streamed time of that `sid`. The website charges only the
increase since the last report it accepted for that `sid` (idempotent, survives lost or repeated
reports) and answers:

```
{"ok": true, "remaining": 5400, "stop": false}
```

`stop: true` (or `remaining <= 0`) → the GPU node ends that session's streams and shows
"Your hours are used up" with a link to `web_url`. If the website can't be reached, the node
keeps streaming until the token's `exp`, then stops.

## 4. Session end

At `exp` the node closes the streams; the page offers **Continue** (back to `web_url`, which mints
a new token while the balance lasts).
