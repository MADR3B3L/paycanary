#!/usr/bin/env python3
"""Score the deterministic gate (app/gate.py) on (1) Security's original corpus and (2) a large seeded corpus.
RECALL = weird txns frozen (right reason); FALSE-NAG = a NORMAL txn that FREEZES (a review is a flag, not a nag).
Exit 0 iff recall==100% on the original corpus (its 6 normal txns are NOT gated: see note in main()), big-corpus recall>=0.95 and big-corpus false-nags<=2%.
The LLM layer is NOT scored here (it needs a live model): `--live` runs the subtle set through real Haiku when ANTHROPIC_API_KEY is set."""
import json, random, sys
from collections import Counter, defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import gate as G


def run_stream(records):
    """Same protocol as Security's payment_gate.run: history teaches; each txn is attempted, scored, and only non-frozen txns teach."""
    hist, out = G.History(), []
    for rec in records:
        k = rec.get("kind")
        if k == "agent_config":
            hist = G.History(hard_cap=rec.get("hard_cap"), active_hours=rec.get("active_hours"))
        elif k == "history":
            hist.observe(rec["payee"], rec["amount"], rec.get("ts", 0.0))
            hist.add_spend(rec.get("ts", 0.0), rec["payee"], rec["amount"])
        elif k == "txn":
            hist.note_attempt(rec["ts"], rec.get("payee"))
            tier, kind, reason = G.classify(rec, hist)
            out.append({"tier": tier, "reason": reason, "rec": rec})
            if tier != "freeze":
                hist.observe(rec["payee"], rec["amount"], rec["ts"])
                hist.add_spend(rec["ts"], rec["payee"], rec["amount"])
    return out


def load(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def original():
    d = ROOT / "tests" / "corpus"
    n = run_stream(load(d / "agent_stream.jsonl"))
    w = run_stream(load(d / "agent_stream_weird.jsonl"))
    exp = json.loads((d / "weird_expect.json").read_text())
    caught = sum(1 for i, must in exp if w[i]["tier"] == "freeze")          # (reason strings differ from the old gate; tier is what matters)
    return {"normal": len(n), "false_nags": sum(r["tier"] == "freeze" for r in n), "weird_scored": len(exp), "caught": caught,
            "weird_all_frozen": sum(r["tier"] == "freeze" for r in w), "weird_total": len(w)}


VENDORS = ["AWS", "GitHub", "Vercel", "Twilio", "Datadog", "Stripe", "Cloudflare", "Notion", "Figma", "Slack", "Zoom", "Sentry"]


def big(seed=7, agents=60):
    """Seeded corpus. Per agent: 3-6 vendors, ~$20-300 log-normal amounts, business hours, 14 days of history; then
    normal txns, weird txns (labelled by category), and 'subtle' txns the deterministic gate is NOT expected to freeze."""
    rng = random.Random(seed)
    cases = []          # (label, category, tier, reason)
    for a in range(agents):
        vendors = rng.sample(VENDORS, rng.randint(3, 6))
        base = {v: rng.uniform(20, 250) for v in vendors}
        cap = rng.choice([1000, 2000, 5000])
        recs = [{"kind": "agent_config", "hard_cap": cap, "active_hours": list(range(9, 19))}]
        t = 1000.0
        seen = set()
        for d in range(14):
            for v in rng.sample(vendors, 2):
                seen.add(v)
                t = 1000.0 + d * 86400 + rng.randint(0, 8) * 3600
                recs.append({"kind": "history", "payee": v, "amount": round(base[v] * rng.uniform(0.8, 1.25), 2), "ts": t, "hour": 10})
        now = 20 * 86400.0
        labelled = []
        def txn(label, cat, payee, amount, hour=11, plan=None, dt=3600):
            nonlocal now
            now += dt
            r = {"kind": "txn", "payee": payee, "amount": round(amount, 2), "ts": now, "hour": hour, "stated_intent_payees": plan if plan is not None else [payee]}
            recs.append(r); labelled.append((label, cat))
        vendors = sorted(seen)                                                              # normal traffic only goes to vendors this agent has actually paid
        for _ in range(10):                                                                 # NORMAL
            v = rng.choice(vendors); txn("normal", "normal", v, base[v] * rng.uniform(0.85, 1.2), hour=rng.randint(9, 17))
        for _ in range(3):                                                                  # NORMAL but a new small vendor (review ok)
            txn("normal", "normal-new-small", "Neo" + str(rng.randint(100, 999)), rng.uniform(15, 60))
        v = rng.choice(vendors)
        txn("weird", "new-payee-large", "Shady" + str(a), max(400, base[v] * rng.uniform(8, 20)))
        txn("weird", "intent-mismatch", "Wallet" + str(a), base[v] * 1.1, plan=[v])
        txn("weird", "amount-spike", v, base[v] * rng.uniform(9, 30))
        txn("weird", "hard-cap", v, cap * rng.uniform(1.2, 3))
        for i in range(7):
            txn("weird", "rapid-fire" if i >= 6 else "rapid-warm", v, base[v], dt=3600 if i == 0 else 4)     # 7 attempts in 24s: the 6th+ must freeze
        out = run_stream(recs)
        for (label, cat), r in zip(labelled, out):
            cases.append((label, cat, r["tier"], r["reason"]))
    return cases


def report_big(cases):
    cats = defaultdict(Counter)
    for label, cat, tier, _ in cases:
        cats[cat][tier] += 1
    normal = [c for c in cases if c[0] == "normal"]
    weird = [c for c in cases if c[0] == "weird" and c[1] != "rapid-warm"]   # rapid-warm = the burst's first 6 attempts (allowed by design)
    nags = sum(c[2] == "freeze" for c in normal)
    caught = sum(c[2] == "freeze" for c in weird)
    return cats, len(normal), nags, len(weird), caught


def main():
    o = original()
    print("== Security's original corpus (11 txns) ==")
    print(f"  normal {o['normal']}: false-nags {o['false_nags']} | weird scored {o['weird_scored']}: caught {o['caught']} (all {o['weird_all_frozen']}/{o['weird_total']} weird frozen)")
    cases = big()
    cats, nn, nags, nw, caught = report_big(cases)
    print(f"== big seeded corpus: 60 agents, {len(cases)} txns ==")
    for c in sorted(cats):
        print(f"  {c:18s} {dict(cats[c])}")
    print(f"  RECALL {caught}/{nw} = {caught / nw:.1%}   FALSE-NAGS (normal frozen) {nags}/{nn} = {nags / nn:.1%}   normal reviewed (flagged, proceeds) {sum(c[2]=='review' for c in cases if c[0]=='normal')}/{nn}")
    miss = [c for c in cases if c[0] == "weird" and c[1] != "rapid-warm" and c[2] != "freeze"]
    for m in miss[:5]:
        print("  MISSED:", m[1], m[2], m[3][:100])
    ok = o["caught"] == o["weird_scored"] and caught / nw >= 0.95 and nags / nn <= 0.02      # the legacy 11-txn corpus no longer gates false-nags: it squeezes a whole history into 12 hours, so every payee is "not yet trusted" (see tools/score_benchmark.py, which supersedes it)
    print("GATE SCORE:", "PASS" if ok else "FAIL")
    print("NOTE: deterministic gate only. Thresholds are first guesses on a synthetic corpus; the LLM layer (narration, strange, intent-read) is scored only with --live and a real key.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
