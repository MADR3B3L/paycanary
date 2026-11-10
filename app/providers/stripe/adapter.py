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
        except Exception as err:
            # Lost response (timeout, dropped connection, bad-amount client error all look alike here): the transfer MAY
            # have been created before the response was lost, so the outcome is UNKNOWN -> in_doubt, reserved against the cap.
            return SendResult("stripe", False, "in_doubt", error=type(err).__name__)
        if status in (200, 201) and isinstance(body, dict) and str(body.get("id", "")).startswith("tr_") and not body.get("reversed"):
            return SendResult("stripe", True, "sent", provider_id=body["id"], raw_status="created", http=status, detail={"destination": dest})
        # A clean 4xx is a client-side rejection Stripe recorded without moving money (confirmed non-send -> failed).
        # A 5xx, or a 200 whose body is malformed/unparseable (we cannot confirm the transfer), is UNKNOWN -> in_doubt.
        if isinstance(status, int) and 400 <= status < 500 and isinstance(body, dict):
            return SendResult("stripe", False, "failed", http=status, error=error_code(body))
        return SendResult("stripe", False, "in_doubt", http=status, error=error_code(body) if isinstance(body, dict) else "unparseable_response")

    @staticmethod
    def _partially_reversed(obj):
        # A reversal removes money from cap accounting only when it is FULL. A PARTIAL reversal (amount_reversed < amount)
        # leaves net money spent, so the payment must stay counted (settled), not dropped. amount / amount_reversed are
        # integer minor units. Returns True only when we can SEE that the reversal is partial; absent amounts -> treat as
        # full (the conservative drop), matching Stripe's boolean `reversed` where no partial amount is given.
        amt, rev = obj.get("amount"), obj.get("amount_reversed")
        return isinstance(amt, (int, float)) and isinstance(rev, (int, float)) and 0 < rev < amt

    def status(self, provider_id):
        status, body = self.client.get_transfer(provider_id)
        if status != 200 or not isinstance(body, dict):
            return StatusResult("stripe", provider_id, "failed", str(status))
        if not body.get("reversed"):
            return StatusResult("stripe", provider_id, "settled", "created")
        partial = self._partially_reversed(body)
        return StatusResult("stripe", provider_id, "settled" if partial else "reversed", "partially_reversed" if partial else "reversed")

    def verify_webhook(self, headers, body):
        try:
            event = json.loads(body.decode() or "{}")
        except ValueError:
            return WebhookEvent("stripe", "", parse_error=True)
        obj = ((event.get("data") or {}).get("object")) or {}
        etype = str(event.get("type", ""))[:80]
        # A transfer.reversed event is only a cap-removing "reversed" when the reversal is FULL; a partial reversal
        # leaves net money spent, so it stays settled (still counted against the cap).
        if etype == "transfer.reversed":
            state = "settled" if self._partially_reversed(obj) else "reversed"
        elif etype in ("transfer.created", "transfer.updated") and not obj.get("reversed"):
            state = "settled"
        else:
            state = None
        ev = WebhookEvent("stripe", etype, provider_id=str(obj.get("id", "")) if str(obj.get("id", "")).startswith("tr_") else "", state=state,
                          raw_status=etype, raw=body.decode()[:20000], verification_configured=bool(self.webhook_secret))
        if self.webhook_secret:
            h = {k.lower(): v for k, v in headers.items()}
            ev.verified = verify_signature(self.webhook_secret, h.get("stripe-signature", ""), body)
        return ev
