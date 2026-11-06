"""Stripe's own config slot. Reads ONLY PAYCANARY_STRIPE_*. A key that is not a test-mode key makes build() raise: a live key can never be loaded, and it is never
echoed. The accounts file (test connected-account ids, gitignored) is this slot's too."""
import json

PREFIX = "PAYCANARY_STRIPE_"
SLOT = (PREFIX + "SECRET_KEY", PREFIX + "WEBHOOK_SECRET", PREFIX + "ACCOUNTS_FILE")
DEFAULT_ACCOUNTS_FILE = ".stripe_test_accounts.json"


def load(env):
    return {"secret_key": env.get(PREFIX + "SECRET_KEY") or "", "webhook_secret": env.get(PREFIX + "WEBHOOK_SECRET") or "",
            "accounts_file": env.get(PREFIX + "ACCOUNTS_FILE") or DEFAULT_ACCOUNTS_FILE}


def configured(env):
    return bool(load(env)["secret_key"])


def load_accounts(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def build(env, opener=None, accounts=None):
    from .adapter import StripeProvider
    from .client import StripeClient
    c = load(env)
    return StripeProvider(StripeClient(c["secret_key"], opener=opener), webhook_secret=c["webhook_secret"],
                          accounts=accounts if accounts is not None else load_accounts(c["accounts_file"]))
