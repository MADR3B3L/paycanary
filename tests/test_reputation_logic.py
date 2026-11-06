"""Standing tests for the PURE cross-agent reputation logic (app/reputation.py), independent of the store/wiring.

These lock the design's guarantees as code:
  * network FREEZES only on independent multi-org negative evidence (confirmed-fraud / >=2 orgs froze / mule / impersonation / ring),
  * network only RELAXES (lean-allow) a review on broad+aged+clean trust,
  * a legit-but-new vendor at a normal amount gets NO new freeze and NO new review (precision guard),
  * poisoning resistance: one org (any number of agents) can NEVER earn trust; a ring of fresh orgs is flagged, not trusted;
    the honest limit (5 aged independent orgs CAN earn trust) is stated and bounded elsewhere by the per-agent caps.
Infra's store (schema + HMAC + wiring) is tested separately; these are the detection-logic proofs."""
import unittest

from app import reputation as R

NOW = 1_000_000_000.0
DAY = 86400.0


def rec(**k):
    base = dict(recipient_key="k", name_skeletons={"mule"}, distinct_agents=0, distinct_orgs=0,
                first_seen_network_ts=NOW, clean_exec_bucket=0, freeze_count=0, distinct_orgs_froze=0,
                confirmed_fraud=False, mule_fanout_hits=0, last_activity_ts=NOW, new_orgs_in_window=0, seen=False)
    base.update(k)
    return base


class NegativeFreezes(unittest.TestCase):
    def test_network_known_mule_freezes_even_when_normal_for_this_agent(self):
        h, why = R.reputation_checks(rec(distinct_orgs_froze=2, distinct_agents=3), now=NOW, new_payee=False, amount=90, agent_typical=90)
        self.assertEqual(h, R.HINT_FREEZE); self.assertIn("independent organ", why)
        self.assertEqual(R.apply("allow", h), "freeze")        # the headline: per-agent would ALLOW, network freezes

    def test_confirmed_fraud_anywhere_freezes(self):
        h, _ = R.reputation_checks(rec(confirmed_fraud=True, distinct_orgs=9, first_seen_network_ts=NOW - 90 * DAY, clean_exec_bucket=99),
                                   now=NOW, new_payee=False, amount=90, agent_typical=90)
        self.assertEqual(h, R.HINT_FREEZE)                     # confirmed fraud overrides even a broad/aged/clean record

    def test_one_org_freeze_is_not_enough(self):
        h, _ = R.reputation_checks(rec(distinct_orgs_froze=1, distinct_agents=2), now=NOW, new_payee=True, amount=90, agent_typical=90)
        self.assertNotEqual(h, R.HINT_FREEZE)                  # a single org's freeze could be a mistake; needs independent corroboration

    def test_mule_fanout_across_orgs_freezes_a_new_payee(self):
        h, why = R.reputation_checks(rec(mule_fanout_hits=2), now=NOW, new_payee=True, amount=90, agent_typical=90)
        self.assertEqual(h, R.HINT_FREEZE); self.assertIn("mule", why)

    def test_network_beneficiary_swap_freezes(self):
        h, why = R.reputation_checks(None, now=NOW, new_payee=True, amount=90, agent_typical=90,
                                     name_skeleton="aws", recipient_key="NEW", known_high_rep={"aws": "TRUSTED"})
        self.assertEqual(h, R.HINT_FREEZE); self.assertIn("beneficiary-swap", why)

    def test_same_key_is_not_its_own_impersonator(self):
        h, _ = R.reputation_checks(None, now=NOW, new_payee=True, amount=90, agent_typical=90,
                                   name_skeleton="aws", recipient_key="AWSKEY", known_high_rep={"aws": "AWSKEY"})
        self.assertNotEqual(h, R.HINT_FREEZE)                  # paying the trusted vendor itself is fine


