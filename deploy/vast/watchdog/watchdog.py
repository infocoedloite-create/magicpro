#!/usr/bin/env python3
"""Node watchdog for a Magic Pro GPU instance (runs under supervisor next to `magicpro`).

Stops this Vast.ai instance (GPU billing stops; storage is still billed) when:
  * grace:     nobody has streamed for GRACE seconds (default 600). A user returning cancels it.
  * dead-man:  nobody has streamed AND the launcher has not been in contact for DEADMAN seconds
               (default 900). This holds even with GRACE=0, so a bug anywhere else can never
               leave the GPU billing all night.
  * request:   someone pressed "Stop GPU now" (the app writes STATE_DIR/stop-request).

"Streaming" comes from the app: GET /api/gpu/status -> active_users (socket open AND heartbeat
fresh), falling back to /api/stats -> browser_clients on an older app. If the app does not
answer, nobody can be streaming, so that counts as idle too.

Pool mode (MAGIC_LAUNCHER_URL and MAGIC_NODE_KEY set): every HEARTBEAT seconds the node tells
the launcher its tunnel URL, streams, load/budget and health, signed with MAGIC_NODE_KEY. A
successful reply is "launcher contact" for the dead-man rule. A reply with keep=true (users were
just sent here, or the pool wants this node warm) resets the idle clock like a stream would.
Just before stopping, the node reports stopping=true so the launcher stops routing to it.

Maintenance hold: `touch STATE_DIR/hold` keeps the GPU on for HOLD_MAX seconds after the file's
mtime (default 4 h), e.g. while benchmarking over SSH. Delete the file to release it.

DRY_RUN=1 (the default) only logs "would stop". Set MAGIC_WATCHDOG_DRY_RUN=0 to stop for real.
Standard library only, so it runs on the system python3.
"""
import hashlib
import hmac
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def env(name, default):
    return os.environ.get(name, default)


APP = env("MAGIC_APP_URL", "http://127.0.0.1:17860")
PORTAL = env("MAGIC_PORTAL_URL", "http://localhost:11111")
EXTERNAL_PORT = env("MAGIC_EXTERNAL_PORT", "10100")
STATE_DIR = Path(env("MAGIC_GPU_STATE_DIR", "/tmp/magicpro-gpu"))
GRACE = float(env("MAGIC_IDLE_GRACE", "600"))          # 0 = off (dead-man still applies)
DEADMAN = float(env("MAGIC_DEADMAN", "900"))
HOLD_MAX = float(env("MAGIC_HOLD_MAX", str(4 * 3600)))
POLL = float(env("MAGIC_WATCHDOG_POLL", "5"))
HEARTBEAT = float(env("MAGIC_NODE_HEARTBEAT", "10"))
KEEP_FOR = 30.0                                          # a keep=true reply holds this long
DRY_RUN = env("MAGIC_WATCHDOG_DRY_RUN", "1") != "0"
VAST_URL = env("VAST_URL", "https://console.vast.ai").rstrip("/")
INSTANCE_ID = env("CONTAINER_ID", "")
API_KEY = env("CONTAINER_API_KEY", "")                  # instance-scoped key Vast puts in the container
LAUNCHER = env("MAGIC_LAUNCHER_URL", "").rstrip("/")
NODE_KEY = env("MAGIC_NODE_KEY", "")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


SESSION_SECRET = env("MAGIC_SESSION_SECRET", "")
# Lets the watchdog read the app's /api/gpu/status although every app route needs a user session
# (docs/handoff-contract.md): X-Magic-Watchdog = hex hmac_sha256(MAGIC_SESSION_SECRET, "watchdog").
WATCHDOG_TOKEN = hmac.new(SESSION_SECRET.encode(), b"watchdog", hashlib.sha256).hexdigest() if SESSION_SECRET else ""


