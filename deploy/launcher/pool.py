"""Autoscaled pool of Vast.ai GPU nodes for Magic Pro.

Source of truth is Vast itself: every pool instance carries the label "<LABEL>:<nonce>". Each
running node's watchdog heartbeats to the launcher every 10 s (signed with a key derived from
POOL_SECRET and that nonce) with its tunnel URL, streams, load and health. So nothing has to
survive a launcher restart: within one heartbeat round the pool is known again.

Capacity is modelled in cost units: every node has a budget (default 4.0) and every stream a
cost (computed by the node from its settings, e.g. 1080p30 + GFPGAN = 1.0, plain = 0.5, 60 fps
doubles). New users go to the ready node with the most free units. When free units across the
pool (plus nodes still booting) drop below one new stream, the next node is started early:
a stopped pool node first (fast, but Vast may have rented its GPU out), else a new instance
from a matching offer. Nodes stop themselves (grace period, dead-man) — the launcher never stops
a node that has streams, except "Stop all" with force.

Hard limits: MAX_NODES running/booting at once, MAX_INSTANCES pool instances in total (stopped
ones still bill storage), MAX_DPH per GPU hour, MIN_CREDIT left on the Vast account.
"""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field

import httpx


def _env_list(name, default):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Config:
    label: str = os.environ.get("POOL_LABEL", "magicpro-pool")
    max_nodes: int = int(os.environ.get("POOL_MAX_NODES", "5"))
    max_instances: int = int(os.environ.get("POOL_MAX_INSTANCES", os.environ.get("POOL_MAX_NODES", "5")))
    min_warm: int = int(os.environ.get("POOL_MIN_WARM", "0"))
    budget: float = float(os.environ.get("POOL_NODE_BUDGET", "4.0"))          # used until a node reports its own
    new_stream_cost: float = float(os.environ.get("POOL_NEW_STREAM_COST", "1.0"))
    image: str = os.environ.get("POOL_IMAGE", "")                             # empty: never create, only restart
    image_login: str = os.environ.get("POOL_IMAGE_LOGIN", "")                 # "-u USER -p PASS REGISTRY" (secret)
    gpu_names: list = field(default_factory=lambda: _env_list("POOL_GPU_NAMES", "RTX PRO 6000 WS,RTX PRO 6000 S"))
    countries: list = field(default_factory=lambda: _env_list("POOL_COUNTRIES", "IN"))
    min_reliability: float = float(os.environ.get("POOL_MIN_RELIABILITY", "0.98"))
    max_dph: float = float(os.environ.get("POOL_MAX_DPH", "1.6"))
    min_credit: float = float(os.environ.get("POOL_MIN_CREDIT", "2.0"))
    disk_gb: int = int(os.environ.get("POOL_DISK_GB", "60"))
    app_port: str = os.environ.get("APP_PORT", "10100")
    launcher_url: str = os.environ.get("LAUNCHER_URL", "")
    secret: str = os.environ.get("POOL_SECRET", "")
    session_secret: str = os.environ.get("MAGIC_SESSION_SECRET", "")          # handoff contract; nodes verify tokens
    heartbeat_ttl: float = 45        # a node is gone after this long without a heartbeat
    reserve_ttl: float = 90          # an assigned user counts as load until their stream shows up
    wait_ttl: float = 30             # a waiting user counts as demand this long after their last poll
    sched_timeout: float = 75        # restart stuck in "scheduling" this long = GPU rented by someone else
    boot_timeout_restart: float = 420
    boot_timeout_create: float = 1200
    bad_cooldown: float = 1800       # don't retry a node that failed to start for this long
    scale_gap: float = 20            # at most one scale-up per this many seconds


# ---------------- node authentication ----------------
def node_key(secret: str, nonce: str) -> str:
    return hmac.new(secret.encode(), f"node|{nonce}".encode(), hashlib.sha256).hexdigest()


def sign(key: str, node_id: str, ts: str, body: bytes) -> str:
    return hmac.new(key.encode(), f"{node_id}|{ts}|{hashlib.sha256(body).hexdigest()}".encode(),
                    hashlib.sha256).hexdigest()


