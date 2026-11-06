"""Standing tests for the PURE external-evidence logic (app/external_evidence.py), independent of the network I/O.

Locks the O9 design guarantees (with infra's I/O-review refinements folded in):
  * a HARD signal (scam/phishing blocklist hit, or EXACT sanctions identifier) can FREEZE (= held-for-human) alone;
  * a SOFT signal (young domain, disposable, name/freemail mismatch, no MX, fuzzy sanctions NAME match) never freezes
    alone -- it accumulates to review/hold;
  * a real new vendor (aged domain, own mail, registry, no feed hit) scores NONE -> no friction (cold-start bar);
  * the cheap/lazy fresh mule (days-old domain / throwaway inbox / business-name-to-freemail / in a feed) is caught;
  * a signal that is ABSENT or in `unavailable` is NO INFORMATION -- never a clean pass, never penalized;
  * enrichment can only ADD suspicion -- NONE never relaxes the base tier (no lean-allow from a clean footprint);
  * the local checks (disposable, name-vs-freemail) are pure and send nothing out.
Infra's external I/O callers + the freeze-and-gather-then-ask wiring are tested separately."""
import unittest

from app import external_evidence as E


class LocalChecksNoNetwork(unittest.TestCase):
    def test_disposable_domain_detected(self):
        self.assertTrue(E.is_disposable("x@mailinator.com"))
        self.assertFalse(E.is_disposable("ap@realvendor.com"))
        self.assertTrue(E.is_disposable("x@my-temp.example", extra={"my-temp.example"}))   # infra can extend the list

    def test_business_name_to_freemail_is_a_mismatch_but_a_person_is_not(self):
        self.assertTrue(E.name_destination_mismatch("Microsoft Corporation", "billing@gmail.com"))
        self.assertTrue(E.name_destination_mismatch("Acme Payments LLC", "acme@yahoo.com"))
        self.assertFalse(E.name_destination_mismatch("Jane Smith", "jane.smith@gmail.com"))   # a person at gmail is normal
        self.assertFalse(E.name_destination_mismatch("Acme LLC", "ap@acme.com"))              # business at its own domain is fine
        self.assertFalse(E.name_destination_mismatch("Bright Kitchen", "bright.kitchen@gmail.com"))  # unsuffixed 2-word name: NOT flagged

    def test_local_checks_fill_only_the_offline_slice(self):
        ev = E.local_checks("Acme LLC", "x@mailinator.com")
        self.assertEqual(set(ev), {"in_disposable", "name_dest_mismatch"})
        self.assertTrue(ev["in_disposable"])


class HardSignalsFreeze(unittest.TestCase):
    def test_exact_sanctions_hit_freezes(self):
        h, why, items = E.score_evidence({"sanctions_exact": True, "checked": ["sanctions_exact"]})
        self.assertEqual(h, E.HINT_FREEZE); self.assertIn("sanction", why.lower())
        self.assertEqual(items[0]["severity"], "hard")

    def test_blocklist_hit_freezes_and_names_the_source(self):
        h, why, items = E.score_evidence({"blocklist_hit": True, "blocklist_source": "URLhaus"})
        self.assertEqual(h, E.HINT_FREEZE); self.assertIn("URLhaus", why)
        self.assertTrue(any("URLhaus" in i["detail"] for i in items))

    def test_a_FUZZY_sanctions_name_match_is_soft_not_a_freeze(self):
        # high false-positive rate on fuzzy name matches -> review-with-evidence, NEVER an auto-freeze
        h, _, items = E.score_evidence({"sanctions_name_similar": True})
        self.assertNotEqual(h, E.HINT_FREEZE)
        self.assertTrue(all(i["severity"] == "soft" for i in items))


