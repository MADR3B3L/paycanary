import json, os, sqlite3, tempfile, unittest
from app import gate as G
from app.reputation_store import Reputation, bucket, normalize, EVENTS
from app.service import Service
from app.store import Store
from tests.test_service import FakePayPal, pay
from tests import read_file

KEY = "k" * 24
DAY = 86400.0
NOW = 1_800_000_000.0


class Privacy(unittest.TestCase):
    ALLOWED = {"rep_event": {"recipient_key", "org_hash", "ts", "event"}, "rep_skeleton": {"recipient_key", "skeleton"}}

    def test_the_shared_schema_cannot_hold_amounts_memos_agent_ids_or_payment_ids(self):
        r = Reputation(KEY)
        tables = {t["name"] for t in r.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, set(self.ALLOWED))
        for t, cols in self.ALLOWED.items():
            self.assertEqual({c["name"] for c in r.db.execute(f"PRAGMA table_info({t})")}, cols)
        for bad in ("amount", "memo", "agent", "payment", "email", "org_id", "currency"):
            for cols in self.ALLOWED.values():
                self.assertFalse(any(bad in c for c in cols), bad)

    def test_no_plaintext_identity_ever_reaches_the_file_and_a_different_key_gives_different_hashes(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3"); os.close(fd)
        try:
            r = Reputation(KEY, path)
            r.contribute("Vendor.Billing@AcmeCorp.example", "Acme Corporation", "customer-org-12345", "executed", 100.0)
            r.db.commit(); r.db.close()
            raw = read_file(path, "rb")
            for needle in (b"vendor.billing", b"acmecorp", b"customer-org-12345", b"Acme Corporation", KEY.encode()):
                self.assertNotIn(needle, raw, needle)
        finally:
            os.unlink(path)
        a, b = Reputation("a" * 24), Reputation("b" * 24)
        self.assertNotEqual(a.recipient_key("x@y.com"), b.recipient_key("x@y.com")); self.assertEqual(a.recipient_key("X@Y.com"), a.recipient_key(" x@y.com "))

    def test_normalize_and_key_requirements(self):
        self.assertEqual(normalize("  Pay+inv42@Vendor.COM "), "pay@vendor.com")
        with self.assertRaises(ValueError): Reputation("short")
        with self.assertRaises(ValueError): Reputation(KEY).contribute("a@b.com", "A", "o", "freeze", 1.0)                 # only typed, payee-attributable events

    def test_a_read_reveals_only_pooled_scalars_about_the_one_payee(self):
        r = Reputation(KEY)
        for i, org in enumerate(("o1", "o2", "o3")): r.contribute("v@x.com", "Vendor", org, "executed", 100.0 + i)
        rec = r.read("v@x.com", "Vendor", NOW)
        self.assertEqual(rec["distinct_orgs"], 3); self.assertEqual(rec["distinct_agents"], 3)             # a lower bound: agent ids are never stored
        blob = json.dumps(rec, default=sorted)
        for needle in ("o1", "o2", "o3", "v@x.com"): self.assertNotIn(needle, blob)


class SemanticsExact(unittest.TestCase):
    def test_as_of_never_sees_the_future(self):
        r = Reputation(KEY); r.contribute("v@x.com", "V", "o1", "executed", 100.0); r.contribute("v@x.com", "V", "o2", "executed", 200.0)
        self.assertEqual([r.read("v@x.com", "V", t)["distinct_orgs"] for t in (50, 150, 250)], [0, 1, 2])
        self.assertFalse(r.read("v@x.com", "V", 50)["seen"])

    def test_distinct_orgs_is_a_set_not_a_counter(self):
        r = Reputation(KEY)
        for i in range(30): r.contribute("v@x.com", "V", "same-org", "executed", 100.0 + i)                              # 30 agents' payments, one customer
        rec = r.read("v@x.com", "V", NOW)
        self.assertEqual(rec["distinct_orgs"], 1); self.assertEqual(rec["clean_exec_bucket"], bucket(30)); self.assertEqual(rec["clean_exec_bucket"], 25)

    def test_negative_evidence_is_counted_per_org_and_confirmed_fraud_is_a_flag(self):
        r = Reputation(KEY)
        r.contribute("m@x.com", "Mule", "o1", "identity_freeze", 10.0); r.contribute("m@x.com", "Mule", "o1", "denied", 11.0); r.contribute("m@x.com", "Mule", "o2", "denied", 12.0)
        rec = r.read("m@x.com", "Mule", 100.0); self.assertEqual((rec["freeze_count"], rec["distinct_orgs_froze"], rec["confirmed_fraud"]), (3, 2, False))
        r.contribute("m@x.com", "Mule", "o3", "confirmed_fraud", 20.0); self.assertTrue(r.read("m@x.com", "Mule", 100.0)["confirmed_fraud"])
        self.assertFalse(r.read("m@x.com", "Mule", 15.0)["confirmed_fraud"])

    def test_right_to_be_forgotten_removes_one_org_only(self):
        r = Reputation(KEY)
        r.contribute("v@x.com", "V", "leaving", "executed", 1.0); r.contribute("v@x.com", "V", "staying", "executed", 2.0)
        self.assertEqual(r.forget_org("leaving"), 1); self.assertEqual(r.read("v@x.com", "V", NOW)["distinct_orgs"], 1)
        r.forget_org("staying"); self.assertFalse(r.read("v@x.com", "V", NOW)["seen"]); self.assertEqual(r.db.execute("SELECT COUNT(*) c FROM rep_skeleton").fetchone()["c"], 0)

    def test_name_skeleton_peers_support_network_lookalike_checks(self):
        r = Reputation(KEY)
        for org in ("o1", "o2", "o3"): r.contribute("billing@aws-real.example", "AWS", org, "executed", 5.0)
        rec = r.read("pay@aws-lookalike.example", "A WS-Billing".replace(" ", ""), 100.0)       # "AWS-Billing" skeletons to the same bucket as "AWS"
        self.assertFalse(rec["seen"]); self.assertEqual(len(rec["skeleton_peers"]), 1); self.assertEqual(rec["skeleton_peers"][0]["distinct_orgs"], 3)


def mk(rep=None, **kw):
    t = [NOW]; pp = FakePayPal()
    svc = Service(Store(), pp, clock=lambda: t[0], default_require_intent=False, default_hard_cap=None, **kw)
    svc.reputation = rep
    return svc, pp, t


class Wiring(unittest.TestCase):
    def events(self, rep, recipient, name="X"):
        return [r["event"] for r in rep.db.execute("SELECT event FROM rep_event WHERE recipient_key=? ORDER BY ts", (rep.recipient_key(recipient),)).fetchall()]

    def test_executed_payments_contribute_but_frozen_and_shadow_ones_do_not(self):
        rep = Reputation(KEY); svc, pp, t = mk(rep); svc.set_agent("a", hard_cap=300, approved_payees=["AWS"], org_id="acme")
        pay(svc, "AWS", "50.00", agent="a", email="aws@example.com")
        self.assertEqual(self.events(rep, "aws@example.com"), ["executed"])
        pay(svc, "AWS", "900.00", agent="a", email="aws@example.com")                                                   # over the cap: a freeze about the AGENT
        self.assertEqual(self.events(rep, "aws@example.com"), ["executed"])
        svc.set_agent("a", hard_cap=10, approved_payees=["AWS"], org_id="acme", mode="shadow"); t[0] += 3600
        pay(svc, "AWS", "60.00", agent="a", email="aws@example.com")                                                    # shadow would-freeze that proceeds
        self.assertEqual(self.events(rep, "aws@example.com"), ["executed", "executed"])                                 # executed yes; no negative event

    def test_only_identity_freezes_and_their_confirmation_become_negative_evidence(self):
        rep = Reputation(KEY); svc, pp, t = mk(rep); svc.set_agent("a", approved_payees=["AWS"], org_id="acme"); pay(svc, "AWS", "50.00", agent="a", email="aws@example.com")
        t[0] += 86400
        out = pay(svc, "AWS", "50.00", agent="a", email="attacker@evil.example")                                         # known payee, new destination
        self.assertEqual(out["status"], "frozen"); self.assertEqual(self.events(rep, "attacker@evil.example"), ["identity_freeze"])
        svc.deny(out["payment_id"], "operator"); self.assertEqual(self.events(rep, "attacker@evil.example"), ["identity_freeze", "denied"])
        t[0] += 86400; svc.set_agent("a", hard_cap=20, approved_payees=["AWS"], org_id="acme"); big = pay(svc, "AWS", "400.00", agent="a", email="aws@example.com")
        svc.deny(big["payment_id"], "operator"); self.assertEqual(self.events(rep, "aws@example.com"), ["executed"])      # denying an over-cap payment says nothing about the payee

    def test_confirm_fraud_is_shared_audited_and_denies_a_frozen_payment(self):
        rep = Reputation(KEY); svc, pp, t = mk(rep); svc.set_agent("a", hard_cap=10, org_id="acme")
        f = pay(svc, "Shady", "400.00", agent="a", email="shady@evil.example"); self.assertEqual(f["status"], "frozen")
        self.assertTrue(svc.confirm_fraud(f["payment_id"], "operator")["ok"])
        self.assertEqual(svc.store.payment(f["payment_id"])["status"], "denied"); self.assertTrue(rep.read("shady@evil.example", "Shady", t[0] + 1)["confirmed_fraud"])
        self.assertIn("confirmed_fraud", [r["event"] for r in svc.audit.rows()]); self.assertEqual(svc.confirm_fraud("nope")["ok"], False)

    def test_org_comes_from_operator_config_and_defaults_to_one_org(self):
        rep = Reputation(KEY); svc, pp, t = mk(rep)
        svc.set_agent("a", org_id="acme"); svc.set_agent("b")
        pay(svc, "AWS", "40.00", agent="a", email="aws@example.com"); t[0] += 3600; pay(svc, "AWS", "41.00", agent="b", email="aws@example.com")
        t[0] += 3600; pay(svc, "AWS", "42.00", agent="c", email="aws@example.com")
        self.assertEqual(rep.read("aws@example.com", "AWS", t[0] + 1)["distinct_orgs"], 2)                           # acme + the shared default org (b and c)

    def test_a_failing_or_absent_reputation_never_changes_or_blocks_a_payment(self):
        class Boom:
            def read(self, *a, **k): raise RuntimeError("down")
            def contribute(self, *a, **k): raise RuntimeError("down")
        base, _, _ = mk(None); a = pay(base, "AWS", "50.00", email="aws@example.com")
        svc, pp, t = mk(Boom()); b = pay(svc, "AWS", "50.00", email="aws@example.com")
        self.assertEqual((a["decision"], a["status"]), (b["decision"], b["status"]))

    def test_the_tenant_row_stores_a_summary_of_the_network_view_never_the_peer_records(self):
        rep = Reputation(KEY); rep.contribute("aws@example.com", "AWS", "other", "executed", NOW - DAY)
        svc, pp, t = mk(rep); pay(svc, "AWS", "50.00", email="aws@example.com")
        ev = json.loads(svc.store.payments("bill-bot")[0]["event_json"])["network"]
        self.assertEqual(set(ev), {"distinct_orgs", "distinct_orgs_froze", "freeze_count", "confirmed_fraud", "seen"})


class Regression(unittest.TestCase):
    def test_with_reputation_attached_and_no_scoring_module_the_benchmark_is_unchanged(self):
        from tools import score_benchmark_service as S
        base = S.run(); shared = S.run(reputation=Reputation(KEY))
        self.assertEqual({t: v[0] for t, v in base.items()}, {t: v[0] for t, v in shared.items()})              # tier for tier, 371/371


if __name__ == "__main__":
    unittest.main()
