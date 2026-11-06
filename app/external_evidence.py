"""External-evidence scoring for new/unknown payees (option O9, the day-one discriminator).

When neither the per-agent history nor the cross-agent network has seen a payee, the AGENT can still gather evidence from
the OUTSIDE WORLD before a payment clears: a real new business has an aged domain, working mail, a registry entry, and is
in no scam feed; a fresh mule often has a days-old domain, a throwaway inbox, is paid to a freemail address while claiming a
business name, or already sits in a scam/sanctions feed. That is the signal newness-to-us lacks, and it DISCRIMINATES
legit-new from mule-new at transaction #1 without nagging the honest vendor (who passes these checks).

This module is PURE and store/network-agnostic by design, exactly like app/reputation.py:
  * the OFFLINE/LOCAL checks (disposable-email, name-vs-destination / freemail mismatch) need no network and no secrets,
  * `score_evidence(evidence)` turns an evidence dict (assembled by the service's I/O callers -- infra) into a risk hint.
Keeping it pure keeps the gate doing no I/O (the service gathers evidence BEFORE classify and passes it in), keeps the
privacy story clean (local checks send nothing out), and keeps the discrimination logic unit-testable with plain dicts.

EVIDENCE, NOT PROOF (how precision is protected): a single SOFT signal (young domain, no MX, disposable inbox, name/dest
mismatch) raises ATTENTION -> review/hold, never a freeze on its own -- a legitimately young real business must not be
blocked. Only a HARD signal (a scam/phishing feed hit on the domain, or an EXACT-identifier sanctions hit) is strong
enough to freeze alone. Soft signals ACCUMULATE: enough together is a hold, because a real new vendor rarely trips several
at once.

Infra constraints folded in (their I/O review):
  * FREEZE here means "held for a human, flagged red" -- PayCanary never auto-denies; so "hard" = stays held with the
    evidence shown, not an automatic block.
  * A sanctions SANCTIONS hit is only HARD on an EXACT identifier match; a fuzzy NAME-similarity hit is soft (high false-
    positive rate), surfaced as review-with-evidence.
  * The evidence dict carries `checked` (signals that actually ran) and `unavailable` (failed/timed out). A signal that is
    absent, or listed in `unavailable`, is NO INFORMATION -- never a clean pass, never penalized. Enrichment can only ADD
    suspicion; it NEVER relaxes a tier (no lean-allow from a "clean footprint"), because a clean external footprint is
    exactly what a well-funded attacker buys.
  * Output is structured: (hint, reason, items) where items = [{signal, detail, severity}], so the approval surface can
    render the bullets and the payment row can store them (the informed freeze-and-gather-then-ask ask).

A NOTE ON WHAT THIS CAN STOP ON DAY ONE (honest sequencing): the Tier-0 OFFLINE checks (disposable, name/freemail) are
available INLINE before the decision, so they can raise a fresh payee to a hold before money moves. The Tier-1 NETWORK
checks (domain age, feeds) run async on an ALREADY-HELD payment (they are not in the /pay critical path), so they INFORM a
hold rather than create one -- unless the product adopts a "hold-pending-enrichment" async pay flow for fresh risky payees.
So O9's day-one BLOCKING power comes from Tier-0 inline + whatever triggers the hold (risky shape / O3); Tier-1 decorates
the hold with deeper evidence for the human. Stated plainly so no one over-claims that a network feed stops the first wire.
"""

# soft-signal weights (accumulate; enough together -> review/hold, never alone a freeze)
YOUNG_DOMAIN_DAYS = 30        # a domain younger than this is "new" ...
VERY_YOUNG_DOMAIN_DAYS = 7    # ... and younger than this is "very new" (weightier)
SOFT_REVIEW_SCORE = 2         # accumulated soft weight at/above this -> review
SOFT_HOLD_SCORE = 3           # ... at/above this -> hold (a risky-shaped, evidence-backed first payment)

# a minimal built-in freemail / consumer-inbox set. Infra can supply a larger list via config; this is the floor so the
# local check works with no dependency. A payment to one of these while the payee NAME looks like a business is the classic
# impersonation tell ("Microsoft" paid to a gmail address).
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "zoho.com",
    "yandex.com", "pm.me", "hey.com",
}

