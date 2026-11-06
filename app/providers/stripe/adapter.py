"""Stripe (test mode) behind the Provider interface, via Connect Transfers: PayCanary moves test money from the platform balance to a CONNECTED ACCOUNT.
Stripe cannot pay an arbitrary e-mail, so the recipient must resolve to a connected account (explicit e-mail mapping, else a deterministic pick from a small test
pool, else the send fails closed with 'recipient_not_onboarded'). Fail-closed everywhere; error detail is a code, never a message."""
import hashlib
import json
import re
from decimal import ROUND_HALF_UP, Decimal

from ..base import Provider, SendResult, StatusResult, WebhookEvent
from .client import error_code, verify_signature

ZERO_DECIMAL = {"JPY"}                                   # the currencies PayCanary supports that Stripe treats as zero-decimal
ACCT = re.compile(r"^acct_[A-Za-z0-9]{6,}$")


def to_minor(amount, currency):
    """'12.34' USD -> 1234; '1500' JPY -> 1500."""
    d = Decimal(str(amount))
    if str(currency).upper() in ZERO_DECIMAL:
        return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class StripeProvider(Provider):
    name = "stripe"

    def __init__(self, client, webhook_secret="", accounts=None):
        self.client, self.webhook_secret = client, webhook_secret
        self.accounts = accounts or {}                    # {"emails": {email: acct_...}, "pool": [acct_...]}

    def resolve(self, recipient):
        r = str(recipient or "")
        if ACCT.match(r):
            return r
        hit = (self.accounts.get("emails") or {}).get(r.lower())
        if hit:
            return hit
        pool = self.accounts.get("pool") or []
        if pool:
            return pool[int(hashlib.sha256(r.lower().encode()).hexdigest(), 16) % len(pool)]   # test-only convenience: deterministic, logged in transfer metadata
        return None

    def send(self, req):
        self._mine(req)
        dest = self.resolve(req.recipient)
        if not dest:
            return SendResult("stripe", False, "failed", error="recipient_not_onboarded")
        try:
            status, body = self.client.create_transfer(to_minor(req.amount, req.currency), req.currency, dest, description="PayCanary st " + req.memo,
                                                       metadata={"paycanary_provider": "stripe", "paycanary_payment_id": req.payment_id, "paycanary_recipient": req.recipient[:100]},
                                                       idempotency_key=req.idempotency_key)
        except Exception as err:                          # network failure, bad amount
            return SendResult("stripe", False, "failed", error=type(err).__name__)
        if status in (200, 201) and isinstance(body, dict) and str(body.get("id", "")).startswith("tr_") and not body.get("reversed"):
            return SendResult("stripe", True, "sent", provider_id=body["id"], raw_status="created", http=status, detail={"destination": dest})
        return SendResult("stripe", False, "failed", http=status, error=error_code(body))

    def status(self, provider_id):
        status, body = self.client.get_transfer(provider_id)
        if status != 200 or not isinstance(body, dict):
            return StatusResult("stripe", provider_id, "failed", str(status))
        return StatusResult("stripe", provider_id, "reversed" if body.get("reversed") else "settled", "reversed" if body.get("reversed") else "created")

    def verify_webhook(self, headers, body):
        try:
            event = json.loads(body.decode() or "{}")
        except ValueError:
            return WebhookEvent("stripe", "", parse_error=True)
        obj = ((event.get("data") or {}).get("object")) or {}
        etype = str(event.get("type", ""))[:80]
        state = "reversed" if etype == "transfer.reversed" else ("settled" if etype in ("transfer.created", "transfer.updated") and not obj.get("reversed") else None)
        ev = WebhookEvent("stripe", etype, provider_id=str(obj.get("id", "")) if str(obj.get("id", "")).startswith("tr_") else "", state=state,
                          raw_status=etype, raw=body.decode()[:20000], verification_configured=bool(self.webhook_secret))
        if self.webhook_secret:
            h = {k.lower(): v for k, v in headers.items()}
            ev.verified = verify_signature(self.webhook_secret, h.get("stripe-signature", ""), body)
        return ev
