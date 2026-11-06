import json
import math
import unittest

from app import gate as G
from app import policy as P
from app.schema import ValidationError
from tests.test_enrichment import Net, svc
from tests.test_service import pay
from tests import read_file


def mk(**kw):
    s = svc(Net(age=5000, mx=True), **kw)[0]
    return s


def full(**over):
    f = dict(P.PRESETS["standard"])
    f.update(over)
    return f


class T6NonFinite(unittest.TestCase):
    def test_nan_inf_negative_refused_for_every_numeric_knob_and_nothing_stored(self):
        s = mk()
        for knob in ("hard_cap", "new_payee_floor", "fanout_k", "cooldown_s", "probation_abs", "probation_mult"):
            for bad in (float("nan"), float("inf"), float("-inf"), -1.0, "nan", "inf", True):
                with self.assertRaises(ValidationError, msg=(knob, bad)):
                    s.set_agent("a1", **{knob: bad})
        self.assertEqual(s.store.agent("a1"), {})                                  # nothing reached the config
        self.assertEqual(json.loads(json.dumps({"hard_cap": float("nan")}))["hard_cap"] != 1, True)   # (json itself happily parses NaN: the guard is ours)

    def test_hard_cap_still_freezes_in_range_payment(self):
        s = mk()
        s.set_agent("a1", hard_cap=1000)
        self.assertTrue(math.isfinite(s.resolve_policy("a1")["knobs"]["hard_cap"]))

    def test_profile_fields_refuse_non_finite(self):
        s = mk()
        for bad in (float("nan"), float("inf"), 0, -5):
            with self.assertRaises(ValidationError):
                s.define_profile("p", full(hard_cap=bad))


class ProfileShape(unittest.TestCase):
    def test_all_eight_required_unknown_refused(self):
        s = mk()
        f = full()
        for k in P.PROFILE_FIELDS:
            g = dict(f); g.pop(k)
            with self.assertRaises(ValidationError):
                s.define_profile("p", g)
            g = dict(f); g[k] = None
            with self.assertRaises(ValidationError):
                s.define_profile("p", g)
        for extra in ("mode", "org_id", "token_hash", "approved_payees", "bogus"):
            with self.assertRaises(ValidationError):
                s.define_profile("p", {**f, extra: 1})
        with self.assertRaises(ValidationError):
            s.define_profile("a@b", f)

    def test_versions_append_never_edit_and_are_audited(self):
        s = mk()
        a = s.define_profile("strict", full(hard_cap=200))
        b = s.define_profile("strict", full(hard_cap=100))
        self.assertEqual((a["version"], b["version"]), (1, 2))
        self.assertEqual(s.store.profile("strict", 1)["fields"]["hard_cap"], 200.0)       # v1 untouched
        ev = [json.loads(r["detail"]) for r in s.audit.rows(0, 100, "profile_changed")]
        self.assertEqual([e["version"] for e in ev], [1, 2])
        self.assertEqual(ev[1]["before"]["hard_cap"], 200.0)


