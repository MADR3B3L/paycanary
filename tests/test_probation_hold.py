"""The opt-in `probation_hold` posture (cooling-off also holds a first payment above the probation ceiling, checked before the probation review).
Pinned properties: flag OFF is a byte-identical no-op against the pre-reorder gate (tests/legacy_gate_78a555c.py, a frozen copy); flag ON changes exactly one
class of outcome (review -> cooling-off hold); a higher first payment never gets LESS friction than a lower one under any preset or flag state; the
boundary and FX paths; end to end hold -> auto-release; and the enrichment-off warning."""
import itertools
import json
import unittest

from app import fx as fxmod
from app import gate as G
from app import policy as P
from tests import legacy_gate_78a555c as L
from tests.test_cooloff import hist_rows
from tests.test_enrichment import Net, svc
from tests.test_service import pay

DAY = 86400.0
GATE_PRISTINE = {k: v for k, v in vars(G).items() if k.isupper() and isinstance(v, (int, float))}
DEFAULTS_PRISTINE = G.History.__init__.__defaults__      # snapshot at import, before any test can touch a constant


def assert_gate_untouched(tc):
    """Fails LOUDLY if another test leaked a mutated gate constant into this one (and says which, plus what threads are alive), instead of letting a
    pinned verdict flip for a confusing reason."""
    now = {k: v for k, v in vars(G).items() if k.isupper() and isinstance(v, (int, float))}
    diff = {k: (GATE_PRISTINE.get(k), v) for k, v in now.items() if GATE_PRISTINE.get(k) != v}
    import threading
    tc.assertEqual(diff, {}, "gate constants were mutated by another test; live threads: %s" % [t.name for t in threading.enumerate()])
    tc.assertEqual(G.History.__init__.__defaults__, DEFAULTS_PRISTINE, "History.__init__ defaults were mutated by another test")


RANK = {"allow": 0, "review": 1, "freeze": 2}


def build(mod, typical, *, cooldown=0.0, approved=(), flag=False, cap=5000.0, floor=100.0, pa=500.0, pm=3.0, fk=3.0):
    kw = dict(hard_cap=cap, approved_payees=list(approved), new_payee_floor=floor, probation_abs=pa, probation_mult=pm, cooldown_s=cooldown, fanout_k=fk)
    if mod is G:
        kw["probation_hold"] = flag
    h = mod.History(**kw)
    for i, a in enumerate([typical * x for x in (0.9, 1.0, 1.1, 0.95, 1.05, 1.0, 0.98, 1.02)]):
        h.observe("P%d" % (i % 4), a, 1000.0 + i * 4 * DAY, 10)
    return h


def txn(payee, amount, **kw):
    return {"kind": "txn", "payee": payee, "amount": float(amount), "ts": 1000.0 + 50 * DAY, "hour": 10, "stated_intent_payees": [payee], "require_intent": True, **kw}


AMOUNTS = sorted({5, 20, 50, 99, 100, 101, 150, 200, 300, 499, 500, 501, 600, 900, 1000, 1400, 1500, 1501, 1600, 2500, 5000, 20000})
TYPICALS = (20, 60, 150, 400, 1000, 3000)


class FlagOffIsANoOp(unittest.TestCase):
    def setUp(self):
        assert_gate_untouched(self)

    def test_identical_to_the_pre_reorder_gate_across_a_wide_grid(self):
        n = 0
        for typ, amt, cd, payee, appr, rel, fx in itertools.product(TYPICALS, AMOUNTS, (0.0, 60.0, 900.0, 7200.0), ("New", "P1"), ((), ("New",)), (False, True), (1.0, 0.0067)):
            t = txn(payee, amt * (1 if fx == 1.0 else 150), fx=fx, cooloff_released=rel)
            new = G._classify_base(dict(t), build(G, typ, cooldown=cd, approved=appr))
            old = L._classify_base(dict(t), build(L, typ, cooldown=cd, approved=appr))
            self.assertEqual(new, old, (typ, amt, cd, payee, appr, rel, fx))
            n += 1
        self.assertGreater(n, 3000)

    def test_flag_on_changes_exactly_one_class_review_to_a_cooling_off_hold(self):
        changed = 0
        for typ, amt, cd, payee, appr, rel in itertools.product(TYPICALS, AMOUNTS, (0.0, 900.0), ("New", "P1"), ((), ("New",)), (False, True)):
            t = txn(payee, amt, cooloff_released=rel)
            new = G._classify_base(dict(t), build(G, typ, cooldown=cd, approved=appr, flag=True))
            old = L._classify_base(dict(t), build(L, typ, cooldown=cd, approved=appr))
            if new != old:
                changed += 1
                self.assertEqual((old[0], new[0]), ("review", "freeze"), (typ, amt, cd, payee, appr, rel, old, new))
                self.assertIn("cooling-off", new[2])
                self.assertTrue(cd > 0 and payee == "New" and not appr and not rel)            # only an unapproved first payment, with cooling-off on, not the release re-check
        self.assertGreater(changed, 0)


