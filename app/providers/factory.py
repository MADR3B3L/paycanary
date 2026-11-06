"""Composition helper: the one place (besides main/seed) that names concrete providers. Core modules never import this."""
from .registry import Registry


def build_registry(env, names=("paypal",), opener=None):
    """Build a Registry holding exactly the providers in `names`, each from its OWN config slot of `env`. A provider whose slot is not configured is left out
    (it is then unknown, never substituted)."""
    reg = Registry()
    for n in names:
        if n == "paypal":
            from .paypal import config as pc
            if pc.configured(env):
                reg.register(pc.build(env, opener=opener))
        elif n == "stripe":
            from .stripe import config as sc
            if sc.configured(env):
                reg.register(sc.build(env, opener=opener))                 # raises on a non-test key: a live key is never silently skipped
        elif n == "mock":
            from .mock import config as mc
            reg.register(mc.build())
        elif n == "demo_rails":                                            # DEMO ONLY: simulated PayPal + Stripe ledgers (ids SIM-PP-/SIM-ST-), nothing is sent
            from .mock import config as mc
            for r in mc.build_demo_rails():
                reg.register(r)
        else:
            raise ValueError("unknown provider %r" % n)
    return reg


def wrap_legacy(obj, webhook_id=""):
    """Service(store, <obj>) has always accepted a PayPal client (or a test fake exposing create_payout). A Provider is used as is; anything else with
    create_payout is wrapped in the PayPal adapter. This is how every existing caller keeps working unchanged."""
    from .base import Provider
    if isinstance(obj, Provider):
        return obj
    from .paypal.adapter import PayPalProvider
    return PayPalProvider(obj, webhook_id=webhook_id)
