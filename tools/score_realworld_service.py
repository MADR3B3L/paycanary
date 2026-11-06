#!/usr/bin/env python3
"""Score the shipped gate on the REAL-WORLD-grounded corpus through the real Service.pay path (not the gate directly), and
report recall/precision/false-nags PER ATTACK CLASS, honestly. Compare to the synthetic 73.7%.

History rows are inserted as executed payments; each test txn is submitted to Service.pay with the clock set to its ts and
a live plan registered just before it. Multi-currency is preserved (the gate has per-currency baselines). The
invoice_beneficiary_swap case carries its own recipient_email (a known payee at a NEW destination); every other txn's
recipient defaults to <payee>@vendor.example so a known payee keeps a stable destination.

Honest per-class scoring: a class is 'caught' when the payment does NOT execute (status frozen). For velocity_drain and
fan_out and structuring the FIRST few of a run can clear by construction (the guard needs the cumulative/velocity to build),
so per-class recall below 100% there is expected and reported with the count that cleared — not hidden."""
import json, sys, types
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import service as SV
from app.service import Service
from app.store import Store
from tests.test_service import FakePayPal


def _recipient_for(payee):
    """Map a corpus payee to a recipient email whose DOMAIN the deterministic enricher can classify: a real-business
    payee gets a clean domain, a mule-shaped one gets a throwaway/young domain. Keeps the fan-out contextual path honest."""
    slug = payee.lower().replace(" ", "")
    MULE = ("mule", "fanout", "drain", "slow-trainer", "burst", "quick-settle", "smurf")
    if any(m in slug for m in MULE):
        # HONEST mix: a savvy attacker does NOT always use a throwaway inbox. Alternate mule footprints so recall is not
        # inflated by assuming every mule is disposable: half @mailinator (caught by Tier-0 offline), half a clean-looking
        # (but young) domain the offline tier does NOT flag -- those must be caught by the BEHAVIORAL layers (velocity,
        # structuring, fan-out counter, ratchet), not by evidence. Keyed on a stable hash so it's deterministic.
        return slug + ("@mailinator.com" if (sum(map(ord, slug)) % 2 == 0) else "@freshcorp-%s.example" % (sum(map(ord, slug)) % 97))
    # everything legit (known vendors AND real-business new payees) shares the clean domain used in HISTORY, so a known
    # payee's test payment is NOT mis-seen as a changed-destination; the enricher resolves vendor.example as aged + MX.
    return slug + "@vendor.example"


