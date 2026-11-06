#!/usr/bin/env python3
"""Stripe TEST-MODE sandbox setup for the PayCanary Stripe adapter. Reads PAYCANARY_STRIPE_SECRET_KEY from the environment/.env (a non-test key is refused by the
client), prints only ids and amounts (never the key), and writes the connected-account pool to the gitignored .stripe_test_accounts.json.
  python3 tools/stripe_sandbox_setup.py probe                 # is the key valid, what is the test balance?
  python3 tools/stripe_sandbox_setup.py provision [N]         # create N test connected accounts (default 3) and save them as the pool
  python3 tools/stripe_sandbox_setup.py fund [CENTS]          # add test money to the platform balance (default 5,000,000 = $50,000.00)"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402
from app.providers.stripe.client import StripeClient, error_code  # noqa: E402

ACCOUNTS = ROOT / ".stripe_test_accounts.json"


def client():
    config.load_env()
    return StripeClient(os.environ.get("PAYCANARY_STRIPE_SECRET_KEY", ""))


def safe_message(body):
    """The provider's message with anything key-shaped removed (setup tool only; the adapter itself never surfaces messages)."""
    import re
    m = (body.get("error") or {}).get("message", "") if isinstance(body, dict) else ""
    return re.sub(r"[sr]k_(test|live)_\S*", "[key]", m)[:300]


def show(status, body):
    print("HTTP", status, "" if status < 400 else "error code: %s | %s" % (error_code(body), safe_message(body)))


def probe(c):
    st, b = c.request("GET", "/v1/balance")
    show(st, b)
    if st == 200:
        print("livemode:", b.get("livemode"), "| available:", [(x["currency"], x["amount"]) for x in b.get("available", [])], "| pending:", [(x["currency"], x["amount"]) for x in b.get("pending", [])])


V2_VERSION = "2026-10-28.endive"


def provision(c, n=3):
    """Create N test-mode CONNECTED ACCOUNTS (Accounts v2, recipient configuration with the stripe_transfers capability) and save them as the pool."""
    pool = []
    for i in range(n):
        st, b = c.request_json("POST", "/v2/core/accounts", {
            "contact_email": "st-pool-%d@st-sandbox.example" % (i + 1), "display_name": "ST Pool Recipient %d" % (i + 1),
            "identity": {"country": "us", "entity_type": "company",
                         "business_details": {"registered_name": "ST Pool Recipient %d LLC" % (i + 1),
                                              "address": {"country": "us", "line1": "354 Oyster Point Blvd", "city": "South San Francisco", "state": "CA", "postal_code": "94080"}}},
            "configuration": {"recipient": {"capabilities": {"stripe_balance": {"stripe_transfers": {"requested": True}}}}},
            "dashboard": "none", "defaults": {"responsibilities": {"fees_collector": "application", "losses_collector": "application"}},
            "include": ["configuration.recipient", "identity", "defaults"]}, idempotency_key="pc-stripe-setup-pool-v3-%d" % (i + 1), version=V2_VERSION)
        show(st, b)
        if st != 200:
            print("  detail:", safe_message(b) or str(b)[:300])
            return
        cap = (((b.get("configuration") or {}).get("recipient") or {}).get("capabilities") or {}).get("stripe_balance", {}).get("stripe_transfers", {})
        print("  account:", b.get("id"), "| stripe_transfers status:", cap.get("status"), cap.get("status_details"))
        st2, b2 = c.request_json("POST", "/v2/core/accounts/%s" % b["id"], {
            "identity": {"attestations": {"terms_of_service": {"account": {"date": "2026-11-03T12:00:00.000Z", "ip": "127.0.0.1"}}}},
            "defaults": {"profile": {"business_url": "https://st-sandbox.example"}}, "include": ["configuration.recipient", "requirements"]}, version=V2_VERSION)
        show(st2, b2)
        if st2 == 200:
            cap2 = (((b2.get("configuration") or {}).get("recipient") or {}).get("capabilities") or {}).get("stripe_balance", {}).get("stripe_transfers", {})
            print("  after test-data attestation | stripe_transfers status:", cap2.get("status"), cap2.get("status_details"))
        pool.append(b["id"])
    ACCOUNTS.write_text(json.dumps({"pool": pool, "emails": {}}, indent=1))
    os.chmod(ACCOUNTS, 0o600)
    print("saved pool of", len(pool), "to", ACCOUNTS.name)


def fund(c, cents=5_000_000):
    """Test money: a test-mode PaymentIntent confirmed with Stripe's published test payment method that lands straight in the AVAILABLE balance."""
    st, b = c.request("POST", "/v1/payment_intents", {"amount": int(cents), "currency": "usd", "payment_method": "pm_card_bypassPending", "confirm": True,
                                                       "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                                                       "description": "PayCanary test-mode balance top-up"},
                      idempotency_key="pc-stripe-setup-fund-pi-%d-%s" % (int(cents), os.environ.get("RUN_TAG", "1")))
    show(st, b)
    if st == 200:
        print("  payment_intent:", b.get("id"), "| status:", b.get("status"), "| amount:", b.get("amount"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    c = client()
    if cmd == "probe":
        probe(c)
    elif cmd == "provision":
        provision(c, int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    elif cmd == "fund":
        fund(c, int(sys.argv[2]) if len(sys.argv) > 2 else 5_000_000)
    else:
        print(__doc__)
