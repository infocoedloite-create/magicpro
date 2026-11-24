"""Accounts, balances, payments, GPU sessions and invites.

Two implementations with one interface: MemoryStore (tests, local runs) and FirestoreStore
(Cloud Run). Every balance change is idempotent by a reference id, so a payment that is both
verified in the browser and reported by the webhook, or a usage report that arrives twice,
counts once.

Collections (Firestore): users/{sub}, ledger/{ref}, orders/{order_id}, sessions/{sid},
invites/{code}. Balances are seconds.
"""
import copy
import threading
import time


class StoreError(Exception):
    pass


class MemoryStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.users, self.ledger, self.orders, self.sessions, self.invites = {}, {}, {}, {}, {}

    # ----- users -----
    def upsert_user(self, sub, email, name):
        with self.lock:
            u = self.users.get(sub)
            now = time.time()
            if u is None:
                u = self.users[sub] = {"sub": sub, "email": email, "name": name, "balance_s": 0.0,
                                       "created": now, "disabled": False}
            u.update(email=email, name=name, last_seen=now)
            return copy.deepcopy(u)

    def get_user(self, sub):
        with self.lock:
            u = self.users.get(sub)
            return copy.deepcopy(u) if u else None

    def list_users(self, limit=500):
        with self.lock:
            return sorted((copy.deepcopy(u) for u in self.users.values()), key=lambda u: -u.get("last_seen", 0))[:limit]

    def set_disabled(self, sub, disabled):
        with self.lock:
            if sub not in self.users:
                raise StoreError("No such user.")
            self.users[sub]["disabled"] = bool(disabled)

    def delete_user(self, sub):
        """Data deletion on request: account, sessions and ledger go; order records stay (accounting),
        with the e-mail removed."""
        with self.lock:
            self.users.pop(sub, None)
            for d in (self.sessions, self.ledger):
                for k in [k for k, v in d.items() if v.get("sub") == sub]:
                    del d[k]
            for o in self.orders.values():
                if o.get("sub") == sub:
                    o.update(sub="deleted", email="")

    # ----- balance -----
    def credit(self, sub, seconds, kind, ref, note=""):
        """Add (or with a negative value remove) seconds once per ref. -> True if applied."""
        with self.lock:
            if ref in self.ledger:
                return False
            u = self.users.get(sub)
            if u is None:
                raise StoreError("No such user.")
            u["balance_s"] = max(0.0, u["balance_s"] + float(seconds))
            self.ledger[ref] = {"ref": ref, "sub": sub, "seconds": float(seconds), "kind": kind, "note": note, "t": time.time()}
            return True

    def list_ledger(self, sub=None, limit=100):
        with self.lock:
            rows = [copy.deepcopy(r) for r in self.ledger.values() if sub is None or r["sub"] == sub]
            return sorted(rows, key=lambda r: -r["t"])[:limit]

    # ----- orders -----
    def create_order(self, order):
        with self.lock:
            self.orders[order["order_id"]] = {**order, "status": "created", "t": time.time()}

    def get_order(self, order_id):
        with self.lock:
            o = self.orders.get(order_id)
            return copy.deepcopy(o) if o else None

    def pay_order(self, order_id, payment_id, amount=None):
        """Mark paid and credit its hours, once. -> (credited_now, order)"""
        with self.lock:
            o = self.orders.get(order_id)
            if o is None:
                raise StoreError("Unknown order.")
            if amount is not None and int(amount) != int(o["amount"]):
                raise StoreError("Paid amount does not match the order.")
            applied = self.credit(o["sub"], o["seconds"], "purchase", f"pay:{order_id}", o.get("pack_label", ""))
            o.update(status="paid", payment_id=payment_id, paid_t=o.get("paid_t") or time.time())
            return applied, copy.deepcopy(o)

    def list_orders(self, sub=None, limit=100):
        with self.lock:
            rows = [copy.deepcopy(o) for o in self.orders.values() if sub is None or o["sub"] == sub]
            return sorted(rows, key=lambda r: -r["t"])[:limit]

    # ----- GPU sessions -----
    def create_session(self, sess):
        with self.lock:
            self.sessions[sess["sid"]] = {**sess, "charged_s": 0.0, "last_report": None}

    def get_session(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
            return copy.deepcopy(s) if s else None

    def charge_usage(self, sid, sub, total_seconds):
        """Charge only the increase of the cumulative total_seconds. -> (charged, balance, user)"""
        with self.lock:
            s = self.sessions.get(sid)
            if s is None or s["sub"] != sub:
                raise StoreError("Unknown session.")
            u = self.users.get(sub)
            if u is None:
                raise StoreError("No such user.")
            delta = max(0.0, float(total_seconds) - s["charged_s"])
            if delta:
                s["charged_s"] = float(total_seconds)
                u["balance_s"] = max(0.0, u["balance_s"] - delta)
            s["last_report"] = time.time()
            return delta, u["balance_s"], copy.deepcopy(u)

    def list_sessions(self, sub=None, limit=50):
        with self.lock:
            rows = [copy.deepcopy(s) for s in self.sessions.values() if sub is None or s["sub"] == sub]
            return sorted(rows, key=lambda r: -r["started"])[:limit]

    # ----- invites -----
    def create_invite(self, inv):
        with self.lock:
            if inv["code"] in self.invites:
                raise StoreError("Code exists.")
            self.invites[inv["code"]] = {**inv, "uses": 0, "revoked": False, "t": time.time()}

    def list_invites(self, limit=200):
        with self.lock:
            return sorted((copy.deepcopy(i) for i in self.invites.values()), key=lambda r: -r["t"])[:limit]

    def revoke_invite(self, code):
        with self.lock:
            if code not in self.invites:
                raise StoreError("No such code.")
            self.invites[code]["revoked"] = True

    def redeem_invite(self, code, sub, now=None):
        now = now or time.time()
        with self.lock:
            inv = self.invites.get(code)
            if inv and f"inv:{code}:{sub}" in self.ledger:
                raise StoreError("You already used this code.")
            _check_invite(inv, now)
            inv["uses"] += 1
            self.credit(sub, inv["hours"] * 3600, "invite", f"inv:{code}:{sub}", f"Code {code}")
            return copy.deepcopy(inv)


def _check_invite(inv, now):
    if inv is None:
        raise StoreError("That code doesn't exist.")
    if inv["revoked"]:
        raise StoreError("That code was withdrawn.")
    if now > inv["expires"]:
        raise StoreError("That code has expired.")
    if inv["uses"] >= inv["max_uses"]:
        raise StoreError("That code has been used up.")


class FirestoreStore:
    """Same interface on Firestore. Multi-document changes run in transactions."""

    def __init__(self, project=None, database="(default)"):
        from google.cloud import firestore  # imported here so tests and local runs don't need it

        self.fs = firestore
        self.db = firestore.Client(project=project, database=database)

    def _c(self, name):
        return self.db.collection(name)

    def upsert_user(self, sub, email, name):
        ref = self._c("users").document(sub)
        fs, now = self.fs, time.time()

        @fs.transactional
        def tx(t):
            snap = ref.get(transaction=t)
            if snap.exists:
                t.update(ref, {"email": email, "name": name, "last_seen": now})
                return {**snap.to_dict(), "email": email, "name": name, "last_seen": now}
            u = {"sub": sub, "email": email, "name": name, "balance_s": 0.0, "created": now, "last_seen": now, "disabled": False}
            t.set(ref, u)
            return u
        return tx(self.db.transaction())

    def get_user(self, sub):
        s = self._c("users").document(sub).get()
        return s.to_dict() if s.exists else None

    def list_users(self, limit=500):
        q = self._c("users").order_by("last_seen", direction=self.fs.Query.DESCENDING).limit(limit)
        return [d.to_dict() for d in q.stream()]

    def set_disabled(self, sub, disabled):
        self._c("users").document(sub).update({"disabled": bool(disabled)})

    def delete_user(self, sub):
        for name in ("sessions", "ledger"):
            for d in self._c(name).where("sub", "==", sub).stream():
                d.reference.delete()
        for d in self._c("orders").where("sub", "==", sub).stream():
            d.reference.update({"sub": "deleted", "email": ""})
        self._c("users").document(sub).delete()

    def _credit_tx(self, t, sub, seconds, kind, ref, note):
        lref, uref = self._c("ledger").document(ref), self._c("users").document(sub)
        if lref.get(transaction=t).exists:
            return False
        u = uref.get(transaction=t)
        if not u.exists:
            raise StoreError("No such user.")
        t.update(uref, {"balance_s": max(0.0, float(u.to_dict().get("balance_s", 0)) + float(seconds))})
        t.set(lref, {"ref": ref, "sub": sub, "seconds": float(seconds), "kind": kind, "note": note, "t": time.time()})
        return True

    def credit(self, sub, seconds, kind, ref, note=""):
        @self.fs.transactional
        def tx(t):
            return self._credit_tx(t, sub, seconds, kind, ref, note)
        return tx(self.db.transaction())

    def list_ledger(self, sub=None, limit=100):
        q = self._c("ledger")
        if sub:
            q = q.where("sub", "==", sub)
        rows = [d.to_dict() for d in q.stream()]
        return sorted(rows, key=lambda r: -r["t"])[:limit]

    def create_order(self, order):
        self._c("orders").document(order["order_id"]).set({**order, "status": "created", "t": time.time()})

    def get_order(self, order_id):
        s = self._c("orders").document(order_id).get()
        return s.to_dict() if s.exists else None

    def pay_order(self, order_id, payment_id, amount=None):
        oref = self._c("orders").document(order_id)

        @self.fs.transactional
        def tx(t):
            snap = oref.get(transaction=t)
            if not snap.exists:
                raise StoreError("Unknown order.")
            o = snap.to_dict()
            if amount is not None and int(amount) != int(o["amount"]):
                raise StoreError("Paid amount does not match the order.")
            applied = self._credit_tx(t, o["sub"], o["seconds"], "purchase", f"pay:{order_id}", o.get("pack_label", ""))
            o.update(status="paid", payment_id=payment_id, paid_t=o.get("paid_t") or time.time())
            t.set(oref, o)
            return applied, o
        return tx(self.db.transaction())

    def list_orders(self, sub=None, limit=100):
        q = self._c("orders")
        if sub:
            q = q.where("sub", "==", sub)
        return sorted((d.to_dict() for d in q.stream()), key=lambda r: -r["t"])[:limit]

    def create_session(self, sess):
        self._c("sessions").document(sess["sid"]).set({**sess, "charged_s": 0.0, "last_report": None})

    def get_session(self, sid):
        s = self._c("sessions").document(sid).get()
        return s.to_dict() if s.exists else None

    def charge_usage(self, sid, sub, total_seconds):
        sref, uref = self._c("sessions").document(sid), self._c("users").document(sub)

        @self.fs.transactional
        def tx(t):
            s, u = sref.get(transaction=t), uref.get(transaction=t)
            if not s.exists or s.to_dict().get("sub") != sub:
                raise StoreError("Unknown session.")
            if not u.exists:
                raise StoreError("No such user.")
            sd, ud = s.to_dict(), u.to_dict()
            delta = max(0.0, float(total_seconds) - float(sd.get("charged_s", 0)))
            bal = float(ud.get("balance_s", 0))
            if delta:
                bal = max(0.0, bal - delta)
                t.update(uref, {"balance_s": bal})
                t.update(sref, {"charged_s": float(total_seconds), "last_report": time.time()})
            else:
                t.update(sref, {"last_report": time.time()})
            return delta, bal, {**ud, "balance_s": bal}
        return tx(self.db.transaction())

    def list_sessions(self, sub=None, limit=50):
        q = self._c("sessions")
        if sub:
            q = q.where("sub", "==", sub)
        return sorted((d.to_dict() for d in q.stream()), key=lambda r: -r["started"])[:limit]

    def create_invite(self, inv):
        ref = self._c("invites").document(inv["code"])
        if ref.get().exists:
            raise StoreError("Code exists.")
        ref.set({**inv, "uses": 0, "revoked": False, "t": time.time()})

    def list_invites(self, limit=200):
        return sorted((d.to_dict() for d in self._c("invites").stream()), key=lambda r: -r["t"])[:limit]

    def revoke_invite(self, code):
        self._c("invites").document(code).update({"revoked": True})

    def redeem_invite(self, code, sub, now=None):
        now = now or time.time()
        iref = self._c("invites").document(code)

        @self.fs.transactional
        def tx(t):
            snap = iref.get(transaction=t)
            inv = snap.to_dict() if snap.exists else None
            if inv and self._c("ledger").document(f"inv:{code}:{sub}").get(transaction=t).exists:
                raise StoreError("You already used this code.")
            _check_invite(inv, now)
            inv["uses"] += 1
            # Firestore transactions need every read before the first write: credit (reads) first
            self._credit_tx(t, sub, inv["hours"] * 3600, "invite", f"inv:{code}:{sub}", f"Code {code}")
            t.update(iref, {"uses": inv["uses"]})
            return inv
        return tx(self.db.transaction())
