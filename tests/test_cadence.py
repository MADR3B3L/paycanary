import random, unittest
from app import gate as G
from tests.test_service import mk, pay

D = 86400.0


def monthly(svc, t, payee="Landlord", n=5, amount=100.0, days=30, jitter=1):
    svc.set_agent("bill-bot", approved_payees=["Landlord", "Contractor"])      # the operator has approved these payees (first payment > $100 needs it)
    rng = random.Random(3)
    for _ in range(n):
        t[0] += (days + rng.uniform(-jitter, jitter)) * D
        out = pay(svc, payee, "%.2f" % (amount * rng.uniform(0.99, 1.01)))
        assert out["decision"] in ("allow", "review"), out


class Cadence(unittest.TestCase):
    def test_regular_monthly_bill_on_cycle_is_allowed_and_scores_lowest(self):
        svc, pp, t = mk(); monthly(svc, t)
        t[0] += 30 * D
        out = pay(svc, "Landlord", "100.00"); row = svc.store.payment(out["payment_id"])
        self.assertEqual(out["decision"], "allow"); self.assertIn("on cadence", out["reasons"][0]); self.assertEqual(row["risk"], 3.0)
        self.assertTrue(G.History().cadence("x", 0) is None)

    def test_same_payee_off_cycle_is_flagged_not_frozen(self):
        svc, pp, t = mk(); monthly(svc, t); t[0] += 30 * D; pay(svc, "Landlord", "100.00")
        t[0] += 7 * D                                                                 # a monthly bill 7 days after the last one
        out = pay(svc, "Landlord", "100.00")
        self.assertEqual(out["decision"], "review"); self.assertIn("off-cycle", out["reasons"][0]); self.assertEqual(out["status"], "sent")
        for _ in range(2):                                                            # ... and again, and again, within one week
            t[0] += 2 * D
            self.assertEqual(pay(svc, "Landlord", "100.00")["decision"], "review")

    def test_a_late_payment_is_not_an_alarm(self):
        svc, pp, t = mk(); monthly(svc, t); t[0] += 75 * D
        self.assertEqual(pay(svc, "Landlord", "100.00")["decision"], "allow")

    def test_irregular_payee_gets_no_cadence_claims(self):
        svc, pp, t = mk(); rng = random.Random(9); svc.set_agent("bill-bot", approved_payees=["Contractor"])
        for _ in range(8):
            t[0] += rng.choice([2, 40, 5, 33, 9, 21]) * D; self.assertIn(pay(svc, "Contractor", "100.00")["decision"], ("allow", "review"))
        h = svc._history("bill-bot", t[0]); self.assertFalse(h.cadence("Contractor", t[0])["regular"])
        for gap in (1, 3, 20):
            t[0] += gap * D; out = pay(svc, "Contractor", "100.00"); self.assertNotEqual(out["decision"], "freeze"); self.assertNotIn("off-cycle", out["reasons"][0])

    def test_cadence_relearns_when_the_rhythm_really_changes(self):
        svc, pp, t = mk(); monthly(svc, t, n=4)
        verdicts = []
        for _ in range(8):                                                            # becomes weekly
            t[0] += 7 * D; verdicts.append(pay(svc, "Landlord", "100.00")["decision"])
        self.assertEqual(verdicts[-2:], ["allow", "allow"]); self.assertLessEqual(verdicts.count("review"), 3)     # a few flags while it adapts, then quiet

    def test_on_cycle_does_not_excuse_a_freeze(self):
        svc, pp, t = mk(); monthly(svc, t); t[0] += 30 * D
        self.assertEqual(pay(svc, "Landlord", "900.00")["decision"], "freeze")        # right on time, ten times the size
        svc.set_agent("bill-bot", hard_cap=50)
        self.assertEqual(pay(svc, "Landlord", "100.00")["decision"], "freeze")        # on time, over the cap


class TimeOfDay(unittest.TestCase):
    def _agent(self, n, hours=range(9, 18)):
        svc, pp, t = mk(); t[0] -= t[0] % D                                            # midnight UTC
        for i in range(n):
            t[0] += D - (t[0] % D) + list(hours)[i % len(list(hours))] * 3600
            pay(svc, ["AWS", "Github", "Stripe"][i % 3], "%.2f" % (50 + i % 4))
        return svc, t

    def test_learns_the_working_window_and_flags_3am(self):
        svc, t = self._agent(30)
        t[0] += D - (t[0] % D) + 3 * 3600
        out = pay(svc, "AWS", "52.00"); self.assertEqual(out["decision"], "review"); self.assertIn("unusual time", out["reasons"][0])
        t[0] += D - (t[0] % D) + 11 * 3600
        self.assertNotIn("unusual time", pay(svc, "AWS", "52.00")["reasons"][0])           # back inside working hours: no time claim

    def test_not_enough_history_or_a_spread_out_agent_gets_no_time_claims(self):
        svc, t = self._agent(8)
        t[0] += D - (t[0] % D) + 3 * 3600; self.assertNotEqual(pay(svc, "AWS", "52.00")["reasons"][0][:7], "unusual")
        svc, t = self._agent(40, hours=range(0, 24))
        t[0] += D - (t[0] % D) + 3 * 3600; self.assertNotIn("unusual time", pay(svc, "AWS", "52.00")["reasons"][0])

    def test_explicit_active_hours_beat_learning(self):
        svc, t = self._agent(30); svc.set_agent("bill-bot", active_hours=[3])
        t[0] += D - (t[0] % D) + 3 * 3600; self.assertNotIn("unusual time", pay(svc, "AWS", "52.00")["reasons"][0])


class Features(unittest.TestCase):
    def test_event_records_the_cadence_features(self):
        svc, pp, t = mk(); monthly(svc, t); t[0] += 30 * D
        import json
        ev = json.loads(svc.store.payment(pay(svc, "Landlord", "100.00")["payment_id"])["event_json"])
        self.assertTrue(ev["cadence"]["on_cycle"]); self.assertAlmostEqual(ev["cadence"]["typical_days"], 30, delta=2)


if __name__ == "__main__":
    unittest.main()
