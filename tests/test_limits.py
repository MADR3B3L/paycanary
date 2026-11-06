import json, unittest
from app.service import Service
from app.store import Store
from tests.test_service import FakePayPal, mk, pay


def prod(**kw):
    t = [1_700_000_000.0]; pp = FakePayPal()
    return Service(Store(), pp, clock=lambda: t[0], **kw), pp, t        # production defaults: plan required, $500 cap, floor, cumulative caps


_n = [0]


def go(svc, t, payee="AWS", amount=None, dt=13, agent="a"):
    _n[0] += 1
    amount = amount or "%.2f" % (499 - (_n[0] % 7) * 0.5)          # varied cents: identical amounts would trip the replay guard first
    t[0] += dt
    svc.register_intent(agent, [payee], "pay")
    t[0] += 1
    return pay(svc, payee, amount, agent=agent)


class Structuring(unittest.TestCase):
    """Red-team round 2, finding 3: 30 x $499 under a $500 cap, paced under the velocity rule, used to move $14,970."""
    def test_thirty_payments_of_499_no_longer_move_14970(self):
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=["AWS"])
        outs = [go(svc, t) for _ in range(30)]
        moved = sum(float(c[1]) for c in pp.calls)
        self.assertLessEqual(moved, 1000.0)                                      # hourly guard: 2 x the $500 cap
        self.assertEqual(sum(o["decision"] == "freeze" for o in outs), 30 - len(pp.calls))
        self.assertIn("structuring guard", [o for o in outs if o["decision"] == "freeze"][0]["reasons"][0])

    def test_pacing_slower_hits_the_daily_and_weekly_and_per_payee_windows(self):
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=["AWS", "GitHub"])
        for i in range(60):
            go(svc, t, ["AWS", "GitHub"][i % 2], dt=3 * 3600)                    # one $499 every 3h for 7.5 days, alternating payees
        rows = [(r["ts"], r["amount"]) for r in svc.store.payments("a") if r["status"] == "sent"]
        for ts, _ in rows:                                                       # every rolling window respects its limit
            self.assertLessEqual(sum(a for t2, a in rows if ts - 86400 < t2 <= ts), 4 * 500 + 1e-6)
            self.assertLessEqual(sum(a for t2, a in rows if ts - 604800 < t2 <= ts), 10 * 500 + 1e-6)
        svc3, pp3, t3 = prod(); svc3.set_agent("a", approved_payees=["AWS"])
        for _ in range(8):
            go(svc3, t3, dt=25 * 3600)                                           # one a day to ONE payee: the per-payee weekly guard (5 x cap)
        wk = [(r["ts"], r["amount"]) for r in svc3.store.payments("a") if r["status"] == "sent"]
        for ts, _ in wk:
            self.assertLessEqual(sum(a for t2, a in wk if ts - 604800 < t2 <= ts), 5 * 500 + 1e-6)
        self.assertTrue(any(r["status"] == "frozen" for r in svc3.store.payments("a")))

    def test_identical_payment_within_minutes_is_a_possible_replay(self):
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=["AWS"])
        first = go(svc, t, amount="120.00"); second = go(svc, t, amount="120.00", dt=20)
        self.assertNotEqual(first["decision"], "freeze"); self.assertEqual(second["decision"], "freeze"); self.assertIn("replay", second["reasons"][0])
        self.assertNotEqual(go(svc, t, amount="120.00", dt=900)["decision"], "freeze")         # same amount 15 minutes later is a normal repeat

    def test_a_released_freeze_counts_as_money_that_moved(self):
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=["AWS"])
        a, b, c = (go(svc, t, dt=400) for _ in range(3))
        self.assertEqual([a["decision"] != "freeze", b["decision"] != "freeze", c["decision"]], [True, True, "freeze"])
        self.assertEqual(svc.release(c["payment_id"], "operator")["status"], "sent")
        d = go(svc, t, dt=400)
        self.assertEqual(d["decision"], "freeze")                                  # 3 x 499 already left this hour

    def test_in_flight_pending_money_counts_too(self):
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=["AWS"])
        go(svc, t, dt=100)
        row = svc.store.payments("a")[0]; svc.store.update_payment(row["payment_id"], status="pending")   # PayPal has not answered yet
        self.assertNotEqual(go(svc, t, dt=100)["decision"], "freeze")            # 998 in the hour so far, pending one included
        self.assertEqual(go(svc, t, dt=100)["decision"], "freeze")

    def test_platform_cap_is_a_ceiling_no_agent_config_can_raise(self):
        svc, pp, t = prod(platform_cap=300.0); svc.set_agent("a", hard_cap=5000, approved_payees=["AWS"])
        self.assertEqual(go(svc, t, amount="400.00")["decision"], "freeze"); self.assertEqual(pp.calls, [])


