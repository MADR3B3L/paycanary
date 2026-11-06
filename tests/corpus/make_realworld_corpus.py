#!/usr/bin/env python3
"""REAL-WORLD-GROUNDED benchmark corpus. The synthetic 227-txn corpus was written by us; this one grounds every case in a
DOCUMENTED agent/payment fraud pattern or a realistic legit-traffic shape, so the headline number means something to a
PayPal fraud engineer. Sources are in docs/DETECTION_DEPTH_RESEARCH.md + docs/REAL_WORLD_REACH_RESEARCH.md and the AML/APP
literature cited there (RFM + multi-window velocity; Nigrini round-number / just-under-threshold; structuring; graph
fan-in/fan-out; mule; prompt-injection redirect; invoice/beneficiary swap).

Design principles that make it realistic (not "bill-bot pays AWS"):
  * agents have RFM-shaped histories (recency/frequency/monetary vary per vendor), realistic cadence, some multi-currency;
  * amounts are drawn from per-vendor distributions, not constants;
  * attack cases copy the SHAPE of real schemes (e.g. structuring just under a control threshold; fan-out across fresh
    mules at a laundering cadence; a beneficiary-swap on a known vendor; a round-number just-under-cap drain);
  * benign cases copy real small-business / agent traffic (payroll runs, monthly SaaS, net-30 invoices, bursty campaign
    launches, gradual growth, multi-currency suppliers, legit new-vendor onboarding) INCLUDING the hard-but-legit ones
    that a naive gate would false-nag.

Output: realworld_corpus.jsonl (stream for the gate/service) + realworld_labels.json (per-txn ground truth + attack class).
Deterministic (fixed seed), stdlib only, no network. Scored by tools/score_realworld_service.py through the real Service.pay.
"""
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
RNG = random.Random(20261103)
DAY = 86400.0

stream, labels = [], []
TID = 0


def emit_txn(agent, payee, amount, ts, hour, intent, label, currency="USD"):
    global TID
    tid = TID; TID += 1
    stream.append(json.dumps({"kind": "txn", "agent": agent, "payee": payee, "amount": round(amount, 2), "ts": ts,
                              "hour": hour, "currency": currency, "stated_intent_payees": intent, "tid": tid}))
    labels.append({"tid": tid, "agent": agent, "label": label})
    return tid


def emit_hist(agent, payee, amount, ts, hour, currency="USD"):
    stream.append(json.dumps({"kind": "history", "agent": agent, "payee": payee, "amount": round(amount, 2),
                              "ts": ts, "hour": hour, "currency": currency}))


# --- realistic agent personas (RFM-shaped: each vendor has its own freq, mean, sd, cadence-days) ---
# (agent, hard_cap, active_hours, [ (vendor, freq_weight, mean, sd, currency) ], cadence_days)
AGENTS = [
    ("ap-bot-acme", 5000, range(8, 19), [
        ("AWS", 5, 2200, 300, "USD"), ("Datadog", 3, 480, 60, "USD"), ("GitHub", 4, 84, 6, "USD"),
        ("Vercel", 2, 150, 25, "USD"), ("Twilio", 3, 210, 55, "USD")], 7),
    ("payroll-bot", 12000, range(8, 11), [
        ("Alice Wong", 1, 4200, 40, "USD"), ("Bob Lee", 1, 3800, 40, "USD"), ("Carol Diaz", 1, 4600, 50, "USD"),
        ("Dan Park", 1, 3100, 30, "USD"), ("Erin Falk", 1, 5200, 60, "USD")], 14),
    ("ads-bot", 8000, range(0, 24), [
        ("GoogleAds", 6, 1800, 600, "USD"), ("MetaAds", 5, 1400, 500, "USD"), ("LinkedInAds", 2, 600, 180, "USD"),
        ("TikTokAds", 2, 500, 220, "USD")], 3),
    ("intl-supply-bot", 20000, range(0, 24), [
        ("ShenzhenParts", 3, 6200, 700, "USD"), ("HanoiTextile", 2, 3400, 400, "USD"),
        ("MunichGmbH", 2, 4100, 500, "EUR"), ("OsakaKK", 2, 520000, 40000, "JPY")], 10),
    ("ops-petty", 600, range(7, 22), [
        ("Uber", 6, 34, 14, "USD"), ("DoorDash", 5, 48, 18, "USD"), ("Amazon", 7, 72, 55, "USD"),
        ("Staples", 2, 96, 40, "USD")], 1),
    ("saas-procure", 6000, range(9, 20), [
        ("Snowflake", 2, 2400, 400, "USD"), ("Figma", 3, 180, 15, "USD"), ("Notion", 2, 96, 8, "USD"),
        ("PagerDuty", 2, 290, 30, "USD"), ("Linear", 2, 128, 10, "USD")], 30),
]


