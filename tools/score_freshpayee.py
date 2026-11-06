#!/usr/bin/env python3
"""Score option O9 (external evidence) on the fresh-payee benchmark: for each case, compute the Tier-0 offline evidence
with the REAL app/external_evidence.local_checks, merge the Tier-1 evidence a network lookup would return, run
score_evidence + apply over a base tier of 'review' (a fresh payee is a first-ever-payee review in the per-agent gate), and
check the outcome against the label.

  mule   -> must be caught, and we distinguish HOW, honestly:
              HELD  = evidence moved the verdict ABOVE the base review (hold/freeze) -> money STOPPED inline/at the hold
              FLAGGED = evidence fired but only reached review (= the base for a new payee) -> money MOVES, just flagged.
            A mule is only a real inline catch when it is HELD. FLAGGED-only is reported as such (not a money-stopping
            catch) -- this is the correction to the earlier "caught = hint fired" criterion (infra's finding: review ==
            the existing first-payee review, so a review-only hint changes nothing).
  legit  -> must pass clean: hint == none (no ADDED friction over the per-agent review); this is the cold-start bar.
  residue-> the honest floor: a bought-clean-footprint mule O9 cannot catch (hint none); reported, not counted as a miss.

Exit 0 iff every mule is at least FLAGGED, every 'held'-expected mule is HELD, zero legit false-nags, residue uncaught."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import external_evidence as E

BASE = "review"   # a fresh (first-ever) payee is already a review in the per-agent gate; O9 can only ADD friction


def evaluate(c):
    ev = dict(c["tier1"])
    ev.update(E.local_checks(c["name"], c["recipient"]))       # real offline checks
    hint, reason, items = E.score_evidence(ev)
    final = E.apply(BASE, hint)
    return hint, final, items


def main():
    cases = json.loads((ROOT / "tests" / "corpus" / "freshpayee_corpus.json").read_text())
    mule = [c for c in cases if c["label"] == "mule"]
    legit = [c for c in cases if c["label"] == "legit"]
    residue = [c for c in cases if c["label"] == "residue"]

    print("=== CHEAP/LAZY FRESH MULES (HELD = money stopped; FLAGGED = review-only, money moves) ===")
    held_ok = flagged_ok = fail = 0
    for c in mule:
        hint, final, items = evaluate(c)
        money_stopped = final in ("hold", "freeze")        # above the base 'review' -> the payment does NOT execute
        flagged = hint != E.HINT_NONE                      # evidence fired (at least review)
        want = c.get("expect") or "flagged"
        if want == "held":
            ok = money_stopped; held_ok += ok
            status = "HELD" if money_stopped else ("flagged-only" if flagged else "MISS")
        else:  # flagged
            ok = flagged; flagged_ok += ok
            status = "flagged" if flagged else "MISS"
        fail += (not ok)
        bullets = "; ".join(i["detail"] for i in items) or "(no evidence)"
        mark = "OK " if ok else "BAD"
        print(f"  [{mark}] want={want:7s} got {final:7s} ({status:12s}) {c['name']:24s}  {bullets[:60]}")

    print("\n=== CLEAN LEGIT-NEW VENDORS (must pass with ZERO added friction) ===")
    nags = 0
    for c in legit:
        hint, final, items = evaluate(c)
        ok = hint == E.HINT_NONE
        nags += (not ok)
        print(f"  [{'PASS' if ok else 'NAG!':6s}] {c['name']:26s} -> {final:7s}  {c.get('note','')[:50]}")

    print("\n=== HONEST RESIDUE (bought-clean-footprint mule: O9 cannot catch; bounded elsewhere) ===")
    residue_ok = True
    for c in residue:
        hint, final, items = evaluate(c)
        uncaught = hint == E.HINT_NONE
        residue_ok = residue_ok and uncaught
        print(f"  [{'as expected' if uncaught else 'UNEXPECTED CATCH':16s}] {c['name']:26s} -> {final:7s}  (caught only by blast-radius bounding O7/O2)")

    n_held_expected = sum(1 for c in mule if (c.get("expect") or "flagged") == "held")
    n_flag_expected = len(mule) - n_held_expected
    print(f"\nMULES: held {held_ok}/{n_held_expected} (money stopped) | flagged-only {flagged_ok}/{n_flag_expected} (money moves, flagged) | wrong {fail}")
    print(f"LEGIT false-nags: {nags}/{len(legit)}  (target 0) | residue honestly uncaught: {residue_ok}")
    ok = fail == 0 and nags == 0 and residue_ok
    print(f"=== O9 FRESH-PAYEE BENCHMARK: {'PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
