"""Audit proofs (Security): (1) O2 is a THORN -- disabling it changes no verdict in the real gate; (2) STRUCTURING has an
open side-door -- slow FAN-OUT across many different new payees drains the account without tripping any guard, because the
cumulative structuring cap is per-payee and the only agent-wide cumulative guard is gated on hard_cap and loose. Includes a
validated prototype of the fix (an agent-wide cumulative cap on untrusted/unapproved payees) for infra to wire into gate.py.

These lock the findings so they can't silently regress or be 'fixed' by accident without the test noticing."""
import copy
import unittest

from app import gate as G
from tests.gate_patch import gate_overrides

DAY = 86400.0


def _agent(mean=100, cap=1000, cv=0.1, n=15):
    h = G.History(hard_cap=cap); ts = 1_700_000_000.0
    import random; r = random.Random(7)
    for i in range(n):
        ts += 20 * 3600; v = max(1, round(r.gauss(mean, mean * cv), 2))
        h.observe("AWS" if i % 2 else "GH", v, ts); h.add_spend(ts, "AWS" if i % 2 else "GH", v)
    return h, ts


def _tier_without_o2(h, txn):
    h2 = copy.deepcopy(h); h2.probation_abs = 10 ** 9; h2.probation_mult = 10 ** 9
    return G._classify_base(txn, h2)[0]


class O2IsInertInIsolationButArmorOnEscalation(unittest.TestCase):
    """CORRECTED: my original audit claimed O2 was a thorn (dead code). That was WRONG -- it came from sweeping ISOLATED
    payments against a STATIC baseline, where O2 indeed changes nothing (the cases below). But on the SLOW-ESCALATION ramp
    (a known-but-unproven payee trained up over time), the per-payee ratchet/ceiling and the z-score ADAPT upward and stop
    flagging, and O2's absolute cap becomes the ONLY remaining flag -- review WITH it, silent allow WITHOUT it. Infra's
    counterexample (tests/test_fresh_attacker_rules.py::O2IsNotDeadCode) is the authoritative proof and O2 is KEPT. These
    two tests document the honest truth: O2 is INERT IN ISOLATION (so it looks like a thorn if you only sweep static
    cases), but it is ARMOR on the escalation ramp. The lesson: a differential audit must include the adaptive-baseline
    path, not just isolated payments."""

    def test_o2_is_inert_on_an_isolated_new_payee_payment(self):
        # true but not the whole story: against a static baseline a new payee is already a review below 2x / freeze above,
        # so O2 adds nothing HERE. This is why an isolated sweep wrongly reads it as dead.
        h, ts = _agent(mean=463, cap=20000)
        for amt in (400, 500, 600, 700, 900, 1000):
            h1 = copy.deepcopy(h); h1.note_attempt(ts + 3600, "nb")
            txn = {"kind": "txn", "payee": "nb", "amount": float(amt), "ts": ts + 3600, "hour": 10, "stated_intent_payees": ["nb"]}
            self.assertEqual(G._classify_base(txn, h1)[0], _tier_without_o2(h, {**txn}))

    def test_o2_IS_armor_on_the_escalation_ramp_see_infra_differential(self):
        # the case the isolated sweep missed: a payee trained up so nothing else flags -> O2 turns a silent allow into a
        # review. Run the real benchmark with and without O2 and confirm >=1 payment flips review<->allow because of it.
        import contextlib, io
        from tools import score_benchmark as B
        with contextlib.redirect_stdout(io.StringIO()):
            on = B.run()
        with gate_overrides(PROBATION_ABS=float("inf"), PROBATION_MULT=float("inf")), contextlib.redirect_stdout(io.StringIO()):
            off = B.run()
        flips = [(on[k][0], off[k][0]) for k in on if on[k][0] != off[k][0]]
        self.assertTrue(flips and all(a == "review" and b == "allow" for a, b in flips))   # O2 is the sole flag here -> armor


