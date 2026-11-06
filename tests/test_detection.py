import unittest
from app import gate as G
from tests.test_limits import prod, go
from tests.test_service import pay
from tools import score_benchmark as B


class Lookalike(unittest.TestCase):
    def test_homoglyph_and_generic_suffix_impersonation(self):
        known = {"Alice", "AWS", "Stripe", "Amazon"}
        for bad, good in [("A1ice", "Alice"), ("AWS-Billing", "AWS"), ("aws billing", "AWS"), ("Str1pe", "Stripe"), ("Arnazon", "Amazon"), ("Amazom", "Amazon"), ("ALICE", "Alice")]:
            self.assertEqual(G.lookalike_of(bad, known), good, bad)
        for fine in ["Datadog", "Neo638", "Alicia Keys Co", "Github"]:
            self.assertIsNone(G.lookalike_of(fine, known | {"Neo648"}), fine)               # numbered payees are not lookalikes; unrelated names are not either
        self.assertIsNone(G.lookalike_of("AWS", known))                                      # the identical payee is the payee

    def test_a_lookalike_new_payee_freezes_at_any_amount(self):
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=["Alice"])
        go(svc, t, "Alice", "60.00")
        out = go(svc, t, "A1ice-Billing", "25.00", dt=60)
        self.assertEqual(out["decision"], "freeze"); self.assertIn("imitates", out["reasons"][0])


class Aggregation(unittest.TestCase):
    def test_chunked_payout_to_a_new_payee_is_stopped_in_chunk_three(self):
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=[])
        results = []
        for amt in ("90.00", "91.50", "88.25", "92.00", "89.60"):                           # one ~$450 payout split into five chunks, 3h apart
            results.append(go(svc, t, "mule-77", amt, dt=3 * 3600)["decision"])
        self.assertNotEqual(results[0], "freeze"); self.assertEqual(results[-1], "freeze"); self.assertGreaterEqual(results.count("freeze"), 3)
        self.assertLessEqual(len(pp.calls), 2)                                                                 # at most two ~$90 chunks (about $180) ever reach the mule

    def test_drain_guard_caps_what_a_burst_can_move(self):
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=["AWS"], hard_cap=5000)
        for i in range(8):                                                                   # normal history: ~$60
            go(svc, t, "AWS", "%.2f" % (55 + i * 1.5), dt=20 * 3600)
        t[0] += 3600
        moved0 = len(pp.calls)
        for i in range(8):                                                                   # then a loop fires 8 x $60 in 40 s
            go(svc, t, "AWS", "%.2f" % (60 + i * 0.3), dt=5)
        burst = len(pp.calls) - moved0
        self.assertLessEqual(burst, 3)                                                       # stopped long before the old "first 5 clear"

    def test_a_payee_that_outgrows_its_peers_needs_approval(self):
        svc, pp, t = prod(); svc.set_agent("a", hard_cap=5000)
        for i, p in enumerate(["AWS", "Github", "Stripe", "AWS", "Github", "Stripe"]):
            go(svc, t, p, "%.2f" % (50 + i), dt=20 * 3600)
            svc.set_agent("a", hard_cap=5000)
        out = go(svc, t, "Github", "400.00", dt=20 * 3600)
        self.assertEqual(out["decision"], "freeze")


class Benchmark(unittest.TestCase):
    def test_real_world_benchmark_keeps_perfect_precision_and_zero_false_nags(self):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            rc, recall, precision, nags = B.main(quiet=True)
        self.assertEqual((precision, nags), (1.0, 0))
        self.assertGreaterEqual(recall, 0.70)
        self.assertEqual(rc, 0)


