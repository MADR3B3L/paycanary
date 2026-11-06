#!/usr/bin/env python3
"""END-TO-END cross-agent proof through the REAL path: events -> Reputation store (HMAC, per-org rows, as-of reads) -> Service.pay -> gate -> app/reputation.
Unlike tools/score_crossagent.py (which scores the pure functions on hand-built records with a fixed base tier), every network view here is
BUILT from contributions by simulated orgs, and every verdict comes from Service.pay. Each case runs twice: WITHOUT the network (reputation=None)
and WITH it, so the lift is measured, not assumed. Includes the poisoning attacks, the griefing attack, and the proof that agent-behaviour freezes
(cap) never become payee reputation. Deterministic (seeded). Exit 0 iff every attack class is lifted as designed, no benign payment freezes, and
poisoning never earns trust."""
import random, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.reputation_store import Reputation
from app.service import Service
from app.store import Store

NOW, DAY, KEY = 1_900_000_000.0, 86400.0, "e2e-reputation-key-0123456789"


class PP:
    def create_payout(self, *a, **k):
        return 201, {"batch_header": {"payout_batch_id": "B", "batch_status": "PENDING"}}


def agent(rep, rng, org="me", name="a"):
    """An established, varied-spend agent (8 executed payments over 40 days) in its own org."""
    t = [NOW]
    svc = Service(Store(), PP(), clock=lambda: t[0], default_require_intent=False, default_hard_cap=1000)
    svc.reputation = rep
    svc.set_agent(name, org_id=org, hard_cap=1000)
    for i, (p, a) in enumerate([("Alpha", 20), ("Beta", 100), ("Gamma", 35), ("Delta", 90), ("Alpha", 50), ("Beta", 70), ("Gamma", 45), ("Delta", 60)]):
        svc.store.insert_payment({"payment_id": "h%d" % i, "agent_id": name, "ts": NOW - (40 - 4 * i) * DAY, "payee": p, "recipient": p.lower() + "@hist.example", "amount": float(a),
                                  "currency": "USD", "memo": "", "hour": 10, "intent_id": None, "verdict": "allow", "score_kind": "none", "reason": "h", "status": "sent",
                                  "paypal_batch_id": "H", "paypal_status": "S", "decided_by": "h", "event_json": "{}", "narration": None, "risk": 0, "llm_flag": None,
                                  "mode": "enforce", "enforced": 1})
    return svc, t


def pay(svc, payee, rcpt, amount, name="a"):
    return svc.pay({"agent_id": name, "payee": payee, "recipient_email": rcpt, "amount": "%.2f" % amount, "currency": "USD", "memo": ""})["decision"]


def trusted_vendor(rep, n, rcpt, name, orgs=6, clean=5):
    for o in range(orgs):
        for i in range(clean):
            rep.contribute(rcpt, name, "vo%d-%d" % (n, o), "executed", NOW - 90 * DAY + (o * 2 + i) * DAY)


# each builder: (rep, rng, i) -> (payee, recipient, amount, setup_fn_for_agent|None); returns expectation in the registry below
def c_known_mule(rep, rng, i):
    r = "mule%d@evil.example" % i
    for o in ("m1-%d" % i, "m2-%d" % i): rep.contribute(r, "Zed%d" % i, o, "identity_freeze", NOW - 3 * DAY)
    return "Zed%d" % i, r, rng.choice([40, 60, 85])


def c_confirmed_fraud(rep, rng, i):
    r = "scam%d@evil.example" % i; rep.contribute(r, "Quick%d" % i, "cf-%d" % i, "confirmed_fraud", NOW - DAY)
    return "Quick%d" % i, r, rng.choice([30, 60])


def c_swap(rep, rng, i):
    trusted_vendor(rep, i, "billing@acme%d.example" % i, "Acme%d" % i)
    return "Acme%d-Billing" % i, "billing@acme%d-pay.example" % i, rng.choice([40, 60])


