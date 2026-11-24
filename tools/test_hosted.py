"""End-to-end test of hosted mode (docs/handoff-contract.md) against a running GPU app.

Start the app with MAGIC_SESSION_SECRET=<secret> MAGIC_USAGE_EVERY=3 and MAGIC_DATA=<empty dir>,
then:  python tools/test_hosted.py --base http://127.0.0.1:17861 --secret <secret> --face face.jpg
A fake website on 127.0.0.1:18999 receives and checks the usage reports.
"""
import argparse
import asyncio
import hashlib
import hmac
import http.server
import json
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.auth import sign_jwt  # noqa: E402

REPORTS, STATE = [], {"stop_after": None}
FAILS = []


def check(cond, what):
    print(("PASS  " if cond else "FAIL  ") + what, flush=True)
    if not cond:
        FAILS.append(what)


class Web(http.server.BaseHTTPRequestHandler):
    secret = b""

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        good = hmac.compare_digest(hmac.new(self.secret, body, hashlib.sha256).hexdigest(),
                                   self.headers.get("X-Magic-Signature", ""))
        rep = json.loads(body)
        rep["sig_ok"] = good
        REPORTS.append(rep)
        stop = STATE["stop_after"] is not None and len(REPORTS) >= STATE["stop_after"]
        ans = {"ok": True, "remaining": 0 if stop and not STATE.get("reason") else 3600, "stop": stop}
        if stop and STATE.get("reason"):
            ans["reason"] = STATE["reason"]
        out = json.dumps(ans).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def token(secret, sub, exp_in=3600, **extra):
    now = int(time.time())
    return sign_jwt({"iss": "magicpro-web", "aud": "magicpro-gpu", "sub": sub, "email": f"{sub}@test", "name": sub,
                     "role": "user", "sid": uuid.uuid4().hex, "iat": now, "exp": now + exp_in, "bal": 3600,
                     "usage_url": "http://127.0.0.1:18999/api/gpu/usage", "web_url": "https://example.test/app", **extra},
                    secret)


