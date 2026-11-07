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

    def test_cooldown_release_books_spend_at_release_time(self):
        # a payment held for 2h then auto-released must count its spend in the CURRENT hour,
        # not the original (held) hour, or the rolling cap under-counts and later payments slip through.
        import json as _json
        HOUR = 3600.0
        svc, pp = mk(mode="enforce", platform_cap=100000.0)
        now = 1_700_000_000.0
        pid = "pay-tsbook01"
        orig_ts = now - 2 * HOUR
        svc.store.insert_payment({"payment_id": pid, "agent_id": "a1", "ts": orig_ts, "payee": "NewVendor",
            "recipient": "x@y.com", "amount": 99.0, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None,
            "verdict": "review", "score_kind": "cooling-off", "reason": "cooling-off: first payment", "status": "frozen",
            "provider": "paypal", "paypal_batch_id": None, "paypal_status": None, "decided_by": "goalie",
            "event_json": _json.dumps({"kind": "txn", "agent": "a1", "payee": "NewVendor", "recipient": "x@y.com",
                                       "amount": 99.0, "currency": "USD", "memo": "m", "hour": 0, "require_intent": False}),
            "narration": None, "risk": 0.1, "llm_flag": None, "mode": "enforce", "enforced": 1,
            "policy_hash": "h", "release_at": orig_ts + 1800})
        svc.store.update_payment(pid, evidence_json=_json.dumps({"hint": "none", "checked": ["domain_age_days", "blocklist"]}))
        svc._try_auto_release(svc.store.payment(pid), now)
        after = svc.store.payment(pid)
        self.assertEqual(after["status"], "sent")
        self.assertAlmostEqual(after["ts"], now, delta=5)                 # spend booked at release time
        self.assertTrue(len(svc.store.window_rows("a1", now - 1800)) >= 1)  # visible in the current window

    def test_manual_release_books_spend_at_release_time(self):
        # a manually released payment held for 2h must count its spend in the CURRENT hour, same as auto-release,
        # or the rolling cap under-counts and later automatic payments slip through.
        import json as _json
        HOUR = 3600.0
        svc, pp = mk(mode="enforce", platform_cap=100000.0)
        now = 1_700_000_000.0
        pid = "pay-manrel01"
        orig_ts = now - 2 * HOUR
        svc.store.insert_payment({"payment_id": pid, "agent_id": "a1", "ts": orig_ts, "payee": "NewVendor",
            "recipient": "x@y.com", "amount": 99.0, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None,
            "verdict": "freeze", "score_kind": "amount", "reason": "held", "status": "frozen",
            "provider": "paypal", "paypal_batch_id": None, "paypal_status": None, "decided_by": "goalie",
            "event_json": _json.dumps({"kind": "txn", "agent": "a1", "payee": "NewVendor", "recipient": "x@y.com",
                                       "amount": 99.0, "currency": "USD", "memo": "m", "hour": 0, "require_intent": False}),
            "narration": None, "risk": 0.5, "llm_flag": None, "mode": "enforce", "enforced": 1,
            "policy_hash": "h", "release_at": None})
        rel = svc.release(pid, by="operator")
        after = svc.store.payment(pid)
        self.assertTrue(rel["ok"])
        self.assertEqual(after["status"], "sent")
        self.assertAlmostEqual(after["ts"], now, delta=5)                 # spend booked at release time
        self.assertTrue(len(svc.store.window_rows("a1", now - 1800)) >= 1)

    def test_release_moves_status_and_timestamp_in_one_write(self):
        # Codex's concurrency finding: if the release flips status to 'pending' (which window_rows counts) in one write
        # and moves ts in a SECOND write, a concurrent payment can see the release in flight but booked in the OLD hour.
        # There must be NO observable DB state where the payment counts as in-flight (pending/sent) while ts is still the
        # held timestamp. We snapshot (status, ts) at every commit during the release and assert that never happens.
        import json as _json
        HOUR = 3600.0
        for path in ("manual", "auto"):
            with self.subTest(path=path):
                svc, pp = mk(mode="enforce", platform_cap=100000.0)
                now = 1_700_000_000.0
                pid = "pay-boundary-" + path
                orig_ts = now - 2 * HOUR
                svc.store.insert_payment({"payment_id": pid, "agent_id": "a1", "ts": orig_ts, "payee": "NewVendor",
                    "recipient": "x@y.com", "amount": 99.0, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None,
                    "verdict": "review" if path == "auto" else "freeze",
                    "score_kind": "cooling-off" if path == "auto" else "amount",
                    "reason": "cooling-off: first payment" if path == "auto" else "held", "status": "frozen",
                    "provider": "paypal", "paypal_batch_id": None, "paypal_status": None, "decided_by": "goalie",
                    "event_json": _json.dumps({"kind": "txn", "agent": "a1", "payee": "NewVendor", "recipient": "x@y.com",
                                               "amount": 99.0, "currency": "USD", "memo": "m", "hour": 0, "require_intent": False}),
                    "narration": None, "risk": 0.5, "llm_flag": None, "mode": "enforce", "enforced": 1,
                    "policy_hash": "h", "release_at": (orig_ts + 1800) if path == "auto" else None})
                if path == "auto":
                    svc.store.update_payment(pid, evidence_json=_json.dumps({"hint": "none", "checked": ["domain_age_days", "blocklist"]}))
                # wrap the raw db.commit so we observe the row state a concurrent reader would see after every write
                bad = []
                real_commit = svc.store.db.commit
                def spy():
                    real_commit()
                    r = svc.store.db.execute("SELECT status, ts FROM payments WHERE payment_id=?", (pid,)).fetchone()
                    if r and r["status"] in ("pending", "sent", "settled") and abs(r["ts"] - orig_ts) < 1.0:
                        bad.append((r["status"], r["ts"]))   # in-flight but still booked in the held hour: the bug
                svc.store.db.commit = spy
                try:
                    if path == "auto":
                        svc._try_auto_release(svc.store.payment(pid), now)
                    else:
                        svc.release(pid, by="operator")
                finally:
                    svc.store.db.commit = real_commit
                self.assertEqual(bad, [], "release was observable as in-flight while still booked in the held hour")
                after = svc.store.payment(pid)
                self.assertEqual(after["status"], "sent")
                self.assertAlmostEqual(after["ts"], now, delta=5)

    def test_manual_release_takes_the_decision_lock(self):
        # Codex's reverse-ordering finding: a manual release that books spend must be serialised against the
        # decision path (read-history -> classify -> insert) by the SAME lock, or an automatic payment that already
        # read stale history can still send. Prove release() actually acquires self._decide_lock around its booking.
        import threading as _th
        svc, pp = mk(mode="enforce", platform_cap=100000.0)
        now = 1_700_000_000.0
        pid = "pay-locktest"
        svc.store.insert_payment({"payment_id": pid, "agent_id": "a1", "ts": now, "payee": "V", "recipient": "x@y.com",
            "amount": 10.0, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None, "verdict": "freeze",
            "score_kind": "amount", "reason": "held", "status": "frozen", "provider": "paypal", "paypal_batch_id": None,
            "paypal_status": None, "decided_by": "goalie", "event_json": "{}", "narration": None, "risk": 0.5,
            "llm_flag": None, "mode": "enforce", "enforced": 1, "policy_hash": "h", "release_at": None})
        svc._decide_lock.acquire()                    # simulate a decision in flight holding the lock
        done = _th.Event()
        t = _th.Thread(target=lambda: (svc.release(pid, by="operator"), done.set()))
        t.start()
        blocked = not done.wait(0.3)                  # release must be unable to finish while the lock is held
        self.assertTrue(blocked, "release() did not wait on the decision lock before booking spend")
        self.assertEqual(svc.store.payment(pid)["status"], "frozen")   # not yet booked
        svc._decide_lock.release()
        t.join(2.0)
        self.assertTrue(done.is_set())
        self.assertEqual(svc.store.payment(pid)["status"], "sent")     # proceeds once the decision finishes

    def test_release_then_automatic_payment_cannot_exceed_the_rolling_cap(self):
        # End-to-end of Codex's repro: ~$90 already spent this hour, operator releases a $99 hold, then a $20
        # automatic payment arrives. $90 + $99 + $20 = $209 > a $200 rolling hourly limit, so the $20 must FREEZE.
        import json as _json
        from tests.test_service import FakePayPal as _FP
        pp = _FP()
        t = [1_700_000_000.0]
        svc = Service(Store(), pp, clock=lambda: t[0], default_require_intent=False, default_hard_cap=None, default_mode="enforce")
        svc.set_agent("a1", hard_cap=100.0)           # WINDOW_MULT hourly = 2x hard_cap = $200 rolling/hour
        now = t[0]
        # $90 already executed this hour (two trusted sends well under the per-payment cap)
        for i, amt in enumerate((45.0, 45.0)):
            svc.store.insert_payment({"payment_id": "spent%d" % i, "agent_id": "a1", "ts": now - 60, "payee": "Known%d" % i,
                "recipient": "k%d@y.com" % i, "amount": amt, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None,
                "verdict": "allow", "score_kind": "none", "reason": "ok", "status": "sent", "provider": "paypal",
                "paypal_batch_id": "B", "paypal_status": "S", "decided_by": "h", "event_json": "{}", "narration": None,
                "risk": 0.0, "llm_flag": None, "mode": "enforce", "enforced": 1, "policy_hash": "h", "release_at": None})
        # a $99 payment frozen earlier (held for the operator)
        svc.store.insert_payment({"payment_id": "held99", "agent_id": "a1", "ts": now - 120, "payee": "BigVendor",
            "recipient": "big@y.com", "amount": 99.0, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None,
            "verdict": "freeze", "score_kind": "amount", "reason": "held", "status": "frozen", "provider": "paypal",
            "paypal_batch_id": None, "paypal_status": None, "decided_by": "goalie", "event_json": "{}", "narration": None,
            "risk": 0.5, "llm_flag": None, "mode": "enforce", "enforced": 1, "policy_hash": "h", "release_at": None})
        rel = svc.release("held99", by="operator")
        self.assertTrue(rel["ok"])                                      # operator approves the hold: $189 now booked this hour
        out = svc.pay({"agent_id": "a1", "payee": "AutoVendor", "recipient_email": "auto@y.com",
                       "amount": "20.00", "currency": "USD", "memo": "m"})   # would bring the hour to $209
        self.assertEqual(out["status"], "frozen")                      # rolling cap catches it: stops at $189, not $209
        self.assertIn("cumulative", (out.get("reasons") or [""])[0].lower() + out.get("reason", "").lower())

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
