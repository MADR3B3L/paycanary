#!/usr/bin/env python3
"""DIVERSE benchmark: real human spread on BOTH sides, reported as a matrix (recall per attacker TIER x false-nags per
benign PERSONA), not one blended number. The point: a partner fraud team needs "how do you do against each attacker tier
and each customer type", because 'normal' differs wildly across lives and a gate that learned one normal false-freezes the
others -- and because naive fraud rules abuse exactly the honest customers (remittances, gig, student, HNW).

BENIGN personas (each its own agent with a lifestyle-shaped history):
  broke (small/irregular/late-night/P2P), gig (bursty, many small varied, odd hours), working-class (regular bills),
  small-biz (vendors/payroll/spiky), HNW (large amounts that are NORMAL for them), retiree (fixed/routine),
  immigrant (regular cross-border remittances, multi-currency), student (tiny/erratic/odd merchants).
ATTACKER tiers (labeled by intelligence), each run against a HOST persona so it's judged vs that persona's real baseline:
  dumb-dumb (obvious scam wallet, round, no cover), dumb (throwaway email/fresh domain/impatient),
  medium (spaced, plausible name, some patience), smart (mimics the agent's amount/cadence, aged-ish domain),
  way-smarter (clean footprint, slow baseline-training, under every threshold, fans across legit-looking mules).

Output: persona_corpus.jsonl + persona_labels.json (label = "benign:<persona>" or "attack:<tier>"). The scorer
(tools/score_persona_service.py) runs it through the real Service.pay with enrichment on and prints the matrix.
Deterministic, stdlib only, no network.
"""
import json, random
from pathlib import Path

HERE = Path(__file__).resolve().parent
RNG = random.Random(20261104)
DAY = 86400.0
rows, labels, TID = [], [], 0


def txn(agent, payee, amount, ts, hour, intent, label, currency="USD", recipient=None):
    global TID
    tid = TID; TID += 1
    r = {"kind": "txn", "agent": agent, "payee": payee, "amount": round(amount, 2), "ts": ts, "hour": hour,
         "currency": currency, "stated_intent_payees": intent, "tid": tid}
    if recipient:
        r["recipient_email"] = recipient
    rows.append(json.dumps(r)); labels.append({"tid": tid, "agent": agent, "label": label})
    return tid


def hist(agent, payee, amount, ts, hour, currency="USD"):
    rows.append(json.dumps({"kind": "history", "agent": agent, "payee": payee, "amount": round(max(1.0, amount), 2),
                            "ts": ts, "hour": hour, "currency": currency}))