# ---------------- Vast.ai API ----------------
class Vast:
    def __init__(self, api_key: str, url: str = "https://console.vast.ai"):
        self.key, self.url = api_key, url.rstrip("/")

    async def _req(self, method, path, json_body=None):
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.request(method, f"{self.url}/api/v0{path}", json=json_body,
                                headers={"Authorization": f"Bearer {self.key}"})
        try:
            body = r.json()
        except ValueError:
            body = {"msg": r.text[:300]}
        if r.status_code >= 400:
            raise RuntimeError(f"Vast.ai {method} {path.split('?')[0]} -> {r.status_code}: "
                               f"{body.get('msg') or body.get('error') or body}")
        return body

    async def instances(self) -> list:
        return (await self._req("GET", "/instances/?owner=me")).get("instances", [])

    async def set_state(self, iid, state) -> dict:
        return await self._req("PUT", f"/instances/{iid}/", {"state": state})

    async def set_label(self, iid, label) -> dict:
        return await self._req("PUT", f"/instances/{iid}/", {"label": label})

    async def offers(self, cfg: Config) -> list:
        q = {"verified": {"eq": True}, "external": {"eq": False}, "rentable": {"eq": True},
             "rented": {"eq": False}, "num_gpus": {"eq": 1}, "gpu_name": {"in": cfg.gpu_names},
             "reliability": {"gt": cfg.min_reliability}, "dph_total": {"lte": cfg.max_dph},
             "direct_port_count": {"gte": 4}, "disk_space": {"gte": cfg.disk_gb},
             "order": [["dph_total", "asc"]], "type": "on-demand", "limit": 50}
        if cfg.countries:
            q["geolocation"] = {"in": cfg.countries}
        return (await self._req("POST", "/bundles/", q)).get("offers", [])

    async def create(self, offer_id, body) -> dict:
        return await self._req("PUT", f"/asks/{offer_id}/", body)

    async def credit(self):
        try:
            return float((await self._req("GET", "/users/current/")).get("credit"))
        except (RuntimeError, TypeError, ValueError):
            return None


# ---------------- pool ----------------
@dataclass
class Node:
    id: str
    nonce: str = ""
    inst: dict = field(default_factory=dict)
    beat: dict = field(default_factory=dict)
    beat_t: float = 0.0
    start_t: float = 0.0             # when we asked Vast to start/create it
    start_kind: str = ""             # restart | create
    bad_until: float = 0.0
    suspect_until: float = 0.0       # a user reported it unreachable
    reserved: list = field(default_factory=list)   # [(t, cost)]


