"""Razorpay: orders, Checkout signature check, webhook signature check, and the live-mode switch.

Test mode is the default and only accepts rzp_test_ keys. Live mode needs RAZORPAY_MODE=live AND
RAZORPAY_LIVE_CONFIRM set to the sentence in LIVE_CONFIRM, a deliberate step for when a
commercial licence for the face-swap model is in place. Hours are credited only after a verified
Checkout signature or a verified payment.captured / order.paid webhook, once per order.
"""
import hashlib
import hmac
import json
import os

import httpx

LIVE_CONFIRM = "I have a commercial licence and want to take real payments"
API = os.environ.get("RAZORPAY_API_URL", "https://api.razorpay.com/v1")


def load_packs() -> list:
    """PACKS='[{"id":"h3","hours":3,"price_inr":499}, ...]'. The default prices are placeholders."""
    default = [{"id": "h3", "hours": 3, "price_inr": 499}, {"id": "h5", "hours": 5, "price_inr": 799},
               {"id": "h10", "hours": 10, "price_inr": 1499}]
    try:
        packs = json.loads(os.environ.get("PACKS", "")) or default
    except ValueError:
        packs = default
    for p in packs:
        p["label"] = f"{p['hours']} hours"
        p["amount"] = int(round(float(p["price_inr"]) * 100))    # paise
    return packs


class Razorpay:
    def __init__(self, key_id="", key_secret="", webhook_secret="", mode=None, confirm=None, transport=None):
        self.key_id, self.key_secret, self.webhook_secret = key_id, key_secret, webhook_secret
        self.mode = (mode if mode is not None else os.environ.get("RAZORPAY_MODE", "test")).lower()
        self.confirm = confirm if confirm is not None else os.environ.get("RAZORPAY_LIVE_CONFIRM", "")
        self.transport = transport

    @property
    def live(self) -> bool:
        return self.mode == "live" and self.confirm == LIVE_CONFIRM

    def problem(self) -> str:
        """Why payments are off right now, or '' when they work."""
        if not (self.key_id and self.key_secret):
            return "Payments are not set up yet."
        if self.live:
            return "" if self.key_id.startswith("rzp_live_") else "Live mode needs a live key."
        if self.mode == "live":
            return "Live payments are switched off."
        return "" if self.key_id.startswith("rzp_test_") else "Payments are in test mode, but the key is not a test key."

    async def create_order(self, amount: int, receipt: str, notes: dict) -> dict:
        if self.problem():
            raise RuntimeError(self.problem())
        async with httpx.AsyncClient(timeout=20, transport=self.transport, auth=(self.key_id, self.key_secret)) as c:
            r = await c.post(f"{API}/orders", json={"amount": amount, "currency": "INR", "receipt": receipt[:40],
                                                   "notes": notes})
        if r.status_code >= 300:
            raise RuntimeError(f"Razorpay refused the order ({r.status_code}).")
        return r.json()

    def checkout_ok(self, order_id: str, payment_id: str, signature: str) -> bool:
        want = hmac.new(self.key_secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        return bool(self.key_secret) and hmac.compare_digest(want, signature or "")

    def webhook_ok(self, raw: bytes, signature: str) -> bool:
        want = hmac.new(self.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        return bool(self.webhook_secret) and hmac.compare_digest(want, signature or "")
