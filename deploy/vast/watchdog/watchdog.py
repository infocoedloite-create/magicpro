#!/usr/bin/env python3
"""Idle watchdog for the Magic Pro GPU instance (runs under supervisor next to `magicpro`).

Stops this Vast.ai instance (GPU billing stops; storage is still billed) when:
  * grace:     nobody has streamed for GRACE seconds (default 600). A user returning cancels it.
  * dead-man:  nobody has streamed AND the launcher has not been in contact for DEADMAN seconds
               (default 900). This holds even with GRACE=0 (grace off, launcher-managed), so a
               bug anywhere else can never leave the GPU billing all night.
  * request:   someone pressed "Stop GPU now" (the app writes STATE_DIR/stop-request).

"Streaming" comes from the app: GET /api/gpu/status -> active_users (socket open AND heartbeat
fresh), falling back to /api/stats -> browser_clients on an older app. If the app does not
answer, nobody can be streaming, so that counts as idle too.

Maintenance hold: `touch STATE_DIR/hold` keeps the GPU on for HOLD_MAX seconds after the file's
mtime (default 4 h), e.g. while benchmarking over SSH. Delete the file to release it.

DRY_RUN=1 (the default) only logs "would stop". Set MAGIC_WATCHDOG_DRY_RUN=0 to stop for real.
Standard library only, so it runs on the system python3.
"""
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def env(name, default):
    return os.environ.get(name, default)


APP = env("MAGIC_APP_URL", "http://127.0.0.1:17860")
STATE_DIR = Path(env("MAGIC_GPU_STATE_DIR", "/tmp/magicpro-gpu"))
GRACE = float(env("MAGIC_IDLE_GRACE", "600"))          # 0 = off (dead-man still applies)
DEADMAN = float(env("MAGIC_DEADMAN", "900"))
HOLD_MAX = float(env("MAGIC_HOLD_MAX", str(4 * 3600)))
POLL = float(env("MAGIC_WATCHDOG_POLL", "5"))
DRY_RUN = env("MAGIC_WATCHDOG_DRY_RUN", "1") != "0"
VAST_URL = env("VAST_URL", "https://console.vast.ai").rstrip("/")
INSTANCE_ID = env("CONTAINER_ID", "")
API_KEY = env("CONTAINER_API_KEY", "")                  # instance-scoped key Vast puts in the container


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def read_app():
    """(active_users, control_contact_age_s or None, reachable)"""
    try:
        s = get_json(f"{APP}/api/gpu/status")
        return int(s.get("active_users", 0)), s.get("control_contact_age_s"), True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return 0, None, False
    except (OSError, ValueError):
        return 0, None, False
    try:   # app without the gpucontrol module
        return int(get_json(f"{APP}/api/stats").get("browser_clients", 0)), None, True
    except (OSError, ValueError):
        return 0, None, False


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
        self.last_users = None
        self.last_log_state = None

    def tick(self, users, contact_age, stop_requested, hold):
        """Returns (reason to stop or None, status dict). Pure apart from the clock."""
        now = self.clock()
        if contact_age is not None:
            self.last_contact = max(self.last_contact, now - float(contact_age))
        if users > 0:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now
        idle = now - self.idle_since if self.idle_since is not None else 0.0
        silent = now - self.last_contact

        reason = None
        if stop_requested:
            reason = "Stop GPU now pressed"
        elif hold > 0:
            reason = None
        elif users == 0 and GRACE > 0 and idle >= GRACE:
            reason = f"nobody streaming for {idle:.0f}s (grace {GRACE:.0f}s)"
        elif users == 0 and idle >= DEADMAN and silent >= DEADMAN:
            reason = f"dead-man: no streams and no launcher contact for {min(idle, silent):.0f}s"

        stop_at = None
        if users == 0 and hold <= 0:
            cands = [max(self.idle_since, self.last_contact) + DEADMAN]
            if GRACE > 0:
                cands.append(self.idle_since + GRACE)
            stop_at = min(cands)
        status = {
            "updated": now, "dry_run": DRY_RUN, "users": users, "idle_s": round(idle),
            "grace_s": GRACE, "deadman_s": DEADMAN, "contact_age_s": round(silent),
            "hold_s": round(hold), "stop_in_s": round(max(0.0, stop_at - now)) if stop_at else None,
            "stopping": self.stopping_since is not None,
        }
        return reason, status


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log(f"watchdog up: instance={INSTANCE_ID or '?'} grace={GRACE:.0f}s deadman={DEADMAN:.0f}s "
        f"dry_run={DRY_RUN} key={'yes' if API_KEY else 'NO'} app={APP}")
    wd = Watchdog()
    req_file = STATE_DIR / "stop-request"
    next_attempt = 0.0
    while True:
        users, contact_age, reachable = read_app()
        requested = req_file.exists()
        now = time.time()
        reason, status = wd.tick(users, contact_age, requested, hold_left(now))
        status["app_reachable"] = reachable
        summary = (users, reachable, reason is not None, status["hold_s"] > 0)
        if summary != wd.last_log_state:
            log(f"users={users} app={'up' if reachable else 'DOWN'} idle={status['idle_s']}s "
                f"stop_in={status['stop_in_s']}s hold={status['hold_s']}s")
            wd.last_log_state = summary
        if reason and now >= next_attempt:
            if stop_instance(reason):
                wd.stopping_since = wd.stopping_since or now
                req_file.unlink(missing_ok=True)
                next_attempt = now if DRY_RUN else now + 300   # still alive 5 min after a stop: ask again
                if DRY_RUN:                             # pretend it happened: restart the clocks
                    wd.idle_since = now if users == 0 else None
                    wd.last_contact = now
            else:
                next_attempt = now + 60                 # retry a failed stop once a minute
        write_state(status)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
