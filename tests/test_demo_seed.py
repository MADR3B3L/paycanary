import json
import os
import tempfile
import unittest

from app import seed as S
from app.store import Store
from tests import read_file


class DemoSeed(unittest.TestCase):
    def test_offline_seed_is_a_realistic_multi_agent_history_with_four_frozen_heroes_and_real_tier0_evidence(self):
        st = Store(os.path.join(tempfile.mkdtemp(), "d.sqlite3"))
        res = S.seed(st, None, None)
        self.assertEqual((res["payments"], res["frozen"]), (72, 4))
        from collections import Counter
        allrows = st.payments(None, None, 500)
        mix = Counter((p["verdict"], p["status"]) for p in allrows)
        self.assertGreaterEqual(mix[("allow", "sent")], 40)                                       # a healthy allowed count, from real passing payments
        self.assertFalse([k for k in mix if k[1] == "paypal_error"])                            # nothing looks broken
        self.assertEqual(mix[("freeze", "frozen")], 4)
        self.assertEqual(sorted({p["agent_id"] for p in allrows}), ["ap-bot", "contractor-bot", "expense-bot", "ops-bot"])
        # established history: every agent has weeks of executed payments, and a known payee reads as known (not 'first-ever') once it has history
        for agent in ("ops-bot", "ap-bot", "contractor-bot", "expense-bot"):
            sent = [p for p in allrows if p["agent_id"] == agent and p["status"] == "sent"]
            self.assertGreaterEqual(len(sent), 12, agent)
            self.assertGreaterEqual(max(p["ts"] for p in sent) - min(p["ts"] for p in sent), 60 * 86400, agent)
        known = [p for p in allrows if p["verdict"] == "allow"]
        self.assertTrue(all(("known payee" in p["reason"]) or ("on cadence" in p["reason"]) for p in known))
        first_ever = [p for p in allrows if p["reason"].startswith("first-ever payee")]
        self.assertEqual(len({(p["agent_id"], p["payee"]) for p in first_ever}), len(first_ever))   # each payee is 'first-ever' exactly once
        rows = {(p["agent_id"], p["payee"]): p for p in allrows if p["status"] == "frozen"}
        self.assertEqual(sorted(rows), [("contractor-bot", "Priya Raman"), ("expense-bot", "Veloxa Payments"), ("ops-bot", "QuickTools LLC")])   # Veloxa is frozen twice (structuring)
        self.assertIn("hard cap", rows[("contractor-bot", "Priya Raman")]["reason"])
        self.assertIn("intent", rows[("ops-bot", "QuickTools LLC")]["reason"])
        self.assertIn("cumulative", rows[("expense-bot", "Veloxa Payments")]["reason"])
        # every payment has its decision in the audit chain
        a = {r["payment_id"] for r in __import__("app.audit", fromlist=["AuditLog"]).AuditLog(st, __import__("time").time).rows(0, 20000, "decision")}
        self.assertTrue(all(p["payment_id"] in a for p in allrows))
        ev = json.loads(rows[("ops-bot", "QuickTools LLC")]["evidence_json"])
        self.assertTrue(any(i["signal"] == "in_disposable" for i in ev["items"]))
        self.assertEqual(ev["checked"], [])                           # offline: nothing claimed as network-checked


    def test_a_failing_real_paypal_cannot_reach_the_seed(self):
        # main._seed passes no PayPal client to the seed: a dry or failing sandbox must not turn the seeded history into paypal_error / no baseline.
        src = read_file(os.path.join(os.path.dirname(__file__), "..", "app", "main.py"))
        self.assertIn("S.seed(_store, None, svc.llm, force=force", src)
        class Bad:
            def create_payout(self, *a, **k):
                return 400, {"name": "INSUFFICIENT_FUNDS"}
        bad = Store(os.path.join(tempfile.mkdtemp(), "b.sqlite3"))
        S.seed(bad, Bad(), None)                                                              # documents WHY: an explicit failing client cascades
        self.assertEqual({p["status"] for p in bad.payments(None, None, 500)} - {"frozen"}, {"paypal_error"})


    def test_forced_reseed_resets_decided_rows_and_keeps_the_audit_log_intact(self):
        import json as _json
        from app.service import Service
        st = Store(os.path.join(tempfile.mkdtemp(), "r.sqlite3"))
        S.seed(st, None, None)
        svc = Service(st, S.OfflinePayPal())
        frozen = [p for p in st.payments(None, None, 500) if p["status"] == "frozen"]
        self.assertEqual(len(frozen), 4)
        svc.deny(frozen[0]["payment_id"]); svc.confirm_fraud(frozen[1]["payment_id"])
        self.assertEqual(sorted({p["status"] for p in st.payments(None, None, 500)}), ["denied", "frozen", "sent"])
        before = len(svc.audit.rows(0, 5000))
        self.assertFalse(S.seed(st, None, None)["seeded"])                                       # without force it refuses and changes nothing
        res = S.seed(st, None, None, force=True)
        self.assertTrue(res["seeded"])
        rows = st.payments(None, None, 500)
        self.assertEqual(len(rows), 72)                                                          # replaced, not stacked on top
        self.assertEqual(sorted({p["status"] for p in rows}), ["frozen", "sent"])                # nothing left denied
        self.assertEqual(sum(p["status"] == "frozen" for p in rows), 4)
        a = svc.audit.rows(0, 5000)
        self.assertGreater(len(a), before)                                                       # the old audit rows are all still there
        self.assertIn("demo_reset", [r["event"] for r in a])
        self.assertTrue(svc.audit.verify())                                                      # and the chain is still valid


    def test_seed_shows_both_rails_each_with_a_frozen_hero_and_simulated_ids(self):
        st = Store(os.path.join(tempfile.mkdtemp(), "d2.sqlite3"))
        S.seed(st, None, None)
        rows = st.payments(None, None, 500)
        for prov, tag in (("paypal", "SIM-PP-"), ("stripe", "SIM-ST-")):
            mine = [r for r in rows if r["provider"] == prov]
            self.assertGreaterEqual(len(mine), 10)
            self.assertTrue([r for r in mine if r["status"] == "frozen"], prov)
            self.assertTrue(all((r["provider_ref"] or "").startswith(prov + ":" + tag) for r in mine if r["status"] == "sent"), prov)
        self.assertFalse([r for r in rows if r["provider"] not in ("paypal", "stripe")])         # nothing is labelled mock or anything else
        self.assertEqual({r["provider"] for r in rows if r["agent_id"] in ("contractor-bot", "expense-bot")}, {"stripe"})


if __name__ == "__main__":
    unittest.main()