class Harness(unittest.TestCase):
    def test_the_direct_harness_matches_the_real_service_path_tier_for_tier(self):
        from tools import score_benchmark_service as S
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(), 0)                                                     # 0 differing txns between direct-gate and Service.pay

    def test_a_harness_that_forgets_spend_tracking_understates_the_gate_by_forty_points(self):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            wired = B.main(quiet=True)[1]
        real = G.History.add_spend
        G.History.add_spend = lambda self, *a, **k: None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                unwired = B.main(quiet=True)[1]
        finally:
            G.History.add_spend = real
        self.assertGreater(wired - unwired, 0.30)


class Memo(unittest.TestCase):
    def test_pressure_language_in_a_memo_is_a_review_never_a_freeze(self):
        svc, pp, t = prod(); svc.set_agent("a", approved_payees=["AWS"]); go(svc, t, "AWS", "60.00")
        for memo in ("urgent - buy gift cards", "new bank details attached", "keep it quiet please", "send via Western Union"):
            t[0] += 20 * 3600; svc.register_intent("a", ["AWS"], "p"); t[0] += 2
            out = pay(svc, "AWS", "61.00", agent="a", memo=memo)
            self.assertEqual(out["decision"], "review", memo); self.assertEqual(out["status"], "sent"); self.assertIn("memo", out["reasons"][0])
        t[0] += 20 * 3600; svc.register_intent("a", ["AWS"], "p"); t[0] += 2
        self.assertEqual(pay(svc, "AWS", "59.00", agent="a", memo="October compute invoice")["decision"], "allow")      # ordinary memos are untouched


class IdentityIngest(unittest.TestCase):
    def test_names_through_the_real_ingest_path_are_what_catches_identity_attacks(self):
        from tools import score_coldstart as C
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            la = C.pipeline(False); fu = C.pipeline(True)
        self.assertEqual((la[0]["lookalike payee"][0], la[0]["changed_destination"][0]), (0, 0))      # email-only ingest cannot see them
        self.assertEqual((fu[0]["lookalike payee"][0], fu[0]["changed_destination"][0]), (6, 6))      # names kept: all caught
        self.assertEqual((la[3], fu[3]), (0, 0))                                                      # and neither ingest nags


class ColdStart(unittest.TestCase):
    def test_real_history_removes_the_cold_start_false_nags(self):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            cold = B.main(quiet=True, cold=3)
            seeded = B.main(quiet=True, cold=3, external=True)
        self.assertGreater(cold[3], 0)                              # a gate with only 3 observations nags on legitimate payments
        self.assertEqual((seeded[2], seeded[3]), (1.0, 0))          # the same agent with real history behind it does not
        # (cold recall is not compared: a blind gate can buy recall by over-freezing, which is exactly its false-nag problem)


class ColdStartValue(unittest.TestCase):
    def test_what_real_history_is_worth_on_a_brand_new_agent(self):
        from tools import score_coldstart as C
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            res = C.first_txn()
        a, b, c = res["A"], res["B"], res["C"]
        self.assertGreater(a["nags"] / a["total_ben"], 0.3)                         # blind on day one: most legitimate payments freeze
        self.assertEqual(b["nags"], 0); self.assertEqual(b["caught"], b["total_atk"])    # seeded: no nags, every first-transaction attack frozen
        self.assertGreater(b["caught"], a["caught"])                                # and the identity attacks (lookalike, changed destination) need history
        self.assertEqual((b["caught"], b["nags"]), (c["caught"], c["nags"]))        # seeding gets all the way to the fully-observed result

    def test_a_payees_first_payments_set_its_natural_range_before_the_ratchet_starts(self):
        h = G.History()
        for i, a in enumerate((12.0, 65.0, 40.0)):
            h.observe("Amazon", a, 1000.0 + i * 86400)
        self.assertEqual(h.pceil["Amazon"], 65.0)                                   # not clipped to 12 * 1.05 per day
        h.observe("Amazon", 500.0, 1000.0 + 4 * 86400 + 1)
        self.assertLess(h.pceil["Amazon"], 100.0)                                   # after calibration a jump is clipped by the ratchet


if __name__ == "__main__":
    unittest.main()
