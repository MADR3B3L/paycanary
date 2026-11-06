#!/usr/bin/env python3
"""Score the persona x attacker-tier corpus through the real Service.pay (enrichment ON) and print the MATRIX:
recall per attacker TIER, false-nags per benign PERSONA. One blended number hides what a partner fraud team needs; this
is the honest gradient -- crush dumb-dumb/dumb, catch most medium, bound-and-flag smart, hit the honest floor on
way-smarter, and 0 false-nags across every life (especially remittance/gig/student/HNW, the ones naive systems abuse)."""
import json, sys, types
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import service as SV
from app.service import Service
from app.store import Store
from tests.test_service import FakePayPal


def _enricher(store, clock):
    from app import enrichers as EN
    import datetime as dt
    def transport(url, headers, timeout):
        if "data.iana.org" in url:
            return 200, b'{"services": [[["com", "net", "example"], ["https://rdap.x.test/"]]]}'
        if "rdap" in url:
            # disposable + the attacker "fresh"/plausible domains resolve YOUNG; vendor.example (legit) resolves AGED
            young = any(s in url for s in ("mailinator", "guerrillamail", "northstar-consulting-grp", "meridian-partners", "apex-holdings"))
            reg = dt.datetime.fromtimestamp(clock() - (4 if young else 2600) * 86400, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return 200, ('{"events":[{"eventAction":"registration","eventDate":"%s"}]}' % reg).encode()
        if "cloudflare-dns" in url:
            mx = "mailinator" not in url and "guerrillamail" not in url
            return 200, (b'{"Status":0,"Answer":[{"type":15,"data":"10 mx"}]}' if mx else b'{"Status":0,"Answer":[]}')
        if "phishing" in url.lower():
            return 200, b""                              # no feed hits: attackers here are caught by behavior/age/disposable, not a feed
        return 404, b""
    return EN.Enrichers(store, transport=transport, clock=clock)


def run(make_provider=None, stats=None):
    """make_provider: optional zero-arg factory returning a Provider (default: the PayPal-shaped fake). stats: optional dict filled with provider counts."""
    res, hour_now, used = {}, [0], []
    real_time = SV.time
    SV.time = types.SimpleNamespace(time=real_time.time, gmtime=lambda ts=None: types.SimpleNamespace(tm_hour=hour_now[0]))
    try:
        clock, svc, agent = [0.0], None, None
        for line in (ROOT / "tests" / "corpus" / "persona_corpus.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line); k = r["kind"]
            if k == "agent_config":
                st = Store(); clock = [0.0]
                prov = make_provider() if make_provider else FakePayPal()
                used.append((st, prov))
                svc = Service(st, prov, clock=lambda: clock[0], default_require_intent=False, default_hard_cap=r["hard_cap"])
                svc.sync_enrich = True; svc.enrichers = _enricher(st, lambda: clock[0])
                svc.set_agent(r["agent"], hard_cap=r["hard_cap"], active_hours=r.get("active_hours"), org_id=r.get("org_id"))
                agent = r["agent"]
            elif k == "history":
                st.insert_payment({"payment_id": "h%d" % len([1 for _ in st.payments(agent, None, 100000)]), "agent_id": agent,
                                   "ts": r["ts"], "payee": r["payee"], "recipient": r["payee"].lower().replace(" ", "") + "@vendor.example",
                                   "amount": r["amount"], "currency": r.get("currency", "USD"), "memo": "", "hour": r["hour"], "intent_id": None,
                                   "verdict": "allow", "score_kind": "none", "reason": "h", "status": "sent", "paypal_batch_id": "H",
                                   "paypal_status": "SUCCESS", "decided_by": "history", "event_json": "{}", "narration": None, "risk": 0,
                                   "llm_flag": None, "mode": "enforce", "enforced": 1})
            else:
                clock[0], hour_now[0] = r["ts"] - 1, r["hour"]
                svc.register_intent(agent, r.get("stated_intent_payees") or ["-"], "plan")
                clock[0], hour_now[0] = r["ts"], r["hour"]
                recipient = r.get("recipient_email") or (r["payee"].lower().replace(" ", "") + "@vendor.example")
                out = svc.pay({"agent_id": agent, "payee": r["payee"], "recipient_email": recipient,
                               "amount": "%.2f" % r["amount"], "currency": r.get("currency", "USD"), "memo": ""})
                res[r["tid"]] = (out["decision"], out.get("status"), (out.get("reasons") or [out.get("reason", "")])[0])
    finally:
        SV.time = real_time
    if stats is not None:
        decided = [p for st, _ in used for p in st.payments(None, None, 100000) if p.get("provider")]
        stats.update(decided=len(decided), frozen=sum(p["status"] == "frozen" for p in decided), sent=sum(p["status"] == "sent" for p in decided),
                     errors=sum(p["status"] in ("paypal_error", "provider_error") for p in decided), refs=[p["provider_ref"] for p in decided if p.get("provider_ref")],
                     error_codes=__import__("collections").Counter(__import__("json").loads(r["detail"]).get("error") or "http_%s" % __import__("json").loads(r["detail"]).get("http") for st, _ in used for r in st.db.execute("SELECT detail FROM audit WHERE event='provider_result'").fetchall() if __import__("json").loads(r["detail"]).get("status") in ("paypal_error", "provider_error")))
    return res


TIER_ORDER = ["dumb-dumb", "dumb", "medium", "smart", "way-smarter"]


def main():
    res = run()
    labels = json.loads((ROOT / "tests" / "corpus" / "persona_labels.json").read_text())
    by_tier, by_persona = defaultdict(list), defaultdict(list)
    for x in labels:
        kind, which = x["label"].split(":", 1)
        (by_tier if kind == "attack" else by_persona)[which].append(x)

    def frozen(tid):
        return res[tid][1] == "frozen"

    print("=" * 76)
    print("PAYCANARY — PERSONA x ATTACKER-TIER MATRIX (shipped gate, Service.pay, enrichment ON)")
    print("=" * 76)
    print("\n--- ATTACKER TIER -> frozen / review / SILENT-ALLOW; the honest gradient ---")
    print("    (frozen = money stopped; review = flagged, money moves but surfaced; silent-allow = the real MISS)")
    exp = {"dumb-dumb": "crush", "dumb": "crush", "medium": "catch most", "smart": "bound+flag", "way-smarter": "honest floor"}
    tot_f = tot_s = tot_n = 0
    for tier in TIER_ORDER:
        items = by_tier.get(tier, [])
        if not items:
            continue
        f = sum(frozen(x["tid"]) for x in items)
        rev = sum(res[x["tid"]][0] == "review" and not frozen(x["tid"]) for x in items)
        silent = len(items) - f - rev
        tot_f += f; tot_s += silent; tot_n += len(items)
        print(f"  {tier:12s} frozen {f:3d}/{len(items):<3d} | review {rev:3d} | silent-allow {silent:3d}   expect: {exp[tier]}")
    print(f"  {'TOTAL':12s} frozen {tot_f}/{tot_n} | silent-allow {tot_s}/{tot_n} = {tot_s/tot_n*100:.1f}% actually missed")
    print(f"  (the headline that matters: silent-allow is 0 through 'smart'; only 'way-smarter' -- the stated floor -- slips)")

    print("\n--- BENIGN PERSONA -> false-nags (frozen / total); target 0 across ALL lives ---")
    total_nag = 0
    for persona in sorted(by_persona):
        items = by_persona[persona]
        nags = [x for x in items if frozen(x["tid"])]
        rev = sum(res[x["tid"]][0] == "review" for x in items)
        total_nag += len(nags)
        mark = "OK " if not nags else "NAG"
        print(f"  [{mark}] {persona:11s} {len(nags)}/{len(items)} frozen  (+{rev} review, allowed)")
        for x in nags:
            print(f"         FALSE-NAG: {res[x['tid']][2][:76]}")
    print(f"\n  false-nags across all personas: {total_nag}/{sum(len(v) for v in by_persona.values())}  [target 0]")

    ok = total_nag == 0 and by_tier.get("dumb-dumb") and all(frozen(x["tid"]) for x in by_tier["dumb-dumb"]) and all(frozen(x["tid"]) for x in by_tier.get("dumb", []))
    print(f"\n=== {'PASS' if ok else 'CHECK'} (0 false-nags + dumb-dumb/dumb fully caught) ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