# a small built-in disposable/throwaway-domain seed. Infra can swap in a full list (160k+ domains exist as offline data);
# this seed keeps the pure check working and testable with no network. Absence from the seed is NOT "not disposable".
DISPOSABLE_SEED = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org", "throwawaymail.com",
    "yopmail.com", "trashmail.com", "getnada.com", "dispostable.com", "maildrop.cc", "fakeinbox.com", "sharklasers.com",
    "guerrillamailblock.com", "mohmal.com", "mintemail.com", "spamgourmet.com", "tempinbox.com",
}


def _domain_of(recipient):
    r = str(recipient or "").strip().lower()
    return r.rpartition("@")[2] if "@" in r else r


def is_disposable(recipient, extra=None):
    """Local, no network: is the recipient's domain a known throwaway. `extra` = an optional larger set from infra/config."""
    d = _domain_of(recipient)
    return bool(d) and (d in DISPOSABLE_SEED or (extra is not None and d in extra))


def _looks_like_business(name):
    """A payee NAME that reads like an organization (not a person). Requires a CORPORATE TOKEN -- NOT merely multi-word,
    because most people have two-word names and flagging "Jane Smith -> gmail" as a mismatch would be a false-nag (the
    cold-start disease). Used only to decide whether 'paid to a freemail address' is suspicious: a business with a
    corporate suffix paid at a consumer inbox is the impersonation tell; an unsuffixed name paid at gmail is ambiguous and
    deliberately NOT flagged on name alone (its risk, if any, comes from other signals)."""
    n = str(name or "").lower()
    corp = ("inc", "llc", "ltd", "corp", "gmbh", "sa", "ag", "plc", "labs", "technologies", "systems",
            "solutions", "software", "payments", "group", "studios", "agency", "incorporated", "limited", "corporation")
    toks = [t for t in "".join(c if c.isalnum() else " " for c in n).split() if t]
    return any(t in corp for t in toks)


def name_destination_mismatch(name, recipient, freemail=None):
    """Local, no network: a business-looking payee NAME paid to a FREEMAIL/consumer inbox -> the classic impersonation
    tell. Returns True only when the name looks like a business AND the destination is a consumer inbox. Paying a person,
    or a business at its own domain, is not a mismatch."""
    fm = freemail if freemail is not None else FREEMAIL
    d = _domain_of(recipient)
    return bool(d) and d in fm and _looks_like_business(name)


def local_checks(name, recipient, *, disposable_extra=None, freemail=None):
    """Run the no-network checks and return the slice of an evidence dict they can fill with no I/O and no privacy leak."""
    return {
        "in_disposable": is_disposable(recipient, disposable_extra),
        "name_dest_mismatch": name_destination_mismatch(name, recipient, freemail),
    }


# evidence keys the scorer understands (all optional; a key absent -- or listed in `unavailable` -- is NO INFORMATION,
# never a clean pass and never penalized):
#   HARD (can freeze alone):  blocklist_hit (+blocklist_source), sanctions_exact
#   SOFT (accumulate -> review/hold): domain_age_days (int|None), in_disposable, name_dest_mismatch, mx_present (bool),
#                                     registry_found (bool), sanctions_name_similar (bool; fuzzy, high FP -> soft only)
HINT_FREEZE, HINT_REVIEW, HINT_HOLD, HINT_NONE = "freeze", "review", "hold", "none"


def _item(signal, detail, severity):
    return {"signal": signal, "detail": detail, "severity": severity}