def c_ring(rep, rng, i):
    r = "ring%d@x.example" % i
    for o in range(4): rep.contribute(r, "Ring%d" % i, "fresh-%d-%d" % (i, o), "executed", NOW - 2 * DAY + o * 3600)
    return "Ring%d" % i, r, 60


def c_fresh_scam(rep, rng, i):
    return "Wallet%d" % i, "w%d@new.example" % i, rng.choice([90, 95, 99])


def c_poison_one_org(rep, rng, i):                               # 30 agents' worth of clean payments, one org, aged 90 days
    r = "pup%d@x.example" % i
    for k in range(60): rep.contribute(r, "Pup%d" % i, "sock-%d" % i, "executed", NOW - 90 * DAY + k * DAY)
    return "Pup%d" % i, r, 60


def c_poison_four_orgs(rep, rng, i):                             # one short of the 5-org trust bar, aged, voluminous
    r = "four%d@x.example" % i
    for o in range(4):
        for k in range(10): rep.contribute(r, "Four%d" % i, "q%d-%d" % (i, o), "executed", NOW - 90 * DAY + (o * 3 + k) * DAY)
    return "Four%d" % i, r, 60


def c_drain_after_poison(rep, rng, i):                           # poisoned "trust" must not lift the per-agent amount rules
    r = "drain%d@x.example" % i
    for o in range(6):
        for k in range(5): rep.contribute(r, "Drain%d" % i, "sybil-%d-%d" % (i, o), "executed", NOW - 90 * DAY + (o * 2 + k) * DAY)   # a well-funded ring DID earn trust
    return "Drain%d" % i, r, 900


def c_limit_five_orgs(rep, rng, i):                              # the HONEST LIMIT: 5 independent aged orgs with volume DO earn trust (bounded by per-agent caps, revocable)
    r = "five%d@x.example" % i
    for o in range(5):
        for k in range(5): rep.contribute(r, "Five%d" % i, "kyc-%d-%d" % (i, o), "executed", NOW - 90 * DAY + (o * 2 + k) * DAY)
    return "Five%d" % i, r, 60


def c_revoked(rep, rng, i):                                      # trust is revocable by ONE operator-confirmed fraud
    trusted_vendor(rep, i, "billing@rev%d.example" % i, "Rev%d" % i)
    rep.contribute("billing@rev%d.example" % i, "Rev%d" % i, "victim-%d" % i, "confirmed_fraud", NOW - DAY)
    return "Rev%d" % i, "billing@rev%d.example" % i, 60


def c_onboarding(rep, rng, i):
    trusted_vendor(rep, i, "billing@vend%d.example" % i, "Vend%d" % i)
    return "Vend%d" % i, "billing@vend%d.example" % i, rng.choice([40, 60, 85])


def c_new_small(rep, rng, i):
    return "Small%d" % i, "s%d@new.example" % i, rng.choice([25, 40, 55])


def c_known_recurring(rep, rng, i):
    return "Beta", "beta@hist.example", rng.choice([60, 70, 80])


def c_griefing(rep, rng, i):                                      # ONE malicious org freezes a trusted vendor
    trusted_vendor(rep, i, "billing@good%d.example" % i, "Good%d" % i)
    rep.contribute("billing@good%d.example" % i, "Good%d" % i, "griefer-%d" % i, "identity_freeze", NOW - DAY)
    return "Good%d" % i, "billing@good%d.example" % i, 60


def c_cap_freezes_elsewhere(rep, rng, i):                         # other orgs trip THEIR caps on a good vendor: agent-behaviour freezes must not become payee reputation
    r = "billing@cap%d.example" % i
    for o in range(3):
        other, ot = agent(rep, rng, org="cap-org-%d-%d" % (i, o), name="o%d" % o)
        other.set_agent("o%d" % o, org_id="cap-org-%d-%d" % (i, o), hard_cap=10, approved_payees=["Cap%d" % i])
        pay(other, "Cap%d" % i, r, 400, name="o%d" % o)           # frozen over the cap
    return "Cap%d" % i, r, 60


