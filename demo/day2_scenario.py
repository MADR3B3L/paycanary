#!/usr/bin/env python3
"""Day-2 live scenario: REAL PayPal sandbox Payouts calls, SIMULATED clock (so seeding a history doesn't trip the
velocity rule). Each step is tagged with the feature it exercises:  A = canary (behavioural scoring + narration),
B = goalie (deterministic in-path freeze before PayPal fires), C = intent-read (stated plan vs the actual payee).
Run:  python3 demo/day2_scenario.py   (needs .env with sandbox credentials)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config
from app.paypal import PayPalClient
from app.service import Service
from app.store import Store

import os
SCALE = float(os.environ.get("DEMO_SCALE", "1"))      # sandbox balance is small: DEMO_SCALE=0.1 shrinks every amount (labels stay nominal)
config.load_env()
clock = [1_700_000_000.0]
if os.environ.get("DEMO_OFFLINE") == "1":          # gate-behaviour check without spending sandbox balance; batch ids are SEED-OFFLINE (NOT PayPal)
    from app.seed import OfflinePayPal
    pp = OfflinePayPal()
else:
    pp = PayPalClient(config.get("PAYPAL_CLIENT_ID"), config.get("PAYPAL_CLIENT_SECRET"), "sandbox")
calls = []
_orig = pp.create_payout
def counting(*a, **k):
    calls.append(a[0]); return _orig(*a, **k)
pp.create_payout = counting
svc = Service(Store(), pp, clock=lambda: clock[0])
rows = []

def step(tag, label, out, expect):
    ok = out["decision"] == expect
    batch = (out.get("paypal") or {}).get("batch_id")
    rows.append((tag, label, out["decision"], out["status"], batch or "— (PayPal NOT called)" if out["decision"] == "freeze" else (batch or "-"), out["reasons"][0] if out.get("reasons") else out.get("reason", ""), "PASS" if ok else "FAIL(expected %s)" % expect))
    return out

def pay(payee, amount, email=None, agent="bill-bot", memo=""):
    return svc.pay({"agent_id": agent, "payee": payee, "recipient_email": email or payee.lower() + "-billing@example.com", "amount": "%.2f" % (float(amount) * SCALE), "currency": "USD", "memo": memo or payee})

svc.set_agent("bill-bot", hard_cap=1000 * SCALE, require_intent=True)
svc.register_intent("bill-bot", ["AWS", "Github", "Stripe", "Datadog", "BigVendor"], "monthly vendor bills")

# --- history: normal payments over simulated days (learned baseline); the agent announces its plan before each task ---
for day, (payee, amt) in enumerate([("AWS", "55.00"), ("Github", "48.00"), ("AWS", "61.00"), ("Stripe", "52.00"), ("Github", "50.00"), ("AWS", "58.00")], 1):
    clock[0] += 86400
    svc.register_intent("bill-bot", ["AWS", "Github", "Stripe", "Datadog", "BigVendor"], "monthly vendor bills")
    step("A,B", f"seed day {day}: {payee} ${amt} (normal)", pay(payee, amt), "review" if day in (1, 2, 4) else "allow")
clock[0] += 3 * 86400          # AWS is paid about every 3 days: this one is ON its cycle (cadence: allow, lowest risk)
svc.register_intent("bill-bot", ["AWS", "Github", "Stripe", "Datadog", "BigVendor"], "monthly vendor bills")
step("A,B", "normal: AWS $59, on its usual cycle (allow)", pay("AWS", "59.00"), "allow")
clock[0] += 60
step("A,B", "new payee Datadog $45, in intent (review: flagged, proceeds)", pay("Datadog", "45.00"), "review")
clock[0] += 60
step("A,B", "known payee AWS $78 (about 4 SD high: review)", pay("AWS", "78.00"), "review")
# --- freezes (the agent announced a fresh plan for the AWS bill only) ---
clock[0] += 120
svc.register_intent("bill-bot", ["AWS", "BigVendor"], "pay the AWS bill and onboard BigVendor")
f1 = step("C→B", "INTENT MISMATCH: AttackerWallet $20 (plan said AWS/BigVendor only)", pay("AttackerWallet", "20.00", "attacker-wallet@example.com"), "freeze")
clock[0] += 120
f2 = step("A→B", "NEW payee BigVendor $900 (in plan, but ~16x typical)", pay("BigVendor", "900.00", "bigvendor@example.com"), "freeze")
clock[0] += 120
step("B", "over the agent's hard cap: AWS $1,200 (cap $1,000)", pay("AWS", "1200.00"), "freeze")
clock[0] += 120
f4 = step("A→B", "known payee AWS $400 (> 5x its historical max, under the cap)", pay("AWS", "400.00"), "freeze")
clock[0] += 3600
svc.register_intent("bill-bot", ["AWS"], "rapid AWS test")
v = []
for i in range(8):
    clock[0] += 1
    v.append(pay("AWS", "50.00")["decision"])
step("B", "VELOCITY: 8 rapid AWS $50 in 8s (first allowed, then freezes)", {"decision": "freeze" if "freeze" in v else "allow", "status": "|".join(v), "reasons": ["decisions in order: " + ",".join(v)]}, "freeze")
# --- human decisions on frozen payments ---
rel = svc.release(f2["payment_id"], "marty")
rows.append(("B", "HUMAN RELEASE of the BigVendor freeze", "release", rel["status"], (rel.get("paypal") or {}).get("batch_id", "-"), "human:marty", "PASS" if rel["status"] == "sent" else "FAIL"))
den = svc.deny(f4["payment_id"], "marty")
rows.append(("B", "HUMAN DENY of the $400 AWS freeze", "deny", den["status"], "— (PayPal NOT called)", "human:marty", "PASS" if den["status"] == "denied" else "FAIL"))

print("| feat | step | decision | status | PayPal batch | why | check |")
print("|---|---|---|---|---|---|---|")
for r in rows:
    print("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |")
frozen_with_batch = [p for p in svc.store.payments("bill-bot") if p["verdict"] == "freeze" and p["paypal_batch_id"] and p["decided_by"] == "goalie"]
sent = [p for p in svc.store.payments("bill-bot") if p["paypal_batch_id"]]
print("\nPayPal create_payout calls made:", len(calls), "| payments with a PayPal batch id:", len(sent),
      "| frozen-by-goalie payments that reached PayPal before a human decision:", len(frozen_with_batch))
print("baseline payees learned (executed only):", sorted({r["payee"] for r in svc.store.baseline_rows("bill-bot")}))
fails = [r for r in rows if r[-1].startswith("FAIL")]
print("RESULT:", "ALL PASS" if not fails and not frozen_with_batch else "FAILURES: %s" % fails)
sys.exit(1 if fails or frozen_with_batch else 0)
