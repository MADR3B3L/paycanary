import json
import unittest

from app.schema import ValidationError
from tests.test_enrichment import Net, svc


class Ruleset(unittest.TestCase):
    def setUp(self):
        self.s = svc(Net(age=5000, mx=True))[0]
        self.s.set_agent("bill-bot", hard_cap=900, require_intent=True, approved_payees=["AWS"], new_payee_floor=50, mode="enforce",
                         org_id="org-1", cooldown_s=60, fanout_k=4, probation_abs=300, probation_mult=2)

    def cfg(self):
        return {k: v for k, v in self.s.store.agent("bill-bot").items() if k != "token_hash"}

    def test_allowlist_edit_keeps_every_other_knob_and_is_audited(self):
        before = self.cfg()
        self.s.modify_agent("bill-bot", approve=["Github", "AWS"])
        after = self.cfg()
        self.assertEqual(after["approved_payees"], ["AWS", "Github"])
        self.assertEqual({k: v for k, v in after.items() if k != "approved_payees"}, {k: v for k, v in before.items() if k != "approved_payees"})
        row = self.s.audit.rows(0, 100, "config_changed")[-1]
        d = json.loads(row["detail"])
        self.assertEqual(d["after"]["approved_payees"], ["AWS", "Github"])
        self.assertEqual(d["before"]["approved_payees"], ["AWS"])
        self.s.modify_agent("bill-bot", unapprove=["AWS"])
        self.assertEqual(self.cfg()["approved_payees"], ["Github"])
        self.assertTrue(self.s.audit.verify()["ok"] if isinstance(self.s.audit.verify(), dict) else self.s.audit.verify())

    def test_mode_flip_is_audited_and_validated(self):
        self.s.modify_agent("bill-bot", mode="shadow")
        self.assertEqual(self.cfg()["mode"], "shadow")
        self.assertEqual(json.loads(self.s.audit.rows(0, 100, "config_changed")[-1]["detail"])["after"]["mode"], "shadow")
        with self.assertRaises(ValidationError):
            self.s.modify_agent("bill-bot", mode="off")

    def test_ruleset_bands_and_history(self):
        self.s.modify_agent("bill-bot", approve=["Github"])
        self.s.modify_agent("bill-bot", mode="shadow")
        self.s.audit.append("confirmed_fraud", "operator", agent_id="bill-bot", payment_id="pay-1", detail={"payee": "AttackerWallet"})
        r = self.s.ruleset()
        a = r["agents"][0]
        self.assertEqual((a["agent_id"], a["mode"], a["approved_payees"]), ("bill-bot", "shadow", ["AWS", "Github"]))
        self.assertEqual([d["payee"] for d in r["denylist"]], ["AttackerWallet"])
        self.assertFalse(r["reputation_active"])
        self.assertEqual(r["history"][0]["mode_to"], "shadow")
        self.assertIn("Github", r["history"][1]["allowlist_added"])


    def test_report_fraud_on_an_already_sent_payment_records_and_denylists_without_undoing(self):
        from tests.test_service import pay
        self.s.set_agent("bill-bot", hard_cap=900, require_intent=False, approved_payees=["AWS"])
        out = pay(self.s, "AWS", "20.00", agent="bill-bot")
        self.assertEqual(self.s.store.payment(out["payment_id"])["status"], "sent")
        r = self.s.confirm_fraud(out["payment_id"])
        self.assertTrue(r["ok"])
        self.assertEqual(self.s.store.payment(out["payment_id"])["status"], "sent")             # nothing is recalled
        self.assertIn("AWS", [d["payee"] for d in self.s.ruleset()["denylist"]])


if __name__ == "__main__":
    unittest.main()