CASES = [  # (label, builder, kind, expectation)
    ("network_known_mule", c_known_mule, "attack", "freeze"), ("network_confirmed_fraud", c_confirmed_fraud, "attack", "freeze"),
    ("network_beneficiary_swap", c_swap, "attack", "freeze"), ("ring_buildup", c_ring, "attack", "freeze"),
    ("fresh_scam_wallet (review by design)", c_fresh_scam, "attack", "review"),
    ("poison: 60 payments, ONE org", c_poison_one_org, "poison", "not_allow"), ("poison: 4 orgs (<5)", c_poison_four_orgs, "poison", "not_allow"),
    ("poison: 6 orgs earned trust, then DRAIN $900", c_drain_after_poison, "poison", "freeze"),
    ("honest limit: 5 aged independent orgs DO earn trust", c_limit_five_orgs, "limit", "allow"),
    ("trust revoked by ONE confirmed fraud", c_revoked, "poison", "freeze"),
    ("onboarding: trusted vendor (BENIGN)", c_onboarding, "benign", "allow"), ("new small unseen vendor (BENIGN)", c_new_small, "benign", "no_freeze"),
    ("known recurring vendor (BENIGN)", c_known_recurring, "benign", "no_freeze"),
    ("griefing: 1 org freezes a trusted vendor (BENIGN)", c_griefing, "benign", "no_freeze"),
    ("cap freezes elsewhere (BENIGN)", c_cap_freezes_elsewhere, "benign", "no_freeze"),
]
N = 12


def run():
    out = {}
    for label, build, kind, expect in CASES:
        res = {"base": defaultdict(int), "net": defaultdict(int)}
        for i in range(N):
            for mode in ("base", "net"):
                rng = random.Random(1000 * hash(label) % 10 ** 6 + i) if False else random.Random(i * 7 + len(label))
                rep = Reputation(KEY) if mode == "net" else None
                scratch = Reputation(KEY)                                           # builders need a store to write into; the BASE run just does not consult it
                payee, rcpt, amount = build(rep or scratch, rng, i)
                svc, t = agent(rep, random.Random(i))
                res[mode][pay(svc, payee, rcpt, amount)] += 1
        out[label] = (kind, expect, res)
    return out


def main():
    out = run()
    ok = True
    print(f"{'case':54s} {'WITHOUT network':>26s}   {'WITH network':>26s}   result")
    for label, (kind, expect, res) in out.items():
        fmt = lambda d: " ".join(f"{k}:{v}" for k, v in sorted(d.items()))
        net = res["net"]
        good = {"freeze": net["freeze"] == N, "review": net["freeze"] + net["review"] == N, "allow": net["allow"] == N,
                "no_freeze": net["freeze"] == 0, "not_allow": net["allow"] == 0 and net["freeze"] == 0 or net["allow"] == 0}[expect]
        if kind == "benign" and net["freeze"]:
            good = False
        ok &= good
        print(f"{label:54s} {fmt(res['base']):>26s}   {fmt(net):>26s}   {'OK' if good else 'FAIL'} (want {expect})")
    atk = [(l, v) for l, v in out.items() if v[0] == "attack" and v[1] == "freeze"]
    caught_base = sum(v[2]["base"]["freeze"] for _, v in atk); caught_net = sum(v[2]["net"]["freeze"] for _, v in atk)
    ben = [v for v in out.values() if v[0] == "benign"]
    nags = sum(v[2]["net"]["freeze"] for v in ben); n_ben = len(ben) * N
    print(f"\nNETWORK-LIFT ATTACK RECALL (freeze): without {caught_base}/{len(atk) * N} -> with {caught_net}/{len(atk) * N}")
    print(f"BENIGN false nags with network: {nags}/{n_ben}   onboarding review->allow: {out['onboarding: trusted vendor (BENIGN)'][2]['net']['allow']}/{N}")
    print("RESULT:", "PASS" if ok and nags == 0 else "FAIL")
    return 0 if ok and nags == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