def pick_vendor(vendors):
    total = sum(v[1] for v in vendors); r = RNG.uniform(0, total); c = 0
    for v in vendors:
        c += v[1]
        if r <= c:
            return v
    return vendors[-1]


for agent, cap, hours, vendors, cadence in AGENTS:
    stream.append(json.dumps({"kind": "agent_config", "agent": agent, "hard_cap": float(cap),
                              "active_hours": list(hours), "org_id": "org:" + agent}))
    hrs = list(hours)
    ts = 100000.0
    # --- RFM-shaped learned history: ~40 payments over ~90 days, per-vendor freq/mean/sd/cadence ---
    for i in range(40):
        v = pick_vendor(vendors)
        name, _, mean, sd, cur = v
        ts += RNG.uniform(0.3, 2.2) * DAY
        amt = max(1.0, RNG.gauss(mean, sd))
        emit_hist(agent, name, amt, ts, RNG.choice(hrs), cur)
    base = ts + 30 * DAY
    typ = sum(v[2] for v in vendors) / len(vendors)
    known = [v[0] for v in vendors]

    # =================== BENIGN (realistic legit traffic; must NOT freeze) ===================
    t = base
    # recurring payments to known vendors at normal amounts, on cadence
    for v in vendors:
        name, _, mean, sd, cur = v
        t += RNG.uniform(0.5, 2) * DAY
        emit_txn(agent, name, max(1.0, RNG.gauss(mean, sd)), t, RNG.choice(hrs), [name], "benign", cur)
    # a mildly-high but legit known payment (~2 SD) — the real "true-up" invoice
    name, _, mean, sd, cur = vendors[0]
    t += DAY; emit_txn(agent, name, mean + 2.2 * sd, t, hrs[0], [name], "benign", cur)
    # gradual growth: a known vendor's amount drifts up ~6%/billing-cycle over several cycles (real SaaS seat growth)
    g = mean
    for _ in range(4):
        g *= 1.06; t += cadence * DAY
        emit_txn(agent, name, g, t, hrs[0], [name], "benign", cur)
    # persona-specific realistic patterns
    if agent == "ads-bot":                         # bursty-but-legit: a campaign launch, several payments close together
        for _ in range(4):
            vv = pick_vendor(vendors); t += 40
            emit_txn(agent, vv[0], max(1.0, RNG.gauss(vv[2], vv[3])), t, 3, known, "benign", vv[4])
    if agent == "payroll-bot":                     # a payroll RUN: all employees paid in one short window, on cycle (legit velocity)
        t += 14 * DAY
        for v in vendors:
            t += 30
            emit_txn(agent, v[0], max(1.0, RNG.gauss(v[2], v[3])), t, 9, known, "benign", v[4])
    if agent == "intl-supply-bot":                 # multi-currency normal payments (must not be judged against a USD baseline)
        for v in vendors:
            t += DAY
            emit_txn(agent, v[0], max(1.0, RNG.gauss(v[2], v[3])), t, 2, [v[0]], "benign", v[4])
    # legit NEW-vendor onboarding: 2 brand-new small vendors in a week (under the fan-out floor; must clear).
    # Use a USD-scale amount (the agent's USD vendors' mean), NOT the all-vendor `typ` which for a multi-currency agent is
    # skewed by a JPY-scale vendor -- emitting a JPY-sized number mislabeled USD would be a correct freeze, not a false-nag.
    usd_means = [v[2] for v in vendors if v[4] == "USD"]
    usd_typ = (sum(usd_means) / len(usd_means)) if usd_means else 100.0
    for j in range(2):
        t += 2 * DAY
        emit_txn(agent, f"NewVendor{j}", usd_typ * 0.5, t, hrs[0], [f"NewVendor{j}"], "benign")
    # BULK legit onboarding: 5 brand-new REAL-business vendors in one day -- this DOES trip the fan-out counter (>K=3),
    # so it exercises the CONTEXTUAL fan-out path: with enrichment on, the clean-business evidence must let them through
    # (review/sent, flagged), not blind-freeze. Labeled benign; payee names carry a clean-domain hint for the enricher.
    for j in range(5):
        t += 1200
        emit_txn(agent, f"realbiz-{agent}-{j}", usd_typ * 0.6, t, hrs[0], [f"realbiz-{agent}-{j}"], "benign")

    # =================== ATTACKS (documented shapes; must FREEZE, or escalate for review-tier classes) ===================
    a = base + 200 * DAY        # attacks well after history so cumulative windows are clean at start

    # prompt_injection_redirect: injected attacker payee, amount pulled to a round number (Nigrini), new destination
    a += DAY
    emit_txn(agent, "quick-settle-llc", 2000.0, a, hrs[0], known, "prompt_injection_redirect")   # round, off-intent, new
    # invoice_beneficiary_swap: a KNOWN vendor's name but a NEW destination email (changed bank details)
    a += DAY
    tid = TID
    stream.append(json.dumps({"kind": "txn", "agent": agent, "payee": known[0], "amount": round(RNG.gauss(vendors[0][2], vendors[0][3]), 2),
                              "ts": a, "hour": hrs[0], "currency": vendors[0][4], "stated_intent_payees": [known[0]],
                              "recipient_email": "attacker@evil.example", "tid": tid}))      # known payee, NEW recipient
    labels.append({"tid": tid, "agent": agent, "label": "invoice_beneficiary_swap"}); TID += 1
    # structuring: ONE large payout split into sub-threshold chunks to one mule, spaced hours apart (classic smurfing)
    a += DAY
    chunk = min(cap * 0.9, typ * 1.4)
    for _ in range(5):
        a += 3 * 3600
        emit_txn(agent, "mule-smurf-01", chunk, a, hrs[0], ["mule-smurf-01"], "structuring")
    # fan_out: the laundering breadth pattern — many fresh mules, each a small sub-threshold payment, spaced out
    a += DAY
    for k in range(8):
        a += 2 * 3600
        emit_txn(agent, f"fanout-mule-{k}", typ * 0.8, a, hrs[0], [f"fanout-mule-{k}"], "fan_out")
    # round_number_just_under_cap: Nigrini — a single drain sized just under the agent's hard cap, round
    a += DAY
    emit_txn(agent, "drain-payee-09", round(cap * 0.95 / 100) * 100, a, hrs[0], ["drain-payee-09"], "just_under_cap")
    # slow_escalation: a new payee trained up over days to normalize a large drain (the Gun-4 patient attack)
    a2 = a
    ramp = typ * 0.8
    for _ in range(6):
        a2 += 2 * DAY; ramp *= 1.9
        emit_txn(agent, "slow-trainer-07", min(ramp, cap * 0.9), a2, hrs[0], ["slow-trainer-07"], "slow_escalation")
    # velocity_drain: rapid-fire to one new payee (compromised-agent fast drain)
    a3 = a2 + DAY
    for _ in range(7):
        a3 += 8
        emit_txn(agent, "burst-mule-02", typ, a3, hrs[0], ["burst-mule-02"], "velocity_drain")

(HERE / "realworld_corpus.jsonl").write_text("\n".join(stream) + "\n")
(HERE / "realworld_labels.json").write_text(json.dumps(labels))

from collections import Counter
by = Counter(x["label"] for x in labels)
n_ben = by.pop("benign", 0)
print(f"agents: {len(AGENTS)}  test txns: {len(labels)}  benign: {n_ben}  attack: {sum(by.values())}")
print("attack classes:", dict(by))
