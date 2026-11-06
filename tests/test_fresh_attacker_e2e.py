"""End-to-end proofs for the fresh-attacker rules through the REAL Service.pay path. Security owns these; they complement
infra's unit tests. Acceptance bar: the rule catches the fresh-attacker shape, and a legitimate payee (trusted or
operator-approved, paid normally) is NOT nagged.

O2 note (RECONCILED, not shadowed): O2 (probation cap) is ARMOR, kept. On the slow-escalation ramp a known-but-unproven
payee paid $525-$2928 is a review WITH O2 and a SILENT ALLOW without it (6 verdicts on the shipped benchmark). An earlier
Security audit wrongly called it a thorn from an isolated-payment sweep; infra's benchmark differential is authoritative and
O2 stays. See PAYCANARY_O2_AND_STRUCTURING_AUDIT.md and tests/test_fresh_attacker_rules.py::O2IsNotDeadCode."""
import unittest
from tests.test_service import mk, pay, seed_history


class O6_NewPayeeVelocity(unittest.TestCase):
    def test_a_new_payee_paid_twice_inside_the_window_freezes_on_the_second(self):
        # small amounts so the SECOND rapid payment trips the new-payee BURST (count>=2), not the drain guard (amount) or
        # the replay guard (identical amount). This isolates O6.
        svc, pp, t = mk(); seed_history(svc, t)
        a = pay(svc, "fresh-mule", "9.00", email="m@newmule.example"); t[0] += 10
        b = pay(svc, "fresh-mule", "11.00", email="m@newmule.example")    # 2nd, small, different -> new-payee burst
        self.assertNotEqual(a["decision"], "freeze")                      # the first clears
        self.assertEqual(b["decision"], "freeze")                        # the SECOND inside 60s is frozen
        self.assertIn("new-payee burst", b["reasons"][0])

    def test_two_payments_to_a_new_payee_spaced_OUT_do_not_burst(self):
        svc, pp, t = mk(); seed_history(svc, t)
        pay(svc, "slow-new", "60.00", email="s@slownew.example"); t[0] += 120     # > 60s apart, different amount below
        b = pay(svc, "slow-new", "65.00", email="s@slownew.example")
        self.assertNotIn("new-payee burst", (b.get("reasons") or [""])[0])        # not a velocity burst (window cleared)

    def test_an_APPROVED_payee_is_not_burst_frozen_on_the_second(self):
        svc, pp, t = mk(); svc.set_agent("bill-bot", approved_payees=["Trusted Vendor"]); seed_history(svc, t)
        pay(svc, "Trusted Vendor", "60.00", email="t@trustedvendor.example"); t[0] += 10
        b = pay(svc, "Trusted Vendor", "70.00", email="t@trustedvendor.example")
        self.assertNotIn("new-payee burst", (b.get("reasons") or [""])[0])        # approved payees are exempt from the burst


