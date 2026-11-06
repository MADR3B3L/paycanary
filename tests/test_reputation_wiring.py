"""End-to-end wiring (store -> service -> gate -> app/reputation.py): proves the plumbing, not the detection proofs (those are Security's)."""
import unittest
from app.reputation_store import Reputation
from tests.test_reputation_store import KEY, mk, pay, DAY, NOW


def seed_trusted(rep, rcpt="billing@bigvendor.example", name="BigVendor", orgs=6, clean=5):
    for o in range(orgs):
        for i in range(clean):
            rep.contribute(rcpt, name, "org%d" % o, "executed", NOW - 90 * DAY + (o * 3 + i) * DAY)


class Wiring(unittest.TestCase):
    def test_a_payee_confirmed_fraudulent_elsewhere_freezes_even_at_a_perfectly_normal_amount(self):
        rep = Reputation(KEY); rep.contribute("mule@evil.example", "Mule", "other-org", "confirmed_fraud", NOW - DAY)
        svc, pp, t = mk(rep); svc.set_agent("a", approved_payees=["Mule"])                 # even operator-approved: the network says fraud
        out = pay(svc, "Mule", "40.00", agent="a", email="mule@evil.example")
        self.assertEqual(out["decision"], "freeze"); self.assertIn("confirmed fraudulent", out["reasons"][0]); self.assertEqual(pp.calls, [])
        ok = pay(svc, "AWS", "40.00", agent="a", email="aws@example.com"); self.assertNotEqual(ok["decision"], "freeze")   # the rest is untouched

    def test_two_independent_orgs_freezing_it_is_enough_one_is_not(self):
        rep = Reputation(KEY); rep.contribute("m@evil.example", "M", "o1", "identity_freeze", NOW - DAY)
        svc, pp, t = mk(rep); svc.set_agent("a", approved_payees=["M"])
        self.assertNotEqual(pay(svc, "M", "40.00", agent="a", email="m@evil.example")["decision"], "freeze")            # one org's freeze is not proof
        rep2 = Reputation(KEY); rep2.contribute("m@evil.example", "M", "o1", "identity_freeze", NOW - DAY); rep2.contribute("m@evil.example", "M", "o2", "denied", NOW - DAY)
        svc2, pp2, t2 = mk(rep2); svc2.set_agent("a", approved_payees=["M"])
        out = pay(svc2, "M", "40.00", agent="a", email="m@evil.example"); self.assertEqual(out["decision"], "freeze"); self.assertIn("independent organizations", out["reasons"][0])

    def test_a_network_trusted_vendor_lean_allows_a_first_payment_and_one_org_can_never_manufacture_that(self):
        rep = Reputation(KEY); seed_trusted(rep)
        svc, pp, t = mk(rep); out = pay(svc, "BigVendor", "60.00", email="billing@bigvendor.example")
        self.assertEqual(out["decision"], "allow"); self.assertIn("trusted across the network", out["reasons"][0])
        base, _, _ = mk(None); self.assertEqual(pay(base, "BigVendor", "60.00", email="billing@bigvendor.example")["decision"], "review")     # without the network: first-payee review
        sock = Reputation(KEY)
        for i in range(60): sock.contribute("puppet@x.example", "Puppet", "one-org", "executed", NOW - 90 * DAY + i * DAY)      # 60 payments from 'many agents', one org
        svc2, _, _ = mk(sock); self.assertNotEqual(pay(svc2, "Puppet", "60.00", email="puppet@x.example")["decision"], "allow")

    def test_a_trusted_vendors_name_at_a_new_destination_freezes(self):
        rep = Reputation(KEY); seed_trusted(rep)
        svc, pp, t = mk(rep); out = pay(svc, "BigVendor-Billing", "60.00", email="billing@bigvendor-pay.example")
        self.assertEqual(out["decision"], "freeze"); self.assertEqual(pp.calls, [])

    def test_unseen_by_the_whole_network_and_large_is_a_review_not_a_freeze(self):
        rep = Reputation(KEY); svc, pp, t = mk(rep); svc.set_agent("a", approved_payees=["Newco"])
        for i, (p, a) in enumerate([("A", 20), ("B", 100), ("C", 35), ("D", 90), ("A", 50), ("B", 70), ("C", 45), ("D", 60)]):          # a varied-spend, established agent
            svc.store.insert_payment({"payment_id": "h%d" % i, "agent_id": "a", "ts": NOW - (40 - 4 * i) * DAY, "payee": p, "recipient": p.lower() + "@example.com", "amount": float(a),
                                      "currency": "USD", "memo": "", "hour": 10, "intent_id": None, "verdict": "allow", "score_kind": "none", "reason": "h", "status": "sent",
                                      "paypal_batch_id": "H", "paypal_status": "S", "decided_by": "h", "event_json": "{}", "narration": None, "risk": 0, "llm_flag": None, "mode": "enforce", "enforced": 1})
        out = pay(svc, "Newco", "95.00", agent="a", email="new@newco.example")
        self.assertEqual(out["decision"], "review"); self.assertEqual(out["status"], "sent")                  # (the per-agent first-payee review already covers this; the network hint adds nothing here)

    def test_a_burst_of_brand_new_orgs_converging_on_one_new_payee_is_flagged(self):
        rep = Reputation(KEY)
        for o in range(4): rep.contribute("ring@x.example", "Ring", "fresh%d" % o, "executed", NOW - 2 * DAY + o * 3600)
        svc, pp, t = mk(rep); svc.set_agent("a", approved_payees=["Ring"])
        out = pay(svc, "Ring", "40.00", agent="a", email="ring@x.example")
        self.assertEqual(out["decision"], "freeze"); self.assertIn("ring-buildup", out["reasons"][0])

    def test_the_network_never_releases_a_per_agent_freeze(self):
        rep = Reputation(KEY); seed_trusted(rep)
        svc, pp, t = mk(rep); svc.set_agent("a", hard_cap=10)
        self.assertEqual(pay(svc, "BigVendor", "400.00", agent="a", email="billing@bigvendor.example")["status"], "frozen")        # trusted vendor, but over the agent's cap


class EndToEnd(unittest.TestCase):
    def test_the_real_path_proof_passes_every_attack_poisoning_and_benign_case(self):
        import contextlib, io
        from tools import score_crossagent_e2e as E
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(E.main(), 0)
        out = E.run()
        for label in [l for l in out if l.startswith("network_") or l == "ring_buildup"]:
            self.assertEqual(out[label][2]["base"]["freeze"], 0, label)                  # the per-agent gate alone froze none of them
            self.assertEqual(out[label][2]["net"]["freeze"], E.N, label)                 # the network froze all of them
        self.assertEqual(out["poison: 60 payments, ONE org"][2]["net"]["allow"], 0)       # one org never earns trust
        self.assertEqual(out["poison: 6 orgs earned trust, then DRAIN $900"][2]["net"]["freeze"], E.N)   # and even earned trust cannot lift the per-agent amount rules


if __name__ == "__main__":
    unittest.main()
