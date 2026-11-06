"""Cross-agent payee reputation: the population layer (the network moat).

Every check in gate.py compares an agent only to ITSELF. This module adds the signal self-comparison cannot see: what the
WHOLE NETWORK knows about a payee. The network scores the PAYEE (how many independent orgs have paid it, did any freeze or
confirm fraud on it, is it brand-new to the network); the per-agent baseline still scores the AMOUNT. Amounts NEVER cross a
customer boundary — this module reads only derived counts/dates, never another agent's transactions.

PURE and store-agnostic by design: these are functions over a ReputationRecord dict (infra's store.py backs it with SQLite,
keyed by HMAC(recipient); the HMAC secret and persistence live there, not here). That keeps the privacy invariant testable
(the shared store holds no amounts/memos/agent-ids) and keeps this detection logic unit-testable with plain dicts.

GOVERNING RULE (how precision stays 100%): a network signal may FREEZE only on independent MULTI-ORG negative evidence
(confirmed fraud, >=2 orgs froze, a trusted vendor's NAME at a NEW destination). It may otherwise only RELAX a per-agent
review toward allow (a network-trusted payee) or RAISE ATTENTION to review (unseen-and-large). Nothing here turns a
legitimate-but-new vendor into a NEW freeze — that is the one thing the design forbids, and the benchmark asserts it.
"""

# --- trust (positive) requires DIVERSITY + AGE + VOLUME. distinct_orgs, not agents: sock-puppets under one org count once. ---
TRUST_MIN_ORGS = 5            # at least this many INDEPENDENT customer orgs have paid it ...
TRUST_MIN_AGE_S = 30 * 86400  # ... and it has been on the network at least this long ...
TRUST_MIN_CLEAN = 25          # ... and this many clean executed payments. MUST be a bucket boundary of reputation_store.BUCKETS
                             # (0,1,2,5,10,25,50,100): clean_exec_bucket is COARSENED, so a non-boundary like 20 reports as
                             # bucket 10 and silently raises the real threshold to the next boundary. 25 = the "broad volume"
                             # boundary. One org can NEVER make a payee trusted (diversity is enforced by TRUST_MIN_ORGS).

# --- negative evidence FREEZES, and is deliberately cheaper to earn than trust (asymmetry is the point) ---
FROZE_ORGS_FREEZE = 2         # this many DISTINCT ORGS froze it -> freeze (one org's freeze is not enough; could be a mistake)
MULE_ORGS_FREEZE = 2          # this many orgs saw a received-then-fanned-out mule pattern -> freeze when also new/untrusted here

# --- attention (review, never a new freeze) for the unseen-and-large case ---
UNSEEN_LARGE_MULT = 1.5       # unseen across the ENTIRE network AND > this x the agent's typical -> review (not allow, not freeze)

# --- ring-buildup: many BRAND-NEW orgs converging on one new payee fast = a poisoning attempt, flag the payee ---
RING_NEW_ORGS = 3            # this many orgs, all themselves younger than RING_ORG_AGE_S, first-paid it inside RING_WINDOW_S
RING_WINDOW_S = 7 * 86400
RING_ORG_AGE_S = 30 * 86400

# verdict hints this module returns; the caller maps them into the gate's allow/review/freeze with the ordering in apply()
HINT_FREEZE = "freeze"
HINT_REVIEW = "review"
HINT_LEAN_ALLOW = "lean_allow"   # downgrade a per-agent REVIEW to allow (network trust); never downgrades a freeze
HINT_NONE = "none"


def _f(record, key, default=0):
    v = record.get(key, default)
    return v if v is not None else default


def trusted_by_network(record, now):
    """True only when a payee is broad (>=TRUST_MIN_ORGS independent orgs), aged (>=TRUST_MIN_AGE_S), and clean
    (>=TRUST_MIN_CLEAN executed, zero live negative evidence). Diversity is measured in ORGS so a pile of sock-puppet
    agents under one org cannot manufacture trust. Any negative evidence disqualifies trust outright."""
    if record is None:
        return False
    if _f(record, "confirmed_fraud", False) or _f(record, "distinct_orgs_froze") > 0:
        return False                                   # any live negative evidence => never "trusted", however broad
    first = _f(record, "first_seen_network_ts", now)
    return (_f(record, "distinct_orgs") >= TRUST_MIN_ORGS
            and (now - first) >= TRUST_MIN_AGE_S
            and _f(record, "clean_exec_bucket") >= TRUST_MIN_CLEAN)


def ring_buildup(record, now):
    """A poisoning attempt in progress: several BRAND-NEW orgs first-paying one payee inside a short window. The caller
    supplies this as a precomputed signal when the store can derive it; here we read the summarized counters the store
    keeps so the pattern is corroborated across orgs, not guessed from one."""
    if record is None:
        return False
    return (_f(record, "new_orgs_in_window") >= RING_NEW_ORGS
            and _f(record, "distinct_orgs") <= RING_NEW_ORGS + 1          # still small/new overall (not a mature vendor)
            and not trusted_by_network(record, now))


def network_impersonation(name_skeleton, recipient_key, known_high_rep):
    """A NEW recipient_key whose NAME skeleton collides with a DIFFERENT high-reputation payee = someone imitating a
    trusted vendor's name while routing to a new destination (beneficiary swap at population scale).
    known_high_rep: {name_skeleton: recipient_key} for network-trusted payees. Returns the imitated key or None."""
    if not name_skeleton:
        return None
    other = known_high_rep.get(name_skeleton)
    if other is not None and other != recipient_key:
        return other
    return None