class FrictionOrdering(unittest.TestCase):
    def setUp(self):
        assert_gate_untouched(self)

    """The 'a higher first payment never gets LESS friction' invariant holds ONLY with probation_hold on and cooling-off on. With the flag OFF (the
    shipping default) the probation review is checked before the cooling-off hold, which leaves a KNOWN, accepted inversion, pinned below."""

    def test_monotonic_with_the_flag_on_across_every_preset_and_a_wide_typical_and_ceiling_grid(self):
        for preset in P.PRESETS:
            f = P.PRESETS[preset]
            for typ in TYPICALS + (250, 280, 300, 320, 340):
                last, last_amt = -1, None
                for amt in sorted(set(AMOUNTS) | {round(typ * x, 2) for x in (0.2, 0.5, 0.8, 1.2, 1.4, 1.5, 1.6, 1.8, 2.0, 2.5, 4, 6, 10)}):
                    h = build(G, typ, cooldown=f["cooldown_s"], flag=True, cap=f["hard_cap"], floor=f["new_payee_floor"], pa=f["probation_abs"], pm=f["probation_mult"], fk=f["fanout_k"])
                    rank = RANK[G._classify_base(txn("New", amt), h)[0]]
                    self.assertGreaterEqual(rank, last, "%s typical=%s: %s got less friction than %s" % (preset, typ, amt, last_amt))
                    last, last_amt = rank, amt

    def test_monotonic_with_the_flag_on_across_probation_ceilings(self):
        for typ, pa in itertools.product((100, 300, 1000), (100, 200, 500, 1000, 5000)):
            last = -1
            for amt in sorted(set(AMOUNTS) | {round(typ * x, 2) for x in (1.2, 1.4, 1.5, 1.6, 1.8, 2.0, 2.2)}):
                rank = RANK[G._classify_base(txn("New", amt), build(G, typ, cooldown=900.0, flag=True, pa=pa))[0]]
                self.assertGreaterEqual(rank, last, (typ, pa, amt)); last = rank

    def test_the_flag_off_inversion_is_pinned_as_known_behaviour(self):
        # typical ~$300 (wide spread so the z-score rule stays quiet) with a $500 probation ceiling: 1.5x typical = 450 < ceiling 500 < 2x typical (the new-payee freeze) = 600.
        # Flag OFF: $460 is a cooling-off HOLD, but a LARGER $550 is only a flagged REVIEW (money moves). Accepted: probation is checked first.
        # It is locked here so nobody 'fixes' the default path by accident; turning probation_hold on removes it (asserted below).
        def hist(mod, **kw):
            k = dict(hard_cap=5000.0, new_payee_floor=100000.0, probation_abs=500.0, probation_mult=3.0, cooldown_s=900.0, fanout_k=3.0)
            k.update(kw)
            h = mod.History(**k)
            for i, a in enumerate([100, 500, 200, 400, 300, 300, 100, 500]):
                h.observe("P%d" % (i % 4), float(a), 1000.0 + i * 4 * DAY, 10)
            return h
        off = lambda a: G._classify_base(txn("New", a), hist(G, probation_hold=False))
        on = lambda a: G._classify_base(txn("New", a), hist(G, probation_hold=True))
        self.assertEqual(off(460.0)[0], "freeze"); self.assertIn("cooling-off", off(460.0)[2])
        self.assertEqual(off(550.0)[:2], ("review", "novelty"))
        self.assertGreater(RANK[off(460.0)[0]], RANK[off(550.0)[0]])                         # the inversion
        self.assertEqual((on(460.0)[0], on(550.0)[0]), ("freeze", "freeze"))                 # flag on: gone
        for cd in (0.0, 900.0):                                                              # and the old gate agrees with the flag-off result at every cooldown
            for a in (460.0, 550.0):
                self.assertEqual(G._classify_base(txn("New", a), hist(G, cooldown_s=cd)), L._classify_base(txn("New", a), hist(L, cooldown_s=cd)))


    def test_the_flag_off_inversion_also_reproduces_on_the_architects_seeded_fixture(self):
        import random
        import time
        now = 1_800_000_000.0
        hr = time.gmtime(now).tm_hour

        def mk(mod, **kw):
            r = random.Random(7)
            h = mod.History(hard_cap=100000.0, cooldown_s=1800.0, **kw)
            for d in range(1, 61, 2):
                for p in ("AWS", "GitHub", "Stripe"):
                    h.observe(p, round(300 * r.uniform(.4, 1.6), 2), ts=now - d * 86400, hour=hr, recipient="x@" + p.lower() + ".com")
            return h

        def call(mod, amount, **kw):
            t = {"kind": "txn", "agent": "a", "payee": "NewCo", "recipient": "a@newco.com", "amount": float(amount), "currency": "USD", "memo": "", "ts": now, "hour": hr,
                 "stated_intent_payees": ["NewCo"], "intent_id": None, "require_intent": True, "fx": 1.0}
            return mod.classify(t, mk(mod, **kw))
        off480, off550 = call(G, 480), call(G, 550)
        self.assertEqual(off480[0], "freeze"); self.assertIn("cooling-off", off480[2])         # smaller: held
        self.assertEqual(off550[:2], ("review", "novelty"))                                     # larger: sent flagged (the inversion)
        self.assertEqual((call(G, 480, probation_hold=True)[0], call(G, 550, probation_hold=True)[0]), ("freeze", "freeze"))
        self.assertEqual((off480, off550), (call(L, 480), call(L, 550)))                        # identical to the pre-reorder gate


