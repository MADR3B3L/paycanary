import unittest
from tests import poison_corpus as P


class Poisoning(unittest.TestCase):
    def test_every_escalation_walk_is_frozen_early(self):
        for a in P.attacks():
            self.assertTrue(a["caught"], a["name"])
            self.assertLessEqual(a["total_executed"], 400, a["name"])          # the old gate let ~$28k out on the noisy walks

    def test_legitimate_gradual_growth_never_false_freezes(self):
        for l in P.legits():
            self.assertEqual(l["freeze"], 0, l["name"])
        self.assertLessEqual(sum(l["review"] for l in P.legits()), 0.1 * sum(l["n"] for l in P.legits()))    # nags (reviews) stay under 10%

    def test_old_gate_loses_the_noisy_trusted_walks(self):
        old = P.ablate(RATCHET_FREEZE_MULT=1e9, RATCHET_REVIEW_MULT=1e9, DIVERGE_FREEZE=1e9, DIVERGE_REVIEW=1e9, TRUST_MIN_COUNT=1, TRUST_MIN_AGE_S=0, OTHER_PAYEE_MULT=1e9, UNTRUSTED_PAYEE_BUDGET_MULT=1e9, BURST_MULT=1e9)
        self.assertGreaterEqual(sum(not a["caught"] for a in old), 3)         # proves the corpus actually exercises the new layers


class Trust(unittest.TestCase):
    def test_trust_needs_time_and_count(self):
        from app import gate as G
        h = G.History()
        for i in range(5):
            h.observe("W", 50, 1000.0 + i * 60)                                 # 5 payments inside 5 minutes
        self.assertFalse(h.trusted("W", 1000.0 + 600))
        self.assertFalse(h.trusted("W", 1000.0 + G.TRUST_MIN_AGE_S - 1))
        self.assertTrue(h.trusted("W", 1000.0 + G.TRUST_MIN_AGE_S + 1))
        h2 = G.History(); h2.observe("V", 50, 0.0)
        self.assertFalse(h2.trusted("V", G.TRUST_MIN_AGE_S * 10))               # old but only one payment


if __name__ == "__main__":
    unittest.main()
