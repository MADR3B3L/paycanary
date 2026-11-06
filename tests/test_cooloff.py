import json, threading, unittest
from app import gate as G
from tests.test_enrichment import Net, svc, NOW
from tests.test_service import pay

DAY = 86400.0


def hist_rows(s, agent="bill-bot"):
    for i, (p, a) in enumerate([("A", 20), ("B", 100), ("C", 35), ("D", 90), ("A", 50), ("B", 70), ("C", 45), ("D", 60)]):
        s.store.insert_payment({"payment_id": "h%d" % i, "agent_id": agent, "ts": NOW - (40 - 4 * i) * DAY, "payee": p, "recipient": p.lower() + "@hist.example", "amount": float(a),
                                "currency": "USD", "memo": "", "hour": 10, "intent_id": None, "verdict": "allow", "score_kind": "none", "reason": "h", "status": "sent",
                                "paypal_batch_id": "H", "paypal_status": "S", "decided_by": "h", "event_json": "{}", "narration": None, "risk": 0, "llm_flag": None,
                                "mode": "enforce", "enforced": 1, "release_at": None})


def mkc(net=None, cooldown=1800, **kw):
    s, pp, t, sent = svc(net or Net(age=5000, mx=True), **kw)
    hist_rows(s); s.set_agent("bill-bot", hard_cap=1000, cooldown_s=cooldown)
    return s, pp, t, sent


def held_payment(s, t, amount="110.00", payee="Newco", email="pay@newco-clean.com"):
    out = pay(s, payee, amount, email=email)
    return out, s.store.payment(out["payment_id"])


class Rule(unittest.TestCase):
    def agent(self, **kw):
        h = G.History(hard_cap=5000, **kw)
        for i, a in enumerate([20, 100, 35, 90, 50, 70, 45, 60]): h.observe("P%d" % (i % 4), a, 1000.0 + i * 4 * DAY, 10)
        return h

    def txn(self, payee, amount, **kw):
        return {"kind": "txn", "payee": payee, "amount": amount, "ts": 1000.0 + 50 * DAY, "hour": 10, "stated_intent_payees": [payee], **kw}

    def test_off_by_default_and_only_for_a_first_payment_to_a_new_unapproved_payee_above_the_band(self):
        self.assertNotIn("cooling-off", G._classify_base(self.txn("New", 110.0), self.agent())[2])                          # cooldown_s = 0 => today's behaviour
        h = self.agent(cooldown_s=1800.0)
        t, k, why = G._classify_base(self.txn("New", 110.0), h); self.assertEqual(t, "freeze"); self.assertIn("cooling-off", why)
        self.assertNotIn("cooling-off", G._classify_base(self.txn("New", 70.0), h)[2])                                         # below 1.5 x typical
        self.assertNotIn("cooling-off", G._classify_base(self.txn("P1", 110.0), h)[2])                                         # a known payee is not a first payment
        self.assertNotIn("cooling-off", G._classify_base(self.txn("New", 110.0), self.agent(cooldown_s=1800.0, approved_payees=["New"]))[2])
        self.assertNotIn("cooling-off", G._classify_base(self.txn("New", 110.0, cooloff_released=True), h)[2])               # the re-check at release time skips this rule only

    def big_typical(self, **kw):
        h = G.History(hard_cap=5000, **kw)
        for i, a in enumerate([900, 1000, 1100, 950, 1050, 1000, 980, 1020]): h.observe("P%d" % (i % 4), a, 1000.0 + i * 4 * DAY, 10)
        return h

    def test_probation_hold_flag_holds_a_first_payment_above_the_probation_ceiling_only_when_on(self):
        # typical ~$1000: probation ceiling = min(500, 3000) = 500, 1.5 x typical = 1500. A $600 first payment to an unknown is a flagged REVIEW (money moves)
        # unless the opt-in `probation_hold` flag is on, in which case cooling-off holds it BEFORE the probation review.
        t = self.txn("New", 600.0)
        self.assertEqual(G._classify_base(t, self.big_typical())[:2], ("review", "novelty"))                                       # cooldown off: unchanged
        self.assertEqual(G._classify_base(t, self.big_typical(cooldown_s=900.0))[:2], ("review", "novelty"))                       # cooldown on, flag OFF (default): unchanged
        tier, kind, why = G._classify_base(t, self.big_typical(cooldown_s=900.0, probation_hold=True))
        self.assertEqual((tier, kind), ("freeze", "novelty")); self.assertIn("cooling-off", why)                                  # flag on: held
        self.assertEqual(G._classify_base(self.txn("New", 1600.0), self.big_typical(cooldown_s=900.0))[0], "freeze")             # > 1.5 x typical is held either way
        on = self.big_typical(cooldown_s=900.0, probation_hold=True)
        self.assertEqual(G._classify_base(self.txn("New", 600.0, cooloff_released=True), on)[:2], ("review", "novelty"))         # the release re-check still sees the probation review
        self.assertNotIn("cooling-off", G._classify_base(self.txn("New", 600.0), self.big_typical(cooldown_s=900.0, probation_hold=True, approved_payees=["New"]))[2])
        self.assertEqual(G._classify_base(self.txn("New", 600.0), self.big_typical(probation_hold=True))[:2], ("review", "novelty"))   # flag without cooldown does nothing


