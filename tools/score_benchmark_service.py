#!/usr/bin/env python3
"""Score Security's benchmark through the REAL service path (Service.pay, the code behind POST /pay), not by calling the gate directly,
and compare tier-by-tier with tools/score_benchmark.py. Purpose: prove the direct harness exercises the same state the live gate uses
(spend windows, velocity attempts, cadence, destinations, caps) so no check silently no-ops and the recall number is not false-low.
How: history rows become executed payments in a fresh store; every test txn is submitted to Service.pay with the clock set to the txn's
timestamp and the agent's plan registered just before it. The corpus's synthetic `hour` is injected by shimming the service's UTC-hour
lookup (the service derives the hour from the clock; the corpus carries it as a separate field). Recipient = <payee>@example.com."""
import json, sys, types
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import service as SV
from app.service import Service
from app.store import Store
from tools import score_benchmark as B


class PP:
    def create_payout(self, *a, **k):
        return 201, {"batch_header": {"payout_batch_id": "B", "batch_status": "PENDING"}}


def run(reputation=None, make_provider=None, recipient=None, stats=None):
    """reputation: optional ONE shared app.reputation_store.Reputation for the whole run (each agent still gets its own fresh Store).
    make_provider: optional zero-arg factory returning a Provider (default: the PayPal-shaped fake above). recipient: optional payee -> email mapper (a provider's own
    namespaced test identities). stats: optional dict that receives per-run provider counts (decided / sent / frozen / provider-side calls)."""
    recipient = recipient or (lambda payee: payee.lower() + "@example.com")
    used = []
    res, hour_now = {}, [0]
    real_time = SV.time
    SV.time = types.SimpleNamespace(time=real_time.time, gmtime=lambda ts=None: types.SimpleNamespace(tm_hour=hour_now[0]))   # corpus carries its own hour
    try:
        clock, svc, cfg, st = [0.0], None, None, None
        for line in (ROOT / "tests" / "corpus" / "benchmark_corpus.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r["kind"] == "agent_config":
                st = Store(); clock = [0.0]
                prov = make_provider() if make_provider else PP()
                used.append((st, prov))
                svc = Service(st, prov, clock=lambda: clock[0], default_require_intent=True, default_hard_cap=r["hard_cap"])
                svc.reputation = reputation
                svc.set_agent(r["agent"], hard_cap=r["hard_cap"], active_hours=r.get("active_hours"), require_intent=True, org_id=r.get("org"))
                agent = r["agent"]
            elif r["kind"] == "history":
                st.insert_payment({"payment_id": "h%d" % len(st.payments(agent, None, 100000)), "agent_id": agent, "ts": r["ts"], "payee": r["payee"],
                                   "recipient": recipient(r["payee"]), "amount": r["amount"], "currency": "USD", "memo": "", "hour": r["hour"],
                                   "intent_id": None, "verdict": "allow", "score_kind": "none", "reason": "history", "status": "sent", "paypal_batch_id": "H",
                                   "paypal_status": "SUCCESS", "decided_by": "history", "event_json": "{}", "narration": None, "risk": 0, "llm_flag": None,
                                   "mode": "enforce", "enforced": 1})
            else:
                clock[0], hour_now[0] = r["ts"] - 1, r["hour"]
                svc.register_intent(agent, r["stated_intent_payees"] or ["-"], "plan")
                clock[0], hour_now[0] = r["ts"], r["hour"]
                out = svc.pay({"agent_id": agent, "payee": r["payee"], "recipient_email": recipient(r["payee"]),
                               "amount": "%.2f" % r["amount"], "currency": "USD", "memo": ""})
                res[r["tid"]] = (out["decision"], out.get("reasons", [out.get("reason", "")])[0])
    finally:
        SV.time = real_time
    if stats is not None:
        decided = [p for st, _ in used for p in st.payments(None, None, 100000) if p.get("provider")]            # rows the gate created (history rows carry no provider)
        stats.update(decided=len(decided), frozen=sum(p["status"] == "frozen" for p in decided), sent=sum(p["status"] == "sent" for p in decided),
                     errors=sum(p["status"] in ("paypal_error", "provider_error") for p in decided), refs=[p["provider_ref"] for p in decided if p.get("provider_ref")],
                     error_codes=__import__("collections").Counter(__import__("json").loads(r["detail"]).get("error") or "http_%s" % __import__("json").loads(r["detail"]).get("http") for st, _ in used for r in st.db.execute("SELECT detail FROM audit WHERE event='provider_result'").fetchall() if __import__("json").loads(r["detail"]).get("status") in ("paypal_error", "provider_error")),
                     provider_calls=sum(getattr(prov, "n", None) if hasattr(prov, "n") else len(getattr(getattr(prov, "client", None), "calls", [])) for _, prov in used))
    return res


def main():
    direct, svc = B.run(), run()
    labels = json.loads((ROOT / "tests" / "corpus" / "benchmark_labels.json").read_text())
    lab = {x["tid"]: x["label"] for x in labels}
    diff = [(t, direct[t][0], svc[t][0]) for t in direct if direct[t][0] != svc[t][0]]
    benign = [t for t in svc if lab[t] == "benign"]; attacks = [t for t in svc if lab[t] not in ("benign", "unscored")]
    caught = sum(svc[t][0] == "freeze" for t in attacks); nags = sum(svc[t][0] == "freeze" for t in benign)
    frozen = [t for t in svc if svc[t][0] == "freeze" and lab[t] != "unscored"]; tp = sum(lab[t] != "benign" for t in frozen)
    print(f"SERVICE PATH (Service.pay): recall {caught}/{len(attacks)} = {caught / len(attacks):.1%} | precision {tp}/{len(frozen)} = {tp / len(frozen):.1%} | false-nags {nags}/{len(benign)}")
    print(f"agreement with the direct-gate harness: {len(direct) - len(diff)}/{len(direct)} txns identical tier" + ("" if not diff else f"; first differences: {diff[:6]}"))
    return len(diff)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
