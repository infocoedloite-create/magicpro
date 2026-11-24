"""Accounts, balances, packages, limits, payments, GPU sessions, invites, stats and the audit log.

All rules live once in `Store`; a backend only runs a transaction and answers simple queries:
`MemoryBackend` (tests, local runs) and `FirestoreBackend` (Cloud Run). Inside a transaction every
write is buffered and applied at the end, so Firestore's "all reads before writes" rule holds
whatever order the logic reads and writes in.

Balances are seconds. Hours with a validity are kept as lots on the user (`lots`: seconds left and
expiry); usage spends the lots that expire first, and expired lots leave the balance. Every balance
change has a reference id and is applied once (a payment verified in the browser and reported by
the webhook counts once; a repeated usage report charges nothing).

Collections: users, ledger, orders, sessions, invites, packages, config (doc "limits"), stats
(one doc per India day), audit.
"""
import copy
import threading
import time
import uuid

IST = 5.5 * 3600   # India has no daylight saving: a fixed offset is exact

DEFAULT_LIMITS = {
    "trial_minutes": 0,           # free minutes for a new account
    "max_session_hours": 4.0,     # cap on one token's lifetime
    "daily_hours": 0.0,           # max streamed hours per user per India day (0 = no cap)
    "single_session": True,       # a new Start ends the user's previous session
    "signups_open": True,         # closed: only existing users and people with a valid invite code
    "max_gpus": 5,                # pool cap
    "idle_grace_minutes": 5,      # a GPU with nobody streaming stops after this (dead-man on the node stays 15 min)
}
USER_OVERRIDES = ("max_session_hours", "daily_hours", "single_session")


class StoreError(Exception):
    pass


def day_key(t=None):
    return time.strftime("%Y-%m-%d", time.gmtime((t or time.time()) + IST))


def day_start(t=None):
    t = t or time.time()
    return (t + IST) // 86400 * 86400 - IST


# ---------------- backends ----------------
class _View:
    """Transaction view: reads through, writes buffered until commit."""

    def __init__(self, read):
        self._read, self.writes = read, {}

    def get(self, coll, doc_id):
        key = (coll, str(doc_id))
        if key in self.writes:
            v = self.writes[key]
            return copy.deepcopy(v) if v is not None else None
        v = self._read(coll, str(doc_id))
        return copy.deepcopy(v) if v is not None else None

    def set(self, coll, doc_id, doc):
        self.writes[(coll, str(doc_id))] = copy.deepcopy(doc)

    def delete(self, coll, doc_id):
        self.writes[(coll, str(doc_id))] = None


class MemoryBackend:
    def __init__(self):
        self.data, self.lock = {}, threading.RLock()

    def run(self, fn):
        with self.lock:
            view = _View(lambda c, i: self.data.get(c, {}).get(i))
            out = fn(view)
            for (c, i), doc in view.writes.items():
                if doc is None:
                    self.data.get(c, {}).pop(i, None)
                else:
                    self.data.setdefault(c, {})[i] = doc
            return out

    def query(self, coll, where=(), order=None, desc=True, limit=None):
        ops = {"==": lambda a, b: a == b, ">=": lambda a, b: a is not None and a >= b}
        with self.lock:
            rows = [copy.deepcopy(d) for d in self.data.get(coll, {}).values()
                    if all(ops[op](d.get(f), v) for f, op, v in where)]
        if order:
            rows.sort(key=lambda d: d.get(order) or 0, reverse=desc)
        return rows[:limit] if limit else rows


class FirestoreBackend:
    def __init__(self, project=None, database="(default)"):
        from google.cloud import firestore  # only needed on Cloud Run

        self.fs = firestore
        self.db = firestore.Client(project=project, database=database)

    def run(self, fn):
        @self.fs.transactional
        def tx(t):
            view = _View(lambda c, i: (lambda s: s.to_dict() if s.exists else None)(self.db.collection(c).document(i).get(transaction=t)))
            out = fn(view)
            for (c, i), doc in view.writes.items():
                ref = self.db.collection(c).document(i)
                if doc is None:
                    t.delete(ref)
                else:
                    t.set(ref, doc)
            return out
        return tx(self.db.transaction())

    def query(self, coll, where=(), order=None, desc=True, limit=None):
        q = self.db.collection(coll)
        for f, op, v in where:
            q = q.where(filter=self.fs.FieldFilter(f, op, v))
        rows = [d.to_dict() for d in q.stream()]   # sorted here: no composite indexes to create
        if order:
            rows.sort(key=lambda d: d.get(order) or 0, reverse=desc)
        return rows[:limit] if limit else rows


