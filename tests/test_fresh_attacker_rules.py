import unittest
from app import gate as G
from tests.gate_patch import gate_overrides

DAY = 86400.0


def agent(payees=("A", "B", "C"), amount=400.0, n=12, **kw):
    h = G.History(hard_cap=20000, **kw)
    for i in range(n):
        h.observe(payees[i % len(payees)], amount + (i % 5) * 40, 1000.0 + i * 3 * DAY, 10)
    return h


def txn(payee, amount, ts, **kw):
    return {"kind": "txn", "payee": payee, "amount": amount, "ts": ts, "hour": 10, "stated_intent_payees": [payee], **kw}


class NewPayeeBurst(unittest.TestCase):          # O6
    def test_the_second_attempt_at_an_untrusted_payee_inside_a_minute_freezes(self):
        h = agent(); ts = 1000.0 + 50 * DAY
        h.note_attempt(ts, "Newco"); self.assertNotEqual(G._classify_base(txn("Newco", 40.0, ts), h)[0], "freeze")      # attempt 1
        h.note_attempt(ts + 20, "Newco"); tier, kind, why = G._classify_base(txn("Newco", 41.0, ts + 20), h)
        self.assertEqual(tier, "freeze"); self.assertIn("new-payee burst", why)
        h2 = agent(); h2.note_attempt(ts, "Newco"); h2.note_attempt(ts + 61, "Newco")
        self.assertNotIn("burst", G._classify_base(txn("Newco", 41.0, ts + 61), h2)[2])                                  # outside the window

    def test_trusted_and_approved_payees_are_not_subject_to_it(self):
        h = agent(); ts = 1000.0 + 50 * DAY
        h.note_attempt(ts, "A"); h.note_attempt(ts + 5, "A"); self.assertNotIn("burst", G._classify_base(txn("A", 400.0, ts + 5), h)[2])      # A is trusted (12 payments over weeks)
        h3 = agent(approved_payees=["Newco"]); h3.note_attempt(ts, "Newco"); h3.note_attempt(ts + 5, "Newco")
        self.assertNotIn("burst", G._classify_base(txn("Newco", 40.0, ts + 5), h3)[2])


class ProbationCap(unittest.TestCase):           # O2
    def untrusted_known(self, **kw):
        h = agent(**kw)
        for i in range(2): h.observe("Prob", 400.0, 1000.0 + 45 * DAY + i * 3600, 10)         # paid twice, just now: not yet trusted
        return h

    def test_a_large_single_payment_to_an_unproven_payee_is_a_review(self):
        h = self.untrusted_known(); ts = 1000.0 + 45 * DAY + 2 * 3600 + 3 * DAY
        tier, kind, why = G._classify_base(txn("Prob", 560.0, ts), h)
        self.assertEqual(tier, "review"); self.assertIn("probation cap", why)                 # 560 > min(500, 3 x 400 typical)

    def test_proven_approved_or_small_payments_are_untouched_and_the_knobs_are_per_agent(self):
        ts = 1000.0 + 45 * DAY + 2 * 3600 + 3 * DAY
        self.assertNotIn("probation", G._classify_base(txn("Prob", 460.0, ts), self.untrusted_known())[2])           # under the cap
        self.assertNotIn("probation", G._classify_base(txn("A", 560.0, ts), agent())[2])                              # A is proven
        self.assertNotIn("probation", G._classify_base(txn("Prob", 560.0, ts), self.untrusted_known(approved_payees=["Prob"]))[2])
        self.assertNotIn("probation", G._classify_base(txn("Prob", 560.0, ts), self.untrusted_known(probation_abs=900.0))[2])     # operator raised this agent's knob
        self.assertIn("probation", G._classify_base(txn("Prob", 460.0, ts), self.untrusted_known(probation_abs=450.0))[2])         # ... or lowered it


