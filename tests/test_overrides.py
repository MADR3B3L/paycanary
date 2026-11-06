"""First-party-fraud visibility (threat axis b): operator overrides are recorded immutably with the flag they overrode, and
the per-principal override pattern is visible. Read-only; proves the complicity-shield / liability record is real and
demonstrable, not just a doc claim."""
import unittest
from app import overrides as O
from tests.test_service import mk, pay, seed_history


class OverrideVisibility(unittest.TestCase):
    def _frozen(self, s, payee, amount, email):
        out = pay(s, payee, amount, email=email)
        assert out["status"] == "frozen", out
        return out["payment_id"], (out.get("reasons") or [""])[0]

    def test_an_override_is_recorded_with_who_how_much_to_whom_and_the_flag_overridden(self):
        s, pp, t = mk(); seed_history(s, t); s.set_agent("bill-bot", hard_cap=1000)
        pid, flag = self._frozen(s, "suspicious", "5000.00", email="x@sus.example")
        s.release(pid, by="operator-jane")                                # the user/operator overrides the flag
        recs = O.override_records(s.store, s.audit)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["by"], "operator-jane")                        # WHO (attributed to the authenticated principal)
        self.assertEqual(r["amount"], 5000.0)                            # HOW MUCH
        self.assertEqual(r["payee"], "suspicious")                       # TO WHOM
        self.assertIn("hard cap", r["overridden_flag"])                 # the FLAG they overrode is on the record

    def test_user_overrode_N_flags_signal_and_per_principal_pattern(self):
        s, pp, t = mk(); seed_history(s, t); s.set_agent("bill-bot", hard_cap=1000)
        for i, amt in enumerate(("2000.00", "3000.00", "4000.00")):
            pid, _ = self._frozen(s, "mule%d" % i, amt, email="m%d@mule.example" % i)
            s.release(pid, by="owner-bob")                                # same principal waves through three flags
        pid, _ = self._frozen(s, "other", "2500.00", email="o@other.example")
        s.release(pid, by="operator-amy")
        summ = O.override_summary(s.store, s.audit)
        self.assertEqual(summ["total_overrides"], 4)                      # "user overrode N flags" headline
        self.assertEqual(summ["by_principal"]["owner-bob"]["count"], 3)   # the pattern: one principal, three overrides
        self.assertEqual(summ["by_principal"]["operator-amy"]["count"], 1)
        self.assertEqual(summ["largest_overrides"][0]["amount"], 4000.0)  # biggest first, for a reviewer
        self.assertIn("observer", summ["positioning"].lower())            # the honest positioning ships with it

    def test_a_denial_is_not_an_override(self):
        # denying a flag is the operator AGREEING with PayCanary -- not an override. Only releases count.
        s, pp, t = mk(); seed_history(s, t); s.set_agent("bill-bot", hard_cap=1000)
        pid, _ = self._frozen(s, "mule", "9000.00", email="m@mule.example")
        s.deny(pid, by="operator-amy")
        self.assertEqual(O.override_summary(s.store, s.audit)["total_overrides"], 0)

    def test_the_override_record_is_read_only_and_decides_nothing(self):
        # calling the summary must not change any payment or append to the audit log
        s, pp, t = mk(); seed_history(s, t); s.set_agent("bill-bot", hard_cap=1000)
        pid, _ = self._frozen(s, "x", "5000.00", email="x@x.example"); s.release(pid, by="op")
        before = len(s.audit.rows(limit=100000)); st_before = s.store.payment(pid)["status"]
        O.override_summary(s.store, s.audit); O.override_records(s.store, s.audit)
        self.assertEqual(len(s.audit.rows(limit=100000)), before)         # no new audit rows
        self.assertEqual(s.store.payment(pid)["status"], st_before)       # no payment changed


if __name__ == "__main__":
    unittest.main()
