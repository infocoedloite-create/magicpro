"""Pool autoscaler and launcher tests against a simulated Vast.ai (no network, no money).
Run: python deploy/launcher/test_pool.py   (website routes: test_web.py)"""
import asyncio
import json
import os
import sys
import time

os.environ.update(VAST_API_KEY="k", LAUNCHER_PASSWORD="pw", POOL_SECRET="s3cret", LAUNCHER_URL="https://launch.example")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool as P  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeVast:
    """Just enough of the Vast API. Calls are recorded; machines can be 'rented away'."""

    def __init__(self, clock):
        self.clock, self.inst, self.calls, self.credit_left = clock, {}, [], 9.84
        self.rented_away, self.next_id = set(), 900
        self.offer_list = [
            {"id": 11, "gpu_name": "RTX PRO 6000 WS", "dph_total": 1.05, "geolocation": "Mumbai, IN", "reliability": 0.99},
            {"id": 12, "gpu_name": "RTX PRO 6000 WS", "dph_total": 2.50, "geolocation": "Delhi, IN", "reliability": 0.99},
        ]

    def add(self, iid, state="stopped", label="magicpro-pool:n1", **kw):
        self.inst[str(iid)] = {"id": int(iid), "label": label, "intended_status": state,
                               "actual_status": "running" if state == "running" else "exited",
                               "jupyter_token": f"tok{iid}", "dph_total": 1.0, "gpu_name": "RTX PRO 6000 WS",
                               "reliability2": 0.99, **kw}

    async def instances(self):
        self.calls.append(("list",))
        return [dict(v) for v in self.inst.values()]

    async def set_state(self, iid, state):
        self.calls.append(("state", str(iid), state))
        i = self.inst[str(iid)]
        i["intended_status"] = state
        if state == "running":
            i["actual_status"] = "scheduling" if str(iid) in self.rented_away else "loading"
            i["status_msg"] = "scheduling: waiting for resources" if str(iid) in self.rented_away else ""
        else:
            i["actual_status"] = "exited"
        return {"success": True}

    async def set_label(self, iid, label):
        self.calls.append(("label", str(iid), label))
        self.inst[str(iid)]["label"] = label
        return {"success": True}

    async def offers(self, cfg):
        self.calls.append(("offers",))
        return list(self.offer_list)

    async def create(self, offer_id, body):
        self.calls.append(("create", offer_id, body))
        self.next_id += 1
        self.add(self.next_id, "running", label=body["label"], actual_status="loading")
        return {"success": True, "new_contract": self.next_id}

    async def credit(self):
        return self.credit_left

    def boot(self, iid):
        self.inst[str(iid)]["actual_status"] = "running"


def make(**cfg):
    c = Clock()
    v = FakeVast(c)
    conf = P.Config(**{"image": "reg/magicpro-node:v1", "launcher_url": "https://launch.example", "secret": "s3cret", **cfg})
    return c, v, P.Pool(v, conf, clock=c, log=lambda *_: None)


def beat(pool, iid, users=0, load=0.0, budget=4.0, cost=1.0, **kw):
    n = pool.nodes[str(iid)]
    return pool.heartbeat(n, {"tunnel": f"https://n{iid}.trycloudflare.com", "users": users, "load": load,
                              "budget": budget, "stream_cost": cost, "healthy": True, **kw})


def run(coro):
    return asyncio.run(coro)


async def scenario_first_user_restarts_stopped_node():
    c, v, pool = make()
    v.add(1)
    await pool.refresh(force=True)
    assert pool.assign("u1") is None                       # nothing running: user waits
    acts = await pool.tick()
    assert acts == [("start", "1")], acts                   # restart before create
    c.t += 60; v.boot(1); beat(pool, 1)
    n = pool.assign("u1")
    assert n and n.id == "1"


async def scenario_scale_up_early_and_route_to_most_free():
    c, v, pool = make()
    v.add(1, "running"); v.add(2)
    await pool.refresh(force=True); beat(pool, 1)
    for u in ("a", "b", "c"):
        assert pool.assign(u).id == "1"
    assert await pool.tick() == []                          # 1 unit still free: enough for the next user
    beat(pool, 1, users=3, load=3.0)
    assert pool.assign("d").id == "1"                       # uses the last unit ...
    acts = await pool.tick()
    assert acts == [("start", "2")], acts                   # ... so node 2 starts before anyone has to wait
    c.t += 100; v.boot(2); beat(pool, 1, users=4, load=4.0); beat(pool, 2, load=0.0)
    assert pool.assign("e").id == "2"
    # most free capacity wins
    c.t += 100; beat(pool, 1, users=1, load=1.0); beat(pool, 2, users=2, load=2.0)
    assert pool.assign("f").id == "1"


async def scenario_rented_gpu_then_create():
    c, v, pool = make()
    v.add(1); v.rented_away.add("1")
    await pool.refresh(force=True)
    pool.assign("u1")
    assert await pool.tick() == [("start", "1")]
    c.t += 80; await pool.refresh(force=True)
    assert pool.state(pool.nodes["1"]) == "unavailable"
    acts = await pool.tick()
    assert acts == [("stop", "1", "GPU rented by someone else")] and ("state", "1", "stopped") in v.calls, acts
    c.t += 25; pool.assign("u1")
    acts = await pool.tick()
    assert acts == [("create",)], acts
    create = [x for x in v.calls if x[0] == "create"][0]
    assert create[1] == 11                                   # cheapest offer under POOL_MAX_DPH
    body = create[2]
    assert body["image"] == "reg/magicpro-node:v1" and body["label"].startswith("magicpro-pool:")
    nonce = body["label"].split(":", 1)[1]
    assert body["env"]["MAGIC_NODE_KEY"] == P.node_key("s3cret", nonce)
    assert body["env"]["MAGIC_LAUNCHER_URL"] == "https://launch.example" and body["env"]["-p 10100:10100"] == "1"
    assert "s3cret" not in json.dumps(body)                  # only the per-node key leaves the launcher
    c.t += 30
    assert pool.state(pool.nodes[str(v.next_id)]) == "booting"


