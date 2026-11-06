"""The mock provider has no keys and reads no environment at all."""
SLOT = ()


def load(env):
    return {}


def build(env=None, **kw):
    from .adapter import MockProvider
    return MockProvider(**kw)


def build_demo_rails():
    """-> [SimulatedRail("paypal"), SimulatedRail("stripe")]: DEMO-ONLY simulated ledgers so the offline dashboard shows both rails. Sends nothing."""
    from .adapter import SimulatedRail
    return [SimulatedRail("paypal", "PP"), SimulatedRail("stripe", "ST")]
