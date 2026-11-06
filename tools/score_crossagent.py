#!/usr/bin/env python3
"""Score the cross-agent reputation layer on its benchmark cases: for each case take the per-agent BASE tier (what the
normal gate alone returns) and apply app/reputation. Report recall per attack class (frozen/total) and the benign outcome
(no benign case frozen; onboarding ALLOWED, proving the network reduces friction). This measures the NETWORK lift on top of
a per-agent decision the gate alone would make. Exit 0 iff every attack frozen, no benign frozen, onboarding all allowed."""
import json, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import reputation as R

NOW = 2_000_000.0


def final_tier(c):
    hint, reason = R.reputation_checks(c["network"], now=NOW, new_payee=c["new_payee"], amount=c["amount"],
                                       agent_typical=c["agent_typical"], name_skeleton=c.get("name_skeleton"),
                                       recipient_key=c.get("recipient_key"), known_high_rep=c.get("known_high_rep"))
    return R.apply(c["base_tier"], hint), hint, reason


def main():
    cases = json.loads((ROOT / "tests" / "corpus" / "crossagent_corpus.json").read_text())
    atk = defaultdict(list); ben = defaultdict(list)
    for c in cases:
        (ben if c["label"].endswith("BENIGN") else atk)[c["label"]].append(c)

    # Honest per-class success criterion: most network attacks must FREEZE (block the money). fresh_scam_wallet is the
    # one class the design deliberately ESCALATES-but-does-not-block (unseen+large is suspicious, not proof; freezing it
    # would risk a false-block on a legit large new vendor). For it, "caught" = not allowed (review or freeze), and the
    # output says so plainly so review is never misread as a block.
    ESCALATE_OK = {"fresh_scam_wallet"}
    print("=== CROSS-AGENT ATTACK RECALL (base tier -> network -> final) ===")
    print("    (freeze = money blocked; fresh_scam_wallet is escalate-to-review by design = flagged, not blocked)")
    caught = total = 0
    for cls in sorted(atk):
        items = atk[cls]; total += len(items)
        want_freeze = cls not in ESCALATE_OK
        f = 0
        for c in items:
            ft, hint, reason = final_tier(c)
            f += (ft == "freeze") if want_freeze else (ft in ("freeze", "review"))
        caught += f
        ex = final_tier(items[0])
        goal = "FREEZE" if want_freeze else "review+"
        print(f"  {cls:28s} {f:3d}/{len(items)}  [{goal}]  base={items[0]['base_tier']} -> {ex[0]}   e.g. {ex[2][:60]}")

    print("\n=== BENIGN (must NOT freeze; onboarding must be ALLOWED = friction reduced) ===")
    nags = 0; onboarding_allowed = onboarding_total = 0
    for cls in sorted(ben):
        items = ben[cls]
        froz = sum(final_tier(c)[0] == "freeze" for c in items)
        nags += froz
        allowed = sum(final_tier(c)[0] == "allow" for c in items)
        note = ""
        if cls.startswith("trusted_vendor_onboarding"):
            onboarding_allowed, onboarding_total = allowed, len(items)
            note = f"  [{allowed}/{len(items)} turned review->ALLOW by the network]"
        print(f"  {cls:34s} frozen {froz}/{len(items)}  allowed {allowed}/{len(items)}{note}")

    recall = caught / total if total else 0.0
    print(f"\nNETWORK-LAYER attack recall: {caught}/{total} = {recall:.1%}")
    print(f"benign false-nags: {nags}  (target 0)")
    print(f"trusted-onboarding friction reduced (review->allow): {onboarding_allowed}/{onboarding_total}")
    ok = caught == total and nags == 0 and onboarding_allowed == onboarding_total
    print(f"\n=== CROSS-AGENT LAYER: {'PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
