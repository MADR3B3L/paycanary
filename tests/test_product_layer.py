import json, sqlite3, threading, unittest
from app import gate as G
from app import notify as N
from app.service import Service
from app.store import Store
from tests.test_service import FakePayPal, pay

NOW = 1_800_000_000.0


def mkp(**kw):
    t = [NOW]; pp = FakePayPal()
    svc = Service(Store(), pp, clock=lambda: t[0], default_require_intent=False, default_hard_cap=None, **kw)
    return svc, pp, t


def req(**kw):
    return {"agent_id": "a", "payee": "AWS", "recipient_email": "aws@example.com", "amount": "42.00", "currency": "USD", "memo": "m", **kw}


class Idempotency(unittest.TestCase):
    def test_same_key_replays_the_first_result_and_pays_once(self):
        svc, pp, t = mkp()
        a = svc.pay(req(), request_id="inv-1"); t[0] += 5; b = svc.pay(req(), request_id="inv-1")
        self.assertEqual(len(pp.calls), 1); self.assertEqual(b["payment_id"], a["payment_id"]); self.assertTrue(b["duplicate"])
        self.assertEqual({k: b[k] for k in ("decision", "status", "paypal")}, {k: a[k] for k in ("decision", "status", "paypal")})

    def test_a_replayed_5xx_stays_a_5xx_and_does_not_retry_paypal(self):
        svc, pp, t = mkp(); pp.fail = True
        a = svc.pay(req(), request_id="k1"); self.assertEqual(a["status"], "paypal_error")
        pp.fail = False; b = svc.pay(req(), request_id="k1")
        self.assertEqual(b["status"], "paypal_error"); self.assertEqual(len(pp.calls), 1)               # the failure is the answer for this key

    def test_a_frozen_verdict_is_replayed_too(self):
        svc, pp, t = mkp(); svc.set_agent("a", hard_cap=10)
        a = svc.pay(req(), request_id="k2"); b = svc.pay(req(), request_id="k2")
        self.assertEqual((a["decision"], b["decision"], b["duplicate"]), ("freeze", "freeze", True)); self.assertEqual(pp.calls, [])

    def test_reused_key_with_different_parameters_is_an_error(self):
        svc, pp, t = mkp()
        svc.pay(req(), request_id="k3"); n = len(svc.store.payments("a"))
        for changed in (req(amount="43.00"), req(payee="GitHub"), req(recipient_email="other@example.com"), req(memo="x")):
            out = svc.pay(changed, request_id="k3")
            self.assertEqual((out["decision"], out["status"]), ("reject", "idempotency_conflict"))
        self.assertEqual(len(svc.store.payments("a")), n); self.assertEqual(len(pp.calls), 1)

    def test_keys_are_opaque_scoped_per_agent_and_validated(self):
        svc, pp, t = mkp()
        self.assertEqual(svc.pay(req(), request_id="same")["status"], "sent")
        self.assertEqual(svc.pay(req(agent_id="b"), request_id="same")["status"], "sent")                # another agent may reuse the string
        for bad in ("has space", "x" * 65, "a@b.com", "k;drop", ""):
            if bad: self.assertEqual(svc.pay(req(), request_id=bad)["status"], "rejected", bad)

    def test_ten_racing_retries_make_one_payout(self):
        svc, pp, t = mkp(); outs = []
        ths = [threading.Thread(target=lambda: outs.append(svc.pay(req(), request_id="race"))) for _ in range(10)]
        [x.start() for x in ths]; [x.join() for x in ths]
        self.assertEqual(len(pp.calls), 1); self.assertEqual(len({o["payment_id"] for o in outs}), 1)