async def scenario_idle_pool_starts_nothing():
    c, v, pool = make()
    v.add(1); v.add(2)
    await pool.refresh(force=True)
    for _ in range(5):
        c.t += 60; pool.last_refresh = 0
        assert await pool.tick() == []
    assert not [x for x in v.calls if x[0] in ("state", "create")]
    c, v, pool = make(min_warm=1)                           # ... unless one GPU should stay warm
    v.add(1); await pool.refresh(force=True)
    assert await pool.tick() == [("start", "1")]


async def scenario_limits():
    c, v, pool = make(max_nodes=2, max_instances=2)
    v.add(1, "running"); v.add(2, "running")
    await pool.refresh(force=True); beat(pool, 1, load=4.0); beat(pool, 2, load=4.0)
    pool.assign("x")
    assert await pool.tick() == [] and "limit of 2" in pool.note
    # no image configured: never creates
    c, v, pool = make(image="")
    await pool.refresh(force=True); pool.assign("x")
    assert await pool.tick() == [] and "POOL_IMAGE" in pool.note
    # low credit: refuses to start anything
    c, v, pool = make(min_credit=20.0)
    v.add(1)
    await pool.refresh(force=True); pool.assign("x")
    await pool.tick()
    assert ("state", "1", "running") not in v.calls and "credit" in pool.note
    # offers over the price cap are ignored
    c, v, pool = make(max_dph=1.0)
    await pool.refresh(force=True); pool.assign("x")
    await pool.tick()
    assert not [x for x in v.calls if x[0] == "create"] and "No matching" in pool.note
    # max_instances counts stopped instances too (they bill storage)
    c, v, pool = make(max_instances=1)
    v.add(1); v.rented_away.add("1")
    await pool.refresh(force=True); pool.nodes["1"].bad_until = c.t + 999
    pool.assign("x")
    assert await pool.tick() == [] and "not creating more" in pool.note


async def scenario_boot_timeout_and_warm():
    c, v, pool = make()
    v.add(1); await pool.refresh(force=True); pool.assign("x"); await pool.tick()
    c.t += 500; pool.last_refresh = 0                         # restart never sent a heartbeat
    acts = await pool.tick()
    assert ("stop", "1", "did not come up") in acts, acts
    # min_warm=1 keeps one ready node from its grace stop; reservations keep a node too
    c, v, pool = make(min_warm=1)
    v.add(1, "running"); v.add(2, "running")
    await pool.refresh(force=True); beat(pool, 1); beat(pool, 2)
    keeps = [beat(pool, 1)["keep"], beat(pool, 2)["keep"]]
    assert sorted(keeps) == [False, True], keeps
    c, v, pool = make()
    v.add(1, "running"); await pool.refresh(force=True); beat(pool, 1)
    assert beat(pool, 1)["keep"] is False
    pool.assign("u")
    assert beat(pool, 1)["keep"] is True                     # a user is on the way: don't stop now
    c.t += 120
    assert beat(pool, 1)["keep"] is False


async def scenario_node_dies_and_auth():
    c, v, pool = make()
    v.add(1, "running"); v.add(2, "running")
    await pool.refresh(force=True); beat(pool, 1); beat(pool, 2, load=2.0)
    assert pool.assign("u").id == "1"
    pool.report_unreachable("1")                              # the page came back: node 1 is gone
    assert pool.assign("u", exclude={"1"}).id == "2"
    c.t += 60                                                 # node 1 silent for 60 s: not routable
    beat(pool, 2, load=2.0)
    assert pool.state(pool.nodes["1"]) != "ready" and pool.assign("v").id == "2"
    # stopping nodes get no new users
    beat(pool, 2, load=0.0, stopping=True)
    assert pool.assign("w") is None
    # signatures
    body, ts = b'{"users":0}', str(int(c.t))
    good = P.sign(P.node_key("s3cret", "n1"), "1", ts, body)
    assert pool.verify("1", ts, good, body) is pool.nodes["1"]
    assert pool.verify("1", ts, good, b'{"users":9}') is None
    assert pool.verify("1", str(int(c.t) - 500), P.sign(P.node_key("s3cret", "n1"), "1", str(int(c.t) - 500), body), body) is None
    assert pool.verify("77", ts, good, body) is None
    v.add(3, "running", label="something-else")
    await pool.refresh(force=True)
    assert "3" not in pool.nodes                              # only labelled pool instances count


async def scenario_stop_all():
    c, v, pool = make()
    v.add(1, "running"); v.add(2, "running")
    await pool.refresh(force=True); beat(pool, 1, users=2, load=2.0); beat(pool, 2)
    assert await pool.stop_all() == ["2"]
    assert sorted(await pool.stop_all(force=True)) == ["1"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("scenario_"):
            run(fn())
            print("ok ", name)
    print("pool: all ok")
