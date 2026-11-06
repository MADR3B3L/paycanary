"""Payment-event schema — the single shape the goalie and the canary both consume.

PaymentRequest (what the agent asks the proxy to do):
  agent_id, payee (display name, e.g. "AWS"), recipient_email, amount ("12.50"), currency ("USD"), memo, intent_id (optional)
Intent (what the agent SAID it would do, announced before the call):
  agent_id, stated_payees [..], plan_text, ts
Txn event (canonical, stored + scored):
  {kind:"txn", agent, payee, recipient, amount, currency, memo, ts, hour, velocity_60s,
   first_seen_payee, first_seen_amount_bucket, stated_intent_payees, intent_id, intent_match,
   verdict, score_kind, reason}
"""
import math
import re
from decimal import Decimal, InvalidOperation

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
from .fx import ZERO_DECIMAL, supported
CURRENCIES = supported()   # USD EUR GBP JPY CAD AUD: caps are in USD, other currencies are compared via app/fx.py, baselines stay per currency
MAX_TEXT = 200


class ValidationError(ValueError):
    pass


def clean_text(value, field, required=True, limit=MAX_TEXT):
    if value is None or value == "":
        if required:
            raise ValidationError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValidationError(f"{field} is too long or has control characters")
    return value


def parse_amount(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError("amount must be a decimal number")
    if not d.is_finite() or d <= 0 or d > Decimal("1000000000") or d.as_tuple().exponent < -2:
        raise ValidationError("amount must be > 0, <= 1,000,000 with at most 2 decimals")
    return d


def validate_request(req):
    """Returns a normalized dict or raises ValidationError. Never trusts types from the agent."""
    if not isinstance(req, dict):
        raise ValidationError("request must be an object")
    agent = clean_text(req.get("agent_id"), "agent_id", limit=64)
    payee = clean_text(req.get("payee"), "payee", limit=80)
    rcpt = clean_text(req.get("recipient_email"), "recipient_email", limit=120)
    if not EMAIL_RE.match(rcpt):
        raise ValidationError("recipient_email is not a valid email address")
    cur = clean_text(req.get("currency") or "USD", "currency", limit=3).upper()
    if cur not in CURRENCIES:
        raise ValidationError("unsupported currency")
    amount = parse_amount(req.get("amount"))
    from .fx import to_usd_rate
    if float(amount) * (to_usd_rate(cur) or 1.0) > 1_000_000:
        raise ValidationError("amount is above the 1,000,000 USD-equivalent ceiling")
    if cur in ZERO_DECIMAL and amount != amount.to_integral_value():
        raise ValidationError("%s amounts must be whole numbers" % cur)
    return {"agent_id": agent, "payee": payee, "recipient_email": rcpt.lower(), "amount": amount, "currency": cur,
            "memo": clean_text(req.get("memo"), "memo", required=False, limit=120),
            "idempotency_key": clean_text(req.get("idempotency_key"), "idempotency_key", required=False, limit=64),
            "intent_id": clean_text(req.get("intent_id"), "intent_id", required=False, limit=64) or None}


def amount_bucket(amount):
    """Order-of-magnitude bucket, e.g. 12.5 -> '10^1', 1500 -> '10^3'."""
    return "10^%d" % int(math.floor(math.log10(max(float(amount), 0.01))))
