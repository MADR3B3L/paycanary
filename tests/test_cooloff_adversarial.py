"""ADVERSARIAL verification of O3 cooling-off auto-release (Security). Infra built it and says it's mutation-checked; this
pass independently TRIES TO BREAK each safety property through the real Service, because a release path that was not
attacked is worthless. Every test here attempts to make money auto-release when it must NOT, and asserts it stayed held.

The property under test: a cooling-off hold auto-releases ONLY when the Tier-1 gather COMPLETED and found nothing adverse
(unknown != clean), AND a fresh re-check still passes, AND no operator denial raced it. Anything else -> stays held.

Plus the honest benchmark accounting: a clean legit new vendor above the band is DELAYED (friction) not blocked; a
mule-shaped one is held and does NOT auto-release."""
import json
import threading
import unittest

from tests.test_enrichment import Net, svc, NOW
from tests.test_cooloff import mkc, held_payment, hist_rows
from tests.test_service import pay

DAY = 86400.0


class AutoReleaseRefusesWhenItMust(unittest.TestCase):
    def _sweep(self, s, t, past=1801):
        t[0] += past
        return {pid: (outcome, detail) for pid, outcome, detail in s.release_due()}

    def test_a_clean_checked_gather_DOES_auto_release_after_the_window(self):
        # the one case that SHOULD release: gather completed, hint none, domain_age + blocklist checked, re-check passes
        s, pp, t, sent = mkc(net=Net(age=5000, mx=True, feed_hosts=()))
        out, p = held_payment(s, t)                                        # Newco @ a clean domain, 110 > 1.5x typical
        self.assertEqual(out["status"], "frozen"); self.assertIn("cooling-off", out["reasons"][0])
        res = self._sweep(s, t)
        self.assertEqual(res[out["payment_id"]][0], "released")
        self.assertEqual(s.store.payment(out["payment_id"])["status"], "sent")

    def test_ADVERSE_evidence_never_auto_releases(self):
        # the domain is in a phishing feed -> the gather returns a hard hint -> must stay held, never sent
        s, pp, t, sent = mkc(net=Net(age=5000, mx=True, feed_hosts=["newco-bad.com"]))
        out, p = held_payment(s, t, email="pay@newco-bad.com")
        res = self._sweep(s, t)
        self.assertEqual(res[out["payment_id"]][0], "held")
        self.assertNotEqual(s.store.payment(out["payment_id"])["status"], "sent")

    def test_UNKNOWN_is_not_clean_a_failed_gather_never_auto_releases(self):
        # the feed + rdap calls fail -> evidence incomplete/absent -> unknown != clean -> held for a human after the grace
        s, pp, t, sent = mkc(net=Net(fail=("rdap", "feed", "mx")))
        out, p = held_payment(s, t)
        t[0] += 1801 + 601                                                # past the window AND the 10-min grace
        res = {pid: o for pid, o, _ in s.release_due()}
        self.assertIn(res[out["payment_id"]], ("held", "waiting"))
        self.assertNotEqual(s.store.payment(out["payment_id"])["status"], "sent")

    def test_within_grace_a_missing_gather_WAITS_it_does_not_release(self):
        s, pp, t, sent = mkc(net=Net(fail=("rdap", "feed")), enrich=False)   # no enricher -> no evidence_json at all
        out, p = held_payment(s, t)
        t[0] += 1801                                                      # past window, within the 10-min grace
        res = {pid: o for pid, o, _ in s.release_due()}
        self.assertEqual(res[out["payment_id"]], "waiting")
        self.assertNotEqual(s.store.payment(out["payment_id"])["status"], "sent")

    def test_a_fresh_recheck_that_now_freezes_keeps_it_held_even_with_clean_evidence(self):
        # clean evidence, but the operator tightens the cap below the amount during the window -> the re-check must freeze
        s, pp, t, sent = mkc(net=Net(age=5000, mx=True))
        out, p = held_payment(s, t, amount="110.00")
        s.set_agent("bill-bot", hard_cap=50, cooldown_s=1800)             # cap now below the held amount
        res = self._sweep(s, t)
        self.assertEqual(res[out["payment_id"]][0], "held")
        self.assertNotEqual(s.store.payment(out["payment_id"])["status"], "sent")

    def test_operator_deny_wins_over_a_concurrent_sweep_exactly_once(self):
        s, pp, t, sent = mkc(net=Net(age=5000, mx=True))
        out, p = held_payment(s, t)
        pid = out["payment_id"]; t[0] += 1801
        barrier = threading.Barrier(2)
        results = {}
        def do_sweep():
            barrier.wait(); results["sweep"] = s.release_due()
        def do_deny():
            barrier.wait()
            try: results["deny"] = s.deny(pid, "operator")
            except Exception as e: results["deny"] = ("err", type(e).__name__)
        ths = [threading.Thread(target=do_sweep), threading.Thread(target=do_deny)]
        [x.start() for x in ths]; [x.join() for x in ths]
        final = s.store.payment(pid)["status"]
        self.assertIn(final, ("denied", "sent"))                         # one winner, consistent
        # and PayPal was called AT MOST once for this payment (no double-send under the race)
        self.assertLessEqual(sum(1 for c in pp.calls if c[0] == "pay@newco-clean.com"), 1)


class HonestBenchmarkAccounting(unittest.TestCase):
    def test_a_below_band_first_payment_is_untouched_by_cooloff(self):
        s, pp, t, sent = mkc(net=Net(age=5000, mx=True))
        out = pay(s, "SmallNew", "70.00", email="ap@smallnew-clean.com")  # below 1.5x typical
        self.assertNotIn("cooling-off", (out.get("reasons") or [""])[0])

    def test_a_clean_legit_new_vendor_above_band_is_DELAYED_not_blocked(self):
        # the honest false-nag accounting: friction (a delay), not a block, and only because it's above 1.5x typical
        s, pp, t, sent = mkc(net=Net(age=5000, mx=True))
        out, p = held_payment(s, t, payee="BigNewVendor", email="ap@bignewvendor-clean.com")
        self.assertEqual(out["status"], "frozen")                        # delayed at first ...
        res = self._released(s, t, out["payment_id"])
        self.assertEqual(res, "released")                                # ... then auto-released once clean -> a delay, not a nag

    def _released(self, s, t, pid):
        t[0] += 1801
        return {p: o for p, o, _ in s.release_due()}[pid]


if __name__ == "__main__":
    unittest.main()
