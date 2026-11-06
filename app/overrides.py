"""First-party-fraud visibility (threat axis (b)): a read-only view of operator OVERRIDES of PayCanary flags.

PayCanary is an observer and a goalie, not a court. When the customer is the bad actor, we cannot police them against
themselves -- but we can make their overrides an IMMUTABLE, ATTRIBUTABLE record: "principal X released $A to Y DESPITE
PayCanary flagging it for <reason>." That record is the complicity shield (we flagged it, un-rewritably) and establishes
their liability (they chose to override, on the record, attributed). This module only READS the hash-chained audit log and
the payment rows; it changes nothing and decides nothing.

An override = a human RELEASE of a payment PayCanary had FROZEN (the flag was raised and a human waved it through). Each
override carries: who (the authenticated principal), how much, to whom, and the FLAG THEY OVERRODE (the payment's gate
reason). A per-principal count surfaces the pattern ("principal X overrode N flags") so a user -- or an insider (axis e) --
who routinely waves through their own payee is visible.
"""
from collections import defaultdict


def override_records(store, audit):
    """Every operator release of a frozen payment, with the flag that was overridden. Read-only.
    Returns [{payment_id, by, amount, payee, overridden_flag, ts}], oldest first."""
    out = []
    for row in audit.rows(event="released", limit=100000):
        pid = row.get("payment_id")
        p = store.payment(pid) if pid else None
        detail = row.get("detail") or {}
        # prefer the flag captured in the immutable audit detail; fall back to the payment row's reason
        flag = detail.get("overridden_reason") if isinstance(detail, dict) else None
        if not flag and p:
            flag = p.get("reason")
        out.append({"payment_id": pid, "by": row.get("actor"), "amount": (detail.get("amount") if isinstance(detail, dict) else None) or (p and p.get("amount")),
                    "payee": (detail.get("payee") if isinstance(detail, dict) else None) or (p and p.get("payee")),
                    "overridden_flag": flag, "ts": row.get("ts")})
    return out


def override_summary(store, audit):
    """Pooled view for the dashboard / a judge: how many flags were overridden, by whom, and the biggest ones.
    'user overrode N flags' is the headline signal. Read-only, decides nothing."""
    recs = override_records(store, audit)
    by_principal = defaultdict(lambda: {"count": 0, "total_amount": 0.0})
    for r in recs:
        b = by_principal[r["by"]]
        b["count"] += 1
        b["total_amount"] += float(r["amount"] or 0.0)
    # the largest overrides (by amount) are the ones a reviewer/auditor cares about most
    top = sorted(recs, key=lambda r: float(r["amount"] or 0.0), reverse=True)[:10]
    return {
        "total_overrides": len(recs),
        "by_principal": {k: v for k, v in sorted(by_principal.items(), key=lambda kv: kv[1]["count"], reverse=True)},
        "largest_overrides": [{"by": r["by"], "amount": r["amount"], "payee": r["payee"],
                               "overrode_flag": (r["overridden_flag"] or "")[:120]} for r in top],
        # the honest one-liner this layer exists to let the product SAY and SHOW:
        "positioning": "PayCanary is an observer and a goalie, not a court: every override is recorded immutably with the "
                       "flag it overrode and the principal who made it. We record; authorities and auditors decide.",
    }