class Boundaries(unittest.TestCase):
    def test_exactly_at_the_probation_threshold_is_not_over_it(self):
        thr = min(500.0, 3.0 * 1000.0)                                              # typical ~1000 -> threshold 500
        h = lambda: build(G, 1000.0, cooldown=900.0, flag=True)
        at = G._classify_base(txn("New", thr), h())
        over = G._classify_base(txn("New", thr + 0.01), h())
        self.assertNotIn("cooling-off", at[2])                                       # amount == threshold: not above it
        self.assertEqual(over[0], "freeze"); self.assertIn("cooling-off", over[2])  # one cent over: held
        self.assertEqual(G._probation_threshold(h(), 1000.0), 500.0)

    def test_fx_path_uses_usd_equivalent_for_eur_and_jpy(self):
        for cur in ("EUR", "JPY"):
            rate = fxmod.to_usd_rate(cur)
            self.assertTrue(rate and rate > 0)
            typ = 1000.0 / rate                                                      # typical == $1000 in this currency
            h = lambda: build(G, typ, cooldown=900.0, flag=True)
            thr_native = 500.0 / rate
            below = G._classify_base(txn("New", round(thr_native * 0.98), fx=rate, currency=cur), h())
            above = G._classify_base(txn("New", round(thr_native * 1.05), fx=rate, currency=cur), h())
            self.assertNotIn("cooling-off", below[2], cur)
            self.assertEqual(above[0], "freeze", cur); self.assertIn("cooling-off", above[2], cur)


