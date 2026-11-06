"""Shadow mode suppresses enforcement for ORDINARY classifier freezes only.
Two hard safety stops are never shadowed, and must never let money move:
  1. a gate crash (the classifier raised) -> fail-closed freeze
  2. the operator's platform cap -> enforced even in shadow
And a decision that cannot be written to the audit log must fail closed:
  3. audit append returns None on the decision record -> nothing is sent.
Regression guard for the two High findings in the external review.
"""
import unittest
from app.service import Service
from app.store import Store
from app import gate as G


class FakePayPal:
    def __init__(self): self.calls = []
    def create_payout(self, recipient, amount, currency="USD", note="", batch_id=None, sender_item_id=None, **kw):
        self.calls.append((recipient, amount))
        return 201, {"batch_header": {"payout_batch_id": "B-1", "batch_status": "PENDING"}}
    def request(self, method, path, body=None, request_id=None): return 200, {"verification_status": "SUCCESS"}


def mk(mode="enforce", platform_cap=None):
    pp = FakePayPal()
    svc = Service(Store(), pp, clock=lambda: 1_700_000_000.0, default_require_intent=False,
                  default_hard_cap=None, default_mode=mode, platform_cap=platform_cap)
    return svc, pp


def req(**o):
    return {"agent_id": "a1", "recipient_email": "x@y.com", "payee": "pay1",
            "amount": "5.00", "currency": "USD", "memo": "m", "intent_id": None, **o}


class ShadowHardStops(unittest.TestCase):
    def test_gate_crash_in_shadow_still_freezes(self):
        svc, pp = mk(mode="shadow")
        orig = G.classify
        G.classify = staticmethod(lambda txn, hist: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            out = svc.pay(req())
        finally:
            G.classify = orig
        self.assertEqual(out["status"], "frozen")
        self.assertEqual(out["decision"], "freeze")
        self.assertEqual(pp.calls, [])          # nothing sent

    def test_platform_cap_in_shadow_still_freezes(self):
        svc, pp = mk(mode="shadow", platform_cap=10.0)
        out = svc.pay(req(amount="500.00"))     # over the platform cap
        self.assertEqual(out["status"], "frozen")
        self.assertEqual(pp.calls, [])          # nothing sent

    def test_ordinary_freeze_in_shadow_still_sends(self):
        # shadow mode must still do its real job for normal classifier freezes
        svc, pp = mk(mode="shadow")
        orig = G.classify
        G.classify = staticmethod(lambda txn, hist: ("freeze", "amount", "test freeze"))
        try:
            out = svc.pay(req(payee="pay4"))
        finally:
            G.classify = orig
        self.assertFalse(out.get("enforced"))
        self.assertEqual(out.get("would_have"), "freeze")
        self.assertEqual(len(pp.calls), 1)      # observed, not blocked

    def test_audit_write_failure_fails_closed(self):
        svc, pp = mk(mode="enforce")
        real = svc.audit.append
        svc.audit.append = lambda event, *a, **k: (None if event == "decision" else real(event, *a, **k))
        out = svc.pay(req(payee="pay2"))        # would normally be allowed
        self.assertEqual(out["status"], "frozen")
        self.assertEqual(pp.calls, [])          # no money moved without an audit record

    def test_foreign_currency_over_usd_platform_cap_freezes_in_shadow(self):
        # EUR 99 is ~USD 107; a USD 100 platform cap must catch it even in shadow mode
        svc, pp = mk(mode="shadow", platform_cap=100.0)
        out = svc.pay(req(amount="99.00", currency="EUR", payee="eurpay"))
        self.assertEqual(out["status"], "frozen")
        self.assertEqual(pp.calls, [])

    def test_manual_release_cannot_exceed_platform_cap(self):
        svc, pp = mk(mode="enforce", platform_cap=100.0)
        out = svc.pay(req(amount="101.00", payee="big"))      # frozen: over the platform cap
        self.assertEqual(out["status"], "frozen")
        rel = svc.release(out["payment_id"], by="operator")
        self.assertFalse(rel["ok"])                           # release refused: still over the cap
        self.assertEqual(pp.calls, [])

    def test_manual_release_fails_closed_without_audit(self):
        svc, pp = mk(mode="enforce", platform_cap=100.0)
        out = svc.pay(req(amount="500.00", payee="frz"))      # frozen
        pid = out["payment_id"]
        svc.platform_cap = 100000.0                           # raise cap so only the audit path is under test
        real = svc.audit.append
        svc.audit.append = lambda event, *a, **k: (None if event == "released" else real(event, *a, **k))
        rel = svc.release(pid, by="operator")
        self.assertFalse(rel["ok"])
        self.assertEqual(pp.calls, [])                        # nothing sent without a release record
        self.assertEqual(svc.store.payment(pid)["status"], "frozen")   # put back on the queue

    def test_in_cap_release_still_sends(self):
        svc, pp = mk(mode="enforce", platform_cap=100.0)
        out = svc.pay(req(amount="500.00", payee="z"))        # frozen
        svc.platform_cap = 100000.0                           # operator raises the ceiling, then releases
        rel = svc.release(out["payment_id"], by="operator")
        self.assertTrue(rel["ok"])
        self.assertEqual(len(pp.calls), 1)

    def test_two_workers_same_idempotency_key_send_once(self):
        # two Service instances sharing one store (two workers); the same key must send exactly once
        store = Store()
        pp = FakePayPal()
        a = Service(store, pp, clock=lambda: 1_700_000_000.0, default_require_intent=False,
                    default_hard_cap=None, default_mode="enforce")
        b = Service(store, pp, clock=lambda: 1_700_000_000.0, default_require_intent=False,
                    default_hard_cap=None, default_mode="enforce")
        a.pay(req(payee="dup"), request_id="SAME-KEY-123")
        out_b = b.pay(req(payee="dup"), request_id="SAME-KEY-123")
        self.assertTrue(out_b.get("duplicate"))
        self.assertEqual(len(pp.calls), 1)

    def test_same_key_different_payload_across_workers_is_a_conflict(self):
        # two workers, same key but DIFFERENT payload: the loser must get a conflict, not a second send
        store = Store()
        pp = FakePayPal()
        a = Service(store, pp, clock=lambda: 1_700_000_000.0, default_require_intent=False,
                    default_hard_cap=None, default_mode="enforce")
        b = Service(store, pp, clock=lambda: 1_700_000_000.0, default_require_intent=False,
                    default_hard_cap=None, default_mode="enforce")
        a.pay(req(payee="v1", amount="5.00"), request_id="K1")
        out_b = b.pay(req(payee="v2", amount="9.00"), request_id="K1")   # same key, different payload
        self.assertEqual(out_b.get("status"), "idempotency_conflict")
        self.assertEqual(len(pp.calls), 1)


if __name__ == "__main__":
    unittest.main()