def _enricher(store, clock):
    """A deterministic Enrichers with a fake transport: clean-biz domains resolve aged + MX + no feed hit; mailinator-type
    resolve young + no MX (and disposable is caught offline by Tier-0). Lets the benchmark exercise the CONTEXTUAL fan-out
    with enrichment ON (the state the legit-bulk-onboarding pass requires)."""
    from app import enrichers as EN
    def transport(url, headers, timeout):
        if "data.iana.org" in url:
            return 200, b'{"services": [[["com", "net", "example"], ["https://rdap.x.test/"]]]}'
        if "rdap" in url:
            import datetime as dt
            young = ("mailinator" in url) or ("freshcorp" in url)     # freshcorp mules have young domains too (realistic)
            reg = dt.datetime.fromtimestamp(clock() - (3 if young else 3000) * 86400, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return 200, ('{"events":[{"eventAction":"registration","eventDate":"%s"}]}' % reg).encode()
        if "cloudflare-dns" in url:
            mx = ("mailinator" not in url)                           # a freshcorp mule can have MX (only the disposable one lacks it)
            return 200, (b'{"Status":0,"Answer":[{"type":15,"data":"10 mx"}]}' if mx else b'{"Status":0,"Answer":[]}')
        if "phishing" in url.lower():
            return 200, b""                          # no feed hits in this benchmark (mules are caught by domain-age + disposable)
        return 404, b""
    return EN.Enrichers(store, transport=transport, clock=clock)


def run(enrich=False):
    res, hour_now = {}, [0]
    real_time = SV.time
    SV.time = types.SimpleNamespace(time=real_time.time, gmtime=lambda ts=None: types.SimpleNamespace(tm_hour=hour_now[0]))
    try:
        clock, svc, agent = [0.0], None, None
        for line in (ROOT / "tests" / "corpus" / "realworld_corpus.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            k = r["kind"]
            if k == "agent_config":
                st = Store(); clock = [0.0]
                svc = Service(st, FakePayPal(), clock=lambda: clock[0], default_require_intent=False,
                              default_hard_cap=r["hard_cap"])
                if enrich:
                    svc.sync_enrich = True
                    svc.enrichers = _enricher(st, lambda: clock[0])
                svc.set_agent(r["agent"], hard_cap=r["hard_cap"], active_hours=r.get("active_hours"), org_id=r.get("org_id"))
                agent = r["agent"]
            elif k == "history":
                st.insert_payment({"payment_id": "h%d" % len([1 for _ in st.payments(agent, None, 100000)]),
                                   "agent_id": agent, "ts": r["ts"], "payee": r["payee"], "recipient": r["payee"].lower().replace(" ", "") + "@vendor.example",
                                   "amount": r["amount"], "currency": r.get("currency", "USD"), "memo": "", "hour": r["hour"], "intent_id": None,
                                   "verdict": "allow", "score_kind": "none", "reason": "h", "status": "sent", "paypal_batch_id": "H",
                                   "paypal_status": "SUCCESS", "decided_by": "history", "event_json": "{}", "narration": None, "risk": 0,
                                   "llm_flag": None, "mode": "enforce", "enforced": 1})
            else:  # txn
                clock[0], hour_now[0] = r["ts"] - 1, r["hour"]
                svc.register_intent(agent, r.get("stated_intent_payees") or ["-"], "plan")
                clock[0], hour_now[0] = r["ts"], r["hour"]
                recipient = r.get("recipient_email") or _recipient_for(r["payee"])
                out = svc.pay({"agent_id": agent, "payee": r["payee"], "recipient_email": recipient,
                               "amount": "%.2f" % r["amount"], "currency": r.get("currency", "USD"), "memo": ""})
                res[r["tid"]] = (out["decision"], out.get("status"), (out.get("reasons") or [out.get("reason", "")])[0])
    finally:
        SV.time = real_time
    return res


def main():
    res = run(enrich=True)
    labels = json.loads((ROOT / "tests" / "corpus" / "realworld_labels.json").read_text())
    benign = [x for x in labels if x["label"] == "benign"]
    attacks = [x for x in labels if x["label"] != "benign"]
    by = defaultdict(list)
    for x in attacks:
        by[x["label"]].append(x)

    def frozen(tid):
        return res[tid][1] == "frozen"

    print("=" * 74)
    print("PAYCANARY REAL-WORLD BENCHMARK (shipped gate, through Service.pay)")
    print("=" * 74)
    print(f"\n{len(labels)} txns | benign {len(benign)} | attack {len(attacks)} | 6 realistic agent personas, RFM-shaped\n")
    print("--- PER-ATTACK-CLASS (frozen = money stopped) ---")
    caught = 0
    for cls in sorted(by):
        items = by[cls]; f = sum(frozen(x["tid"]) for x in items); caught += f
        ex = next((res[x["tid"]][2] for x in items if frozen(x["tid"])), res[items[0]["tid"]][2])
        bar = "FULL" if f == len(items) else ("PART" if f else "MISS")
        print(f"  [{bar}] {cls:26s} {f:3d}/{len(items):<3d}  e.g. {ex[:58]}")

    nags = [x for x in benign if frozen(x["tid"])]
    reviewed = sum(res[x["tid"]][0] == "review" for x in benign)
    all_frozen = [t for t, v in res.items() if v[1] == "frozen"]
    lab = {x["tid"]: x["label"] for x in labels}
    tp = sum(1 for t in all_frozen if lab[t] != "benign")
    precision = tp / len(all_frozen) if all_frozen else 0.0
    recall = caught / len(attacks) if attacks else 0.0

    print("\n--- HEADLINE (real-world corpus) ---")
    print(f"  recall (frozen):   {caught}/{len(attacks)} = {recall*100:.1f}%")
    print(f"  precision:         {tp}/{len(all_frozen)} = {precision*100:.1f}%")
    print(f"  false-nags:        {len(nags)}/{len(benign)} = {len(nags)/len(benign)*100:.1f}%  [target 0]")
    print(f"  benign reviewed (allowed, flagged): {reviewed}/{len(benign)}")
    print(f"\n  SYNTHETIC corpus for comparison was: recall 73.7%, precision 100%, false-nags 0")
    if nags:
        print("\n  FALSE NAGS (each a product-killer):")
        for x in nags[:12]:
            print(f"    [{x['agent']}] {res[x['tid']][2][:80]}")
    return 0 if (precision == 1.0 and not nags) else 1


if __name__ == "__main__":
    sys.exit(main())
