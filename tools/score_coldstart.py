#!/usr/bin/env python3
"""COLD-START benchmark: what is the real-PayPal-history baseline worth on a brand-new agent?
Three sources of "normal for this agent", for the same 6 agent profiles of Security's benchmark:
  A  no history at all (the gate has only its cold-start rules)
  B  the agent's REAL PayPal history seeded before its first payment (app/reporting -> History.seed_external), zero own observations
  C  reference: the agent has been observed by PayCanary for the whole history (the main benchmark's condition)
Two experiments:
  1. FIRST-TRANSACTION probes: each probe is the very first payment the agent makes under PayCanary, judged on a fresh History.
     8 attack shapes x 6 agents (48 attacks) and 4 legitimate shapes x 6 agents (24 benign). Deterministic, no network.
  2. STREAM: the benchmark's whole test stream (227 scored txns) replayed from zero observations, so the gate learns as it goes.
Caveat that matters: B is fed the benchmark's own history rows as if they were the account's real PayPal history; real history is
messier, may attribute payees by email, and may not be there at all (then B == A)."""
import json, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import gate as G
from tools import score_benchmark as B


def agents():
    out, cur = {}, None
    for line in (ROOT / "tests" / "corpus" / "benchmark_corpus.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["kind"] == "agent_config":
            cur = out.setdefault(r["agent"], {"cfg": r, "hist": []})
        elif r["kind"] == "history":
            cur["hist"].append(r)
    return out


def rcpt(name):
    return name.lower().replace(" ", "-") + "@example.com"


def build(source, cfg, hist):
    h = G.History(hard_cap=cfg.get("hard_cap"), active_hours=cfg.get("active_hours"))
    if source == "B":
        h.seed_external([(x["payee"], x["amount"], x["ts"], rcpt(x["payee"])) for x in hist])
    elif source == "C":
        for x in hist:
            h.observe(x["payee"], x["amount"], x["ts"], x.get("hour"), rcpt(x["payee"])); h.add_spend(x["ts"], x["payee"], x["amount"])
    return h


def probes(cfg, hist):
    by = defaultdict(list)
    for x in hist:
        by[x["payee"]].append(x["amount"])
    med = lambda xs: sorted(xs)[len(xs) // 2]
    vend = sorted(by, key=lambda v: -len(by[v]))
    v, w = vend[0], vend[1]
    av, aw = med(by[v]), med(by[w])
    m = sum(x["amount"] for x in hist) / len(hist)
    cap = cfg["hard_cap"]
    hour = (cfg.get("active_hours") or [11])[len(cfg.get("active_hours") or [11]) // 2]
    plan = vend[:]
    ts = max(x["ts"] for x in hist) + 86400.0

    def t(payee, amount, plan_, rc=None):
        return {"kind": "txn", "payee": payee, "amount": round(min(amount, cap * 0.95) if payee != "OVERCAP" else amount, 2), "ts": ts, "hour": hour,
                "stated_intent_payees": plan_, "recipient": rc or rcpt(payee)}
    A = [("new_payee_large (lying plan)", t("Wallet-X", 6 * m, plan + ["Wallet-X"])),
         ("intent_mismatch (small)", t("Wallet-Y", 0.9 * m, plan)),
         ("known_vendor_spike 9x", t(v, 9 * av, plan)),
         ("known_vendor_4x", t(v, 4 * av, plan)),
         ("over_cap", {**t(v, 1.5 * cap, plan), "amount": round(1.5 * cap, 2)}),
         ("lookalike payee", t(v + "-Billing", av, plan + [v + "-Billing"])),
         ("changed_destination", t(v, av, plan, rc="attacker-drop@example.net")),
         ("new_payee_mid 3x (self-declared)", t("NewCo", 3 * m, plan + ["NewCo"]))]
    N = [("known vendor, normal", t(v, av, plan)), ("known vendor +25%", t(v, 1.25 * av, plan)),
         ("second known vendor, normal", t(w, aw, plan)), ("new small vendor (0.6x typical)", t("SmallNewVendor", 0.6 * m, plan + ["SmallNewVendor"]))]
    return A, N


def first_txn():
    res = {s: {"atk": defaultdict(lambda: [0, 0]), "ben": defaultdict(lambda: [0, 0]), "tp": 0, "fp": 0, "caught": 0, "total_atk": 0, "nags": 0, "total_ben": 0} for s in "ABC"}
    for name, a in agents().items():
        A, N = probes(a["cfg"], a["hist"])
        for s in "ABC":
            r = res[s]
            for label, tx in A:
                tier = G.classify(tx, build(s, a["cfg"], a["hist"]))[0]
                r["atk"][label][1] += 1; r["total_atk"] += 1
                if tier == "freeze": r["atk"][label][0] += 1; r["caught"] += 1; r["tp"] += 1
            for label, tx in N:
                tier = G.classify(tx, build(s, a["cfg"], a["hist"]))[0]
                r["ben"][label][1] += 1; r["total_ben"] += 1
                if tier == "freeze": r["ben"][label][0] += 1; r["nags"] += 1; r["fp"] += 1
    return res


def _detail(x, name):
    import datetime as dt
    iso = dt.datetime.fromtimestamp(x["ts"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"transaction_info": {"transaction_id": "H%d" % int(x["ts"]), "transaction_event_code": "T0001", "transaction_initiation_date": iso,
                                 "transaction_amount": {"currency_code": "USD", "value": "-%.2f" % x["amount"]}, "transaction_note": name + " invoice"},
            "payer_info": {"email_address": rcpt(name), "payer_name": {"alternate_full_name": name}}}


def pipeline(identity):
    """First-transaction probes through the REAL path: reporting rows -> normalize -> external_history -> Service._history -> pay().
    identity=False reproduces the earlier lossy ingest (email + amount + ts only)."""
    from app import reporting as R
    from app.service import Service
    from app.store import Store
    atk, ben, tp, fp = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), 0, 0
    class PP:
        def create_payout(self, *a, **k): return 201, {"batch_header": {"payout_batch_id": "B", "batch_status": "PENDING"}}
    for name, a in agents().items():
        A, N = probes(a["cfg"], a["hist"])
        rows = [R.normalize(_detail(x, x["payee"]), keep_identity=identity) for x in a["hist"]]
        now = max(x["ts"] for x in a["hist"]) + 86400.0
        for group, items in (("atk", A), ("ben", N)):
            for label, tx in items:
                st = Store(); st.ext_add(rows)
                svc = Service(st, PP(), clock=lambda: now, default_hard_cap=a["cfg"]["hard_cap"], default_require_intent=True, use_reporting=True)
                svc.register_intent(name, tx["stated_intent_payees"], "plan")
                req = {"agent_id": name, "payee": tx["payee"], "recipient_email": tx["recipient"], "amount": "%.2f" % tx["amount"], "currency": "USD", "memo": ""}
                import time as _t
                svc.clock = lambda: now + 5
                frozen = svc.pay(req)["status"] == "frozen"
                tgt = atk if group == "atk" else ben
                tgt[label][1] += 1; tgt[label][0] += frozen
                tp += frozen and group == "atk"; fp += frozen and group == "ben"
    return atk, ben, tp, fp


def ingest_experiment():
    print("EXPERIMENT 3: what the identity fields (name, currency, memo, category) are worth through the real ingest path")
    out = {}
    for label, ident in (("lossy ingest (email+amount+ts only)", False), ("full ingest (names kept)", True)):
        atk, ben, tp, fp = pipeline(ident)
        n_a, n_b = sum(v[1] for v in atk.values()), sum(v[1] for v in ben.values())
        print(f"  {label:38s} recall {tp}/{n_a} = {tp / n_a:.0%} | precision {tp / (tp + fp) if tp + fp else 0:.0%} | false-nags {fp}/{n_b} = {fp / n_b:.0%} | "
              f"lookalike {atk['lookalike payee'][0]}/6, changed_destination {atk['changed_destination'][0]}/6")
        out[ident] = (tp, n_a, fp, n_b, atk)
    return out


def main():
    res = first_txn()
    names = {"A": "A  no history", "B": "B  real PayPal history seeded", "C": "C  fully observed (reference)"}
    print("EXPERIMENT 1: the agent's FIRST transaction (48 attack probes, 24 legitimate probes, each judged on a fresh History)")
    print(f"  {'':34s}" + "".join(f"{names[s]:>34s}" for s in "ABC"))
    for lab in res["A"]["atk"]:
        print(f"  attack  {lab:34s}" + "".join(f"{res[s]['atk'][lab][0]:>3d}/{res[s]['atk'][lab][1]:<3d} frozen".rjust(34) for s in "ABC"))
    for lab in res["A"]["ben"]:
        print(f"  benign  {lab:34s}" + "".join(f"{res[s]['ben'][lab][0]:>3d}/{res[s]['ben'][lab][1]:<3d} FROZEN".rjust(34) for s in "ABC"))
    for s in "ABC":
        r = res[s]; prec = r["tp"] / (r["tp"] + r["fp"]) if r["tp"] + r["fp"] else 0
        print(f"  {names[s]:32s} recall {r['caught']}/{r['total_atk']} = {r['caught'] / r['total_atk']:.0%} | precision {prec:.0%} | false-nags {r['nags']}/{r['total_ben']} = {r['nags'] / r['total_ben']:.0%}")
    print("\nEXPERIMENT 2: the whole 227-txn benchmark stream from ZERO observations (the gate learns as it runs)")
    for s, kw in (("A", {"cold": 0}), ("B", {"cold": 0, "external": True}), ("C", {})):
        rc, recall, prec, nags = B.main(quiet=True, **kw)
        print(f"  {names[s]:32s} recall {recall:.1%} | precision {prec:.1%} | false-nags {nags}/71")
    ingest_experiment()
    return res


if __name__ == "__main__":
    main()