class FanOutGuard(unittest.TestCase):
    """Structuring BREADTH: many different new payees, each paid once. Per-payee budgets never accumulate; this agent-wide cap does."""
    def run_fan(self, h, n=30, spacing=300, amt=90.0, pay_ids=None):
        t = 1000.0 + 50 * DAY; total = 0.0
        for i in range(n):
            t += spacing; p = (pay_ids or ["mule%d" % i])[i % len(pay_ids or [0])] if pay_ids else "mule%d" % i
            h.note_attempt(t, p)
            tier, kind, why = G._classify_base(txn(p, amt, t), h)
            if tier == "freeze": return i + 1, total, why
            h.observe(p, amt, t, 10); h.add_spend(t, p, amt); total += amt
        return None, total, ""

    def test_thirty_new_mules_are_stopped_after_a_handful_and_the_reason_says_why(self):
        for cap in (1000, None):
            h = agent(amount=60.0, n=12, **({} if cap else {})); h.hard_cap = cap
            at, total, why = self.run_fan(h)
            self.assertIsNotNone(at, cap); self.assertLessEqual(total, 5 * h.mean_sd()[0] + 90); self.assertIn("structuring guard (fan-out)", why)

    def test_trusted_and_operator_approved_payees_never_count(self):
        h = agent(amount=60.0, n=12)                                       # A, B, C are trusted (12 payments over weeks): a busy day of known vendors is $0 here
        t = 1000.0 + 50 * DAY
        for i in range(40):
            t += 400; p = ("A", "B", "C")[i % 3]; h.note_attempt(t, p)
            self.assertNotIn("fan-out", G._classify_base(txn(p, 100.0, t), h)[2]); h.add_spend(t, p, 100.0)
        h2 = agent(amount=60.0, n=12, approved_payees=["m%d" % i for i in range(30)])
        at, total, why = self.run_fan(h2, pay_ids=["m%d" % i for i in range(30)]); self.assertNotIn("fan-out", why)

    def test_a_small_legit_onboarding_of_a_few_new_vendors_in_a_day_is_not_frozen(self):
        h = agent(amount=60.0, n=12); at, total, why = self.run_fan(h, n=3, amt=40.0)            # three new vendors, $120 in a day, typical ~$75
        self.assertIsNone(at)


class DivergenceOnTrustedPayees(unittest.TestCase):
    """A legit trusted vendor's true-up then steady growth reads as escalation (fast >> slow). For a TRUSTED payee that is a review (its own
    ratcheted ceiling still freezes real jumps); a not-yet-trusted payee still freezes on divergence."""
    def hist(self):
        h = agent(amount=100.0, n=14)
        h.fast, h.slow = 500.0, 100.0                       # recent payments run 3.3x the long-run level
        for p in ("A", "B", "C", "Prob"): h.pceil[p] = 1000.0; h.pceil_ts[p] = 1000.0 + 50 * DAY
        h.observe("Prob", 100.0, 1000.0 + 49 * DAY, 10); h.fast, h.slow = 500.0, 100.0
        return h

    def test_review_for_a_trusted_payee_freeze_for_an_unproven_one(self):
        h = self.hist(); ts = 1000.0 + 50 * DAY
        self.assertEqual(G._classify_base(txn("A", 150.0, ts), h)[0], "review"); self.assertIn("escalating", G._classify_base(txn("A", 150.0, ts), h)[2])
        tier, kind, why = G._classify_base(txn("Prob", 150.0, ts), h)
        self.assertEqual(tier, "freeze"); self.assertIn("escalation", why)


class O2IsNotDeadCode(unittest.TestCase):
    """Security's audit claimed disabling the probation cap changes no verdict. It does: on the benchmark's slow-escalation ramp, a known-but-unproven
    payee paid $2.1k-$2.9k is a REVIEW with it and a silent ALLOW without it. Keep this as the standing differential so the claim cannot return unchecked."""
    def test_disabling_probation_turns_flagged_payments_into_silent_allows_on_the_benchmark_ramp(self):
        import contextlib, io
        from tools import score_benchmark as B
        with contextlib.redirect_stdout(io.StringIO()):
            on = B.run()
        with gate_overrides(PROBATION_ABS=float("inf"), PROBATION_MULT=float("inf")), contextlib.redirect_stdout(io.StringIO()):
            off = B.run()
        changed = [(k, on[k][0], off[k][0]) for k in on if on[k][0] != off[k][0]]
        self.assertGreaterEqual(len(changed), 6)
        self.assertTrue(all(a == "review" and b == "allow" for _, a, b in changed))


if __name__ == "__main__":
    unittest.main()