def req(base, path, cookie=None, method="GET", data=None, headers=None):
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = f"mp_session={cookie}"
    r = urllib.request.Request(base + path, data=data, headers=h, method=method)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(r, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def multipart(fields, file_field, filename, content):
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f'Content-Type: image/jpeg\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={b}"}


async def stream(base, cookie, seconds, packets, H=struct.Struct("<BBI")):
    import websockets
    url = base.replace("http", "ws") + "/ws/h264"
    got, end_msg, code = 0, None, None
    try:
        async with websockets.connect(url, additional_headers={"Cookie": f"mp_session={cookie}"} if cookie else {},
                                      max_size=None) as ws:
            t0, i = time.time(), 0

            async def send():
                nonlocal i
                while time.time() - t0 < seconds:
                    key, data = packets[i % len(packets)]
                    i += 1
                    await ws.send(H.pack(1, 1 if key else 0, i) + data)
                    await asyncio.sleep(1 / 30)

            async def recv():
                nonlocal got, end_msg
                while True:
                    m = await ws.recv()
                    if isinstance(m, str):
                        d = json.loads(m)
                        if d.get("t") == "end":
                            end_msg = d
                    else:
                        got += 1
            tasks = [asyncio.create_task(send()), asyncio.create_task(recv())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION, timeout=seconds + 5)
            for t in pending:
                t.cancel()
            code = ws.close_code
    except websockets.exceptions.ConnectionClosed as e:
        code = e.rcvd.code if e.rcvd else None
    except websockets.exceptions.InvalidStatus as e:
        code = e.response.status_code
    return got, end_msg, code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:17861")
    ap.add_argument("--secret", required=True)
    ap.add_argument("--face", required=True)
    a = ap.parse_args()
    Web.secret = a.secret.encode()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 18999), Web)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    B = a.base

    st, _, _ = req(B, "/")
    check(st == 401, "page without a session is refused (401)")
    st, _, body = req(B, "/api/state")
    check(st == 401 and b"web_url" in body, "API without a session is refused with the website link")
    st, _, _ = req(B, "/session?t=" + token("forged-secret-xxxxxxxxxxxxxxxxxxxxxxxx", "alice"))
    check(st == 401, "link signed with another secret is rejected")
    st, _, _ = req(B, "/session?t=" + token(a.secret, "alice", exp_in=-10))
    check(st == 401, "expired link is rejected")
    st, _, _ = req(B, "/session?t=" + token(a.secret, "alice", aud="someone-else"))
    check(st == 401, "link for another audience is rejected")

    ta, tb = token(a.secret, "alice"), token(a.secret, "bob")
    st, hdr, _ = req(B, "/session?t=" + ta)
    check(st == 303 and "mp_session=" in hdr.get("set-cookie", hdr.get("Set-Cookie", "")) and "HttpOnly" in str(hdr),
          "valid link sets an HttpOnly session cookie and opens the app")
    st, _, body = req(B, "/api/me", ta)
    check(st == 200 and json.loads(body).get("name") == "alice", "/api/me shows the signed-in user")

    data, h = multipart({"name": "alice face", "consent": "true"}, "file", "face.jpg", Path(a.face).read_bytes())
    st, _, body = req(B, "/api/library", ta, "POST", data, h)
    item = json.loads(body) if st == 200 else {}
    check(st == 200 and item.get("id"), "alice uploads a face to her library")
    st, _, body = req(B, "/api/library", ta)
    check(len(json.loads(body)) == 1, "alice sees 1 face")
    st, _, body = req(B, "/api/library", tb)
    check(st == 200 and json.loads(body) == [], "bob sees an empty library (isolation)")
    st, _, _ = req(B, f"/library-files/{item.get('id')}_thumb.jpg", tb)
    check(st == 404, "bob cannot load alice's thumbnail")
    st, _, _ = req(B, f"/library-files/{item.get('id')}_thumb.jpg", ta)
    check(st == 200, "alice loads her own thumbnail")
    st, _, _ = req(B, "/library-files/..%2Fsettings.json", ta)
    check(st == 404, "path tricks on library files are refused")

    req(B, "/api/settings", ta, "POST", json.dumps({"mask_blur": 0.55, "source_ids": [item.get("id")],
                                                    "disclosure_label": False}).encode(), {"Content-Type": "application/json"})
    sa = json.loads(req(B, "/api/state", ta)[2])["settings"]
    sb = json.loads(req(B, "/api/state", tb)[2])["settings"]
    check(sa["mask_blur"] == 0.55 and sb["mask_blur"] == 0.3, "settings are per user")
    check(sa["disclosure_label"] is True, "hosted users cannot switch the AI label off")
    for p in ("/video.mjpg", "/snapshot.jpg", "/api/cameras"):
        check(req(B, p, ta)[0] == 403, f"{p} is blocked in hosted mode (would show others' video)")

    st, _, _ = req(B, "/api/gpu/status")
    check(st == 401, "GPU status without a session or watchdog signature is refused")
    wd = hmac.new(a.secret.encode(), b"watchdog", hashlib.sha256).hexdigest()
    st, _, body = req(B, "/api/gpu/status", headers={"X-Magic-Watchdog": wd})
    check(st == 200 and "active_users" in json.loads(body), "the node watchdog reads GPU status with its signature")
    st, _, _ = req(B, "/api/gpu/status", headers={"X-Magic-Watchdog": "0" * 64})
    check(st == 401, "a wrong watchdog signature is refused")
    st, _, _ = req(B, "/api/gpu/stop", ta, "POST", b'{"cid":"x"}', {"Content-Type": "application/json"})
    check(st == 403, "a customer cannot stop the GPU for everyone")
    st, _, body = req(B, "/api/gpu/heartbeat", ta, "POST", b'{"cid":"t1","streaming":false}', {"Content-Type": "application/json"})
    check(st == 200, "page heartbeats work with a session")

    from tools.loadtest import make_clip
    packets = make_clip(a.face, 1280, 720, 30)
    got, end_msg, code = asyncio.run(stream(B, None, 3, packets))
    check(got == 0 and code in (4401, 403), f"video stream without a session is refused (code {code})")

    REPORTS.clear()
    got, end_msg, code = asyncio.run(stream(B, ta, 12, packets))
    time.sleep(2)
    check(got > 100, f"alice's stream returns swapped video ({got} frames in 12 s)")
    tot = [r["total_seconds"] for r in REPORTS]
    check(len(REPORTS) >= 3, f"usage reported during the stream ({len(REPORTS)} reports)")
    check(all(r["sig_ok"] for r in REPORTS), "every usage report carries a valid signature")
    check(tot == sorted(tot) and 10 <= tot[-1] <= 15, f"cumulative streamed time grows and ends near 12 s ({tot})")
    check(REPORTS and REPORTS[-1]["streaming"] is False, "a final report is sent when the stream ends")

    REPORTS.clear()
    STATE["stop_after"] = 2
    t0 = time.time()
    got, end_msg, code = asyncio.run(stream(B, ta, 30, packets))
    check(end_msg is not None and "used up" in end_msg.get("reason", ""), f"website 'stop' ends the stream with a message ({end_msg})")
    check(code == 4402 and time.time() - t0 < 20, f"stream closed by the server ({code}) after {time.time() - t0:.0f} s")
    STATE["stop_after"] = None

    # the website's reason reaches the page, and a stop does not stick to the session: when the
    # website says ok again (a newer Start that was abandoned), the same session streams again
    REPORTS.clear()
    STATE.update(stop_after=1, reason="replaced")
    got, end_msg, code = asyncio.run(stream(B, ta, 15, packets))
    check(end_msg is not None and "another tab" in end_msg.get("reason", ""), f"'replaced' tells the user why ({end_msg})")
    STATE.update(stop_after=None, reason=None)
    got, end_msg, code = asyncio.run(stream(B, ta, 8, packets))
    check(end_msg is None and got > 100, f"the same session streams again once the website says ok ({got} frames, end={end_msg})")

    short = token(a.secret, "carol", exp_in=8)
    got, end_msg, code = asyncio.run(stream(B, short, 20, packets))
    check(end_msg is not None and code == 4402, f"session past its expiry is ended ({end_msg})")

    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