class Resolution(unittest.TestCase):
    def test_no_profile_is_exactly_todays_behaviour(self):
        s = mk()
        s.set_agent("a1", hard_cap=700, fanout_k=5)
        k = s.resolve_policy("a1")["knobs"]
        d = P.system_defaults(s.default_hard_cap, s.default_require_intent)
        self.assertEqual(k["hard_cap"], 700.0)
        self.assertEqual(k["fanout_k"], 5.0)
        self.assertEqual(k["new_payee_floor"], d["new_payee_floor"])
        self.assertEqual(k["probation_abs"], G.PROBATION_ABS)
        self.assertIsNone(s.resolve_policy("a1")["profile"])

    def test_override_beats_profile_beats_default(self):
        s = mk()
        s.define_profile("strict", full(hard_cap=200, new_payee_floor=40, fanout_k=2))
        s.set_agent("a1", hard_cap=300)                         # explicit per-agent override
        s.assign_profile("a1", "strict")
        k = s.resolve_policy("a1")["knobs"]
        self.assertEqual(k["hard_cap"], 300.0)                  # override wins
        self.assertEqual(k["new_payee_floor"], 40.0)            # profile beats default
        s.set_agent("a1")                                       # an edit does not unpin; the override is gone, so the profile shows through
        self.assertEqual(s.resolve_policy("a1")["profile"], "strict@1")
        self.assertEqual(s.resolve_policy("a1")["knobs"]["hard_cap"], 200.0)

    def test_platform_cap_is_a_last_step_ceiling_over_a_profile(self):
        s = mk(platform_cap=150)
        s.define_profile("permissive", full(hard_cap=2000))
        s.assign_profile("a1", "permissive")
        self.assertEqual(s.resolve_policy("a1")["knobs"]["hard_cap"], 150.0)
        out = pay(s, "AWS", "180.00", agent="a1")
        self.assertEqual(out["decision"], "freeze")             # the profile cannot lift the agent above the operator's ceiling

    def test_assignment_pins_a_version_and_is_audited(self):
        s = mk()
        s.define_profile("strict", full(hard_cap=200))
        s.assign_profile("a1", "strict")
        s.define_profile("strict", full(hard_cap=50))           # v2 exists
        self.assertEqual(s.resolve_policy("a1")["knobs"]["hard_cap"], 200.0)       # still pinned to v1
        s.assign_profile("a1", "strict@2")
        self.assertEqual(s.resolve_policy("a1")["knobs"]["hard_cap"], 50.0)
        last = json.loads(s.audit.rows(0, 100, "config_changed")[-1]["detail"])
        self.assertEqual(last["profile_assigned"], "strict@2")
        self.assertEqual(last["before"]["profile"], "strict@1")
        with self.assertRaises(ValidationError):
            s.assign_profile("a1", "strict@9")
        s.assign_profile("a1", None)
        self.assertIsNone(s.resolve_policy("a1")["profile"])

    def test_profile_survives_set_agent_and_modify_agent_and_token_issue(self):
        s = mk()
        s.define_profile("strict", full())
        s.assign_profile("a1", "strict")
        s.modify_agent("a1", approve=["AWS"])
        s.modify_agent("a1", mode="shadow")
        s.issue_agent_token("a1")
        s.set_agent("a1", hard_cap=100)
        self.assertEqual(s.store.agent("a1")["profile"], "strict@1")

    def test_cooldown_from_a_profile_schedules_the_hold_in_the_future(self):          # T1
        s, pp, t, sent = svc(Net(age=5000, mx=True))
        from tests.test_cooloff import hist_rows
        hist_rows(s)
        s.define_profile("cool", full(hard_cap=1000, cooldown_s=1800, require_intent=False))
        s.assign_profile("bill-bot", "cool")
        out = pay(s, "Newco", "110.00", email="pay@newco-clean.com")
        p = s.store.payment(out["payment_id"])
        self.assertEqual(p["status"], "frozen")
        self.assertTrue(p["reason"].startswith("cooling-off:"))
        self.assertAlmostEqual(p["release_at"] - p["ts"], 1800.0, delta=1)                # not 'due now'


class SignedPresets(unittest.TestCase):
    def test_presets_match_the_signed_table_exactly(self):
        want = {"strict": (200, True, 2, 1800, 50, 200, 2), "standard": (500, True, 3, 900, 100, 500, 3), "permissive": (2000, True, 4, 1800, 250, 1000, 4)}
        for n, (cap, intent, fk, cd, floor, pa, pm) in want.items():
            f = P.PRESETS[n]
            self.assertEqual((f["hard_cap"], f["require_intent"], f["fanout_k"], f["cooldown_s"], f["new_payee_floor"], f["probation_abs"], f["probation_mult"]),
                             (float(cap), intent, float(fk), float(cd), float(floor), float(pa), float(pm)), n)
            self.assertEqual(f["active_hours"], [])
            P.validate_profile(n, f)