# ---------------- the rules ----------------
def _settle(u, now):
    """Drop expired lots from a user dict (in place). -> seconds that expired."""
    gone, keep = 0.0, []
    for lot in u.get("lots", []):
        if lot["exp"] <= now:
            gone += lot["left"]
        elif lot["left"] > 0:
            keep.append(lot)
    u["lots"] = keep
    u["balance_s"] = max(0.0, float(u.get("balance_s", 0)) - gone)
    return gone


def _spend(u, seconds):
    """Take seconds from the balance, soonest-expiring lots first."""
    u["balance_s"] = max(0.0, float(u.get("balance_s", 0)) - seconds)
    left = seconds
    for lot in sorted(u.get("lots", []), key=lambda x: x["exp"]):
        take = min(lot["left"], left)
        lot["left"] -= take
        left -= take
        if left <= 0:
            break
    u["lots"] = [x for x in u.get("lots", []) if x["left"] > 0]


class Store:
    def __init__(self, backend=None, clock=time.time):
        self.b = backend or MemoryBackend()
        self.clock = clock

    # ----- limits and packages -----
    def get_limits(self):
        doc = self.b.run(lambda v: v.get("config", "limits")) or {}
        return {**DEFAULT_LIMITS, **{k: doc[k] for k in DEFAULT_LIMITS if k in doc}}

    def set_limits(self, patch):
        clean = _clean_limits(patch, DEFAULT_LIMITS)

        def fn(v):
            doc = v.get("config", "limits") or {}
            doc.update(clean)
            v.set("config", "limits", doc)
            return {**DEFAULT_LIMITS, **doc}
        return self.b.run(fn)

    def user_limits(self, u):
        lim = self.get_limits()
        for k, val in (u.get("limits") or {}).items():
            if k in USER_OVERRIDES and val is not None:
                lim[k] = val
        return lim

    def list_packages(self, enabled_only=False):
        rows = self.b.query("packages", order="position", desc=False)
        return [p for p in rows if p.get("enabled") or not enabled_only]

    def save_package(self, pkg, create=False):
        """Create (no id, or create=True) or update a package. Orders keep their own copy of hours,
        price and validity, so edits never touch orders already made or paid."""
        p = _clean_package(pkg)

        def fn(v):
            pid = p.get("id") or uuid.uuid4().hex[:10]
            old = v.get("packages", pid) or {}
            if pkg.get("id") and not old and not create:
                raise StoreError("No such package.")
            if not old and not all(k in p for k in ("name", "hours", "price_inr")):
                raise StoreError("A new package needs a name, hours and a price.")
            doc = {**old, **p, "id": pid, "updated": self.clock()}
            doc.setdefault("position", int(self.clock() * 1000))
            v.set("packages", pid, doc)
            return doc
        return self.b.run(fn)

    def reorder_packages(self, ids):
        def fn(v):
            for i, pid in enumerate(ids):
                p = v.get("packages", pid)
                if p:
                    p["position"] = i
                    v.set("packages", pid, p)
        self.b.run(fn)

    def seed_packages(self, packs):
        if not self.b.query("packages", limit=1):
            for i, p in enumerate(packs):
                self.save_package({"id": p["id"], "name": p.get("name") or f"{p['hours']:g} hours", "hours": p["hours"],
                                   "price_inr": p["price_inr"], "popular": i == 1, "position": i, "enabled": True}, create=True)

    # ----- users -----
    def sign_in(self, sub, email, name, allow_new=True, trial_s=0.0):
        """-> user dict, or raises StoreError when signups are closed for this new user."""
        now = self.clock()

        def fn(v):
            u = v.get("users", sub)
            if u is None:
                if not allow_new:
                    raise StoreError("signups closed")
                u = {"sub": sub, "email": email, "name": name, "balance_s": 0.0, "lots": [], "created": now,
                     "disabled": False, "admin": False, "limits": {}, "used_total_s": 0.0, "day": "", "day_used_s": 0.0}
                if trial_s > 0:
                    u["balance_s"] = float(trial_s)
                    v.set("ledger", f"trial:{sub}", {"ref": f"trial:{sub}", "sub": sub, "seconds": float(trial_s),
                                                     "kind": "trial", "note": "Free trial", "t": now})
            u.update(email=email, name=name, last_seen=now)
            _settle(u, now)
            v.set("users", sub, u)
            return u
        return self.b.run(fn)

    def get_user(self, sub):
        u = self.b.run(lambda v: v.get("users", sub))
        if u:
            _settle(u, self.clock())
            if u.get("day") != day_key(self.clock()):
                u["day_used_s"] = 0.0
        return u

    def find_user_by_email(self, email):
        rows = self.b.query("users", [("email", "==", email.lower())], limit=1)
        return rows[0] if rows else None

    def list_users(self, search="", limit=500):
        s = search.strip().lower()
        rows = [u for u in self.b.query("users", order="last_seen")
                if not s or s in u.get("email", "").lower() or s in u.get("name", "").lower()]
        now = self.clock()
        for u in rows:
            _settle(u, now)
        return rows[:limit]

    def set_user(self, sub, patch):
        """Admin changes: disabled, admin, limits (per-user overrides; None removes one)."""
        def fn(v):
            u = v.get("users", sub)
            if u is None:
                raise StoreError("No such user.")
            if "disabled" in patch:
                u["disabled"] = bool(patch["disabled"])
            if "admin" in patch:
                u["admin"] = bool(patch["admin"])
            if "limits" in patch:
                lim = dict(u.get("limits") or {})
                for k, val in (patch["limits"] or {}).items():
                    if k not in USER_OVERRIDES:
                        continue
                    if val is None or val == "":
                        lim.pop(k, None)
                    else:
                        lim.update(_clean_limits({k: val}, DEFAULT_LIMITS))
                u["limits"] = lim
            v.set("users", sub, u)
            return u
        return self.b.run(fn)

    def delete_user(self, sub):
        """Data deletion on request: account, sessions and ledger go; orders stay for accounting, without the e-mail."""
        for coll in ("sessions", "ledger"):
            for d in self.b.query(coll, [("sub", "==", sub)]):
                key = d.get("sid") or d.get("ref")
                self.b.run(lambda v, c=coll, k=key: v.delete(c, k))
        for o in self.b.query("orders", [("sub", "==", sub)]):
            self.b.run(lambda v, o=o: v.set("orders", o["order_id"], {**o, "sub": "deleted", "email": ""}))
        self.b.run(lambda v: v.delete("users", sub))

    # ----- balance -----
    def _credit(self, v, sub, seconds, kind, ref, note="", expires=None, now=None):
        now = now or self.clock()
        if v.get("ledger", ref) is not None:
            return False
        u = v.get("users", sub)
        if u is None:
            raise StoreError("No such user.")
        _settle(u, now)
        if seconds >= 0:
            u["balance_s"] = float(u.get("balance_s", 0)) + float(seconds)
            if expires:
                u.setdefault("lots", []).append({"ref": ref, "left": float(seconds), "exp": float(expires)})
        else:
            _spend(u, -float(seconds))
        v.set("users", sub, u)
        v.set("ledger", ref, {"ref": ref, "sub": sub, "seconds": float(seconds), "kind": kind, "note": note,
                              "t": now, "expires": expires})
        return True

    def credit(self, sub, seconds, kind, ref, note="", expires=None):
        return self.b.run(lambda v: self._credit(v, sub, seconds, kind, ref, note, expires))

    def list_ledger(self, sub=None, limit=100):
        return self.b.query("ledger", [("sub", "==", sub)] if sub else (), order="t", limit=limit)

    # ----- orders -----
    def create_order(self, order):
        self.b.run(lambda v: v.set("orders", order["order_id"], {**order, "status": "created", "t": self.clock()}))

    def get_order(self, order_id):
        return self.b.run(lambda v: v.get("orders", order_id))

    def pay_order(self, order_id, payment_id, amount=None):
        """Mark paid and credit the order's own hours (as bought), once. -> (credited_now, order)"""
        now = self.clock()

        def fn(v):
            o = v.get("orders", order_id)
            if o is None:
                raise StoreError("Unknown order.")
            if amount is not None and int(amount) != int(o["amount"]):
                raise StoreError("Paid amount does not match the order.")
            days = o.get("validity_days")
            applied = self._credit(v, o["sub"], o["seconds"], "purchase", f"pay:{order_id}", o.get("pack_label", ""),
                                   expires=now + days * 86400 if days else None, now=now)
            if applied:
                st = v.get("stats", day_key(now)) or {"day": day_key(now)}
                st["sold_s"] = st.get("sold_s", 0) + o["seconds"]
                field = "revenue_live_paise" if o.get("live") else "revenue_test_paise"
                st[field] = st.get(field, 0) + int(o["amount"])
                v.set("stats", day_key(now), st)
            o.update(status="paid", payment_id=payment_id, paid_t=o.get("paid_t") or now)
            v.set("orders", order_id, o)
            return applied, o
        return self.b.run(fn)

    def list_orders(self, sub=None, limit=100):
        return self.b.query("orders", [("sub", "==", sub)] if sub else (), order="t", limit=limit)

    # ----- GPU sessions -----
    def create_session(self, sess, single=True):
        """Store a new session; with single=True it becomes the user's only live one."""
        def fn(v):
            v.set("sessions", sess["sid"], {**sess, "charged_s": 0.0, "last_report": None, "streaming": False})
            u = v.get("users", sess["sub"])
            if u is not None:
                u["active_sid"] = sess["sid"] if single else u.get("active_sid")
                v.set("users", sess["sub"], u)
        self.b.run(fn)

    def get_session(self, sid):
        return self.b.run(lambda v: v.get("sessions", sid))

    def charge_usage(self, sid, sub, total_seconds, streaming=True):
        """Charge only the increase of the cumulative total. -> (charged, user, session)"""
        now = self.clock()

        def fn(v):
            s, u = v.get("sessions", sid), v.get("users", sub)
            if s is None or s["sub"] != sub:
                raise StoreError("Unknown session.")
            if u is None:
                raise StoreError("No such user.")
            _settle(u, now)
            today = day_key(now)
            if u.get("day") != today:
                u["day"], u["day_used_s"] = today, 0.0
            delta = max(0.0, float(total_seconds) - float(s.get("charged_s", 0)))
            if delta:
                s["charged_s"] = float(total_seconds)
                _spend(u, delta)
                u["used_total_s"] = float(u.get("used_total_s", 0)) + delta
                u["day_used_s"] += delta
                st = v.get("stats", today) or {"day": today}
                st["used_s"] = st.get("used_s", 0) + delta
                v.set("stats", today, st)
            s["last_report"], s["streaming"] = now, bool(streaming)
            v.set("sessions", sid, s)
            v.set("users", sub, u)
            return delta, u, s
        return self.b.run(fn)

    def list_sessions(self, sub=None, since=None, limit=50):
        where = [("sub", "==", sub)] if sub else []
        if since is not None:
            where.append(("last_report", ">=", since))
        return self.b.query("sessions", where, order="started", limit=limit)

    # ----- invites -----
    def create_invite(self, inv):
        def fn(v):
            if v.get("invites", inv["code"]):
                raise StoreError("Code exists.")
            v.set("invites", inv["code"], {**inv, "uses": 0, "revoked": False, "t": self.clock()})
        self.b.run(fn)

    def get_invite(self, code):
        return self.b.run(lambda v: v.get("invites", code))

    def invite_usable(self, code):
        try:
            _check_invite(self.get_invite(code), self.clock())
            return True
        except StoreError:
            return False

    def list_invites(self, limit=200):
        return self.b.query("invites", order="t", limit=limit)

    def revoke_invite(self, code):
        def fn(v):
            inv = v.get("invites", code)
            if inv is None:
                raise StoreError("No such code.")
            inv["revoked"] = True
            v.set("invites", code, inv)
        self.b.run(fn)

    def redeem_invite(self, code, sub):
        now = self.clock()

        def fn(v):
            inv = v.get("invites", code)
            if inv and v.get("ledger", f"inv:{code}:{sub}") is not None:
                raise StoreError("You already used this code.")
            _check_invite(inv, now)
            inv["uses"] += 1
            self._credit(v, sub, inv["hours"] * 3600, "invite", f"inv:{code}:{sub}", f"Code {code}", now=now)
            v.set("invites", code, inv)
            return inv
        return self.b.run(fn)

    # ----- stats and audit -----
    def add_gpu_cost(self, usd, seconds, now=None):
        today = day_key(now or self.clock())

        def fn(v):
            st = v.get("stats", today) or {"day": today}
            st["gpu_usd"] = st.get("gpu_usd", 0) + usd
            st["gpu_s"] = st.get("gpu_s", 0) + seconds
            v.set("stats", today, st)
        self.b.run(fn)

    def stats_days(self, days):
        return [self.b.run(lambda v, d=d: v.get("stats", d)) or {"day": d} for d in days]

    def audit(self, admin, action, target="", detail=""):
        t = self.clock()
        self.b.run(lambda v: v.set("audit", f"{int(t * 1000)}-{uuid.uuid4().hex[:6]}",
                                   {"t": t, "admin": admin, "action": action, "target": target, "detail": str(detail)[:300]}))

    def list_audit(self, target=None, limit=100):
        return self.b.query("audit", [("target", "==", target)] if target else (), order="t", limit=limit)


