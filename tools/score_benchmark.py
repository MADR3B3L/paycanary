#!/usr/bin/env python3
"""Score app/gate.py (the real shipped gate) on Security's real-world benchmark (tests/corpus/benchmark_*): 6 agent profiles,
227 scored txns = 71 benign + 156 attack in 10 classes. Same history semantics as the service: only non-frozen txns teach the
baseline and count as spend; every attempt counts toward velocity. NOTE: the benchmark doc's published numbers were measured on
the earlier prototype gate (payment_gate.py), not on this one.  Exit 0 iff overall recall >= 0.70, precision == 1.0, false-nags == 0. (Recall counts every labelled txn, so the unavoidable first copy of a replay and the first
few payments of a burst can never be frozen: replay is capped at 50%, bursts at ~(n-5)/n. Per-class lines show it.)"""
import json, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import gate as G


def run(cold=None, external=False):
    """cold=N: each agent starts with only its LAST N history rows as its own observations (a cold start). external=True: the earlier
    rows are supplied instead as real PayPal history (seed_external: teaches the baseline, is not counted as this agent's spend)."""
    hist, res, pend = None, {}, []
    for line in (ROOT / "tests" / "corpus" / "benchmark_corpus.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        k = r["kind"]
        if k == "agent_config":
            hist = G.History(hard_cap=r.get("hard_cap"), active_hours=r.get("active_hours")); pend = []
        elif k == "history":
            pend.append(r)
        else:
            if pend is not None:
                old, own = ((pend[:len(pend) - cold], pend[len(pend) - cold:]) if cold is not None else ([], pend))
                if external:
                    hist.seed_external([(x["payee"], x["amount"], x["ts"], x["payee"].lower() + "@example.com") for x in old])
                for x in own:
                    hist.observe(x["payee"], x["amount"], x["ts"], x.get("hour")); hist.add_spend(x["ts"], x["payee"], x["amount"])
                pend = None
            hist.note_attempt(r["ts"], r["payee"])
            tier, kind, reason = G.classify(r, hist)
            res[r["tid"]] = (tier, reason)
            if tier != "freeze":
                hist.observe(r["payee"], r["amount"], r["ts"], r.get("hour")); hist.add_spend(r["ts"], r["payee"], r["amount"])
    return res


def main(quiet=False, cold=None, external=False):
    res = run(cold, external)
    labels = json.loads((ROOT / "tests" / "corpus" / "benchmark_labels.json").read_text())
    benign = [x for x in labels if x["label"] == "benign"]
    attacks = [x for x in labels if x["label"] not in ("benign", "unscored")]
    by = defaultdict(list)
    for x in attacks:
        by[x["label"]].append(x)
    caught = 0
    out = []
    for c in sorted(by):
        f = sum(res[x["tid"]][0] == "freeze" for x in by[c]); caught += f
        out.append(f"  {c:28s} {f:3d}/{len(by[c])}   e.g. {res[by[c][0]['tid']][1][:80]}")
    nags = [x for x in benign if res[x["tid"]][0] == "freeze"]
    rev = sum(res[x["tid"]][0] == "review" for x in benign)
    scored_frozen = [t for t, (tier, _) in res.items() if tier == "freeze" and next(x["label"] for x in labels if x["tid"] == t) != "unscored"]
    tp = sum(1 for t in scored_frozen if next(x["label"] for x in labels if x["tid"] == t) != "benign")
    recall, prec = caught / len(attacks), (tp / len(scored_frozen) if scored_frozen else 0.0)
    if not quiet:
        print("per-class recall (frozen / total):"); print("\n".join(out))
        print(f"OVERALL recall {caught}/{len(attacks)} = {recall:.1%} | precision {tp}/{len(scored_frozen)} = {prec:.1%} | false-nags {len(nags)}/{len(benign)} | benign reviewed {rev}/{len(benign)}")
        for x in nags[:6]:
            print("  FALSE NAG:", x["agent"], res[x["tid"]][1][:110])
    return (0 if recall >= 0.70 and prec == 1.0 and not nags else 1), recall, prec, len(nags)


if __name__ == "__main__":
    if "--cold" in sys.argv:
        print("BASELINE SOURCE EXPERIMENT (history truncated to the last 3 own observations per agent):")
        for label, kw in (("full own history (reference)", {}), ("cold start, no external history", {"cold": 3}), ("cold start + real PayPal history seeded", {"cold": 3, "external": True})):
            rc, recall, prec, nags = main(quiet=True, **kw); print(f"  {label:44s} recall {recall:.1%} | precision {prec:.1%} | false-nags {nags}")
        sys.exit(0)
    sys.exit(main()[0])