class Shadow(unittest.TestCase):
    def test_shadow_lets_it_through_but_records_what_it_would_have_done(self):
        svc, pp, t = mkp(); svc.set_agent("a", hard_cap=10, mode="shadow")
        out = svc.pay(req(amount="400.00"))
        self.assertEqual((out["decision"], out["would_have"], out["enforced"], out["status"]), ("review", "freeze", False, "sent")); self.assertEqual(len(pp.calls), 1)
        row = svc.store.payments("a")[0]
        self.assertEqual((row["verdict"], row["enforced"], row["mode"], row["decided_by"]), ("freeze", 0, "shadow", "goalie-shadow"))
        ev = [r for r in svc.audit.rows() if r["event"] == "decision"][0]; self.assertFalse(json.loads(ev["detail"])["enforced"])
        svc.set_agent("a", hard_cap=10, mode="enforce")
        self.assertEqual(svc.pay(req(amount="401.00"))["status"], "frozen")                              # flip to enforce: same payment now stops

    def test_global_default_and_per_agent_override(self):
        svc, pp, t = mkp(default_mode="shadow")
        svc.set_agent("a", hard_cap=10); svc.set_agent("b", hard_cap=10, mode="enforce")
        self.assertEqual(svc.pay(req(amount="300.00"))["status"], "sent")                                # inherits the global shadow default
        self.assertEqual(svc.pay(req(agent_id="b", amount="300.00"))["status"], "frozen")                # agent override wins
        with self.assertRaises(Exception): Service(Store(), FakePayPal(), default_mode="yolo")

    def test_the_platform_cap_is_never_shadowed(self):
        svc, pp, t = mkp(platform_cap=500.0); svc.set_agent("a", hard_cap=10, mode="shadow")
        out = svc.pay(req(amount="900.00"))
        self.assertEqual(out["status"], "frozen"); self.assertEqual(pp.calls, []); self.assertIn("platform cap", out["reasons"][0])

    def test_shadow_does_not_teach_the_baseline_from_would_have_frozen_payments(self):
        svc, pp, t = mkp(); svc.set_agent("a", hard_cap=10, mode="shadow"); svc.pay(req(amount="400.00"))
        self.assertEqual(list(svc.store.baseline_rows("a")), [])


class AuditLog(unittest.TestCase):
    def test_chain_verifies_and_records_who_did_what(self):
        svc, pp, t = mkp(); svc.set_agent("a", hard_cap=10)
        f = svc.pay(req(amount="400.00")); svc.release(f["payment_id"], "operator"); g = svc.pay(req(amount="401.00")); svc.deny(g["payment_id"], "operator")
        tok = svc.issue_agent_token("a")
        ev = [r["event"] for r in svc.audit.rows(limit=100)]
        for need in ("config_changed", "decision", "released", "paypal_result", "denied", "token_issued"):
            self.assertIn(need, ev, need)
        self.assertEqual(svc.audit.verify()["ok"], True)
        self.assertNotIn(tok, json.dumps(svc.audit.rows(limit=1000)))                                    # a credential never lands in the log
        cfg = [r for r in svc.audit.rows() if r["event"] == "config_changed"][0]; self.assertEqual(cfg["actor"], "operator")

    def test_release_and_deny_record_the_flag_they_overrode_inside_the_immutable_log(self):
        svc, pp, t = mkp(); svc.set_agent("a", hard_cap=10)
        f = svc.pay(req(amount="400.00")); svc.release(f["payment_id"], "operator"); g = svc.pay(req(amount="401.00")); svc.deny(g["payment_id"], "operator")
        for ev in ("released", "denied"):
            row = [r for r in svc.audit.rows(limit=100) if r["event"] == ev][0]; d = json.loads(row["detail"])
            self.assertIn("hard cap", d["overridden_reason"]); self.assertEqual(d["payee"], "AWS")
        self.assertTrue(svc.audit.verify()["ok"])

    def test_the_table_refuses_edits_and_deletes(self):
        svc, pp, t = mkp(); svc.pay(req())
        for sql in ("UPDATE audit SET actor='nobody' WHERE seq=1", "DELETE FROM audit WHERE seq=1"):
            with self.assertRaises(sqlite3.DatabaseError): svc.store.db.execute(sql)

    def test_tampering_with_the_file_is_detected(self):
        svc, pp, t = mkp()
        for i in range(4): t[0] += 400; svc.pay(req(amount="%.2f" % (40 + i)))
        db = svc.store.db
        db.execute("DROP TRIGGER audit_no_update"); db.execute("UPDATE audit SET actor='someone-else' WHERE seq=2"); db.commit()
        v = svc.audit.verify(); self.assertEqual((v["ok"], v["rows"], v["first_bad_seq"]), (False, 1, 2))
        svc2, pp2, t2 = mkp()
        for i in range(4): t2[0] += 400; svc2.pay(req(amount="%.2f" % (40 + i)))
        svc2.store.db.execute("DROP TRIGGER audit_no_delete"); svc2.store.db.execute("DELETE FROM audit WHERE seq=3"); svc2.store.db.commit()
        self.assertFalse(svc2.audit.verify()["ok"])                                                      # a hole in the sequence

    def test_head_changes_with_every_event(self):
        svc, pp, t = mkp(); h0 = svc.audit.head(); svc.pay(req()); self.assertNotEqual(svc.audit.head()["hash"], h0["hash"])


