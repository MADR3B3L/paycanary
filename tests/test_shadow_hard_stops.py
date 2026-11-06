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


if __name__ == "__main__":
    unittest.main()