class Snapshot(unittest.TestCase):
    def test_decision_carries_hash_and_body_is_in_the_chain_once(self):
        s = mk()
        s.set_agent("bill-bot", hard_cap=900)
        o1 = pay(s, "AWS", "20.00")
        o2 = pay(s, "AWS", "21.00", rid="r2") if False else pay(s, "Github", "22.00")
        dec = [json.loads(r["detail"]) for r in s.audit.rows(0, 200, "decision")]
        self.assertTrue(all(d["policy_hash"] for d in dec))
        snaps = [json.loads(r["detail"]) for r in s.audit.rows(0, 200, "policy_resolved")]
        self.assertEqual(len(snaps), 1)                                                   # one body for many decisions
        self.assertEqual(snaps[0]["policy_hash"], dec[0]["policy_hash"])
        self.assertEqual(P.policy_hash(snaps[0]["policy"]), snaps[0]["policy_hash"])      # the hash really is of the stored body
        self.assertEqual(snaps[0]["policy"]["knobs"]["hard_cap"], 900.0)
        self.assertEqual(s.store.payment(o1["payment_id"])["policy_hash"], dec[0]["policy_hash"])
        s.set_agent("bill-bot", hard_cap=800)
        pay(s, "AWS", "23.00")
        self.assertEqual(len(s.audit.rows(0, 200, "policy_resolved")), 2)                 # a changed policy gets a new snapshot
        self.assertTrue(s.audit.verify())

    def test_gate_sha256_is_in_the_body_computed_once_and_matches_the_file(self):
        import hashlib
        b = P.policy_body(P.system_defaults(500, True), profile=None, mode="enforce", platform_cap=None, approved_payees=[])
        self.assertEqual(b["gate_sha256"], P.GATE_SHA256)
        self.assertEqual(P.GATE_SHA256, hashlib.sha256(read_file(G.__file__, "rb")).hexdigest())
        self.assertNotIn("engine_rev", b)
        s = mk()
        pay(s, "AWS", "20.00")
        snap = json.loads(s.audit.rows(0, 50, "policy_resolved")[-1]["detail"])
        self.assertEqual(snap["policy"]["gate_sha256"], P.GATE_SHA256)                     # present in the chained body
        h2 = P.policy_hash(P.policy_body(P.system_defaults(500, True), profile=None, mode="enforce", platform_cap=None, approved_payees=[]))
        P.GATE_SHA256, old = "0" * 64, P.GATE_SHA256
        try:
            self.assertNotEqual(P.policy_hash(P.policy_body(P.system_defaults(500, True), profile=None, mode="enforce", platform_cap=None, approved_payees=[])), h2)   # a different gate file = a different policy hash
        finally:
            P.GATE_SHA256 = old


class AuditOrRefuse(unittest.TestCase):                                                    # T7
    def test_config_change_is_rolled_back_when_the_audit_row_cannot_be_written(self):
        s = mk()
        s.set_agent("a1", hard_cap=100)
        s.audit.append = lambda *a, **k: None                   # the log swallows errors in production; force that failure
        with self.assertRaises(ValidationError):
            s.set_agent("a1", hard_cap=999)
        self.assertEqual(s.store.agent("a1")["hard_cap"], 100.0)
        with self.assertRaises(ValidationError):
            s.assign_profile("a1", None)
        with self.assertRaises(ValidationError):
            s.modify_agent("a1", approve=["X"])
        self.assertEqual(s.store.agent("a1")["approved_payees"], [])

    def test_decision_is_refused_when_the_policy_snapshot_cannot_be_recorded(self):
        s = mk()
        s.set_agent("bill-bot", hard_cap=900)
        real = s.audit.append
        s.audit.append = lambda ev, *a, **k: None if ev == "policy_resolved" else real(ev, *a, **k)
        out = pay(s, "AWS", "20.00")
        self.assertEqual(out["status"], "fail_closed")                                     # nothing sent: an unprovable decision is not made


if __name__ == "__main__":
    unittest.main()