class O2_ProbationCap_IsALargeAgentBackstop(unittest.TestCase):
    """CORRECTED finding (infra was right; my earlier 'dead code' call used a small-typical agent). The probation cap is
    min($500, 3x typical): the absolute $500 term sits BELOW the 2x-typical freeze line only once typical >= ~$250. So for
    a LARGE agent O2 is the ONLY thing flagging an unproven payee between $500 and 2x typical -- an absolute-dollar
    probation ceiling so a big-ticket agent can't send $600 to a never-proven payee unflagged. For a SMALL agent it is
    shadowed by the 2x ceiling (documented, not a defect)."""

    def _large_agent(self):
        from app import gate as G
        h = G.History(hard_cap=None); ts = 1_700_000_000.0
        import random; r = random.Random(2)
        for i in range(15):
            ts += 20 * 3600; v = max(1, round(r.gauss(470, 38), 2))
            h.observe("AWS" if i % 2 else "GH", v, ts); h.add_spend(ts, "AWS" if i % 2 else "GH", v)
        return h, ts

    def test_large_agent_probation_cap_reviews_an_unproven_payee_between_500_and_2x(self):
        from app import gate as G
        h, ts = self._large_agent(); h.note_attempt(ts + 3600, "newbig")
        tier, _, reason = G._classify_base({"payee": "newbig", "amount": 600.0, "ts": ts + 3600, "hour": 10,
                                            "stated_intent_payees": ["newbig"]}, h)
        self.assertEqual(tier, "review"); self.assertIn("probation cap", reason)   # $600 (<2x~924, >$500) -> probation review

    def test_small_agent_probation_cap_is_shadowed_by_the_2x_ceiling(self):
        from app import gate as G
        h = G.History(hard_cap=None); ts = 1_700_000_000.0
        import random; r = random.Random(3)
        for i in range(15):
            ts += 20 * 3600; v = max(1, round(r.gauss(108, 9), 2)); h.observe("AWS", v, ts); h.add_spend(ts, "AWS", v)
        h.note_attempt(ts + 3600, "newsmall")
        tier, _, reason = G._classify_base({"payee": "newsmall", "amount": 260.0, "ts": ts + 3600, "hour": 10,
                                            "stated_intent_payees": ["newsmall"]}, h)
        self.assertEqual(tier, "freeze"); self.assertNotIn("probation cap", reason)   # frozen by the 2x ceiling first

    def test_a_normal_small_payment_to_a_new_payee_clears(self):
        svc, pp, t = mk(); seed_history(svc, t)
        out = pay(svc, "smallvendor", "70.00", email="sv@smallvendor.example")      # ~typical, first payment
        self.assertIn(out["decision"], ("allow", "review"))              # a normal-sized first payment is not blocked


class O9_DisposableInboxHeldInline(unittest.TestCase):
    def test_a_first_payment_to_a_disposable_inbox_is_held_inline_no_network(self):
        svc, pp, t = mk(); seed_history(svc, t)
        before = len(pp.calls)
        out = pay(svc, "Acme Payments LLC", "60.00", email="payout@mailinator.com")
        self.assertEqual(out["status"], "frozen")                        # held for an informed review (Tier-0, weight 3)
        self.assertEqual(len(pp.calls), before)                          # no NEW PayPal call -- money did not move

    def test_a_first_payment_to_a_real_domain_is_not_held_by_evidence(self):
        svc, pp, t = mk(); seed_history(svc, t)
        out = pay(svc, "Acme Robotics", "70.00", email="ap@acmerobotics.com")       # clean domain, Tier-0 finds nothing
        self.assertNotEqual(out["status"], "frozen")                     # a real new vendor is not held by external evidence


class ZeroAddedFalseNags(unittest.TestCase):
    def test_the_legit_baseline_is_untouched_by_the_new_rules(self):
        svc, pp, t = mk(); seed_history(svc, t)
        for _ in range(4):
            t[0] += 20 * 3600
            out = pay(svc, "AWS", "60.00", email="aws@example.com")
            self.assertIn(out["decision"], ("allow", "review"))
            self.assertNotEqual(out["status"], "frozen")

    def test_a_new_vendor_paid_twice_MINUTES_apart_stays_clear_after_the_window(self):
        # infra's suggested legit case: a real new vendor paid a 2nd time just OUTSIDE the 60s velocity window must not
        # trip the new-payee burst (O6). Different amounts so the replay guard is not in play either.
        svc, pp, t = mk(); seed_history(svc, t)
        a = pay(svc, "RealNewVendor", "60.00", email="ap@realnewvendor.com"); t[0] += 70   # > 60s
        b = pay(svc, "RealNewVendor", "65.00", email="ap@realnewvendor.com")
        self.assertNotEqual(a["status"], "frozen")
        self.assertNotIn("new-payee burst", (b.get("reasons") or [""])[0])


