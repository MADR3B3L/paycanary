"""PayPal's own config slot. Reads ONLY PAYCANARY_PAYPAL_* (and the deprecated un-prefixed PAYPAL_* names this project used before providers existed).
It is handed an environment mapping and touches no other key: no other provider's slot is ever readable from here."""
PREFIX = "PAYCANARY_PAYPAL_"
LEGACY = {"CLIENT_ID": "PAYPAL_CLIENT_ID", "CLIENT_SECRET": "PAYPAL_CLIENT_SECRET", "ENV": "PAYPAL_ENV", "WEBHOOK_ID": "PAYPAL_WEBHOOK_ID"}   # deprecated aliases
SLOT = tuple(PREFIX + k for k in LEGACY) + tuple(LEGACY.values())                                                                                   # every key this module may read


def load(env):
    def get(name, default=""):
        return env.get(PREFIX + name) or env.get(LEGACY[name]) or default
    return {"client_id": get("CLIENT_ID"), "client_secret": get("CLIENT_SECRET"), "env": get("ENV", "sandbox"), "webhook_id": get("WEBHOOK_ID")}


def configured(env):
    c = load(env)
    return bool(c["client_id"] and c["client_secret"])


def build(env, opener=None):
    """-> a PayPalProvider from this slot of `env`, or raises ValueError (PayPal's client itself refuses anything but the sandbox)."""
    from .adapter import PayPalProvider
    from .client import PayPalClient
    c = load(env)
    return PayPalProvider(PayPalClient(c["client_id"], c["client_secret"], c["env"], opener=opener), webhook_id=c["webhook_id"])