def MemoryStore():  # noqa: N802  kept for older callers
    return Store(MemoryBackend())


def _check_invite(inv, now):
    if inv is None:
        raise StoreError("That code doesn't exist.")
    if inv["revoked"]:
        raise StoreError("That code was withdrawn.")
    if now > inv["expires"]:
        raise StoreError("That code has expired.")
    if inv["uses"] >= inv["max_uses"]:
        raise StoreError("That code has been used up.")


def _clean_limits(patch, defaults):
    out = {}
    for k, val in (patch or {}).items():
        if k not in defaults:
            continue
        d = defaults[k]
        try:
            if isinstance(d, bool):
                out[k] = val if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")
            elif isinstance(d, int):
                out[k] = int(float(val))
            else:
                out[k] = float(val)
        except (TypeError, ValueError) as e:
            raise StoreError(f"Bad value for {k}.") from e
        if not isinstance(d, bool) and out[k] < 0:
            raise StoreError(f"{k} can't be negative.")
    if "max_gpus" in out and not 0 <= out["max_gpus"] <= 50:
        raise StoreError("max_gpus must be 0-50.")
    if "max_session_hours" in out and not 0 < out["max_session_hours"] <= 24:
        raise StoreError("max_session_hours must be above 0 and at most 24.")
    return out


def _clean_package(p):
    out = {}
    if "id" in p and p["id"]:
        out["id"] = str(p["id"])
    if "name" in p:
        out["name"] = str(p["name"]).strip()[:40]
        if not out["name"]:
            raise StoreError("A package needs a name.")
    for k, lo, hi in (("hours", 0.25, 1000), ("price_inr", 1, 10_000_000)):
        if k in p:
            try:
                out[k] = float(p[k])
            except (TypeError, ValueError) as e:
                raise StoreError(f"Bad {k}.") from e
            if not lo <= out[k] <= hi:
                raise StoreError(f"{k} must be between {lo} and {hi}.")
    if "validity_days" in p:
        vd = p["validity_days"]
        out["validity_days"] = int(vd) if vd not in (None, "", 0, "0") else None
        if out["validity_days"] is not None and not 1 <= out["validity_days"] <= 3650:
            raise StoreError("Validity must be 1-3650 days, or empty for no expiry.")
    for k in ("enabled", "popular"):
        if k in p:
            out[k] = bool(p[k])
    if "position" in p:
        out["position"] = int(p["position"])
    return out