class ServicePathAgreement(unittest.TestCase):
    """The reconciliation the CEO asked for: the fresh-payee benchmark's INLINE verdict must match what the real
    Service.pay gate does (the 371/371-style agreement). Proves 'disposable-inbox mule held inline' is true in the SHIPPED
    gate, not only in the pure harness."""
    def test_inline_tier0_agreement_between_pure_scorer_and_real_service(self):
        import contextlib, io
        from tools import score_freshpayee_service as S
        with contextlib.redirect_stdout(io.StringIO()):
            rc = S.main()
        self.assertEqual(rc, 0)   # 16/16: service holds iff the pure Tier-0 scorer says hold


class StructuringFanOutE2E(unittest.TestCase):
    """The fan-out side-door, closed, proven through the REAL Service.pay. Breadth structuring (many different new mules,
    each a small sub-threshold payment, spaced beyond the velocity/drain windows) is now frozen by the agent-wide
    untrusted-payee cumulative guard. Distinct recipients + amounts so the replay / new-payee-burst guards don't mask it."""

    def _fan_out(self, cap):
        s, pp, t = mk(); seed_history(s, t)                           # typical ~60
        if cap is not None:
            s.set_agent("bill-bot", hard_cap=cap)
        before = len(pp.calls); frozen_at = None
        for i in range(30):
            t[0] += 300                                              # 5 min apart: past velocity (>5/60s), burst (2/60s), drain (60s)
            out = pay(s, "mule%d" % i, "%.2f" % (90 + i * 0.01), email="m%d@mule%d.example" % (i, i))  # distinct amount + recipient
            if out["decision"] == "freeze":
                frozen_at = (i + 1, out["reasons"][0]); break
        new_calls = len(pp.calls) - before
        return frozen_at, new_calls

    def test_fan_out_is_frozen_around_the_sixth_mule_with_a_hard_cap(self):
        frozen_at, calls = self._fan_out(cap=1000)
        self.assertIsNotNone(frozen_at); self.assertLessEqual(frozen_at[0], 7)
        self.assertIn("structuring guard (fan-out)", frozen_at[1])
        self.assertLessEqual(calls, 6)                               # at most ~5 mules' money ($450) left, not $2700

    def test_fan_out_is_frozen_even_with_no_hard_cap(self):
        frozen_at, calls = self._fan_out(cap=None)                   # the bonus hole: cap-independent guard
        self.assertIsNotNone(frozen_at); self.assertLessEqual(frozen_at[0], 7)
        self.assertIn("structuring guard (fan-out)", frozen_at[1])

    def test_a_legit_known_vendor_day_is_untouched(self):
        s, pp, t = mk(); seed_history(s, t)                          # AWS/GH are trusted (count + age)
        for _ in range(8):
            t[0] += 2 * 3600
            out = pay(s, "AWS", "60.00", email="aws@example.com")
            self.assertNotEqual(out["status"], "frozen")             # trusted payees contribute $0 to the untrusted sum

    def test_friction_onboarding_new_vendors_in_a_day(self):
        # the measured cost: a legit agent onboarding several NEW vendors in one day. A few clear; many trip the guard;
        # operator approval clears them. This is the honest friction the fan-out guard imposes (stated in the README).
        s, pp, t = mk(); seed_history(s, t)
        cleared = 0
        for i in range(8):
            t[0] += 3600
            out = pay(s, "newvendor%d" % i, "60.00", email="ap@newvendor%d.example" % i)
            if out["decision"] != "freeze":
                cleared += 1
            else:
                self.assertIn("structuring guard (fan-out)", out["reasons"][0]); break
        self.assertGreaterEqual(cleared, 3)                          # a few new vendors a day clear (no false-nag on light onboarding)
        self.assertLess(cleared, 8)                                  # but bulk onboarding in one day trips the guard (the measured cost)
        # operator approval clears the blocked ones: approve the whole batch, they go through
        s.set_agent("bill-bot", hard_cap=None, approved_payees=["newvendor%d" % i for i in range(8)])
        t[0] += 3600
        out = pay(s, "newvendor7", "60.00", email="ap@newvendor7.example")
        self.assertNotEqual(out["status"], "frozen")                 # approved payees are exempt -> the escape hatch works


if __name__ == "__main__":
    unittest.main()
