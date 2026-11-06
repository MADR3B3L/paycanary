"""Demo seed: populate a fresh database with a believable week for one agent and a queue that SHOWS the product —
allow / review / FREEZE rows, frozen ones left waiting for a human (so the dashboard has live Release/Deny buttons).
Real PayPal sandbox payouts for the allowed/review rows (pass `paypal`), or `paypal=None` for an offline run on the MOCK provider whose ids
are MOCK-000001... A simulated clock backdates the history so the velocity rule is not tripped; the last row is
~2 minutes ago, so live /pay traffic afterwards behaves normally.  CLI:  python3 -m app.seed [--offline] [--force]"""
import sys
import random
import time

from .audit import AuditLog
from .providers.mock.adapter import MockProvider
from .service import Service

AGENT = "ops-bot"                                                   # kept for API compatibility; the seed now builds several agents
DAY = 86400.0


def _series(latest, period, n):
    """n payment day-offsets (negative = days ago) ending `latest` days ago, one every `period` days."""
    return [-(abs(latest) + period * k) for k in range(n - 1, -1, -1)]


def _pays(latest, period, n, amounts):
    amts = amounts if isinstance(amounts, list) else [amounts] * n
    return list(zip(_series(latest, period, n), amts))


# Four agents with believable roles, each paying its established vendors on their real cadence for up to ~3 months. Each agent's largest vendor was onboarded
# first (so the baseline is never contradicted by a bigger new payee), and the operator approved the established vendors when onboarding them.
# Payments are executed through the REAL gate with a simulated clock, so every row, reason and audit entry is what the product would have produced.
AGENTS = {
    "ops-bot": dict(cap=2500, hours=(14, 17), plan_text="pay this month's cloud and developer-tool invoices", payees={
        "AWS": ("ar@aws-billing.example", _pays(4, 30, 4, [1098.60, 1142.30, 1187.65, 1236.40])),
        "GitHub": ("billing@github.example", _pays(19, 30, 3, [84.00, 84.00, 88.00])),
        "Datadog": ("billing@datadoghq.example", _pays(11, 30, 3, [405.00, 412.00, 412.00])),
        "Vercel": ("billing@vercel.example", _pays(3, 14, 6, [40.00, 40.00, 52.35, 40.00, 47.80, 40.00])),
    }),
    "ap-bot": dict(cap=3000, hours=(15, 18), plan_text="pay approved vendor invoices", payees={
        "Brightline Legal": ("billing@brightlinelaw.example", _pays(14, 30, 4, [1740.00, 1925.50, 1800.00, 1862.25])),
        "Harbor Coworking": ("ar@harborcowork.example", _pays(2, 30, 4, 1450.00)),
        "CleanCo Facilities": ("invoices@cleanco.example", _pays(6, 14, 6, [320.00, 320.00, 335.50, 320.00, 348.25, 320.00])),
        "Northwind Office Supply": ("orders@northwindsupply.example", [(-71, 187.40), (-39, 243.10), (-9, 212.85)]),
    }),
    "contractor-bot": dict(cap=4000, hours=(13, 16), plan_text="pay this period's contractor invoices", payees={
        "Dev Patel": ("dev@patelwebworks.example", _pays(6, 14, 7, [2400.00, 2400.00, 2250.00, 2400.00, 2520.00, 2400.00, 2310.00])),
        "Maria Okafor": ("maria@okafordesign.example", _pays(5, 14, 6, [1800.00, 1650.00, 1800.00, 1920.00, 1800.00, 1740.00])),
        "Priya Raman": ("priya@ramancopy.example", _pays(12, 30, 3, [880.00, 950.00, 1020.00])),
    }),
    "expense-bot": dict(cap=500, hours=(16, 19), plan_text="pay this month's team software subscriptions", payees={
        "Zoom": ("billing@zoom.example", _pays(10, 30, 4, [134.91, 149.90, 149.90, 164.89])),
        "Notion": ("billing@notion.example", _pays(6, 30, 3, [88.00, 96.00, 104.00])),
        "Slack": ("billing@slack.example", _pays(7, 30, 3, [78.75, 83.13, 91.88])),
        "Figma": ("billing@figma.example", _pays(1, 30, 3, [45.00, 45.00, 60.00])),
        "Loom": ("billing@loom.example", _pays(2, 30, 3, [24.00, 24.00, 32.00])),
    }),
}


OfflinePayPal = MockProvider                              # compatibility name: the demo's simulated payouts are the MOCK provider (ids MOCK-000001...), never PayPal