# ---- 8 benign personas: (name, cap, hours, vendors[(payee, mean, sd, cur)], hist_n, cadence_hint) ----
PERSONAS = {
    "broke": (800, list(range(18, 26 % 24)) or [19, 20, 21, 22, 23, 0, 1],
              [("Venmo-Rent", 600, 40, "USD"), ("CashApp-Mom", 40, 20, "USD"), ("Corner-Store", 12, 7, "USD"),
               ("Phone-Bill", 55, 5, "USD"), ("Payday-Loan", 90, 15, "USD")], 36, "irregular"),
    "gig": (1500, list(range(0, 24)),
            [("GasStation", 45, 18, "USD"), ("CarRepair", 180, 90, "USD"), ("AppStore", 8, 3, "USD"),
             ("Grocery", 60, 35, "USD"), ("Venmo-Split", 25, 15, "USD"), ("Toll", 9, 4, "USD")], 60, "bursty"),
    "working": (2500, list(range(7, 20)),
                [("Mortgage", 1650, 20, "USD"), ("Electric", 140, 30, "USD"), ("Car-Insurance", 180, 10, "USD"),
                 ("Grocery-Chain", 220, 60, "USD"), ("Internet", 70, 5, "USD")], 40, "regular"),
    "smallbiz": (8000, list(range(8, 19)),
                 [("Supplier-A", 2400, 400, "USD"), ("Payroll-Jo", 2800, 50, "USD"), ("Payroll-Sam", 2600, 50, "USD"),
                  ("SaaS-Stack", 320, 40, "USD"), ("Landlord", 3200, 20, "USD")], 48, "spiky"),
    "hnw": (250000, list(range(9, 23)),
            [("Wealth-Mgr", 85000, 20000, "USD"), ("Art-Dealer", 42000, 15000, "USD"), ("Yacht-Svc", 28000, 9000, "USD"),
             ("Private-Chef", 9000, 1500, "USD"), ("Estate-Staff", 16000, 2000, "USD")], 40, "large-normal"),
    "retiree": (3000, list(range(8, 17)),
                [("Pharmacy", 85, 25, "USD"), ("Medicare-Supp", 210, 5, "USD"), ("Utilities", 160, 30, "USD"),
                 ("Grocery", 110, 30, "USD"), ("Church", 50, 0, "USD")], 40, "fixed"),
    "immigrant": (12000, list(range(5, 23)),
                  [("Remit-Manila", 70000, 5000, "PHP"), ("Remit-Mexico", 1800, 200, "USD"), ("Rent", 1400, 20, "USD"),
                   ("Grocery", 95, 40, "USD"), ("Phone", 45, 5, "USD")], 44, "remittance"),
    "student": (500, list(range(10, 27 % 24)) or [10, 11, 14, 16, 20, 22, 23, 1, 2],
                [("Ramen-Shop", 11, 5, "USD"), ("Textbook-Site", 85, 60, "USD"), ("Streaming", 12, 2, "USD"),
                 ("Venmo-Friends", 18, 12, "USD"), ("Coffee", 6, 3, "USD"), ("Thrift", 22, 14, "USD")], 50, "erratic"),
}
# currencies the shipped service accepts (PHP/MXN aren't in the FX table -> remittance uses supported ones for the demo)
SUPPORTED = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD"}


def clean_email(payee):
    # MUST match the history-insert recipient exactly (payee.lower().replace(" ","") + "@vendor.example"), or a known
    # payee's test payment looks like a changed destination and false-freezes. Do NOT strip hyphens here.
    return payee.lower().replace(" ", "") + "@vendor.example"