class Idempotency(unittest.TestCase):
    def test_same_key_returns_the_first_answer_and_pays_once_even_under_a_race(self):
        import threading
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=["AWS"]); svc.register_intent("a", ["AWS"], "p"); t[0] += 2
        outs = []
        def one(): outs.append(svc.pay({"agent_id": "a", "payee": "AWS", "recipient_email": "aws@example.com", "amount": "42.00", "idempotency_key": "inv-77"}))
        ths = [threading.Thread(target=one) for _ in range(10)]; [x.start() for x in ths]; [x.join() for x in ths]
        self.assertEqual(len(pp.calls), 1); self.assertEqual(len({o["payment_id"] for o in outs}), 1); self.assertEqual(sum(1 for o in outs if o.get("duplicate")), 9)
        other = svc.pay({"agent_id": "b", "payee": "AWS", "recipient_email": "aws@example.com", "amount": "42.00", "idempotency_key": "inv-77"})
        self.assertNotEqual(other["payment_id"], outs[0]["payment_id"])                      # keys are scoped per agent


class NewPayeeFloor(unittest.TestCase):
    """Findings 4 + 7: a self-asserted intent does not whitelist a payee, and a cold agent's first big payment is not just a 'review'."""
    def test_agent_names_the_attacker_in_its_own_plan_and_pays_big_cold(self):
        svc, pp, t = prod()
        svc.register_intent("a", ["AttackerWallet"], "totally legit"); t[0] += 2
        out = pay(svc, "AttackerWallet", "499.00", agent="a")
        self.assertEqual(out["decision"], "freeze"); self.assertTrue("floor" in out["reasons"][0] or "fan-out" in out["reasons"][0]); self.assertEqual(pp.calls, [])

    def test_small_first_payment_to_a_new_named_payee_is_review_and_operator_approval_lifts_the_floor(self):
        svc, pp, t = prod(); svc.register_intent("a", ["Newco"], "p"); t[0] += 2
        self.assertEqual(pay(svc, "Newco", "60.00", agent="a")["decision"], "review")
        svc.set_agent("b", approved_payees=["Landlord"]); svc.register_intent("b", ["Landlord"], "rent"); t[0] += 2
        self.assertIn(pay(svc, "Landlord", "450.00", agent="b")["decision"], ("allow", "review"))
        svc.register_intent("b", ["Stranger"], "x"); t[0] += 2
        self.assertEqual(pay(svc, "Stranger", "450.00", agent="b")["decision"], "freeze")

    def test_unsupported_currencies_and_fractional_yen_are_rejected(self):
        svc, pp, t = prod()
        for cur, amt in (("XYZ", "10.00"), ("BTC", "1.00"), ("JPY", "100.50")):
            out = svc.pay({"agent_id": "a", "payee": "X", "recipient_email": "x@example.com", "amount": amt, "currency": cur})
            self.assertEqual(out["decision"], "reject", cur)


if __name__ == "__main__":
    unittest.main()