class StructuringFanOutOpenPort(unittest.TestCase):
    """Slow fan-out -- many small payments to many DIFFERENT new payees, spaced beyond the 60s drain window -- drains the
    account without any guard firing. The per-payee cumulative cap never accumulates (one payment each) and the agent-wide
    WINDOW_MULT guard is gated on hard_cap and loose (and OFF entirely when hard_cap is None)."""

    def _fan_out(self, cap, n=30, spacing=300, amt=90.0):
        h, ts = _agent(cap=cap); t = ts; total = 0.0; frozen_at = None
        for i in range(n):
            t += spacing
            h.note_attempt(t, "mule%d" % i)
            tier = G._classify_base({"kind": "txn", "payee": "mule%d" % i, "amount": amt, "ts": t, "hour": 10,
                                     "stated_intent_payees": ["mule%d" % i]}, h)[0]
            if tier != "freeze":
                h.observe("mule%d" % i, amt, t); h.add_spend(t, "mule%d" % i, amt); total += amt
            else:
                frozen_at = (i + 1, total); break
        return frozen_at, total

    def test_slow_fan_out_across_many_new_payees_is_now_caught_with_a_cap(self):
        # FIXED (fan-out guard, FANOUT_K x typical on money to not-yet-trusted, non-approved payees in 24h): was $2700 across 30 mules, never frozen.
        frozen_at, total = self._fan_out(cap=1000)
        self.assertIsNotNone(frozen_at); self.assertLessEqual(total, 600)

    def test_slow_fan_out_is_now_caught_with_no_cap_too(self):
        frozen_at, total = self._fan_out(cap=None)                       # independent of hard_cap: the old WINDOW_MULT guard is off when cap is None
        self.assertIsNotNone(frozen_at); self.assertLessEqual(total, 600)

    def test_single_payee_structuring_IS_caught_the_gap_is_specifically_fan_out(self):
        # contrast: the SAME attack to ONE payee is caught by the untrusted-payee budget (per-payee). The gap is fan-out.
        h, ts = _agent(cap=None); t = ts; total = 0.0; frozen = False
        for i in range(15):
            t += 4000
            h.note_attempt(t, "onemule")
            tier = G._classify_base({"kind": "txn", "payee": "onemule", "amount": 90.0, "ts": t, "hour": 10,
                                     "stated_intent_payees": ["onemule"]}, h)[0]
            if tier != "freeze":
                h.observe("onemule", 90.0, t); h.add_spend(t, "onemule", 90.0); total += 90
            else:
                frozen = True; break
        self.assertTrue(frozen, "single-payee structuring should be caught by the untrusted-payee budget")


class FanOutFixPrototype(unittest.TestCase):
    """Validates the proposed fix for infra to wire into gate.py: an agent-wide cumulative cap on money to NOT-YET-TRUSTED,
    NON-APPROVED payees over 24h (K x typical, independent of hard_cap). Catches the fan-out; does NOT nag a legit agent
    paying its KNOWN (trusted) vendors, which are excluded from the sum."""
    K = 5

    def _untrusted_window_sum(self, h, now, secs):
        return sum(a for (t, p, a) in h.spend if now - secs <= t <= now and p not in h.approved and not h.trusted(p, now))

    def test_fix_catches_fan_out(self):
        h, ts = _agent(cap=1000); m, _ = h.mean_sd(); budget = self.K * m
        t = ts; caught = None
        for i in range(30):
            t += 300
            if self._untrusted_window_sum(h, t, DAY) + 90 > budget:
                caught = i + 1; break
            h.observe("mule%d" % i, 90.0, t); h.add_spend(t, "mule%d" % i, 90.0)
        self.assertIsNotNone(caught); self.assertLessEqual(caught, 8)   # caught within a handful of mules, not 30

    def test_fix_does_not_nag_a_legit_known_vendor_day(self):
        h, ts = _agent(cap=1000); m, _ = h.mean_sd(); budget = self.K * m
        self.assertTrue(h.trusted("AWS", ts))                           # known vendors are trusted (count + age)
        # a day of payments to trusted vendors contributes 0 to the untrusted sum
        for i in range(8):
            self.assertEqual(self._untrusted_window_sum(h, ts + i * 3600, DAY), 0.0)


if __name__ == "__main__":
    unittest.main()