class Alerts(unittest.TestCase):
    def mk(self, on=("freeze",), transport=None, **kw):
        sent = []
        def tr(url, body, headers, timeout): sent.append((url, body, headers)); return 200
        svc, pp, t = mkp(**kw)
        svc.notifier = N.Notifier("https://ops.example.com/hook", "s3cret", on=on, audit=svc.audit, transport=transport or tr, sync=True)
        return svc, pp, sent

    def test_a_freeze_posts_one_signed_event(self):
        svc, pp, sent = self.mk(); svc.set_agent("a", hard_cap=10)
        out = svc.pay(req(amount="400.00"))
        self.assertEqual(len(sent), 1); url, body, headers = sent[0]
        ev = json.loads(body)
        self.assertEqual((ev["event"], ev["payment_id"], ev["verdict"], ev["enforced"], ev["amount"]), ("payment.frozen", out["payment_id"], "freeze", True, 400.0))
        self.assertTrue(N.verify("s3cret", headers["x-paycanary-signature"], body)); self.assertFalse(N.verify("wrong", headers["x-paycanary-signature"], body))
        self.assertFalse(N.verify("s3cret", headers["x-paycanary-signature"], body + b" ")); self.assertIn("alert_sent", [r["event"] for r in svc.audit.rows()])

    def test_stale_signatures_are_rejected_and_review_is_opt_in(self):
        body = b"{}"; hdr = N.sign("k", 1_000, body)
        self.assertFalse(N.verify("k", hdr, body, now=1_000 + 301)); self.assertTrue(N.verify("k", hdr, body, now=1_000 + 10))
        svc, pp, sent = self.mk(); svc.pay(req())                                                          # a plain allow / first-payee review: not alerted by default
        self.assertEqual(sent, [])
        svc2, pp2, sent2 = self.mk(on=("freeze", "review")); svc2.pay(req()); self.assertEqual(json.loads(sent2[0][1])["event"], "payment.review")

    def test_shadow_freezes_alert_as_would_freeze(self):
        svc, pp, sent = self.mk(on=("freeze", "shadow_freeze")); svc.set_agent("a", hard_cap=10, mode="shadow"); svc.pay(req(amount="400.00"))
        ev = json.loads(sent[0][1]); self.assertEqual((ev["event"], ev["enforced"]), ("payment.would_freeze", False))

    def test_a_dead_receiver_never_touches_the_payment(self):
        def boom(*a): raise ConnectionError("receiver down")
        svc, pp, sent = self.mk(transport=boom); svc.set_agent("a", hard_cap=10)
        out = svc.pay(req(amount="400.00"))
        self.assertEqual((out["decision"], out["status"]), ("freeze", "frozen")); self.assertEqual(pp.calls, [])
        self.assertIn("alert_failed", [r["event"] for r in svc.audit.rows()])

    def test_config_must_be_sane(self):
        with self.assertRaises(ValueError): N.Notifier("ftp://x", "s")
        with self.assertRaises(ValueError): N.Notifier("https://x.example.com", "")


class FailClosed(unittest.TestCase):
    def test_a_broken_gate_freezes_instead_of_allowing(self):
        svc, pp, t = mkp(); real = G.classify
        G.classify = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug"))
        try: out = svc.pay(req())
        finally: G.classify = real
        self.assertEqual((out["decision"], out["status"]), ("freeze", "frozen")); self.assertIn("fail-closed", out["reasons"][0]); self.assertEqual(pp.calls, [])

    def test_no_verdict_means_no_money(self):
        svc, pp, t = mkp(); svc.store.insert_payment = lambda row: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error"))
        out = svc.pay(req())
        self.assertEqual((out["decision"], out["status"]), ("error", "fail_closed")); self.assertEqual(pp.calls, [])

    def test_history_failure_blocks_too(self):
        svc, pp, t = mkp(); svc._history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store down"))
        self.assertEqual(svc.pay(req())["status"], "fail_closed"); self.assertEqual(pp.calls, [])

    def test_an_unconfirmed_paypal_call_is_never_reported_as_sent(self):
        svc, pp, t = mkp(); pp.fail = True
        out = svc.pay(req()); self.assertEqual(out["status"], "paypal_error"); self.assertNotEqual(svc.store.payments("a")[0]["status"], "sent")


if __name__ == "__main__":
    unittest.main()
