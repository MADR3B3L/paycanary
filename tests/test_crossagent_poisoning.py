"""END-TO-END poisoning hard-test for the cross-agent layer (CEO requirement #3): run the actual sock-puppet attacks
through the service + reputation store, not just the pure logic. Proves the resistance, and states the honest limit
truthfully — a patient attacker with enough real aged orgs CAN earn network trust, but that trust is BOUNDED by the
per-agent goalie (it never lifts the hard cap / structuring / drain) and is REVOCABLE by a single confirmed-fraud.

Security owns these proofs. They complement infra's test_reputation_wiring.py (plumbing) and test_reputation_logic.py
(pure scoring) by exercising the whole path and the honest-limit blast radius."""
import unittest
from app.reputation_store import Reputation
from tests.test_reputation_store import KEY, mk, DAY, NOW
from tests.test_service import pay


class SockPuppetCannotManufactureTrust(unittest.TestCase):
    def test_many_agents_under_one_org_never_make_a_payee_network_trusted(self):
        rep = Reputation(KEY)
        # 100 clean payments to a mule, aged 90 days, but all from ONE customer org (as many "agents" as you like)
        for i in range(100):
            rep.contribute("mule@x.example", "Mule", "attacker-org", "executed", NOW - 90 * DAY + i * DAY)
        svc, pp, t = mk(rep)
        out = pay(svc, "Mule", "60.00", email="mule@x.example")
        # the first payment to this mule is NOT lean-allowed (no network trust) — at best a per-agent first-payee review
        self.assertNotEqual(out["decision"], "allow")

    def test_a_ring_of_fresh_orgs_converging_is_frozen_not_trusted(self):
        rep = Reputation(KEY)
        for o in range(4):                                              # 4 brand-new orgs, all first-paying the same payee this week
            rep.contribute("ring@x.example", "Ring", "fresh-org-%d" % o, "executed", NOW - 2 * DAY + o * 3600)
        svc, pp, t = mk(rep); svc.set_agent("a", approved_payees=["Ring"], org_id="victim")
        out = pay(svc, "Ring", "60.00", agent="a", email="ring@x.example")
        self.assertEqual(out["decision"], "freeze")
        self.assertIn("ring-buildup", out["reasons"][0])               # the poisoning attempt is itself the detection


class HonestLimitIsBounded(unittest.TestCase):
    """The truthful part: 5 real, aged, independent orgs CAN earn trust. We do not claim otherwise. We prove the damage
    is bounded and revocable, which is the real security property."""

    def _trusted_mule(self):
        rep = Reputation(KEY)
        for o in range(6):                                             # 6 independent orgs, aged, clean: genuinely earns network trust
            for i in range(5):
                rep.contribute("mule@slow.example", "SlowMule", "kyc-org-%d" % o, "executed", NOW - 90 * DAY + (o * 5 + i) * DAY)
        return rep

    def test_earned_trust_lean_allows_a_normal_payment(self):
        rep = self._trusted_mule()
        svc, pp, t = mk(rep)
        out = pay(svc, "SlowMule", "60.00", email="mule@slow.example")
        self.assertEqual(out["decision"], "allow")                    # honest: patient attacker reaches lean-allow on a NORMAL amount
        self.assertIn("trusted across the network", out["reasons"][0])

    def test_but_a_drain_through_the_trusted_mule_is_still_frozen_by_the_per_agent_cap(self):
        rep = self._trusted_mule()
        svc, pp, t = mk(rep); svc.set_agent("a", hard_cap=100, org_id="victim")
        out = pay(svc, "SlowMule", "5000.00", agent="a", email="mule@slow.example")
        self.assertEqual(out["status"], "frozen")                     # network trust NEVER lifts the per-agent goalie: blast radius bounded
        self.assertNotEqual(pp.calls, None)

    def test_one_confirmed_fraud_revokes_the_earned_trust_immediately(self):
        rep = self._trusted_mule()
        rep.contribute("mule@slow.example", "SlowMule", "a-victim-org", "confirmed_fraud", NOW - 1 * DAY)
        svc, pp, t = mk(rep)
        out = pay(svc, "SlowMule", "60.00", email="mule@slow.example")
        self.assertEqual(out["decision"], "freeze")                   # a single confirmed-fraud outweighs all the earned trust
        self.assertEqual(pp.calls, [])


if __name__ == "__main__":
    unittest.main()