class Attention(unittest.TestCase):
    def test_fresh_scam_wallet_unseen_and_large_is_review_not_freeze(self):
        h, why = R.reputation_checks(None, now=NOW, new_payee=True, amount=900, agent_typical=90)
        self.assertEqual(h, R.HINT_REVIEW); self.assertIn("never been paid by anyone", why)
        self.assertEqual(R.apply("allow", h), "review")        # raises attention ...
        self.assertEqual(R.apply("freeze", h), "freeze")       # ... but never weakens a per-agent freeze

    def test_unseen_but_normal_amount_is_not_flagged(self):
        h, _ = R.reputation_checks(None, now=NOW, new_payee=True, amount=90, agent_typical=90)
        self.assertEqual(h, R.HINT_NONE)                       # unseen alone is not suspicious; only unseen AND large

    def test_known_but_network_unseen_is_NOT_flagged_without_a_network_size_signal(self):
        # the honest limit: a payee the agent knows (new_payee=False) that the network has never seen is NOT flagged,
        # because at the per-payee level "unseen" is indistinguishable from an empty/young network. Firing here would nag
        # day-one payments and break "reputation attached with no data changes nothing". Documented as a future enhancement.
        h, _ = R.reputation_checks(None, now=NOW, new_payee=False, amount=400, agent_typical=90)
        self.assertEqual(h, R.HINT_NONE)
        self.assertEqual(R.apply("allow", h), "allow")         # a known payee the per-agent gate allows is left alone


class PositiveTrustReducesFriction(unittest.TestCase):
    def test_network_trusted_vendor_onboarding_leans_allow(self):
        tr = rec(distinct_orgs=8, distinct_agents=40, first_seen_network_ts=NOW - 60 * DAY, clean_exec_bucket=50)
        h, why = R.reputation_checks(tr, now=NOW, new_payee=True, amount=90, agent_typical=90)
        self.assertEqual(h, R.HINT_LEAN_ALLOW); self.assertIn("trusted across the network", why)
        self.assertEqual(R.apply("review", h), "allow")        # network REDUCES friction on a trusted vendor's first payment here
        self.assertEqual(R.apply("freeze", h), "freeze")       # but never releases a per-agent freeze


class PrecisionGuard(unittest.TestCase):
    def test_a_legit_but_new_vendor_at_a_normal_amount_gets_no_new_freeze_or_review(self):
        # the one thing the design forbids: network must NOT turn a plausible new vendor into a block
        h = R.reputation_checks(None, now=NOW, new_payee=True, amount=90, agent_typical=90)[0]
        self.assertEqual(h, R.HINT_NONE)
        self.assertEqual(R.apply("review", h), "review")       # whatever the per-agent gate said stands, unchanged
        self.assertEqual(R.apply("allow", h), "allow")


class PoisoningResistance(unittest.TestCase):
    def test_one_org_with_many_agents_can_never_earn_trust(self):
        sock = rec(distinct_orgs=1, distinct_agents=50, first_seen_network_ts=NOW - 60 * DAY, clean_exec_bucket=500)
        self.assertFalse(R.trusted_by_network(sock, NOW))      # diversity is ORGS, not agents

    def test_a_ring_of_fresh_orgs_converging_is_flagged_not_trusted(self):
        ring = rec(new_orgs_in_window=3, distinct_orgs=3, first_seen_network_ts=NOW - 1 * DAY)
        self.assertTrue(R.ring_buildup(ring, NOW))
        h, why = R.reputation_checks(ring, now=NOW, new_payee=True, amount=90, agent_typical=90)
        self.assertEqual(h, R.HINT_FREEZE); self.assertIn("ring-buildup", why)   # the poisoning attempt becomes a detection

    def test_any_negative_evidence_disqualifies_trust(self):
        broad_but_froze = rec(distinct_orgs=9, first_seen_network_ts=NOW - 90 * DAY, clean_exec_bucket=99, distinct_orgs_froze=1)
        self.assertFalse(R.trusted_by_network(broad_but_froze, NOW))

    def test_honest_limit_five_aged_independent_orgs_can_earn_trust(self):
        # stated honestly: a patient attacker with 5 real aged orgs CAN earn trust. It is bounded by the per-agent caps
        # (trust only lean-allows; it never lifts hard cap / structuring / drain / velocity) and revocable by one fraud report.
        legit = rec(distinct_orgs=5, distinct_agents=5, first_seen_network_ts=NOW - 40 * DAY, clean_exec_bucket=25)
        self.assertTrue(R.trusted_by_network(legit, NOW))
        # and even trusted, the hint only ever lean-allows — it cannot by itself move money past the per-agent goalie
        self.assertEqual(R.apply("freeze", R.HINT_LEAN_ALLOW), "freeze")


if __name__ == "__main__":
    unittest.main()
