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

    def test_platform_cap_rejects_nan_and_nonfinite(self):
        # Codex: a NaN platform cap defeats every `amount > cap` comparison, so everything sends. Reject it at construction.
        for bad in (float("nan"), float("inf"), -float("inf"), 0.0, -5.0):
            with self.subTest(cap=bad):
                with self.assertRaises(ValueError):
                    Service(Store(), FakePayPal(), clock=lambda: 1_700_000_000.0, default_require_intent=False,
                            default_hard_cap=None, default_mode="enforce", platform_cap=bad)
        # a finite positive cap and None both remain valid
        Service(Store(), FakePayPal(), default_mode="enforce", platform_cap=100.0)
        Service(Store(), FakePayPal(), default_mode="enforce", platform_cap=None)

    def test_fx_override_cannot_redefine_usd_or_use_nonfinite(self):
        # Codex: PAYCANARY_FX_JSON could set {"USD":0.01}, shrinking a $101 payment to $1.01 and sneaking it under a $100 cap.
        import os as _os
        from app import fx as _fx
        old = _os.environ.get("PAYCANARY_FX_JSON")
        try:
            _os.environ["PAYCANARY_FX_JSON"] = '{"USD": 0.01, "EUR": "nan", "GBP": -2, "JPY": 0.01}'
            t = _fx.table()
            self.assertEqual(t["USD"], 1.0)                 # USD is the unit of account: never redefined
            self.assertEqual(t["EUR"], 1.08)                # NaN ignored, default kept
            self.assertEqual(t["GBP"], 1.27)                # negative ignored, default kept
            self.assertEqual(t["JPY"], 0.01)                # a finite positive override IS honoured
            self.assertEqual(_fx.to_usd_rate("USD"), 1.0)
        finally:
            if old is None: _os.environ.pop("PAYCANARY_FX_JSON", None)
            else: _os.environ["PAYCANARY_FX_JSON"] = old

    def test_cooldown_release_applies_current_intent_policy_not_the_held_one(self):
        # Codex: a hold captured require_intent=False; an operator later turns required-intent ON; the sweep must
        # re-apply the CURRENT policy and NOT auto-release a payment that now has no stated plan.
        import json as _json
        svc, pp = mk(mode="enforce", platform_cap=100000.0)
        now = 1_700_000_000.0
        pid = "pay-intentpolicy"
        svc.store.insert_payment({"payment_id": pid, "agent_id": "a1", "ts": now - 3600, "payee": "NewVendor",
            "recipient": "x@y.com", "amount": 50.0, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None,
            "verdict": "review", "score_kind": "cooling-off", "reason": "cooling-off: first payment", "status": "frozen",
            "provider": "paypal", "paypal_batch_id": None, "paypal_status": None, "decided_by": "goalie",
            "event_json": _json.dumps({"kind": "txn", "agent": "a1", "payee": "NewVendor", "recipient": "x@y.com",
                                       "amount": 50.0, "currency": "USD", "memo": "m", "hour": 0,
                                       "stated_intent_payees": None, "intent_id": None, "require_intent": False}),
            "narration": None, "risk": 0.1, "llm_flag": None, "mode": "enforce", "enforced": 1,
            "policy_hash": "h", "release_at": now - 1})
        svc.store.update_payment(pid, evidence_json=_json.dumps({"hint": "none", "checked": ["domain_age_days", "blocklist"]}))
        svc.set_agent("a1", require_intent=True)            # operator turns required-intent ON while it is held
        res = svc._try_auto_release(svc.store.payment(pid), now)
        self.assertEqual(res[0], "held")                    # current policy requires an intent the payment never had: held for a human
        self.assertEqual(svc.store.payment(pid)["status"], "frozen")
        self.assertEqual(pp.calls, [])

    def test_audit_append_under_a_wrong_key_is_refused(self):
        # Codex: a chain sealed under key A can be APPENDED under key B (the row inserts), but verify() then fails.
        # A money-moving caller trusts a non-None append, so append MUST refuse when the key does not match the sealed chain.
        store = Store()
        a = Service(store, FakePayPal(), clock=lambda: 1_700_000_000.0, default_require_intent=False,
                    default_hard_cap=None, default_mode="enforce", audit_key="A" * 32)
        a.pay(req(payee="seedchain"))                        # seals the chain under key A and writes a keyed decision
        self.assertTrue(a.audit.verify()["ok"])
        # reopen the SAME store with a DIFFERENT key
        from app.audit import AuditLog
        wrong = AuditLog(store, clock=lambda: 1_700_000_000.0, key="B" * 32)
        seq = wrong.append("released", "operator", agent_id="a1", payment_id="x", detail={})
        self.assertIsNone(seq)                               # refused: no row written under the wrong key
        self.assertTrue(a.audit.verify()["ok"])              # chain still verifies under the real key A
        # and a money-moving release through a wrong-key service fails closed (nothing sent)
        pp2 = FakePayPal()
        bad = Service(store, pp2, clock=lambda: 1_700_000_000.0, default_require_intent=False,
                      default_hard_cap=None, default_mode="enforce", audit_key="B" * 32, platform_cap=100000.0)
        pid = "frz-wrongkey"
        bad.store.insert_payment({"payment_id": pid, "agent_id": "a1", "ts": 1_700_000_000.0, "payee": "V",
            "recipient": "x@y.com", "amount": 10.0, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None,
            "verdict": "freeze", "score_kind": "amount", "reason": "held", "status": "frozen", "provider": "paypal",
            "paypal_batch_id": None, "paypal_status": None, "decided_by": "goalie", "event_json": "{}", "narration": None,
            "risk": 0.5, "llm_flag": None, "mode": "enforce", "enforced": 1, "policy_hash": "h", "release_at": None})
        rel = bad.release(pid, by="operator")
        self.assertFalse(rel["ok"])                          # audit refused under the wrong key -> fail closed
        self.assertEqual(pp2.calls, [])
        self.assertEqual(bad.store.payment(pid)["status"], "frozen")

    def test_corrupted_chain_under_the_correct_key_refuses_sends(self):
        # Codex full-audit blocker: the key/seal check is not enough. If an existing decision row is tampered (chain
        # broken) while the seal is untouched, reopening with the CORRECT key still verifies False — and a new payment
        # must NOT send. A successful INSERT is not proof of a valid log; the chain must be healthy before any send.
        store = Store()
        clk = [1_700_000_000.0]                        # advance the clock between payments so velocity/drain guards never trip; only the audit gate can freeze
        a = Service(store, FakePayPal(), clock=lambda: clk[0], default_require_intent=False,
                    default_hard_cap=None, default_mode="enforce", audit_key="A" * 32)
        a.pay(req(payee="seed1")); clk[0] += 86400     # seals the chain under key A, writes a keyed decision
        a.pay(req(payee="seed2")); clk[0] += 86400
        self.assertTrue(a.audit.verify()["ok"])
        # corrupt a decision row's detail WITHOUT touching the seal (simulated DB corruption); drop the no-update guard first
        try:
            store.db.execute("DROP TRIGGER IF EXISTS audit_no_update")
        except Exception:
            pass
        row = store.db.execute("SELECT seq FROM audit WHERE event='decision' ORDER BY seq LIMIT 1").fetchone()
        store.db.execute("UPDATE audit SET detail=? WHERE seq=?", ('{"tampered":true}', row["seq"]))
        store.db.commit()
        # reopen the service with the CORRECT key A
        clk[0] += 86400
        pp = FakePayPal()
        reopened = Service(store, pp, clock=lambda: clk[0], default_require_intent=False,
                           default_hard_cap=None, default_mode="enforce", audit_key="A" * 32, platform_cap=100000.0)
        self.assertFalse(reopened.audit.verify()["ok"])    # correct key, but the chain is broken
        out = reopened.pay(req(payee="aftercorruption"))   # an ordinary allow that would normally send (no velocity/drain trip)
        self.assertIn(out["status"], ("frozen", "fail_closed"))   # no money moves on a broken audit chain
        self.assertNotEqual(out["status"], "sent")
        self.assertEqual(pp.calls, [])                     # nothing reached the provider

    def test_chain_corrupted_WHILE_running_refuses_the_next_send(self):
        # Fault-injection finding 3a: healthy() caches its verdict, but corruption can happen mid-run. A stale cached
        # True must NOT let a later payment move money on a now-broken chain. The money path re-verifies (fresh_check).
        store = Store()
        clk = [1_700_000_000.0]
        pp = FakePayPal()
        svc = Service(store, pp, clock=lambda: clk[0], default_require_intent=False,
                      default_hard_cap=None, default_mode="enforce", audit_key="A" * 32, platform_cap=100000.0)
        svc.pay(req(payee="seed1")); clk[0] += 86400       # seals + warms the healthy() cache (chain OK)
        self.assertTrue(svc.audit.healthy())               # cache is now True
        # corrupt a decision row on the SAME running service (live DB corruption), seal untouched
        try:
            store.db.execute("DROP TRIGGER IF EXISTS audit_no_update")
        except Exception:
            pass
        row = store.db.execute("SELECT seq FROM audit WHERE event='decision' ORDER BY seq LIMIT 1").fetchone()
        store.db.execute("UPDATE audit SET detail=? WHERE seq=?", ('{"tampered":true}', row["seq"]))
        store.db.commit()
        clk[0] += 86400
        before = len(pp.calls)
        out = svc.pay(req(payee="aftercorruption"))        # same long-lived service, cache still says True
        self.assertIn(out["status"], ("frozen", "fail_closed"))   # fresh re-verify catches it: no send
        self.assertNotEqual(out["status"], "sent")
        self.assertEqual(len(pp.calls), before)            # nothing reached the provider

    def test_deleting_the_anchor_on_a_keyed_chain_refuses_sends(self):
        # Fault-injection finding 3b: on an ALREADY-KEYED chain a missing anchor is tampering (tail-truncation evidence
        # removed), not a first-time migration. verify() reports anchor_missing with keyed=True -> must fail closed.
        import os, tempfile
        store = Store()
        clk = [1_700_000_000.0]
        anchor = os.path.join(tempfile.mkdtemp(), "anchor.log")
        svc = Service(store, FakePayPal(), clock=lambda: clk[0], default_require_intent=False, default_hard_cap=None,
                      default_mode="enforce", audit_key="A" * 32, audit_anchor=anchor, platform_cap=100000.0)
        svc.pay(req(payee="seed1")); clk[0] += 86400       # seals the chain and writes anchor lines
        self.assertTrue(os.path.exists(anchor))
        self.assertTrue(svc.audit.verify()["ok"])
        # delete the anchor of an already-keyed chain (tamper, not migration)
        os.remove(anchor)
        v = svc.audit.verify()
        self.assertTrue(v["keyed"]); self.assertEqual(v["reason"], "anchor_missing")
        # reopen with the correct key but no anchor present; the next payment must fail closed
        clk[0] += 86400
        pp = FakePayPal()
        reopened = Service(store, pp, clock=lambda: clk[0], default_require_intent=False, default_hard_cap=None,
                           default_mode="enforce", audit_key="A" * 32, audit_anchor=anchor, platform_cap=100000.0)
        self.assertFalse(reopened.audit.healthy())         # keyed chain + anchor gone = unhealthy
        out = reopened.pay(req(payee="afteranchorloss"))
        self.assertIn(out["status"], ("frozen", "fail_closed"))
        self.assertNotEqual(out["status"], "sent")
        self.assertEqual(pp.calls, [])

    def test_currency_change_keeps_recipient_identity_not_a_brand_new_payee(self):
        # Fault-injection finding 2: a payee paid in USD, then paid in EUR, must NOT be treated as a brand-new recipient.
        # Identity carries across currencies (not first_seen); the amount baseline stays currency-separate.
        store = Store()
        clk = [1_700_000_000.0]
        svc = Service(store, FakePayPal(), clock=lambda: clk[0], default_require_intent=False,
                      default_hard_cap=None, default_mode="enforce")
        pr = lambda cur, amt: svc.pay({"agent_id": "a", "payee": "Acme", "recipient_email": "acme@x.com",
                                       "amount": amt, "currency": cur, "memo": "m"})
        for _ in range(12):
            clk[0] += 20 * 3600; pr("USD", "60.00")        # build USD history for Acme
        clk[0] += 120
        # now a EUR payment to the SAME payee: history is rebuilt for the EUR decision
        hist = svc._history("a", clk[0], hint=("acme@x.com", "Acme", "EUR"))
        self.assertIn("Acme", hist.payees)                 # identity known across currencies -> NOT first_seen
        self.assertEqual(hist.payees.get("Acme"), 0)       # but no EUR amount trust yet (count 0): first-EUR-amount caution still applies
        self.assertNotIn("Acme", [p for p, amts in hist.by_payee.items()])   # no EUR amount baseline pooled from USD

    def test_released_payment_establishes_the_recipient_for_later_address_checks(self):
        # Codex 7-pass: when an operator releases a frozen payment to payee P at destination D, a LATER payment to P at a
        # DIFFERENT destination must be caught as a changed-destination. The released payment establishes D as known for
        # P (identity only, not the amount baseline).
        store = Store()
        clk = [1_700_000_000.0]
        svc = Service(store, FakePayPal(), clock=lambda: clk[0], default_require_intent=False, default_hard_cap=None,
                      default_mode="enforce", platform_cap=1_000_000.0)
        # first payment to a brand-new payee freezes (new payee / new destination); operator releases it
        f = svc.pay({"agent_id": "a", "payee": "Vendor", "recipient_email": "pay@vendor.com", "amount": "9000.00", "currency": "USD", "memo": "m"})
        self.assertEqual(f["status"], "frozen")
        rel = svc.release(f["payment_id"], "operator"); self.assertTrue(rel["ok"])   # money moved to pay@vendor.com
        clk[0] += 3600
        # the released recipient is now a KNOWN destination for Vendor
        hist = svc._history("a", clk[0], hint=("pay@vendor.com", "Vendor", "USD"))
        self.assertIn("pay@vendor.com", hist.payee_recipients.get("Vendor", set()))
        # but the amount baseline is NOT taught by the released freeze
        self.assertNotIn("Vendor", [p for p, amts in hist.by_payee.items()])

    def test_a_nonpayment_event_cannot_resurrect_a_deleted_anchor(self):
        # Codex 7-pass finding 4: if the anchor is deleted mid-run, a NON-payment audit event (e.g. auth_denied) must not
        # recreate a partial anchor file that then lets the next payment's verify pass. A deleted anchor stays deleted.
        import os, tempfile
        store = Store()
        clk = [1_700_000_000.0]
        anchor = os.path.join(tempfile.mkdtemp(), "anchor.log")
        pp = FakePayPal()
        svc = Service(store, pp, clock=lambda: clk[0], default_require_intent=False, default_hard_cap=None,
                      default_mode="enforce", audit_key="A" * 32, audit_anchor=anchor, platform_cap=100000.0)
        svc.pay(req(payee="seed1")); clk[0] += 86400         # seals the chain, writes anchor
        self.assertTrue(os.path.exists(anchor))
        os.remove(anchor)                                    # tamper: anchor deleted while the service runs
        svc.audit.append("auth_denied", "unknown", detail={"why": "probe"})   # a non-payment event
        self.assertFalse(os.path.exists(anchor), "a non-payment event must NOT recreate the deleted anchor")
        before = len(pp.calls)
        out = svc.pay(req(payee="afterprobe"))               # the next payment re-verifies and sees the anchor still gone
        self.assertIn(out["status"], ("frozen", "fail_closed")); self.assertNotEqual(out["status"], "sent")
        self.assertEqual(len(pp.calls), before)

    def test_gate_freezes_a_non_finite_amount_fail_closed(self):
        # Region A hardening: the gate is the fail-closed in-path backstop and must not rely on upstream validation to
        # never pass NaN/Inf. NaN <= 0 is False, so without a finiteness check NaN falls through every > / >= freeze test
        # to 'allow'. The gate itself must freeze a non-finite amount.
        from app import gate as _G
        hist = _G.History(hard_cap=None)                 # a real, empty history (no hard cap) so nothing but the gate's own guards can freeze
        for bad in (float("nan"), float("inf"), float("-inf")):
            tier, _kind, _reason = _G.classify({"payee": "V", "amount": bad, "ts": 0.0, "hour": 0}, hist)
            self.assertEqual(tier, "freeze", "non-finite amount %r must freeze" % bad)

    def test_reversed_is_terminal_a_replayed_settle_webhook_cannot_un_reverse(self):
        # Boundary finding (Dot's method, state-machine promise): a full reversal drops a payment from the cap. Webhooks
        # are at-least-once and can reorder, so a replayed/out-of-order settle event must NOT flip reversed -> settled
        # (that would re-count returned money against the cap and falsely record the payout as settled). reversed is terminal.
        import json as _json
        from app.providers.registry import Registry
        from app.providers.stripe.adapter import StripeProvider
        store = Store()
        reg = Registry(); reg.register(StripeProvider(object(), webhook_secret=""))     # no secret -> dev_webhooks path
        svc = Service(store, None, clock=lambda: 1_700_000_000.0, default_require_intent=False, default_hard_cap=None,
                      default_mode="enforce", providers=reg, default_provider="stripe", dev_webhooks=True, platform_cap=100000.0)
        pid = "pay-rev01"
        store.insert_payment({"payment_id": pid, "agent_id": "a1", "ts": 1_700_000_000.0, "payee": "V", "recipient": "x@y.com",
            "amount": 500.0, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None, "verdict": "allow",
            "score_kind": "ok", "reason": "", "status": "sent", "provider": "stripe", "paypal_batch_id": None,
            "paypal_status": None, "decided_by": None, "event_json": "{}", "narration": None, "risk": 0.1, "llm_flag": None,
            "mode": "enforce", "enforced": 1, "policy_hash": "h", "release_at": None})
        store.update_payment(pid, provider_ref="stripe:tr_rev01")
        def wh(obj): return svc.handle_webhook({}, _json.dumps({"type": obj.pop("_type"), "data": {"object": obj}}).encode(), provider="stripe")
        # full reversal: drops from the cap
        wh({"_type": "transfer.reversed", "id": "tr_rev01", "amount": 50000, "amount_reversed": 50000})
        self.assertEqual(store.payment(pid)["status"], "reversed")
        self.assertEqual(sum(r["amount"] for r in store.window_rows("a1", 0)), 0.0)   # money came back, excluded from cap
        # replayed / reordered settle event must NOT un-reverse it
        wh({"_type": "transfer.created", "id": "tr_rev01"})
        wh({"_type": "transfer.updated", "id": "tr_rev01"})
        self.assertEqual(store.payment(pid)["status"], "reversed")                    # still terminal
        self.assertEqual(sum(r["amount"] for r in store.window_rows("a1", 0)), 0.0)   # returned money NOT re-counted

    def test_concurrent_reversed_and_settle_webhooks_never_leave_a_payment_settled(self):
        # Boundary finding (Dot's method, NEARBY VARIANT of the replay test above, concurrency angle): the same two events
        # can arrive CONCURRENTLY, not just reordered. A read-then-write guard (read status -> decide legal -> write) would
        # let both a 'reversed' and a 'settled' thread read 'sent', both judge the move legal, and both write -- so a settle
        # could land AFTER a reverse and re-count returned money. The transition must be a compare-and-swap whose WHERE clause
        # is the legality check. Promise: once reversed, a racing settle can never un-reverse the payment or revive its cap spend.
        import json as _json, threading
        from app.providers.registry import Registry
        from app.providers.stripe.adapter import StripeProvider
        for trial in range(40):                                                          # many trials: the race window is tiny
            store = Store()
            reg = Registry(); reg.register(StripeProvider(object(), webhook_secret=""))
            svc = Service(store, None, clock=lambda: 1_700_000_000.0, default_require_intent=False, default_hard_cap=None,
                          default_mode="enforce", providers=reg, default_provider="stripe", dev_webhooks=True, platform_cap=100000.0)
            pid = "pay-race%02d" % trial
            store.insert_payment({"payment_id": pid, "agent_id": "a1", "ts": 1_700_000_000.0, "payee": "V", "recipient": "x@y.com",
                "amount": 500.0, "currency": "USD", "memo": "m", "hour": 0, "intent_id": None, "verdict": "allow",
                "score_kind": "ok", "reason": "", "status": "sent", "provider": "stripe", "paypal_batch_id": None,
                "paypal_status": None, "decided_by": None, "event_json": "{}", "narration": None, "risk": 0.1, "llm_flag": None,
                "mode": "enforce", "enforced": 1, "policy_hash": "h", "release_at": None})
            store.update_payment(pid, provider_ref="stripe:tr_race%02d" % trial)
            def wh(obj):
                obj = dict(obj); t = obj.pop("_type")
                svc.handle_webhook({}, _json.dumps({"type": t, "data": {"object": obj}}).encode(), provider="stripe")
            rev = {"_type": "transfer.reversed", "id": "tr_race%02d" % trial, "amount": 50000, "amount_reversed": 50000}
            set_ = {"_type": "transfer.updated", "id": "tr_race%02d" % trial}
            start = threading.Barrier(2)
            def run(ev):
                start.wait()                                                             # release both threads together
                wh(ev)
            ths = [threading.Thread(target=run, args=(rev,)), threading.Thread(target=run, args=(set_,))]
            [x.start() for x in ths]; [x.join() for x in ths]
            # whichever order the two threads ran, 'reversed' is terminal: a settle can never be the final state,
            # and returned money is never counted against the cap.
            self.assertEqual(store.payment(pid)["status"], "reversed", "trial %d left it settled" % trial)
            self.assertEqual(sum(r["amount"] for r in store.window_rows("a1", 0)), 0.0, "trial %d re-counted returned money" % trial)

    def test_an_adapter_that_raises_mid_send_is_in_doubt_not_failed(self):
        # Defense-in-depth (security fault-injection note): if a provider adapter raises an UNEXPECTED exception mid-send
        # (not a structural ProviderError), _send must treat it as UNKNOWN -> in_doubt (reserved against the cap), never
        # a confirmed 'failed' that drops from the cap.
        svc, pp = mk()
        def boom(_req): raise RuntimeError("adapter forgot to catch")
        svc.providers.get("paypal").send = boom                 # make the adapter raise unexpectedly mid-send
        out = svc.pay(req(amount="60.00"))
        self.assertEqual(out["status"], "in_doubt"); self.assertNotEqual(out["status"], "sent")
        reserved = sum(r["amount"] for r in svc.store.window_rows("a1", 1_700_000_000.0 - 3600))
        self.assertGreaterEqual(reserved, 60.0)                 # the uncertain send is reserved against the cap

    def test_paypal_malformed_success_body_is_in_doubt_not_failed(self):
        # Codex confirm-pass edge 1: a 2xx whose batch_header is an unexpected type (not a dict with a batch id) means
        # PayPal most likely ACCEPTED the payout but we can't parse the id -> in_doubt (reserved), never a confirmed non-send.
        from app.providers.paypal.adapter import PayPalProvider
        from app.providers.base import SendRequest
        class Malformed:
            def create_payout(self, *a, **k): return 201, {"batch_header": "oops-a-string"}   # wrong type
        res = PayPalProvider(Malformed()).send(SendRequest("paypal", "p1", "x@y.com", "100.00", "USD", "m", "k1"))
        self.assertEqual(res.state, "in_doubt"); self.assertFalse(res.ok)
        class ListHdr:
            def create_payout(self, *a, **k): return 200, {"batch_header": ["nope"]}
        self.assertEqual(PayPalProvider(ListHdr()).send(SendRequest("paypal", "p2", "x@y.com", "100.00", "USD", "m", "k2")).state, "in_doubt")
        class CleanFail:
            def create_payout(self, *a, **k): return 400, {"name": "VALIDATION_ERROR"}
        self.assertEqual(PayPalProvider(CleanFail()).send(SendRequest("paypal", "p3", "x@y.com", "100.00", "USD", "m", "k3")).state, "failed")

    def test_emptied_audit_anchor_is_treated_as_tampered(self):
        # Codex confirm-pass edge 2: truncating the anchor to an EMPTY file (not deleting it) must not pass verification.
        # An empty anchor on a chain with rows wiped the published entries -> tamper -> fail closed.
        import os, tempfile
        store = Store()
        clk = [1_700_000_000.0]
        anchor = os.path.join(tempfile.mkdtemp(), "anchor.log")
        svc = Service(store, FakePayPal(), clock=lambda: clk[0], default_require_intent=False, default_hard_cap=None,
                      default_mode="enforce", audit_key="A" * 32, audit_anchor=anchor, platform_cap=100000.0)
        svc.pay(req(payee="seed1")); clk[0] += 86400
        self.assertTrue(svc.audit.verify()["ok"])
        open(anchor, "w").close()                              # truncate to empty (not delete)
        v = svc.audit.verify()
        self.assertFalse(v["ok"]); self.assertEqual(v["reason"], "anchor_missing")
        clk[0] += 86400
        pp = FakePayPal()
        reopened = Service(store, pp, clock=lambda: clk[0], default_require_intent=False, default_hard_cap=None,
                           default_mode="enforce", audit_key="A" * 32, audit_anchor=anchor, platform_cap=100000.0)
        out = reopened.pay(req(payee="afteremptyanchor"))
        self.assertIn(out["status"], ("frozen", "fail_closed")); self.assertNotEqual(out["status"], "sent")
        self.assertEqual(pp.calls, [])

    def test_service_is_published_only_fully_configured(self):
        # Codex confirm-pass edge 3: main.service() must not expose a half-configured _service (e.g. before the reputation
        # guard loads). It builds a LOCAL svc, configures it, and assigns _service as the LAST statement. Assert that any
        # thread observing a non-None _service sees reputation already attached when the config asks for it.
        import os, threading
        from app import main
        from app.store import Store as S
        os.environ["PAYCANARY_REPUTATION"] = "1"
        os.environ.setdefault("PAYCANARY_REPUTATION_KEY", "k" * 32)
        try:
            main._store = S(":memory:"); main._service = None; main._registry = None
            observed = []
            barrier = threading.Barrier(8)
            def worker():
                barrier.wait()
                svc = main.service()
                observed.append(getattr(svc, "reputation", None) is not None)   # must be fully configured when seen
            ts = [threading.Thread(target=worker) for _ in range(8)]
            for t in ts: t.start()
            for t in ts: t.join()
            self.assertTrue(all(observed), "every thread must see a fully-configured service (reputation attached)")
        finally:
            os.environ.pop("PAYCANARY_REPUTATION", None)
            main._service = None

    def test_malformed_signed_reversal_keeps_money_reserved(self):
        # Codex confirm-pass hardening: a signed but MALFORMED reversal (inconsistent amounts) must not release the full
        # reservation. Only a clean full reversal releases; malformed -> keep reserved (settled).
        from app.providers.stripe.adapter import StripeProvider
        import json as _j
        p = StripeProvider(object(), webhook_secret="")
        def ev(obj): return p.verify_webhook({}, _j.dumps({"type": "transfer.reversed", "data": {"object": obj}}).encode()).state
        self.assertEqual(ev({"id": "tr_1", "amount": 10000, "amount_reversed": 10000}), "reversed")   # clean full -> release
        self.assertEqual(ev({"id": "tr_1", "amount": 10000, "amount_reversed": 3000}), "settled")     # partial -> reserved
        self.assertEqual(ev({"id": "tr_1", "amount_reversed": 5000}), "settled")                      # amount missing -> malformed -> reserved
        self.assertEqual(ev({"id": "tr_1", "amount": -5, "amount_reversed": -5}), "settled")          # negative -> malformed -> reserved
        self.assertEqual(ev({"id": "tr_1"}), "reversed")                                              # bare legitimate event -> full

    def test_explicit_stale_intent_id_is_rejected_not_accepted(self):
        # Codex 7-pass: supplying an OLD intent_id explicitly must still be subject to the 15-min freshness window,
        # exactly like the implicit latest_intent lookup. An expired intent cannot satisfy require_intent.
        store = Store()
        clk = [1_700_000_000.0]
        svc = Service(store, FakePayPal(), clock=lambda: clk[0], default_require_intent=True, default_hard_cap=None,
                      default_mode="enforce")
        reg = svc.register_intent("a", ["AWS"], "pay AWS")
        iid = reg["intent_id"]
        clk[0] += 16 * 60                                    # advance past the 15-minute freshness window
        out = svc.pay({"agent_id": "a", "payee": "AWS", "recipient_email": "aws@x.com", "amount": "10.00",
                       "currency": "USD", "memo": "m", "intent_id": iid})
        self.assertEqual(out["decision"], "reject")
        self.assertIn("expired", out["reason"])

    def test_stripe_lost_response_is_in_doubt_and_reserved(self):
        # Codex 7-pass finding 2: the Stripe adapter must treat a lost response / 5xx as in_doubt (reserved against the
        # cap), not a confirmed failure. Mirror of the PayPal fix. Unit-test the adapter directly (no network).
        from app.providers.stripe.adapter import StripeProvider
        from app.providers.base import SendRequest
        class RaisingClient:
            def create_transfer(self, *a, **k): raise TimeoutError("lost")
        prov = StripeProvider(RaisingClient(), accounts={"emails": {"x@y.com": "acct_test_1"}})
        res = prov.send(SendRequest("stripe", "pay-1", "x@y.com", "100.00", "USD", "m", "k1"))
        self.assertEqual(res.state, "in_doubt"); self.assertFalse(res.ok)
        class FiveHundred:
            def create_transfer(self, *a, **k): return 503, {"error": {"message": "server"}}
        prov2 = StripeProvider(FiveHundred(), accounts={"emails": {"x@y.com": "acct_test_1"}})
        res2 = prov2.send(SendRequest("stripe", "pay-2", "x@y.com", "100.00", "USD", "m", "k2"))
        self.assertEqual(res2.state, "in_doubt")             # 5xx: outcome unknown, reserved
        class FourHundred:
            def create_transfer(self, *a, **k): return 400, {"error": {"message": "bad amount"}}
        prov3 = StripeProvider(FourHundred(), accounts={"emails": {"x@y.com": "acct_test_1"}})
        res3 = prov3.send(SendRequest("stripe", "pay-3", "x@y.com", "100.00", "USD", "m", "k3"))
        self.assertEqual(res3.state, "failed")               # clean 4xx: confirmed non-send

    def test_concurrent_first_requests_build_one_service_not_two(self):
        # Codex 7-pass finding 3: two simultaneous FIRST requests must not each construct a Service (each with its own
        # _decide_lock), which would defeat the cap serialisation. main.service() uses a lock + double-checked locking.
        import threading
        from app import main
        from app.store import Store
        main._store = Store(":memory:")
        main._service = None
        main._registry = None
        seen = []
        barrier = threading.Barrier(8)
        def worker():
            barrier.wait()
            seen.append(id(main.service()))
        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(len(set(seen)), 1, "all threads must get the same single Service instance")

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
