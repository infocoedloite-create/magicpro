"""App side of GPU on/off (app/gpucontrol.py) with a fake engine: python deploy/vast/watchdog/test_gpucontrol.py"""
import os
import sys
import tempfile
import time

os.environ["MAGIC_GPU_STATE_DIR"] = tempfile.mkdtemp(prefix="magicpro-gpu-test-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import gpucontrol as g  # noqa: E402


class FakeEngine:
    browser_clients = 0

    def cluster_stats(self):
        return {"browser_clients": self.browser_clients, "status": "live"}

    def settings_dict(self):
        return {"fps": 30, "resolution": "1080p", "enhancer": "gfpgan_1.4", "swapper": "inswapper_128_fp16"}


e = FakeEngine()
a = FastAPI()
g.attach(a, e)
c = TestClient(a)
# cost model: enhanced 1080p30 = 1.0, plain = 0.5, 60 fps doubles, presets multiply the swap part
assert g.stream_cost({"fps": 30, "enhancer": "x"}) == 1.0 and g.stream_cost({"fps": 30}) == 0.5
assert g.stream_cost({"fps": 60}) == 1.0 and g.stream_cost({"fps": 60, "enhancer": "x"}) == 2.0
assert g.stream_cost({"fps": 30, "swapper": "inswapper_512"}) == 2.5
# a user counts while socket open AND heartbeat fresh
e.browser_clients = 2
c.post("/api/gpu/heartbeat", json={"cid": "a", "streaming": True})
s = c.get("/api/gpu/status").json()
assert s["active_users"] == 1 and s["load"] == 1.0 and s["budget"] == 4.0, s
c.post("/api/gpu/heartbeat", json={"cid": "b", "streaming": True})
assert c.get("/api/gpu/status").json()["active_users"] == 2
assert c.post("/api/gpu/stop", json={"cid": "a"}).status_code == 409          # b still streaming
f = g.BEATS / "b.json"
os.utime(f, (time.time() - 20, time.time() - 20))                             # b's Wi-Fi dropped
assert c.get("/api/gpu/status").json()["active_users"] == 1
assert c.post("/api/gpu/stop", json={"cid": "a"}).json()["ok"] and (g.STATE_DIR / "stop-request").exists()
c.post("/api/gpu/heartbeat", json={"cid": "../../x", "streaming": True})
assert not (g.STATE_DIR / "x.json").exists()                                  # ids are sanitised
print("gpucontrol: all ok")