for name, (cap, hours, vendors, hist_n, hint) in PERSONAS.items():
    agent = "p-" + name
    # map unsupported remittance currencies to supported ones so they exercise the FX path (demo-honest: real PHP/MXN later)
    vendors = [(p, m, s, (c if c in SUPPORTED else ("JPY" if "Manila" in p else "EUR"))) for (p, m, s, c) in vendors]
    rows.append(json.dumps({"kind": "agent_config", "agent": agent, "hard_cap": float(cap),
                            "active_hours": list(hours), "org_id": "org:" + agent}))
    hrs = list(hours)
    ts = 100000.0
    for i in range(hist_n):
        p, m, s, cur = RNG.choice(vendors)
        ts += RNG.uniform(0.2, 2.5) * DAY
        hist(agent, p, RNG.gauss(m, s), ts, RNG.choice(hrs), cur)
    base = ts + 20 * DAY
    usd_means = [m for (p, m, s, c) in vendors if c == "USD"] or [100]
    usd_typ = sum(usd_means) / len(usd_means)

    # ---------- BENIGN: this persona's own normal traffic (must NOT freeze) ----------
    t = base
    for p, m, s, cur in vendors:
        t += RNG.uniform(0.3, 2) * DAY
        txn(agent, p, max(1.0, RNG.gauss(m, s)), t, RNG.choice(hrs), [p], "benign:" + name, cur, clean_email(p))
    # a mildly-high but normal one (~2 SD) for this persona
    p, m, s, cur = vendors[0]
    t += DAY; txn(agent, p, m + 2.0 * s, t, hrs[0], [p], "benign:" + name, cur, clean_email(p))
    # persona-flavored extra normal behavior
    if name in ("broke", "student", "gig"):            # lots of small odd-hour P2P / varied small merchants
        for _ in range(4):
            p, m, s, cur = RNG.choice(vendors); t += RNG.uniform(0.1, 0.6) * DAY
            txn(agent, p, max(1.0, RNG.gauss(m, s)), t, RNG.choice(hrs), [p], "benign:" + name, cur, clean_email(p))
    if name == "immigrant":                            # the recurring overseas remittance naive rules wrongly flag
        for _ in range(3):
            t += 14 * DAY
            txn(agent, "Remit-Manila", RNG.gauss(70000, 5000), t, 7, ["Remit-Manila"], "benign:" + name, "JPY", clean_email("Remit-Manila"))
    if name == "hnw":                                  # a large-but-normal-for-HNW payment that would scream fraud elsewhere
        t += 3 * DAY
        txn(agent, "Wealth-Mgr", 120000, t, 10, ["Wealth-Mgr"], "benign:" + name, "USD", clean_email("Wealth-Mgr"))
    if name == "smallbiz":                             # a payroll run (legit velocity) + 2 legit new vendors
        t += 14 * DAY
        for p, m, s, cur in vendors[1:3]:
            t += 60; txn(agent, p, max(1.0, RNG.gauss(m, s)), t, 9, [p], "benign:" + name, cur, clean_email(p))
        for j in range(2):
            t += DAY; txn(agent, f"NewSupplier{j}", usd_typ * 0.5, t, 10, [f"NewSupplier{j}"], "benign:" + name, "USD", f"newsupplier{j}@vendor.example")

    # ---------- ATTACKS: each tier, judged against THIS persona's baseline ----------
    a = base + 150 * DAY
    scam = usd_typ            # a "normal-for-this-agent" scale, so the attack is calibrated to the persona (HNW attack is big, student attack is tiny)

    def atk(tier, payee, amount, ts_, intent, cur="USD", recipient=None):
        return txn(agent, payee, amount, ts_, hrs[0], intent, "attack:" + tier, cur, recipient)

    # dumb-dumb: obvious scam wallet, huge round number, off-intent, throwaway inbox
    a += DAY
    atk("dumb-dumb", "FREE-MONEY-WALLET", round(cap * 0.9 / 1000) * 1000 or 1000, a, ["FREE-MONEY-WALLET"], "USD", "freemoney@mailinator.com")
    # dumb: throwaway email, fresh domain, impatient (new payee, large-ish, off stated intent)
    a += DAY
    atk("dumb", "quickcash-payout", scam * 3, a, [vendors[0][0]], "USD", "quickcash@guerrillamail.com")
    # medium: spaced, plausible business name, some patience -> structuring sub-threshold chunks over hours to one mule
    a += DAY
    for _ in range(5):
        a += 3 * 3600
        atk("medium", "Northstar-Consulting", scam * 1.4, a, ["Northstar-Consulting"], "USD", "ap@northstar-consulting-grp.example")
    # smart: mimics the agent's amount/cadence to a NEW payee with an aged-ish domain, fans across a few legit-looking mules
    a += DAY
    for k in range(4):
        a += 6 * 3600
        atk("smart", f"Meridian-Partners-{k}", max(1.0, RNG.gauss(usd_typ, usd_typ * 0.1)), a, [f"Meridian-Partners-{k}"], "USD", f"billing@meridian-partners-{k}.example")
    # way-smarter: patient slow-escalation to a clean-footprint payee, each step under threshold (the irreducible floor)
    a2 = a
    ramp = usd_typ * 0.7
    for _ in range(6):
        a2 += 3 * DAY; ramp *= 1.35
        atk("way-smarter", "Apex-Holdings-LLC", ramp, a2, ["Apex-Holdings-LLC"], "USD", "treasury@apex-holdings-llc.example")

(HERE / "persona_corpus.jsonl").write_text("\n".join(rows) + "\n")
(HERE / "persona_labels.json").write_text(json.dumps(labels))
from collections import Counter
by = Counter(x["label"].split(":")[0] for x in labels)
perc = Counter(x["label"] for x in labels if x["label"].startswith("benign"))
tc = Counter(x["label"] for x in labels if x["label"].startswith("attack"))
print(f"agents/personas: {len(PERSONAS)}  txns: {len(labels)}  benign: {by['benign']}  attack: {by['attack']}")
print("benign per persona:", {k.split(':')[1]: v for k, v in sorted(perc.items())})
print("attack per tier:", {k.split(':')[1]: v for k, v in sorted(tc.items())})