class SoftSignalsAccumulateNeverFreezeAlone(unittest.TestCase):
    def test_a_young_domain_alone_is_at_most_review_never_freeze(self):
        self.assertEqual(E.score_evidence({"domain_age_days": 20})[0], E.HINT_NONE)      # young, not very young: +1, below review
        self.assertEqual(E.score_evidence({"domain_age_days": 3})[0], E.HINT_REVIEW)     # very young: +2 -> review, NOT freeze

    def test_several_soft_signals_accumulate_to_a_hold_not_a_freeze(self):
        h, _, items = E.score_evidence({"domain_age_days": 3, "in_disposable": True, "name_dest_mismatch": True})
        self.assertEqual(h, E.HINT_HOLD)
        self.assertGreaterEqual(len(items), 3)
        self.assertTrue(all(i["severity"] == "soft" for i in items))

    def test_the_cheap_fresh_mule_is_caught(self):
        h, _, _ = E.score_evidence({"domain_age_days": 2, "name_dest_mismatch": True})
        self.assertIn(h, (E.HINT_REVIEW, E.HINT_HOLD))          # caught (held/reviewed), not cleared blind

    def test_a_disposable_inbox_ALONE_holds_but_a_freemail_mismatch_ALONE_is_only_review(self):
        # the calibration that makes the inline catch REAL (infra's finding): Tier-0 alone must exceed the base 'review'
        # for a new payee or it changes nothing. A throwaway inbox is weight 3 -> HOLD alone (no legit vendor is paid at
        # one). A business-name-to-freemail is weight 2 -> review alone (a sole-prop can use gmail), holds only combined.
        self.assertEqual(E.score_evidence({"in_disposable": True})[0], E.HINT_HOLD)
        self.assertEqual(E.apply("review", E.HINT_HOLD), "hold")          # money stopped inline, no network
        self.assertEqual(E.score_evidence({"name_dest_mismatch": True})[0], E.HINT_REVIEW)
        self.assertEqual(E.apply("review", E.HINT_REVIEW), "review")      # same as base: flagged, money still moves
        # combined, the freemail mismatch + any other soft signal does hold
        self.assertEqual(E.score_evidence({"name_dest_mismatch": True, "domain_age_days": 3})[0], E.HINT_HOLD)


class LegitNewVendorPasses(unittest.TestCase):
    def test_a_real_new_vendor_scores_none_no_friction(self):
        h, _, _ = E.score_evidence({"domain_age_days": 900, "in_disposable": False, "name_dest_mismatch": False,
                                    "mx_present": True, "registry_found": True})
        self.assertEqual(h, E.HINT_NONE)
        self.assertEqual(E.apply("review", h), "review")        # adds nothing to the per-agent first-payee review
        self.assertEqual(E.apply("allow", h), "allow")

    def test_a_modestly_young_but_otherwise_clean_vendor_is_not_held(self):
        h, _, _ = E.score_evidence({"domain_age_days": 20, "mx_present": True, "registry_found": True})
        self.assertEqual(h, E.HINT_NONE)


class UnavailableIsNotClean(unittest.TestCase):
    def test_absent_checks_contribute_nothing(self):
        self.assertEqual(E.score_evidence({})[0], E.HINT_NONE)
        self.assertEqual(E.score_evidence(None)[0], E.HINT_NONE)

    def test_a_timed_out_signal_is_no_information_not_a_clean_pass_and_not_a_penalty(self):
        # the feed timed out: it must NOT read as "clean" (no relax) and must NOT read as a hit (no penalty)
        ev = {"blocklist_hit": True, "unavailable": ["blocklist_hit"], "domain_age_days": 900}
        h, _, items = E.score_evidence(ev)
        self.assertEqual(h, E.HINT_NONE)                        # the unavailable blocklist does not freeze ...
        self.assertFalse(any(i["signal"] == "blocklist_hit" for i in items))   # ... and is not reported as evidence

    def test_mx_and_registry_penalize_only_on_explicit_false(self):
        self.assertEqual(E.score_evidence({"domain_age_days": 20, "mx_present": None})[0], E.HINT_NONE)
        self.assertEqual(E.score_evidence({"mx_present": False, "registry_found": False})[0], E.HINT_REVIEW)  # +1+1


class ApplyOnlyAddsFriction(unittest.TestCase):
    def test_hard_freeze_overrides_non_freeze_base(self):
        self.assertEqual(E.apply("allow", E.HINT_FREEZE), "freeze")

    def test_evidence_never_weakens_a_per_agent_freeze(self):
        for hint in (E.HINT_HOLD, E.HINT_REVIEW, E.HINT_NONE):
            self.assertEqual(E.apply("freeze", hint), "freeze")

    def test_none_never_relaxes_the_base_tier(self):
        # a clean external footprint is NOT a vouch -- it must not lower review->allow (that's what an attacker buys)
        self.assertEqual(E.apply("review", E.HINT_NONE), "review")
        self.assertEqual(E.apply("hold", E.HINT_NONE), "hold")

    def test_hold_raises_allow_but_a_soft_signal_never_makes_allow_a_freeze(self):
        self.assertEqual(E.apply("allow", E.HINT_HOLD), "hold")
        self.assertEqual(E.apply("review", E.HINT_REVIEW), "review")


if __name__ == "__main__":
    unittest.main()