class EndToEnd(unittest.TestCase):
    def mk(self, net, flag):
        s, pp, t, sent = svc(net)
        hist_rows(s)                                                                  # typical ~$60
        s.set_agent("bill-bot", hard_cap=1000, cooldown_s=1800, require_intent=False, probation_hold=flag, probation_abs=70.0)
        return s, pp, t

    def test_band_payment_is_a_flagged_review_by_default_and_held_then_auto_released_when_opted_in_with_clean_evidence(self):
        # typical ~$60: 1.5x = 90. Set the probation ceiling to 70 so $80 sits in the band (over the ceiling, under 1.5x typical).
        s2, pp2, t2 = self.mk(Net(age=5000, mx=True), flag=False)
        off = pay(s2, "Newco", "80.00", email="pay@newco-clean.com")
        self.assertNotEqual(off["status"], "frozen")                                    # flag off: sent flagged (review), exactly as before
        s3, pp3, t3 = self.mk(Net(age=5000, mx=True), flag=True)
        on = pay(s3, "Newco", "80.00", email="pay@newco-clean.com")
        row = s3.store.payment(on["payment_id"])
        self.assertEqual(row["status"], "frozen"); self.assertTrue(row["reason"].startswith("cooling-off:"))
        self.assertEqual(pp3.calls, [])
        t3[0] += 1801 + 600
        res = s3.release_due()
        self.assertEqual([r[1] for r in res], ["released"])                              # clean evidence + re-check passes -> auto-release
        self.assertEqual(s3.store.payment(on["payment_id"])["status"], "sent")

    def test_without_enrichment_the_hold_waits_for_a_human_and_the_operator_is_warned(self):
        s, pp, t, sent = svc(None)
        hist_rows(s)
        s.set_agent("bill-bot", hard_cap=1000, cooldown_s=1800, require_intent=False, probation_hold=True, probation_abs=70.0)
        s.enrichers = None
        self.assertTrue(any("enrichment is off" in w for w in s.policy_warnings(s.resolve_policy("bill-bot"))))
        out = pay(s, "Newco", "80.00", email="pay@newco-clean.com")
        self.assertEqual(s.store.payment(out["payment_id"])["status"], "frozen")
        t[0] += 1800 + 601
        res = s.release_due()
        self.assertEqual([r[1] for r in res], ["held"])                                  # never auto-releases without evidence
        self.assertEqual(s.store.payment(out["payment_id"])["status"], "frozen")

    def test_assigning_a_cooldown_profile_returns_the_warning_and_no_warning_when_cooldown_is_off(self):
        s, pp, t, sent = svc(None)
        s.enrichers = None
        f = dict(P.PRESETS["standard"])
        s.define_profile("standard", f)
        r = s.assign_profile("a1", "standard")
        self.assertTrue(r["warnings"])
        s.define_profile("nocool", {**f, "cooldown_s": 0.0})
        self.assertEqual(s.assign_profile("a2", "nocool")["warnings"], [])


class ProfileFlagRules(unittest.TestCase):
    def test_default_is_permanently_false_and_old_eight_field_versions_resolve_false(self):
        self.assertIs(P.system_defaults(500, True)["probation_hold"], False)
        s, *_ = svc(Net(age=5000, mx=True))
        legacy8 = {k: v for k, v in P.PRESETS["standard"].items() if k != "probation_hold"}
        s.store.put_profile("old", legacy8, 1.0)                                              # a pre-flag stored version (validation bypassed on purpose)
        s.assign_profile("a1", "old@1")
        self.assertIs(s.resolve_policy("a1")["knobs"]["probation_hold"], False)
        self.assertNotIn("probation_hold", s.store.profile("old", 1)["fields"])               # never rewritten

    def test_flag_with_no_cooldown_is_refused_in_a_profile_and_warned_on_an_override(self):
        from app.schema import ValidationError
        s, *_ = svc(Net(age=5000, mx=True))
        with self.assertRaises(ValidationError):
            s.define_profile("bad", {**P.PRESETS["standard"], "probation_hold": True, "cooldown_s": 0.0})
        s.set_agent("a1", probation_hold=True, cooldown_s=0)                                  # an override can produce the combo: warn
        self.assertTrue(any("no effect" in w for w in s.policy_warnings(s.resolve_policy("a1"))))
        s.set_agent("a2", probation_hold=True, cooldown_s=900)
        self.assertFalse(any("no effect" in w for w in s.policy_warnings(s.resolve_policy("a2"))))


class ProfileFlag(unittest.TestCase):
    def test_flag_is_a_ninth_profile_field_default_off_and_snapshotted(self):
        self.assertIn("probation_hold", P.PROFILE_FIELDS)
        for n, f in P.PRESETS.items():
            self.assertIs(f["probation_hold"], False, n)                                 # no shipped preset enables it
        s, *_ = svc(Net(age=5000, mx=True))
        s.define_profile("tight", {**P.PRESETS["standard"], "probation_hold": True})
        s.assign_profile("a1", "tight@1")
        pol = s.resolve_policy("a1")
        self.assertIs(pol["knobs"]["probation_hold"], True)
        self.assertIs(pol["body"]["knobs"]["probation_hold"], True)                      # in the content-addressed snapshot
        s.set_agent("a1", probation_hold=False)                                          # per-agent override beats the profile
        self.assertIs(s.resolve_policy("a1")["knobs"]["probation_hold"], False)
        from app.schema import ValidationError
        with self.assertRaises(ValidationError):
            s.define_profile("bad", {**P.PRESETS["standard"], "probation_hold": "yes"})


if __name__ == "__main__":
    unittest.main()
