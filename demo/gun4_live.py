#!/usr/bin/env python3
"""Live gun-4 run against the PayPal SANDBOX: (1) the slow-escalation attack ($50>$120>$300>$800>$2000 to one new wallet)
must be frozen; (2) legitimate gradual growth must NOT false-freeze. Amounts are shrunk by DEMO_SCALE because the sandbox
balance is small (the gate is scale-free). Only allowed/review payments hit PayPal.  Needs funded sandbox + .env.
Run:  DEMO_SCALE=0.002 python3 demo/gun4_live.py"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config
from app.paypal import PayPalClient
from tests import poison_corpus as P

config.load_env()
P.SCALE = float(os.environ.get("DEMO_SCALE", "0.002")); P.WARM_DAYS = 10
real = PayPalClient(config.get("PAYPAL_CLIENT_ID"), config.get("PAYPAL_CLIENT_SECRET"), "sandbox")
calls = []
class Live:
    def create_payout(self, *a, **k):
        calls.append(a[1]); return real.create_payout(*a, **k)
P._PP = Live

a = P.attack(P.WALK, 3600, "LIVE walk 50>120>300>800>2000 to one new wallet")
print("ATTACK:", "CAUGHT" if a["caught"] else "MISSED", "| first freeze at step", a["first_freeze_step"], "| trace:", [(x, d) for x, d in a["trace"]])
steps = [round(60 * 1.04 ** i, 2) for i in range(10)]
l = P.legit("LIVE legit vendor +4%/step", steps, 7 * P.DAY, ["AWS"])
print("LEGIT:", l)
print("real create_payout calls:", len(calls))
ok = a["caught"] and l["freeze"] == 0
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
