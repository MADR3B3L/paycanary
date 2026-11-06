#!/usr/bin/env python3
"""Fresh-payee external-evidence benchmark (option O9): cheap/lazy fresh MULES that must be caught (held/review) vs clean
legit-new VENDORS that must pass with zero added friction (the cold-start acceptance bar). Each case carries the payee
name, the recipient, and the Tier-1 evidence a network lookup WOULD return (domain_age_days, mx_present, registry_found,
blocklist/sanctions) so the benchmark scores the full evidence picture; the Tier-0 offline part (disposable, name/freemail)
is computed by the real app/external_evidence.local_checks at score time. Labels: 'mule' must end review/hold/freeze;
'legit' must end none (no added friction over the per-agent base).

Deliberately includes the HARD cases: a legit vendor with a genuinely YOUNG domain (must still pass — young alone is not
enough), and a mule with a CLEAN footprint (must pass O9 — the honest residue; it is caught only by blast-radius bounding,
not by external evidence). Those two keep the benchmark honest rather than flattering."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
cases = []


def case(label, name, recipient, tier1=None, note="", expect=None):
    # expect (mules only): "held" = evidence must move the verdict above review (money stopped); "flagged" = review only
    # (money moves, flagged) because the single signal present (e.g. freemail-mismatch alone) is not strong enough to hold.
    cases.append({"label": label, "name": name, "recipient": recipient, "tier1": tier1 or {}, "note": note, "expect": expect})


# ===== CHEAP / LAZY FRESH MULES =====
# throwaway inbox -> HELD inline (weight 3, no legit vendor is paid at a disposable domain). Tier-0, no network.
case("mule", "Acme Payments LLC", "payout@mailinator.com", expect="held", note="disposable inbox -> HELD inline (Tier-0)")
case("mule", "Global Tech Ltd", "x@guerrillamail.com", expect="held", note="disposable inbox -> HELD inline (Tier-0)")
# business name -> freemail: FLAGGED only (review). A sole-prop CAN use gmail, so alone it does not hold; money moves,
# flagged. Honest: this is NOT a money-stopping inline catch on its own (it holds only when another signal is present).
case("mule", "Microsoft Corporation", "ms-billing@gmail.com", expect="flagged", note="business->freemail: FLAGGED only (review)")
case("mule", "Stripe Payments Inc", "stripe.payouts@yahoo.com", expect="flagged", note="business->freemail: FLAGGED only (review)")
# very young domain + another soft signal (Tier-1, async during a hold) -> HELD
case("mule", "Nimbus Cloud LLC", "ap@nimbus-cloud-pay.example", tier1={"domain_age_days": 2, "mx_present": False},
     expect="held", note="2-day domain + no MX -> HELD (Tier-1 decorates)")
case("mule", "Vertex Labs", "pay@vertex-labs-invoice.example", tier1={"domain_age_days": 4, "registry_found": False},
     expect="held", note="4-day domain + no registry -> HELD")
# in a scam/phishing feed (HARD -> freeze/held-red)
case("mule", "Quick Refund Co", "refunds@refund-portal.example", tier1={"blocklist_hit": True, "blocklist_source": "Phishing Army"},
     expect="held", note="in a phishing feed (HARD -> freeze)")
# exact sanctions identifier (HARD) -- note: not wired to a live source in v1, kept as a contract/logic case
case("mule", "Shell Entity", "ops@sanctioned.example", tier1={"sanctions_exact": True}, expect="held", note="exact sanctions (HARD, logic)")
# disposable + young domain: the full lazy stack -> HELD
case("mule", "Payables Group LLC", "payables@10minutemail.com", tier1={"domain_age_days": 1},
     expect="held", note="disposable + 1-day domain -> HELD")

# ===== CLEAN LEGIT-NEW VENDORS (must pass: none / no added friction) =====
case("legit", "Acme Robotics", "ap@acmerobotics.com", tier1={"domain_age_days": 1400, "mx_present": True, "registry_found": True})
case("legit", "Blue Harbor Design", "invoices@blueharbor.design", tier1={"domain_age_days": 900, "mx_present": True, "registry_found": True})
case("legit", "Jane Smith", "jane.smith@gmail.com", tier1={"domain_age_days": 9000, "mx_present": True},
     note="a person paid at gmail: NOT a mismatch")
case("legit", "Northwind Traders", "billing@northwind.co.uk", tier1={"domain_age_days": 3200, "mx_present": True, "registry_found": True})
case("legit", "Contoso", "accounts@contoso.io", tier1={"domain_age_days": 600, "mx_present": True, "registry_found": True})
# HONEST HARD CASE 1: a legit vendor with a genuinely YOUNG domain must STILL pass (young alone is not enough to flag)
case("legit", "Fresh Start Bakery", "pay@freshstartbakery.com", tier1={"domain_age_days": 20, "mx_present": True, "registry_found": True},
     note="legit but 20-day domain: young ALONE must not flag")
# HONEST HARD CASE 2: a mule with a CLEAN bought footprint passes O9 -> labeled 'residue' (not a false-nag; the honest floor)
case("residue", "Legit-Looking Front LLC", "ap@aged-front.example", tier1={"domain_age_days": 400, "mx_present": True, "registry_found": True},
     note="attacker who BOUGHT a clean footprint: O9 cannot catch; bounded by O7/O2, not by evidence")

(HERE / "freshpayee_corpus.json").write_text(json.dumps(cases, indent=0))
from collections import Counter
by = Counter(c["label"] for c in cases)
print(f"fresh-payee cases: {len(cases)}  mule: {by['mule']}  legit: {by['legit']}  residue(honest floor): {by['residue']}")
