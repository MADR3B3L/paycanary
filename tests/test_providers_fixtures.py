"""Each provider's test identities are its own and clearly labelled: no payee name, e-mail, domain, id prefix or idempotency prefix is shared."""
import importlib
import os
import unittest

PROV = os.path.join(os.path.dirname(__file__), "..", "app", "providers")
NAMES = sorted(d for d in os.listdir(PROV) if os.path.isdir(os.path.join(PROV, d)) and not d.startswith("_"))
fx = {n: importlib.import_module("app.providers.%s.fixtures" % n) for n in NAMES}


class Fixtures(unittest.TestCase):
    def test_every_provider_has_labelled_fixtures(self):
        for n in NAMES:
            f = fx[n]
            for attr in ("NAMESPACE", "IDEMPOTENCY_PREFIX", "RECIPIENT_DOMAIN", "PAYEES", "recipient"):
                self.assertTrue(hasattr(f, attr), (n, attr))
            self.assertTrue(f.PAYEES)

    def test_namespaces_domains_and_prefixes_are_distinct_and_not_prefixes_of_each_other(self):
        for attr in ("NAMESPACE", "RECIPIENT_DOMAIN", "IDEMPOTENCY_PREFIX"):
            vals = [getattr(fx[n], attr) for n in NAMES]
            self.assertEqual(len(set(vals)), len(vals), attr)
        pre = [fx[n].IDEMPOTENCY_PREFIX for n in NAMES]
        for a in pre:
            for b in pre:
                if a is not b:
                    self.assertFalse(b.startswith(a), (a, b))

    def test_no_payee_name_or_email_is_shared_across_providers(self):
        names, emails = {}, {}
        for n in NAMES:
            for nm, em in fx[n].PAYEES:
                self.assertNotIn(nm, names, "%s and %s share the payee name %r" % (n, names.get(nm), nm))
                self.assertNotIn(em, emails, "%s and %s share %r" % (n, emails.get(em), em))
                names[nm], emails[em] = n, n
                self.assertTrue(em.endswith(fx[n].RECIPIENT_DOMAIN), (n, em))
                self.assertTrue(nm.lower().startswith(fx[n].NAMESPACE), (n, nm))                # the name itself says whose it is

    def test_recipient_mapping_is_namespaced_and_differs_per_provider_for_the_same_payee(self):
        got = {n: fx[n].recipient("AWS Billing") for n in NAMES}
        self.assertEqual(len(set(got.values())), len(got))
        for n, e in got.items():
            self.assertTrue(e.startswith(fx[n].NAMESPACE + "-") and e.endswith("@" + fx[n].RECIPIENT_DOMAIN), (n, e))


if __name__ == "__main__":
    unittest.main()