def seed(store, paypal=None, llm=None, now=None, agent=AGENT, force=False, audit_key=None, audit_anchor=None):
    if store.payments(None, None, 1) and not force:
        return {"seeded": False, "reason": "payments already exist (use force)"}
    if force:
        n = store.reset_demo_data()                      # a forced re-seed starts from a clean slate, not on top of previously decided rows
        AuditLog(store, time.time, key=audit_key, anchor_path=audit_anchor).append("demo_reset", "operator", detail={"removed": n})
    now = now or time.time()
    rng = random.Random(20261102)                        # deterministic jitter: the same seed looks the same every time
    midnight = now - (now % DAY)
    events = []
    for name, a in AGENTS.items():
        for payee, (email, pays) in a["payees"].items():
            for d, amt in pays:
                hr = a["hours"][0] + rng.random() * (a["hours"][1] - a["hours"][0])
                ts = midnight + d * DAY + hr * 3600
                if ts > now - 7200:                      # never in the future / inside the live window
                    ts -= DAY
                events.append((ts, name, payee, email, amt))
    events.sort()
    clock = [events[0][0] - 3600]
    if paypal is None:                                  # offline demo: simulated PayPal + Stripe rails so the dashboard shows both (ids SIM-PP-... / SIM-ST-...)
        from .providers.factory import build_registry
        svc = Service(store, providers=build_registry({}, names=("demo_rails",)), default_provider="paypal", clock=lambda: clock[0], audit_key=audit_key, audit_anchor=audit_anchor)
    else:
        svc = Service(store, paypal, clock=lambda: clock[0], audit_key=audit_key, audit_anchor=audit_anchor)
    svc.offline_evidence, svc.sync_enrich = True, True          # real Tier-0 evidence on the held rows, no network
    for name, a in AGENTS.items():
        svc.set_agent(name, hard_cap=a["cap"], require_intent=True, approved_payees=list(a["payees"]))
    if paypal is None:                                  # contractor + expense agents pay over Stripe, ops + AP over PayPal
        for name in ("contractor-bot", "expense-bot"):
            svc.assign_provider(name, "stripe")
    out = []

    def go(ts, agent_id, payee, email, amount, plan, plan_text, memo=None):
        clock[0] = ts - 5
        svc.register_intent(agent_id, plan, plan_text)
        clock[0] = ts
        r = svc.pay({"agent_id": agent_id, "payee": payee, "recipient_email": email, "amount": "%.2f" % amount, "currency": "USD",
                     "memo": memo or payee + " invoice"})
        out.append((agent_id, payee, amount, r["decision"]))
        return r

    for ts, name, payee, email, amt in events:           # the established history, oldest first
        go(ts, name, payee, email, amt, list(AGENTS[name]["payees"]), AGENTS[name]["plan_text"])
    if llm:                                              # narrate only the live window below (not 60+ history rows)
        svc.llm, svc.on_decided = llm, svc.enrich

    def recent(minutes):
        return now - minutes * 60
    ops, ap, con, exp = (AGENTS[k] for k in ("ops-bot", "ap-bot", "contractor-bot", "expense-bot"))
    # ap-bot: the monthly Harbor Coworking rent shows up again two days after it was paid -> flagged as off-cycle (a likely duplicate invoice), money still moves
    go(recent(95), "ap-bot", "Harbor Coworking", ap["payees"]["Harbor Coworking"][0], 1450.00, list(ap["payees"]), ap["plan_text"], "Harbor Coworking invoice (resent)")
    # expense-bot: a payout split into $95 chunks (just under the $100 new-payee floor) to a payee that is in the stated plan: structuring guard
    plan_x = list(exp["payees"]) + ["Veloxa Payments"]
    for m in (74, 51, 29):
        go(recent(m), "expense-bot", "Veloxa Payments", "payouts@veloxa-pay.example", 95.00, plan_x, exp["plan_text"], "Veloxa Payments invoice")
    # contractor-bot: a fat-fingered invoice, 10x the usual, over the agent's hard cap
    go(recent(62), "contractor-bot", "Priya Raman", con["payees"]["Priya Raman"][0], 9500.00, list(con["payees"]), con["plan_text"], "Priya Raman invoice")
    # ap-bot: a genuinely new vendor, small and in the stated plan -> flagged for a look but allowed through
    go(recent(41), "ap-bot", "Delta Supply Co", "accounts@deltasupply.example", 85.00, list(ap["payees"]) + ["Delta Supply Co"], ap["plan_text"], "Delta Supply Co first order")
    # ops-bot: a payee that is NOT in the stated plan, paid to a throwaway inbox -> frozen before the call
    go(recent(18), "ops-bot", "QuickTools LLC", "quicktools.billing@mailinator.com", 18.00, list(ops["payees"]), ops["plan_text"], "QuickTools LLC subscription")
    return {"seeded": True, "payments": len(out), "frozen": sum(1 for *_, d in out if d == "freeze"), "agents": list(AGENTS), "rows": out}


if __name__ == "__main__":
    from . import config, llm as L
    from .paypal import PayPalClient
    from .store import Store
    import os
    config.load_env()
    store = Store(os.environ.get("PAYCANARY_DB", str(config.ROOT / "paycanary.sqlite3")))
    offline = "--live" not in sys.argv or not config.get("PAYPAL_CLIENT_ID")      # default: simulated payouts; --live uses real sandbox payouts (spends sandbox funds)
    pp = None if offline else PayPalClient(config.get("PAYPAL_CLIENT_ID"), config.get("PAYPAL_CLIENT_SECRET"), "sandbox")
    from . import auditkey
    ak, aa = auditkey.load(os.environ, os.environ.get("PAYCANARY_DB", str(config.ROOT / "paycanary.sqlite3")))
    res = seed(store, pp, L.from_config(store), force="--force" in sys.argv, audit_key=ak, audit_anchor=aa)
    print(("OFFLINE (no PayPal calls) " if offline else "PayPal sandbox ") + str({k: v for k, v in res.items() if k != "rows"}))