class Flow(unittest.TestCase):
    def test_hold_then_clean_evidence_then_auto_release_after_the_window(self):
        s, pp, t, sent = mkc()
        out, row = held_payment(s, t)
        self.assertEqual((out["status"], row["release_at"]), ("frozen", NOW + 1800)); self.assertEqual(pp.calls, []); self.assertIn("cooling-off", out["reasons"][0])
        ev = json.loads(row["evidence_json"]); self.assertEqual(ev["hint"], "none"); self.assertTrue({"domain_age_days", "blocklist"} <= set(ev["checked"]))
        self.assertEqual(s.release_due(), []); t[0] += 1799; self.assertEqual(s.release_due(), []); self.assertEqual(pp.calls, [])           # not before the window ends
        t[0] += 2; res = s.release_due()
        self.assertEqual([r[1] for r in res], ["released"]); self.assertEqual(len(pp.calls), 1)
        after = s.store.payment(out["payment_id"]); self.assertEqual((after["status"], after["decided_by"]), ("sent", "auto:cooldown"))
        self.assertIn("auto_released_after_cooldown", [r["event"] for r in s.audit.rows(limit=200)])
        self.assertEqual(s.release_due(), []); self.assertEqual(len(pp.calls), 1)                                                      # idempotent: never a second payout

    def test_cooldown_release_respects_usd_platform_cap_for_foreign_currency(self):
        # EUR 99 (~USD 107) held in cooldown; lower the USD platform cap to 100 before the window ends.
        # The auto-release must NOT send: the cap is a hard USD ceiling, not native-currency units.
        s, pp, t, sent = mkc(platform_cap=100000.0)
        out, row = held_payment(s, t, amount="99.00", payee="EuroVendor", email="pay@eurovendor-clean.com")
        # re-stamp as a EUR payment with clean evidence already gathered
        import json as _json
        ev = _json.loads(row["event_json"]); ev["currency"] = "EUR"
        s.store.update_payment(out["payment_id"], currency="EUR", event_json=_json.dumps(ev),
                               evidence_json=_json.dumps({"hint": "none", "checked": ["domain_age_days", "blocklist"]}))
        s.platform_cap = 100.0                                   # operator lowers the ceiling below ~USD 107
        t[0] += 1801
        res = s.release_due()
        self.assertEqual([r[1] for r in res], ["held"])         # held for a human, not auto-released
        self.assertEqual(pp.calls, [])                          # nothing sent over the USD cap

    def test_adverse_evidence_keeps_it_held_for_a_human(self):
        s, pp, t, sent = mkc(Net(feed_hosts=["bad-newco.com"]))
        out, row = held_payment(s, t, email="pay@bad-newco.com"); t[0] += 1801
        self.assertEqual([r[1] for r in s.release_due()], ["held"]); self.assertEqual(pp.calls, [])
        cur = s.store.payment(out["payment_id"]); self.assertEqual((cur["status"], cur["release_at"]), ("frozen", None))
        self.assertIn("cooldown_release_blocked", [r["event"] for r in s.audit.rows(limit=200)]); t[0] += 5000; self.assertEqual(s.release_due(), [])

    def test_unknown_is_not_clean_a_gather_that_could_not_run_never_auto_releases(self):
        s, pp, t, sent = mkc(Net(fail=("rdap", "mx", "feed")))
        out, row = held_payment(s, t); t[0] += 1801
        self.assertEqual([r[1] for r in s.release_due()], ["held"]); self.assertEqual(pp.calls, []); self.assertEqual(s.store.payment(out["payment_id"])["status"], "frozen")

    def test_no_evidence_at_all_waits_a_grace_period_then_goes_to_a_human_never_to_PayPal(self):
        s, pp, t, sent = mkc(enrich=False)                                                                                           # enrichment not configured: nothing is ever gathered
        out, row = held_payment(s, t); self.assertIsNone(row["evidence_json"]); t[0] += 1801
        self.assertEqual([r[1] for r in s.release_due()], ["waiting"]); self.assertEqual(pp.calls, [])
        t[0] += 600; self.assertEqual([r[1] for r in s.release_due()], ["held"]); self.assertEqual(pp.calls, []); self.assertEqual(s.store.payment(out["payment_id"])["status"], "frozen")

    def test_an_operator_denial_always_wins(self):
        s, pp, t, sent = mkc(); out, row = held_payment(s, t)
        self.assertTrue(s.deny(out["payment_id"], "operator")["ok"]); t[0] += 1801
        self.assertEqual(s.release_due(), []); self.assertEqual(pp.calls, []); self.assertEqual(s.store.payment(out["payment_id"])["status"], "denied")

    def test_the_recheck_blocks_release_when_something_changed(self):
        s, pp, t, sent = mkc(); out, row = held_payment(s, t); t[0] += 1801
        s.set_agent("bill-bot", hard_cap=50, cooldown_s=1800)                                                                       # the operator lowered the cap meanwhile
        res = s.release_due(); self.assertEqual(res[0][1], "held"); self.assertIn("hard cap", res[0][2]); self.assertEqual(pp.calls, [])

    def test_tier0_evidence_holds_and_hard_freezes_have_no_timer(self):
        s, pp, t, sent = mkc()
        out = pay(s, "Throwaway Co", "30.00", email="x@mailinator.com")                                                              # disposable inbox: held for an informed review
        self.assertEqual(out["status"], "frozen"); self.assertIsNone(s.store.payment(out["payment_id"])["release_at"])
        t[0] += 400; big = pay(s, "Wallet", "5000.00", email="w@x-wallet.com"); self.assertEqual(big["status"], "frozen"); self.assertIsNone(s.store.payment(big["payment_id"])["release_at"])
        t[0] += 5000; self.assertEqual(s.release_due(), []); self.assertEqual(pp.calls, [])

    def test_off_by_default_the_same_payment_just_proceeds_as_a_review(self):
        s, pp, t, sent = mkc(cooldown=0); out, row = held_payment(s, t)
        self.assertEqual((out["decision"], out["status"]), ("review", "sent")); self.assertIsNone(row["release_at"])

    def test_concurrent_sweeps_send_once(self):
        s, pp, t, sent = mkc(); out, row = held_payment(s, t); t[0] += 1801
        ths = [threading.Thread(target=s.release_due) for _ in range(8)]; [x.start() for x in ths]; [x.join() for x in ths]
        self.assertEqual(len(pp.calls), 1)

    def test_an_auto_release_contributes_to_reputation_like_any_executed_payment(self):
        from app.reputation_store import Reputation
        s, pp, t, sent = mkc(); s.reputation = Reputation("cooloff-test-key-0123456789"); out, row = held_payment(s, t); t[0] += 1801; s.release_due()
        self.assertTrue(s.reputation.read("pay@newco-clean.com", "Newco", t[0] + 1)["seen"])


if __name__ == "__main__":
    unittest.main()
