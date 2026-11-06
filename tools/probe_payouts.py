#!/usr/bin/env python3
"""Day-1 probe: does the sandbox token work, and does the Payouts API accept our app? Prints only statuses/ids — never
the secret or token. With PAYOUT_RECIPIENT_EMAIL set it sends ONE $1.00 sandbox payout; without it, it sends a
deliberately EMPTY payout (no money can move) just to read how the API answers (validation error = reachable+authorized,
401/403 = auth/capability problem). Falls back to testing Orders if Payouts is blocked."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config
from app.paypal import PayPalClient, PayPalError, redact

config.load_env()
c = PayPalClient(config.get("PAYPAL_CLIENT_ID"), config.get("PAYPAL_CLIENT_SECRET"), config.get("PAYPAL_ENV", "sandbox"))
try:
    c.token(force=True)
except (PayPalError, ValueError) as e:
    print("TOKEN: FAIL", e, json.dumps(getattr(e, "body", None))[:300]); sys.exit(2)
print("TOKEN: OK; scope mentions payouts:", "payouts" in c.scope.lower(), "| scope count:", len(c.scope.split()))
rcpt = config.get("PAYOUT_RECIPIENT_EMAIL")
if rcpt:
    status, body = c.create_payout(rcpt, "1.00", note="PayCanary day-1 probe")
    hdr = body.get("batch_header", {}) if isinstance(body, dict) else {}
    print("PAYOUT $1.00:", status, "| batch:", hdr.get("payout_batch_id"), "| status:", hdr.get("batch_status"))
    if status >= 400: print("  error:", json.dumps(redact(body))[:400])
else:
    status, body = c.request("POST", "/v1/payments/payouts", {"sender_batch_header": {"sender_batch_id": "pc-probe-empty"}, "items": []})
    print("PAYOUT empty-probe:", status, "|", json.dumps(redact(body))[:300])
ostatus, obody = c.create_order("1.00", description="PayCanary day-1 orders probe")
print("ORDERS create:", ostatus, "| id:", obody.get("id") if isinstance(obody, dict) else None, "| status:", obody.get("status") if isinstance(obody, dict) else None)