def score_evidence(evidence):
    """Pure: turn a (possibly partial) evidence dict into (hint, reason, items). Evidence-not-proof.
      * HARD -> freeze (held-for-human, flagged red): a scam/phishing blocklist hit on the domain, or an EXACT-identifier
        sanctions hit. A FUZZY sanctions name-match is NOT hard (high false-positive rate) -- it is a soft review item.
      * SOFT -> accumulate: >= SOFT_HOLD_SCORE -> hold, >= SOFT_REVIEW_SCORE -> review, else none.
    `items` is the structured evidence for the approval surface (each {signal, detail, severity}); `reason` is a one-line
    summary. A signal that is absent or in `unavailable` contributes NOTHING -- enrichment can only ADD suspicion, never
    relax a tier and never read a failed/missing check as a clean pass."""
    if evidence is None:
        return HINT_NONE, "", []
    unavailable = set(evidence.get("unavailable") or ())
    def have(k):                                    # a signal counts only if it ran (not absent, not timed out)
        return k in evidence and k not in unavailable and evidence.get(k) is not None
    items, soft = [], 0

    # ---- HARD signals (stay held, flagged red) ----
    if have("sanctions_exact") and evidence["sanctions_exact"]:
        items.append(_item("sanctions_exact", "destination exactly matches a sanctions-list identifier", "hard"))
        return HINT_FREEZE, "payee destination matches a sanctions list (exact)", items
    if have("blocklist_hit") and evidence["blocklist_hit"]:
        src = evidence.get("blocklist_source") or "a scam/phishing feed"
        items.append(_item("blocklist_hit", f"domain appears in {src}", "hard"))
        return HINT_FREEZE, f"payee domain appears in {src}", items

    # ---- SOFT signals: accumulate; none alone is a freeze ----
    if have("domain_age_days"):
        age = evidence["domain_age_days"]
        if isinstance(age, (int, float)):
            if age < VERY_YOUNG_DOMAIN_DAYS:
                soft += 2; items.append(_item("domain_age_days", f"domain registered only {age:g} days ago", "soft"))
            elif age < YOUNG_DOMAIN_DAYS:
                soft += 1; items.append(_item("domain_age_days", f"domain registered {age:g} days ago", "soft"))
    if have("in_disposable") and evidence["in_disposable"]:
        # weight 3 = HOLD on its own. A payment to a throwaway/disposable inbox is a near-certain mule signal: a real
        # vendor is essentially NEVER paid at mailinator.com et al. This is the one Tier-0 OFFLINE signal strong enough to
        # hold a fresh payee's first payment INLINE (no network) -- without it, Tier-0 alone only reaches review, which a
        # new payee already is, so the inline catch would be a no-op (infra's finding). False-nag risk is ~zero because
        # disposable domains exist precisely to be throwaway.
        soft += 3; items.append(_item("in_disposable", "paid to a disposable/throwaway email domain", "soft"))
    if have("name_dest_mismatch") and evidence["name_dest_mismatch"]:
        # weight 2 = review alone, holds when combined. Kept lower than disposable on purpose: a tiny real sole-proprietor
        # CAN use gmail, so a business-suffixed-name-to-freemail alone is review (accumulates to a hold with any other
        # signal), not an on-its-own hold -- protecting the legit-new vendor who happens to use a consumer inbox.
        soft += 2; items.append(_item("name_dest_mismatch", "business-looking payee paid to a personal/freemail address", "soft"))
    if have("sanctions_name_similar") and evidence["sanctions_name_similar"]:
        soft += 1; items.append(_item("sanctions_name_similar", "payee NAME is similar to a sanctions entry (fuzzy, verify)", "soft"))
    if have("mx_present") and evidence["mx_present"] is False:    # explicit False only; absent/unavailable = unknown
        soft += 1; items.append(_item("mx_present", "destination domain has no working mail (no MX)", "soft"))
    if have("registry_found") and evidence["registry_found"] is False:
        soft += 1; items.append(_item("registry_found", "no business-registry entry found", "soft"))

    if soft >= SOFT_HOLD_SCORE:
        return HINT_HOLD, "multiple external risk signals on a new payee", items
    if soft >= SOFT_REVIEW_SCORE:
        return HINT_REVIEW, "external risk signal on a new payee", items
    # no suspicion raised. NOTE: this is "no adverse evidence found", NOT a vouch -- it never relaxes the base tier.
    return HINT_NONE, "", items