def reputation_checks(record, *, now, new_payee, amount, agent_typical, name_skeleton=None,
                      recipient_key=None, known_high_rep=None):
    """Pure network verdict for ONE payee the agent is about to pay. Returns (hint, reason).

    record: the pooled ReputationRecord for this payee (or None if the network has never seen it).
    new_payee: is this payee new to THIS agent (from the per-agent History).
    amount, agent_typical: this agent's amount and its own typical (for the unseen-and-large attention check only — the
        number is the agent's own, never pooled).
    name_skeleton/recipient_key/known_high_rep: for the cross-network impersonation check.

    Order matters: hard negative evidence first (can freeze), then impersonation (can freeze), then ring (freeze the
    poisoning attempt), then positive trust (lean-allow), then unseen-and-large attention (review). Never allow->freeze
    on novelty alone."""
    known_high_rep = known_high_rep or {}

    # 1) hard negative: operator-confirmed fraud anywhere on the network
    if record is not None and _f(record, "confirmed_fraud", False):
        return HINT_FREEZE, "payee was confirmed fraudulent by an operator elsewhere on the network (confirmed-fraud guard)"

    # 2) independent multi-org freezes: real vendors do not accumulate these
    if record is not None and _f(record, "distinct_orgs_froze") >= FROZE_ORGS_FREEZE:
        return HINT_FREEZE, (f"payee was frozen by {_f(record, 'distinct_orgs_froze')} independent organizations on the "
                             f"network (network-known-mule guard)")

    # 3) multi-org mule pattern, when also new/untrusted for this agent
    if record is not None and _f(record, "mule_fanout_hits") >= MULE_ORGS_FREEZE and (new_payee or not trusted_by_network(record, now)):
        return HINT_FREEZE, (f"payee shows a received-then-fanned-out mule pattern across {_f(record, 'mule_fanout_hits')} "
                             f"organizations (network mule guard)")

    # 4) cross-network impersonation: a NEW destination wearing a trusted vendor's name
    if new_payee:
        imitated = network_impersonation(name_skeleton, recipient_key, known_high_rep)
        if imitated is not None:
            return HINT_FREEZE, ("payee's name imitates a network-trusted vendor but routes to a new, unrecognized "
                                 "destination (network beneficiary-swap guard)")

    # 5) ring-buildup: the poisoning attempt itself (many fresh orgs converging on a new payee) -> freeze the payee
    if ring_buildup(record, now):
        return HINT_FREEZE, (f"payee is being paid by a burst of brand-new organizations at once "
                             f"({_f(record, 'new_orgs_in_window')} fresh orgs); possible coordinated reputation-building "
                             f"(ring-buildup guard)")

    # 6) POSITIVE: network-trusted -> lean-allow (downgrade a per-agent review to allow; reduces friction, cuts false-nags)
    if trusted_by_network(record, now):
        return HINT_LEAN_ALLOW, (f"payee is trusted across the network ({_f(record, 'distinct_orgs')} independent orgs, "
                                 f"clean, aged): reduced friction")

    # 7) ATTENTION (review, never a new freeze): unseen across the network AND new to this agent AND large for it.
    # Kept gated on new_payee ON PURPOSE. The tempting generalization -- fire for a payee the AGENT knows but the NETWORK
    # has never seen -- is NOT safe at the per-payee level: an EMPTY/young network makes EVERY payee "unseen", so without a
    # global network-population signal (which the store does not expose today) that generalization would nag every large
    # payment on day one and break the "reputation attached with no data changes nothing" invariant. For a new payee this
    # overlaps the per-agent first-payee review (apply() leaves it a review either way), so it costs no precision; it earns
    # its keep for a new payee the agent's own ceiling would have ALLOWED (new payee under 2x typical) that is also unseen
    # network-wide and large. Promoting it to the known-but-network-unseen case is a real future enhancement once the store
    # exposes network size -- noted, not faked.
    unseen = record is None or not _f(record, "seen", False)
    if unseen and new_payee and agent_typical and amount > UNSEEN_LARGE_MULT * agent_typical:
        return HINT_REVIEW, (f"payee has never been paid by anyone on the network and this is a large amount for this "
                             f"agent ({amount:g} vs typical {agent_typical:.0f}): unseen-and-large")

    return HINT_NONE, ""


def apply(base_tier, hint):
    """Combine the per-agent gate's tier with the network hint, preserving precision:
      - a network FREEZE overrides any base tier (it is independent multi-org evidence),
      - a network REVIEW raises allow->review (attention) but never touches a freeze,
      - a LEAN_ALLOW lowers a review->allow ONLY (network trust), never a freeze,
      - NONE leaves the base tier untouched.
    This is the only place network signal changes a decision, and it can only add a freeze on strong negative evidence or
    relax a review on strong positive evidence — never turn a legit-but-new vendor's allow into a freeze."""
    rank = {"allow": 0, "review": 1, "freeze": 2}
    if hint == HINT_FREEZE:
        return "freeze"
    if base_tier == "freeze":
        return "freeze"                                  # per-agent freeze stands; network never releases
    if hint == HINT_REVIEW:
        return "review" if rank[base_tier] < 1 else base_tier
    if hint == HINT_LEAN_ALLOW:
        return "allow" if base_tier == "review" else base_tier
    return base_tier
