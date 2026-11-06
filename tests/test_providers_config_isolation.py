"""Each provider reads ONLY its own config slot. Handed an environment that contains every other provider's keys, it must not touch them,
and it must not become configured from them."""
import importlib
import os
import unittest

PROV = os.path.join(os.path.dirname(__file__), "..", "app", "providers")
NAMES = sorted(d for d in os.listdir(PROV) if os.path.isdir(os.path.join(PROV, d)) and not d.startswith("_"))
cfg = {n: importlib.import_module("app.providers.%s.config" % n) for n in NAMES}


class Guarded(dict):
    """An environment that records every key read, and raises if a key outside `allowed` is read."""
    def __init__(self, data, allowed):
        super().__init__(data)
        self.allowed, self.touched = set(allowed), []

    def get(self, key, default=None):
        self.touched.append(key)
        if key not in self.allowed:
            raise AssertionError("config read %r, which is outside its own slot" % key)
        return super().get(key, default)


def everyone_elses_keys(name):
    env = {}
    for other in NAMES:
        if other != name:
            for k in cfg[other].SLOT:
                env[k] = "someone-elses-secret-" + other
    return env


class ConfigIsolation(unittest.TestCase):
    def test_every_provider_declares_its_slot_and_the_slots_are_disjoint(self):
        seen = {}
        for n in NAMES:
            self.assertTrue(hasattr(cfg[n], "SLOT"), n)
            for k in cfg[n].SLOT:
                self.assertNotIn(k, seen, "%s and %s both claim %s" % (n, seen.get(k), k))
                seen[k] = n

    def test_a_provider_reads_only_its_own_slot_even_when_handed_other_providers_keys(self):
        for n in NAMES:
            env = Guarded({**everyone_elses_keys(n), **{k: "mine" for k in cfg[n].SLOT}}, allowed=cfg[n].SLOT)
            cfg[n].load(env)                                       # must not raise: it never leaves its slot
            self.assertTrue(set(env.touched) <= set(cfg[n].SLOT), (n, env.touched))

    def test_other_providers_keys_never_configure_this_provider(self):
        for n in NAMES:
            if hasattr(cfg[n], "configured"):
                self.assertFalse(cfg[n].configured(everyone_elses_keys(n)), n)

    def test_the_paypal_slot_is_paypal_named_and_stays_out_of_stripe_names(self):
        for k in cfg["paypal"].SLOT:
            self.assertIn("PAYPAL", k)
            self.assertNotIn("STRIPE", k)

    def test_the_stripe_slot_is_stripe_named_and_stays_out_of_paypal_names(self):
        for k in cfg["stripe"].SLOT:
            self.assertIn("STRIPE", k)
            self.assertNotIn("PAYPAL", k)

    def test_paypal_keys_never_configure_stripe_and_stripe_keys_never_configure_paypal(self):
        self.assertFalse(cfg["stripe"].configured({"PAYCANARY_PAYPAL_CLIENT_ID": "a", "PAYPAL_CLIENT_ID": "b", "PAYCANARY_PAYPAL_CLIENT_SECRET": "c"}))
        self.assertFalse(cfg["paypal"].configured({"PAYCANARY_STRIPE_SECRET_KEY": "sk_test_" + "x" * 20}))

    def test_the_mock_provider_reads_nothing(self):
        env = Guarded({"PAYCANARY_PAYPAL_CLIENT_ID": "x", "PAYPAL_CLIENT_ID": "y"}, allowed=())
        cfg["mock"].load(env)
        self.assertEqual(env.touched, [])


    def test_no_secret_shaped_literal_is_committed_in_the_provider_code(self):
        import re
        pat = re.compile(r"(sk|rk|pk)_(live|test)_[A-Za-z0-9]{8,}|whsec_[A-Za-z0-9]{8,}|A[A-Za-z0-9_-]{60,}")
        for dp, _, fs in os.walk(PROV):
            for f in fs:
                if f.endswith(".py"):
                    with open(os.path.join(dp, f)) as fh: text = fh.read()
                    self.assertIsNone(pat.search(text), f)


    def test_factory_builds_each_provider_from_its_own_slot_and_leaves_unconfigured_ones_out(self):
        from app.providers.factory import build_registry
        self.assertEqual(build_registry({}, names=("paypal",)).names(), [])                                          # not configured -> simply not registered
        self.assertEqual(build_registry({"PAYCANARY_PAYPAL_CLIENT_ID": "a", "PAYCANARY_PAYPAL_CLIENT_SECRET": "b"}, names=("paypal",)).names(), ["paypal"])
        self.assertEqual(build_registry({"PAYPAL_CLIENT_ID": "a", "PAYPAL_CLIENT_SECRET": "b"}, names=("paypal",)).names(), ["paypal"])   # deprecated alias still works
        self.assertEqual(build_registry({}, names=("mock",)).names(), ["mock"])
        with self.assertRaises(ValueError):
            build_registry({"PAYCANARY_PAYPAL_CLIENT_ID": "a", "PAYCANARY_PAYPAL_CLIENT_SECRET": "b", "PAYCANARY_PAYPAL_ENV": "live"}, names=("paypal",))   # PayPal client refuses non-sandbox


if __name__ == "__main__":
    unittest.main()