def get_json(url, timeout=3):
    req = urllib.request.Request(url, headers={"X-Magic-Watchdog": WATCHDOG_TOKEN} if WATCHDOG_TOKEN and url.startswith(APP) else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def read_app():
    """-> (status dict, reachable). status always has active_users."""
    try:
        return get_json(f"{APP}/api/gpu/status"), True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return {"active_users": 0}, False
    except (OSError, ValueError):
        return {"active_users": 0}, False
    try:   # app without the gpucontrol module
        return {"active_users": int(get_json(f"{APP}/api/stats").get("browser_clients", 0))}, True
    except (OSError, ValueError):
        return {"active_users": 0}, False


TUNNEL_DIRECT = env("MAGIC_TUNNEL_DIRECT", "0") == "1"   # handoff contract: tunnel straight to the app port
TUNNEL_TARGET = env("MAGIC_TUNNEL_TARGET", "http://localhost:17860")
_warned = {"open": False}


def _text(url, method="GET", timeout=10):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode().strip()
    try:
        v = json.loads(body)
    except ValueError:
        return body
    return v if isinstance(v, str) else (v.get("tunnelUrl") or v.get("url") or "") if isinstance(v, dict) else ""


def app_requires_session():
    """Only expose the app port publicly when it refuses requests without a session (HTTP 401)."""
    try:
        urllib.request.urlopen(f"{APP}/", timeout=3)
        return False
    except urllib.error.HTTPError as e:
        return e.code == 401
    except OSError:
        return False


def read_tunnel():
    if TUNNEL_DIRECT:
        if not app_requires_session():
            if not _warned["open"]:
                log("NOT opening a public tunnel to the app: it answers without a session (needs the session-auth build)")
                _warned["open"] = True
            return None
        _warned["open"] = False
        target = urllib.parse.quote(TUNNEL_TARGET, safe="")
        try:
            return _text(f"{PORTAL}/get-existing-quick-tunnel/{target}", timeout=5) or None
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return None
        except OSError:
            return None
        try:
            log(f"starting a quick tunnel to {TUNNEL_TARGET}")
            _text(f"{PORTAL}/start-quick-tunnel/{target}", method="POST", timeout=60)
            return _text(f"{PORTAL}/get-existing-quick-tunnel/{target}", timeout=5) or None
        except (OSError, ValueError):
            return None
    try:
        for t in get_json(f"{PORTAL}/get-all-quick-tunnels", timeout=5):
            if str(t.get("targetUrl", "")).rstrip("/").endswith(f":{EXTERNAL_PORT}"):
                return t.get("tunnelUrl")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def sign(key, node_id, ts, body):
    return hmac.new(key.encode(), f"{node_id}|{ts}|{hashlib.sha256(body).hexdigest()}".encode(),
                    hashlib.sha256).hexdigest()


def send_heartbeat(beat):
    """-> launcher reply dict, or None when it could not be reached / refused us."""
    if not (LAUNCHER and NODE_KEY and INSTANCE_ID):
        return None
    body = json.dumps(beat, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    req = urllib.request.Request(f"{LAUNCHER}/api/node/heartbeat", data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Node-Id": INSTANCE_ID, "X-Node-Time": ts,
        "X-Node-Sig": sign(NODE_KEY, INSTANCE_ID, ts, body)})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        log(f"launcher refused heartbeat: {e.code}")
    except (OSError, ValueError) as e:
        log(f"launcher unreachable: {e}")
    return None


def stop_instance(reason):
    if DRY_RUN:
        log(f"DRY RUN: would stop instance {INSTANCE_ID or '?'} now ({reason})")
        return True
    if not (INSTANCE_ID and API_KEY):
        log("cannot stop: CONTAINER_ID / CONTAINER_API_KEY not set")
        return False
    req = urllib.request.Request(
        f"{VAST_URL}/api/v0/instances/{INSTANCE_ID}/", method="PUT",
        data=json.dumps({"state": "stopped"}).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.load(r)
        ok = body.get("success", True)
        log(f"stop requested ({reason}): {'ok' if ok else body.get('msg') or body}")
        return bool(ok)
    except (OSError, ValueError) as e:
        log(f"stop request failed ({reason}): {e}")
        return False


def write_state(d):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_DIR / "watchdog.json.tmp"
        tmp.write_text(json.dumps(d))
        tmp.replace(STATE_DIR / "watchdog.json")
    except OSError as e:
        log(f"cannot write state: {e}")


def hold_left(now):
    try:
        return max(0.0, HOLD_MAX - (now - (STATE_DIR / "hold").stat().st_mtime))
    except OSError:
        return 0.0


class Watchdog:
    def __init__(self, clock=time.time):
        self.clock = clock
        now = clock()
        self.started = now
        self.idle_since = now          # a fresh boot gets a full grace period to be used
        self.last_contact = now        # boot counts as contact (the launcher just started us)
        self.stopping_since = None
        self.last_log_state = None

    def tick(self, users, contact_age, stop_requested, hold, keep=False):
        """Returns (reason to stop or None, status dict). Pure apart from the clock."""
        now = self.clock()
        if contact_age is not None:
            self.last_contact = max(self.last_contact, now - float(contact_age))
        if users > 0 or keep:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now
        idle = now - self.idle_since if self.idle_since is not None else 0.0
        silent = now - self.last_contact

        reason = None
        if stop_requested:
            reason = "Stop GPU now pressed"
        elif hold > 0 or users > 0 or keep:
            reason = None
        elif GRACE > 0 and idle >= GRACE:
            reason = f"nobody streaming for {idle:.0f}s (grace {GRACE:.0f}s)"
        elif idle >= DEADMAN and silent >= DEADMAN:
            reason = f"dead-man: no streams and no launcher contact for {min(idle, silent):.0f}s"

        stop_at = None
        if users == 0 and not keep and hold <= 0:
            cands = [max(self.idle_since, self.last_contact) + DEADMAN]
            if GRACE > 0:
                cands.append(self.idle_since + GRACE)
            stop_at = min(cands)
        status = {
            "updated": now, "dry_run": DRY_RUN, "users": users, "idle_s": round(idle),
            "grace_s": GRACE, "deadman_s": DEADMAN, "contact_age_s": round(silent),
            "hold_s": round(hold), "keep": keep, "stop_in_s": round(max(0.0, stop_at - now)) if stop_at else None,
            "stopping": self.stopping_since is not None,
        }
        return reason, status


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    pool = bool(LAUNCHER and NODE_KEY)
    log(f"watchdog up: instance={INSTANCE_ID or '?'} grace={GRACE:.0f}s deadman={DEADMAN:.0f}s "
        f"dry_run={DRY_RUN} key={'yes' if API_KEY else 'NO'} app={APP} "
        f"launcher={LAUNCHER if pool else 'none (single-GPU mode)'}")
    wd = Watchdog()
    req_file = STATE_DIR / "stop-request"
    next_attempt = next_beat = next_tunnel = 0.0
    keep_until, contact, tunnel = 0.0, None, None
    while True:
        app, reachable = read_app()
        users = int(app.get("active_users", 0))
        now = time.time()
        if now >= next_tunnel:
            tunnel, next_tunnel = read_tunnel(), now + (30 if tunnel else 5)
        requested = req_file.exists()
        reason, status = wd.tick(users, (now - contact) if contact else None, requested,
                                 hold_left(now), keep=now < keep_until)
        status["app_reachable"] = reachable
        status["stopping"] = status["stopping"] or bool(reason)
        if pool:
            status.update(launcher=LAUNCHER, node=INSTANCE_ID)
        beat = {"tunnel": tunnel, "users": users, "load": app.get("load", 0), "budget": app.get("budget"),
                "stream_cost": app.get("stream_cost"), "healthy": reachable and app.get("healthy", True),
                "error": app.get("error", ""), "fps": app.get("fps"), "dry_run": DRY_RUN,
                "idle_s": status["idle_s"], "stop_in_s": status["stop_in_s"], "stopping": bool(reason)}
        if pool and (now >= next_beat or reason):
            next_beat = now + HEARTBEAT
            reply = send_heartbeat(beat)
            if reply is not None:
                contact = time.time()
                if reply.get("keep"):
                    keep_until = contact + KEEP_FOR
        summary = (users, reachable, reason is not None, status["hold_s"] > 0, status["keep"])
        if summary != wd.last_log_state:
            log(f"users={users} app={'up' if reachable else 'DOWN'} idle={status['idle_s']}s "
                f"stop_in={status['stop_in_s']}s hold={status['hold_s']}s keep={status['keep']}")
            wd.last_log_state = summary
        if reason and now >= next_attempt:
            if stop_instance(reason):
                wd.stopping_since = wd.stopping_since or now
                req_file.unlink(missing_ok=True)
                next_attempt = now if DRY_RUN else now + 300   # still alive 5 min after a stop: ask again
                if DRY_RUN:                             # pretend it happened: restart the clocks
                    wd.idle_since = now if users == 0 else None
                    wd.last_contact = now
                    wd.stopping_since = None
            else:
                next_attempt = now + 60                 # retry a failed stop once a minute
        write_state(status)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
