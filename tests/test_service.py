import json, unittest
from app.schema import ValidationError, validate_request
from app.service import Service
from app.store import Store


class FakePayPal:
    def __init__(self): self.calls, self.fail = [], False
    def create_payout(self, recipient, amount, currency="USD", note="", batch_id=None, sender_item_id=None, **kw):
        self.calls.append((recipient, amount, currency, batch_id))
        if self.fail: raise RuntimeError("network")
        return 201, {"batch_header": {"payout_batch_id": "B-" + str(len(self.calls)), "batch_status": "PENDING"}}
    def request(self, method, path, body=None, request_id=None): return 200, {"verification_status": "SUCCESS"}


def mk():
    t = [1_700_000_000.0]
    pp = FakePayPal()
    svc = Service(Store(), pp, clock=lambda: t[0], default_require_intent=False, default_hard_cap=None)   # legacy-permissive for the unit tests; defaults are tested below
    return svc, pp, t


def pay(svc, payee="AWS", amount="50.00", email="aws@example.com", agent="bill-bot", **kw):
    return svc.pay({"agent_id": agent, "payee": payee, "recipient_email": email, "amount": amount, "currency": "USD", "memo": "m", **kw})


def seed_history(svc, t, n=12):
    for i in range(n):
        t[0] += 20 * 3600
        out = pay(svc, "AWS" if i % 3 else "Github", "%.2f" % (55 + (i % 5) * 4))
        assert out["decision"] in ("allow", "review"), out


def seed_history_with_intent(svc, t, n=8):
    for i in range(n):
        t[0] += 20 * 3600; svc.register_intent("bill-bot", ["AWS", "Github"], "bills"); t[0] += 5
        out = pay(svc, "AWS" if i % 3 else "Github", "%.2f" % (55 + (i % 5) * 4))
        assert out["decision"] in ("allow", "review"), out
    t[0] += 120                                      # let the new-payee burst window (60s) pass before the test acts


