#!/usr/bin/env python3
"""Reconcile the fresh-payee O9 benchmark with the REAL Service.pay gate path (the 371/371-style agreement check the main
benchmark has). The point: prove the pure scorer's verdict for the INLINE (Tier-0) evidence equals what the live gate does
when the same payee is paid through Service.pay -- so "disposable-inbox mule held inline" is true in the SHIPPED gate, not
only in the pure harness.

Honest split, surfaced by this tool:
  * TIER-0 cases (disposable inbox, business-name-to-freemail): evidence is computed INLINE by Service._evidence (no I/O)
    and fed to classify as txn["evidence"]. So the pure scorer and Service.pay MUST agree on these -- and the disposable
    case must end FROZEN (held), the freemail-only case REVIEW-AND-PROCEED (money moves), on BOTH paths.
  * TIER-1 cases (young domain, no MX, scam/sanctions feed): these are NOT available inline in the real service (they need
    a network gather, which runs async only on an already-held/ new-payee payment). So through Service.pay a Tier-1-only
    mule's FIRST payment is NOT held by evidence -- it clears (review as a new payee) and the gather then DECORATES it.
    This tool reports that honestly rather than pretending Tier-1 blocks the first wire.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_service import mk, pay, seed_history
from app import external_evidence as E

# which corpus signals are INLINE (Tier-0, no network) vs TIER-1 (network gather, not inline in /pay)
TIER0_KEYS = {"in_disposable", "name_dest_mismatch"}


def is_tier0_only(c):
    return not (set(c["tier1"]) & {"domain_age_days", "mx_present", "registry_found", "blocklist_hit", "sanctions_exact",
                                   "sanctions_name_similar"})


def service_first_payment(name, recipient, amount="60.00"):
    """Pay a brand-new payee through the REAL Service.pay (Tier-0 evidence computed inline by the service) and return the
    (decision, status, reason)."""
    svc, pp, t = mk(); seed_history(svc, t)
    out = pay(svc, name, amount, email=recipient)
    return out["decision"], out["status"], (out.get("reasons") or [""])[0]


def main():
    import json
    cases = json.loads((ROOT / "tests" / "corpus" / "freshpayee_corpus.json").read_text())
    print("=== fresh-payee corpus through the REAL Service.pay (inline Tier-0 only) ===")
    disagree = 0
    for c in cases:
        decision, status, reason = service_first_payment(c["name"], c["recipient"])
        held = status == "frozen"
        # what the PURE scorer says using ONLY the inline Tier-0 slice (what the service actually has in /pay)
        t0 = E.local_checks(c["name"], c["recipient"])
        pure_hint = E.score_evidence(t0)[0]
        pure_inline_holds = E.apply("review", pure_hint) in ("hold", "freeze")
        # agreement on the INLINE decision: does the service hold iff the pure Tier-0 scorer says hold?
        ok = (held == pure_inline_holds)
        tier = "Tier-0" if is_tier0_only(c) else "Tier-1"
        flag = "" if ok else "  <-- DISAGREE"
        disagree += (not ok)
        print(f"  [{c['label']:7s}|{tier}] {c['name']:24s} service={status:7s}/{decision:6s}  pure-inline-holds={pure_inline_holds}{flag}  {reason[:40]}")
    print(f"\ninline Tier-0 agreement: {len(cases)-disagree}/{len(cases)} (service holds iff pure Tier-0 says hold)")
    print("Note: Tier-1-only mules are NOT held inline by the service (they need an async gather) -- they clear on the")
    print("first payment and the gather decorates the hold/alert. This is the honest v1 behavior, stated in the doc.")
    return 1 if disagree else 0


if __name__ == "__main__":
    sys.exit(main())