class Pool:
    def __init__(self, vast, cfg: Config | None = None, clock=time.time, log=print):
        self.vast, self.cfg, self.clock, self.log = vast, cfg or Config(), clock, log
        self.nodes: dict[str, Node] = {}
        self.waiting: dict[str, float] = {}
        self.last_scale = 0.0
        self.last_refresh = 0.0
        self.note = ""                # last scaling message, shown in the pool view
        self.lock = asyncio.Lock()

    # ----- state -----
    async def refresh(self, force=False):
        now = self.clock()
        if not force and now - self.last_refresh < 5:
            return
        self.last_refresh = now
        seen = set()
        for inst in await self.vast.instances():
            label = inst.get("label") or ""
            if not (label == self.cfg.label or label.startswith(self.cfg.label + ":")):
                continue
            iid = str(inst["id"])
            seen.add(iid)
            n = self.nodes.setdefault(iid, Node(iid))
            n.inst, n.nonce = inst, label.partition(":")[2]
            if inst.get("intended_status") == "running" and not n.start_t and not self._fresh(n):
                n.start_t, n.start_kind = now, n.start_kind or "unknown"   # booting when we first saw it
        for iid in [i for i in self.nodes if i not in seen and not (self.nodes[i].start_kind == "create"
                                                                     and now - self.nodes[i].start_t < 180)]:
            del self.nodes[iid]

    def _fresh(self, n: Node) -> bool:
        return bool(n.beat_t) and self.clock() - n.beat_t <= self.cfg.heartbeat_ttl

    def state(self, n: Node) -> str:
        now, inst = self.clock(), n.inst
        actual, intended = inst.get("actual_status") or "", inst.get("intended_status") or ""
        if intended == "stopped":
            return "stopping" if actual == "running" or self._fresh(n) else "stopped"
        if self._fresh(n):
            if n.beat.get("stopping"):
                return "stopping"
            if not n.beat.get("healthy", True) or not n.beat.get("tunnel") or now < n.suspect_until:
                return "unhealthy"
            return "ready"
        waited = now - n.start_t if n.start_t else 0
        msg = (inst.get("status_msg") or "").lower()
        if n.start_kind in ("restart", "unknown") and (actual == "scheduling" or "schedul" in msg) and waited > self.cfg.sched_timeout:
            return "unavailable"
        limit = self.cfg.boot_timeout_restart if n.start_kind == "restart" else self.cfg.boot_timeout_create
        if waited > limit:
            return "failed"
        return "booting"

    def budget(self, n: Node) -> float:
        return float(n.beat.get("budget") or self.cfg.budget)

    def reserved(self, n: Node) -> float:
        now = self.clock()
        n.reserved = [(t, c) for t, c in n.reserved if now - t < self.cfg.reserve_ttl]
        return sum(c for _, c in n.reserved)

    def free(self, n: Node) -> float:
        return self.budget(n) - float(n.beat.get("load") or 0) - self.reserved(n)

    def stream_cost(self, n: Node | None = None) -> float:
        reported = float(n.beat.get("stream_cost") or 0) if n else 0.0
        return max(self.cfg.new_stream_cost, reported)

    def summary(self) -> dict:
        nodes = []
        for n in sorted(self.nodes.values(), key=lambda n: int(n.id) if n.id.isdigit() else 0):
            st = self.state(n)
            nodes.append({"id": n.id, "state": st, "users": n.beat.get("users", 0) if self._fresh(n) else 0,
                          "load": round(float(n.beat.get("load") or 0) + self.reserved(n), 2), "budget": self.budget(n),
                          "gpu": n.inst.get("gpu_name"), "price_per_hour": round(float(n.inst.get("dph_total") or 0), 3),
                          "stop_in_s": n.beat.get("stop_in_s") if self._fresh(n) else None,
                          "dry_run": n.beat.get("dry_run"), "where": n.inst.get("geolocation")})
        billing = [x for x in nodes if x["state"] in ("ready", "booting", "unhealthy", "stopping")]
        return {"nodes": nodes, "waiting": len(self._waiting()), "note": self.note,
                "billing_per_hour": round(sum(x["price_per_hour"] for x in billing), 3),
                "limits": {"max_nodes": self.cfg.max_nodes, "max_instances": self.cfg.max_instances,
                           "min_warm": self.cfg.min_warm, "max_dph": self.cfg.max_dph}}

    def _waiting(self):
        now = self.clock()
        self.waiting = {c: t for c, t in self.waiting.items() if now - t < self.cfg.wait_ttl}
        return self.waiting

    # ----- node heartbeat -----
    def verify(self, node_id: str, ts: str, sig: str, body: bytes) -> Node | None:
        n = self.nodes.get(str(node_id))
        if not (n and n.nonce and self.cfg.secret):
            return None
        try:
            if abs(self.clock() - float(ts)) > 120:
                return None
        except ValueError:
            return None
        good = sign(node_key(self.cfg.secret, n.nonce), n.id, ts, body)
        return n if hmac.compare_digest(good, sig or "") else None

    def heartbeat(self, n: Node, beat: dict) -> dict:
        prev = int(n.beat.get("users") or 0) if self._fresh(n) else 0
        arrived = max(0, int(beat.get("users") or 0) - prev)
        self.reserved(n)                                   # drop expired ones
        n.reserved = n.reserved[arrived:]                  # streams that showed up are load now, not reservations
        n.beat, n.beat_t = beat, self.clock()
        n.start_t = 0.0
        n.bad_until = 0.0
        # keep = don't let the grace period stop this node: users are on their way, or it is warm
        keep = self.reserved(n) > 0 or n.id in self._warm_set()
        return {"keep": keep, "t": self.clock()}

    def _warm_set(self):
        if self.cfg.min_warm <= 0:
            return set()
        ready = sorted((n for n in self.nodes.values() if self.state(n) == "ready"),
                       key=lambda n: (-float(n.beat.get("load") or 0), n.id))
        return {n.id for n in ready[: self.cfg.min_warm]}

    # ----- users -----
    def pick(self, cost: float, exclude=()) -> Node | None:
        best = None
        for n in self.nodes.values():
            if n.id in exclude or self.state(n) != "ready":
                continue
            if self.free(n) + 1e-9 >= max(cost, self.stream_cost(n)) and (best is None or self.free(n) > self.free(best)):
                best = n
        return best

    def assign(self, cid: str, exclude=()) -> Node | None:
        cost = self.cfg.new_stream_cost
        n = self.pick(cost, exclude)
        if n:
            n.reserved.append((self.clock(), self.stream_cost(n)))
            self.waiting.pop(cid, None)
        else:
            self.waiting[cid] = self.clock()
        return n

    def report_unreachable(self, node_id: str):
        n = self.nodes.get(str(node_id))
        if n:
            n.suspect_until = self.clock() + 30

    # ----- scaling -----
    def plan(self) -> list:
        """Pure decision step -> list of actions: ("stop", id, why) | ("start", id) | ("create",)."""
        now, cfg = self.clock(), self.cfg
        acts, states = [], {i: self.state(n) for i, n in self.nodes.items()}
        for i, st in states.items():
            if st in ("unavailable", "failed"):
                acts.append(("stop", i, "GPU rented by someone else" if st == "unavailable" else "did not come up"))
        active = [i for i, st in states.items() if st in ("ready", "booting", "unhealthy")]
        supply = sum(max(0.0, self.free(self.nodes[i])) for i in active if states[i] == "ready")
        supply += sum(self.budget(self.nodes[i]) for i in active if states[i] == "booting")
        waiting = len(self._waiting())
        # headroom of one stream while the pool is in use; with nothing running and nobody
        # waiting, nothing starts (unless min_warm asks for it)
        demand = cfg.new_stream_cost * (waiting + (1 if active else 0))
        warm_short = len([i for i in active if states[i] in ("ready", "booting")]) < cfg.min_warm
        if not (demand and supply < demand or warm_short):
            return acts
        if len(active) >= cfg.max_nodes:
            self.note = f"Pool is at its limit of {cfg.max_nodes} GPUs."
            return acts
        if now - self.last_scale < cfg.scale_gap:
            return acts
        stopped = [n for i, n in self.nodes.items() if states[i] == "stopped" and n.bad_until <= now]
        if stopped:
            best = max(stopped, key=lambda n: (float(n.inst.get("reliability2") or n.inst.get("reliability") or 0),
                                               -float(n.inst.get("dph_total") or 9)))
            acts.append(("start", best.id))
        elif not cfg.image:
            self.note = "No stopped GPU to restart and POOL_IMAGE is not set, so no new GPU can be created."
        elif len(self.nodes) >= cfg.max_instances:
            self.note = f"All {cfg.max_instances} pool instances are busy or unavailable; not creating more."
        else:
            acts.append(("create",))
        return acts

    async def tick(self):
        """Refresh from Vast, decide, act. Safe to call on every request."""
        async with self.lock:
            try:
                await self.refresh()
            except RuntimeError as e:
                self.note = str(e)
                return []
            acts = self.plan()
            for a in acts:
                try:
                    await self._do(a)
                except RuntimeError as e:
                    self.note = str(e)
                    self.log(f"pool: {a} failed: {e}")
            return acts

    async def _do(self, a):
        now = self.clock()
        if a[0] == "stop":
            n = self.nodes[a[1]]
            await self.vast.set_state(n.id, "stopped")
            n.inst["intended_status"] = "stopped"
            n.bad_until, n.start_t = now + self.cfg.bad_cooldown, 0.0
            self.note = f"GPU {n.id} stopped: {a[2]}."
            self.log(f"pool: stop {n.id} ({a[2]})")
            return
        credit = await self.vast.credit()
        if credit is not None and credit < self.cfg.min_credit:
            self.last_scale = now
            self.note = f"Vast.ai credit is ${credit:.2f}, below the ${self.cfg.min_credit:.2f} minimum: not starting more GPUs."
            return
        self.last_scale = now
        if a[0] == "start":
            n = self.nodes[a[1]]
            r = await self.vast.set_state(n.id, "running")
            if r.get("success") is False:
                n.bad_until = now + self.cfg.bad_cooldown
                raise RuntimeError(f"restart of {n.id} refused: {r.get('msg')}")
            n.inst["intended_status"] = "running"
            n.start_t, n.start_kind = now, "restart"
            self.note = f"Restarting GPU {n.id}…"
            self.log(f"pool: restart {n.id}")
        elif a[0] == "create":
            offers = await self.vast.offers(self.cfg)
            offers = [o for o in offers if float(o.get("dph_total") or 99) <= self.cfg.max_dph]
            if not offers:
                self.note = "No matching GPU offer on Vast.ai right now (model, country, reliability, price)."
                return
            o = offers[0]
            nonce = secrets.token_hex(8)
            r = await self.vast.create(o["id"], self.create_body(nonce))
            iid = str(r.get("new_contract") or "")
            if r.get("success") is False or not iid:
                raise RuntimeError(f"create from offer {o['id']} refused: {r.get('msg') or r}")
            n = self.nodes.setdefault(iid, Node(iid))
            n.nonce, n.start_t, n.start_kind = nonce, now, "create"
            n.inst = {"id": iid, "intended_status": "running", "actual_status": "loading", "label": f"{self.cfg.label}:{nonce}",
                      "dph_total": o.get("dph_total"), "gpu_name": o.get("gpu_name"), "geolocation": o.get("geolocation")}
            self.last_refresh = 0.0
            self.note = f"Creating GPU {iid} ({o.get('gpu_name')}, {o.get('geolocation')}, ${float(o.get('dph_total') or 0):.2f}/h)…"
            self.log(f"pool: create {iid} from offer {o['id']}")

    def create_body(self, nonce: str) -> dict:
        p = self.cfg.app_port
        env = {
            f"-p {p}:{p}": "1", "-p 1111:1111": "1",
            "OPEN_BUTTON_PORT": "1111",
            "PORTAL_CONFIG": f"localhost:1111:11111:/:Instance Portal|localhost:{p}:17860:/:Magic Pro",
            "MAGIC_LAUNCHER_URL": self.cfg.launcher_url,
            "MAGIC_NODE_KEY": node_key(self.cfg.secret, nonce),
            "MAGIC_SESSION_SECRET": self.cfg.session_secret,
            "MAGIC_TUNNEL_DIRECT": "1",    # the node's public tunnel goes straight to the app (it enforces sessions)
            "MAGIC_WATCHDOG_DRY_RUN": os.environ.get("POOL_NODE_DRY_RUN", "0"),
        }
        return {"client_id": "me", "image": self.cfg.image, "env": env, "disk": self.cfg.disk_gb,
                "label": f"{self.cfg.label}:{nonce}", "runtype": "jupyter_direc ssh_direc ssh_proxy",
                "onstart": "entrypoint.sh", "cancel_unavail": True, "image_login": self.cfg.image_login or None}

    async def stop_all(self, force=False) -> list:
        async with self.lock:
            await self.refresh(force=True)
            done = []
            for n in self.nodes.values():
                if n.inst.get("intended_status") == "stopped":
                    continue
                if not force and self._fresh(n) and n.beat.get("users"):
                    continue
                await self.vast.set_state(n.id, "stopped")
                n.inst["intended_status"] = "stopped"
                done.append(n.id)
            return done


def env_lines(cfg: Config, nonce: str) -> str:
    """Settings for /etc/magicpro-watchdog.env when adopting an existing instance."""
    return "\n".join([f"MAGIC_LAUNCHER_URL={cfg.launcher_url}", f"MAGIC_NODE_KEY={node_key(cfg.secret, nonce)}"])


def dumps(d) -> bytes:
    return json.dumps(d, separators=(",", ":")).encode()