class T(unittest.TestCase):
    def test_validation(self):
        for bad in ({}, {"agent_id": "a", "payee": "p", "recipient_email": "nope", "amount": "5"},
                    {"agent_id": "a", "payee": "p", "recipient_email": "a@b.co", "amount": "-5"},
                    {"agent_id": "a", "payee": "p", "recipient_email": "a@b.co", "amount": "1.234"},
                    {"agent_id": "a", "payee": "p", "recipient_email": "a@b.co", "amount": "NaN"},
                    {"agent_id": "a", "payee": "p\x00", "recipient_email": "a@b.co", "amount": "5"}):
            with self.assertRaises(ValidationError): validate_request(bad)

    def test_first_payee_review_then_allow_and_paypal_called(self):
        svc, pp, t = mk()
        a = pay(svc); t[0] += 120; b = pay(svc, amount="52.00")
        self.assertEqual((a["decision"], a["status"]), ("review", "sent"))
        self.assertEqual((b["decision"], b["status"]), ("allow", "sent"))
        self.assertEqual(len(pp.calls), 2)

    def test_freeze_never_calls_paypal_and_does_not_teach_baseline(self):
        svc, pp, t = mk(); seed_history(svc, t); n = len(pp.calls)
        t[0] += 60
        f = pay(svc, "NewVendor", "1900.00", "new@example.com")
        self.assertEqual((f["decision"], f["status"], f["paypal"]), ("freeze", "frozen", None))
        self.assertEqual(len(pp.calls), n)                              # PayPal untouched
        t[0] += 60
        f2 = pay(svc, "NewVendor", "1900.00", "new@example.com")
        self.assertEqual(f2["decision"], "freeze")                      # the first freeze did not teach the baseline

    def test_intent_mismatch_freezes(self):
        svc, pp, t = mk(); seed_history(svc, t)
        svc.register_intent("bill-bot", ["AWS"], "pay the AWS bill")
        t[0] += 30
        out = pay(svc, "AttackerWallet", "20.00", "x@example.com")
        self.assertEqual(out["decision"], "freeze"); self.assertIn("intent", out["reasons"][0])
        ok = pay(svc, "AWS", "55.00")
        self.assertIn(ok["decision"], ("allow", "review"))

    def test_expired_intent_does_not_silently_disable_the_check(self):
        svc, pp, t = mk(); svc.set_agent("bill-bot", require_intent=True); seed_history_with_intent(svc, t)
        svc.register_intent("bill-bot", ["AWS"], "plan"); t[0] += 16 * 60          # the plan expires (15 min TTL)
        out = pay(svc, "AWS", "55.00")
        self.assertEqual(out["decision"], "freeze"); self.assertIn("no live stated intent", out["reasons"][0])
        svc.register_intent("bill-bot", ["AWS"], "plan"); t[0] += 120
        self.assertIn(pay(svc, "AWS", "55.00")["decision"], ("allow", "review"))

    def test_concurrent_payments_are_serialised(self):
        import threading
        svc, pp, t = mk(); seed_history_with_intent(svc, t)
        svc.register_intent("bill-bot", ["AWS"], "burst"); t[0] += 5
        outs = []
        ths = [threading.Thread(target=lambda: outs.append(pay(svc, "AWS", "55.00")["decision"])) for _ in range(12)]
        [x.start() for x in ths]; [x.join() for x in ths]
        self.assertLessEqual(len(outs) - outs.count("freeze"), 5, outs)   # velocity cap is 5 per 60s: no burst slips past it
        self.assertGreaterEqual(outs.count("freeze"), 7, outs)

    def test_safe_by_default_unconfigured_agent_without_a_plan_is_frozen(self):
        t = [1_700_000_000.0]; pp = FakePayPal(); svc = Service(Store(), pp, clock=lambda: t[0])         # production defaults
        out = pay(svc, "AttackerWallet", "20.00", agent="never-configured")
        self.assertEqual(out["decision"], "freeze"); self.assertIn("no live stated intent", out["reasons"][0]); self.assertEqual(pp.calls, [])
        svc.register_intent("never-configured", ["AWS"], "pay aws"); t[0] += 120
        self.assertEqual(pay(svc, "AttackerWallet", "20.00", agent="never-configured")["decision"], "freeze")   # plan says AWS only
        self.assertEqual(pp.calls, []); t[0] += 120
        out = pay(svc, "AWS", "700.00", agent="never-configured")                                          # default cap $500
        self.assertEqual(out["decision"], "freeze"); self.assertIn("hard cap 500", out["reasons"][0]); t[0] += 120
        self.assertIn(pay(svc, "AWS", "40.00", agent="never-configured")["decision"], ("allow", "review"))  # a declared, small payment still flows
        svc.set_agent("loose", require_intent=False, hard_cap=5000)                                        # an operator can loosen it explicitly ...
        self.assertEqual(pay(svc, "Whoever", "900.00", agent="loose")["decision"], "freeze")               # ... but a never-paid payee above the floor still freezes
        svc.set_agent("loose", require_intent=False, hard_cap=5000, approved_payees=["Whoever"]); t[0] += 120          # until the operator approves that payee
        self.assertIn(pay(svc, "Whoever", "900.00", agent="loose")["decision"], ("allow", "review"))

    def test_hard_cap_and_max_multiple(self):
        svc, pp, t = mk(); svc.set_agent("bill-bot", hard_cap=500); seed_history(svc, t)
        t[0] += 30; self.assertEqual(pay(svc, "AWS", "600.00")["decision"], "freeze")
        svc2, _, t2 = mk(); seed_history(svc2, t2); t2[0] += 30
        self.assertEqual(pay(svc2, "AWS", "1500.00")["decision"], "freeze")

    def test_velocity_freezes(self):
        svc, pp, t = mk(); seed_history(svc, t); t[0] += 3600
        out = [pay(svc, "AWS", "55.00")["decision"] for _ in range(8)]
        self.assertIn("freeze", out); self.assertEqual(out[0], "allow")

    def test_release_calls_paypal_once_deny_does_not(self):
        svc, pp, t = mk(); seed_history(svc, t); n = len(pp.calls); t[0] += 30
        f = pay(svc, "NewVendor", "1900.00", "new@example.com")
        r = svc.release(f["payment_id"], "marty"); self.assertEqual(r["status"], "sent"); self.assertEqual(len(pp.calls), n + 1)
        self.assertFalse(svc.release(f["payment_id"])["ok"])             # cannot release twice
        t[0] += 30; f2 = pay(svc, "Other", "1800.00", "o@example.com")
        self.assertEqual(svc.deny(f2["payment_id"])["status"], "denied"); self.assertEqual(len(pp.calls), n + 1)
        self.assertNotIn("NewVendor", [r["payee"] for r in svc.store.baseline_rows("bill-bot")])   # a released freeze does not teach the baseline

    def test_paypal_network_failure_is_in_doubt_never_sent(self):
        # A raised create_payout (lost response / network failure) is an UNKNOWN outcome, not a confirmed non-send:
        # the money may have moved. It must never be reported as sent, and must be reserved against the cap.
        svc, pp, t = mk(); pp.fail = True
        out = pay(svc); self.assertEqual(out["status"], "in_doubt")
        self.assertNotEqual(out["status"], "sent")
        self.assertEqual(svc.store.payment(out["payment_id"])["status"], "in_doubt")

    def test_in_doubt_payment_is_reserved_against_the_cap(self):
        # Finding 1: an unconfirmed payout (lost response) must count against the rolling cap until reconciled, or the
        # next payment can breach it. The fix is that window_rows (the cap accounting) counts 'in_doubt', unlike a
        # confirmed 'failed'. Prove it: a send that raises is booked in_doubt AND shows up in the cap window.
        svc, pp, t = mk(); seed_history(svc, t); t[0] += 120
        pp.fail = True
        d1 = pay(svc, "AWS", "60.00"); self.assertEqual(d1["status"], "in_doubt")       # in-pattern amount: allowed, attempts send, response lost
        pp.fail = False
        reserved = sum(r["amount"] for r in svc.store.window_rows("bill-bot", t[0] - 3600))
        self.assertGreaterEqual(reserved, 60.0)                                         # the in_doubt $60 is reserved in the cap window
        # contrast: a CONFIRMED failure (4xx) does NOT reserve. Simulate by forcing the stored status to 'failed'.
        svc.store.update_payment(d1["payment_id"], status="paypal_error")
        after = sum(r["amount"] for r in svc.store.window_rows("bill-bot", t[0] - 3600))
        self.assertLess(after, reserved)                                                # a confirmed non-send drops out of the window

    def test_unverified_webhook_never_changes_state_by_default(self):
        svc, pp, t = mk(); out = pay(svc, "AWS", "50.00")
        ev = {"event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS", "resource": {"batch_header": {"payout_batch_id": out["paypal"]["batch_id"], "batch_status": "SUCCESS"}}}
        self.assertTrue(svc.handle_webhook({}, json.dumps(ev).encode())["ok"])                 # logged ...
        self.assertEqual(svc.store.payment(out["payment_id"])["status"], "sent")               # ... but a forged event cannot settle anything

    def test_webhook_logged_and_settles(self):
        svc, pp, t = mk(); svc.dev_webhooks = True; out = pay(svc); batch = out["paypal"]["batch_id"]
        ev = {"event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS", "resource": {"batch_header": {"payout_batch_id": batch, "batch_status": "SUCCESS"}}}
        self.assertTrue(svc.handle_webhook({}, json.dumps(ev).encode())["ok"])
        self.assertEqual(svc.store.payment(out["payment_id"])["status"], "settled")
        self.assertEqual(len(svc.store.webhooks()), 1)
        self.assertFalse(svc.handle_webhook({}, b"{not json")["ok"])

    def test_unverified_webhook_ignored_when_verification_on(self):
        svc, pp, t = mk(); svc.webhook_id = "WH-1"; pp.request = lambda *a, **k: (200, {"verification_status": "FAILURE"})
        out = pay(svc); batch = out["paypal"]["batch_id"]
        ev = {"event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS", "resource": {"batch_header": {"payout_batch_id": batch, "batch_status": "SUCCESS"}}}
        svc.handle_webhook({}, json.dumps(ev).encode())
        self.assertEqual(svc.store.payment(out["payment_id"])["status"], "sent")   # state unchanged


if __name__ == "__main__":
    unittest.main()