def apply(base_tier, hint):
    """Combine the per-agent/network base tier with the external-evidence hint, preserving precision:
      * a HARD-evidence FREEZE overrides a non-freeze base (a feed hit is strong enough),
      * a HOLD raises allow/review -> hold (held-with-evidence for an informed ask), never weakens a freeze,
      * a REVIEW raises allow -> review, never touches a freeze or an existing hold,
      * NONE leaves the base tier untouched (a clean external footprint adds no friction -- the legit-new vendor passes).
    External evidence, like the network layer, only ADDS friction on risk or leaves a clean payee alone; it never turns a
    per-agent freeze into something weaker and never, on a soft signal alone, turns a clean allow into a freeze."""
    rank = {"allow": 0, "review": 1, "hold": 2, "freeze": 3}
    if hint == HINT_FREEZE:
        return "freeze"
    if base_tier == "freeze":
        return "freeze"
    if hint == HINT_HOLD:
        return "hold" if rank.get(base_tier, 0) < rank["hold"] else base_tier
    if hint == HINT_REVIEW:
        return "review" if rank.get(base_tier, 0) < rank["review"] else base_tier
    return base_tier


# ---- fan-out upgrade (CEO/Marty): the deterministic counter TRIGGERS; the evidence on the payee-SET DECIDES. ----
# The blunt counter can't tell a business onboarding 5 real vendors from an attacker fanning out to 5 mules -- they are
# identical to a counter. So when the fan-out counter trips, don't blind-freeze: judge the SET of brand-new payees in the
# window by the same external evidence (O9). Real-business set -> review (legit onboarding passes); mule-shaped set ->
# freeze; evidence unavailable/unknown -> HOLD (fail-safe, never silent-allow a fan-out). This ends the re-tuning loop:
# the dollar/count constant stops being the decision, the evidence does.
FANOUT_MULE_FELT = 1          # this many mule-shaped new payees in the set -> freeze the fan-out (one confirmed mule is enough)


def fanout_verdict(payee_evidence):
    """Decide a tripped fan-out from the evidence on the SET of brand-new payees in the window.
    payee_evidence: {payee: evidence_dict_or_None} for each brand-new, not-yet-trusted payee counted by the trigger.
    Returns (hint, reason, items):
      * any payee is MULE-shaped (score_evidence -> freeze/hold, i.e. a feed/sanctions hit or multiple soft signals) -> FREEZE
      * every payee looks like a REAL BUSINESS (score_evidence -> none, with at least some signal actually checked) -> REVIEW
        (legit onboarding: allowed-with-evidence, flagged for the operator, NOT frozen -- kills the dumb-counter false-nag)
      * evidence is UNAVAILABLE/UNKNOWN for the set (nothing checked, or None records) -> HOLD (fail-safe: a fan-out we
        cannot vet is held for a human, never silent-allowed).
    Pure; the service assembles payee_evidence (Tier-0 local + any cached Tier-1) before calling. No I/O here."""
    mules, clean, unknown, items = [], [], [], []
    for payee, ev in (payee_evidence or {}).items():
        hint = score_evidence(ev)[0] if ev is not None else HINT_NONE
        checked = bool(ev) and bool(set(ev.get("checked") or ()) - set(ev.get("unavailable") or ()))
        if hint in (HINT_FREEZE, HINT_HOLD):
            mules.append(payee); items.append(_item("fanout_mule", "new payee '%s' looks like a mule (%s)" % (payee, hint), "hard"))
        elif ev is not None and checked:
            clean.append(payee)
        else:
            unknown.append(payee)            # no evidence actually ran for this payee -> unknown, not clean
    if len(mules) >= FANOUT_MULE_FELT:
        return HINT_FREEZE, ("fan-out to %d new payee(s) that look like mules: %s" % (len(mules), ", ".join(mules[:5])) +
                             " (payee-set evidence, not a blind count)"), items
    if unknown:
        return HINT_HOLD, ("fan-out to %d new payee(s) we could not vet (unknown is not clean) -- held for review" % len(unknown)), items
    if clean:
        return HINT_REVIEW, ("fan-out to %d new payee(s) that look like real businesses -- onboarding, flagged not blocked" % len(clean)), items
    return HINT_HOLD, "fan-out with no payee evidence available -- held for review (fail-safe)", items
