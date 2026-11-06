#!/usr/bin/env python3
"""Cross-agent benchmark cases: the attack classes the per-agent gate CANNOT catch, plus the benign precision tests the
network must NOT make worse. Each case carries (a) the per-agent picture (is the payee new to this agent, the amount, the
agent's typical) and (b) the pooled NETWORK record for the payee. The scorer computes the per-agent base tier the normal
gate would give, then applies the reputation layer, and checks the final tier against the label.

This is deliberately separate from benchmark_corpus.jsonl (the per-agent corpus) so the network lift is measured on its own
and does not disturb the 73.7%/100%/0 per-agent numbers. Labels: an attack must end FROZEN; a benign case must NOT be
frozen (and the onboarding case must be ALLOWED, not merely review — the network must REDUCE friction there)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOW = 2_000_000.0
DAY = 86400.0


def net(**k):
    """A pooled ReputationRecord. Defaults = a payee the network has never seen."""
    base = dict(distinct_agents=0, distinct_orgs=0, first_seen_network_ts=NOW, clean_exec_bucket=0,
                freeze_count=0, distinct_orgs_froze=0, confirmed_fraud=False, mule_fanout_hits=0,
                last_activity_ts=NOW, new_orgs_in_window=0)
    base.update(k)
    return base


TRUSTED = net(distinct_agents=40, distinct_orgs=8, first_seen_network_ts=NOW - 60 * DAY, clean_exec_bucket=50)

cases = []


def case(label, *, payee, amount, agent_typical, new_payee, network, base_tier,
         name_skeleton=None, recipient_key=None, known_high_rep=None):
    """base_tier = what the per-agent gate alone returns for this payment (so the test measures the NETWORK delta on top of
    a realistic per-agent decision). For the headline cases base_tier is 'allow'/'review' — i.e. the per-agent gate would
    let it through — which is exactly why the network layer is needed."""
    cases.append({"label": label, "payee": payee, "amount": amount, "agent_typical": agent_typical,
                  "new_payee": new_payee, "network": network, "base_tier": base_tier,
                  "name_skeleton": name_skeleton, "recipient_key": recipient_key, "known_high_rep": known_high_rep or {}})


# ===== ATTACKS (must end FROZEN; per-agent base tier is allow/review — the gate alone would NOT stop them) =====
# network_known_mule: a payee two other orgs froze, paid an amount perfectly normal for THIS agent.
for i in range(6):
    case("network_known_mule", payee=f"mule-known-{i}", amount=90.0, agent_typical=90.0, new_payee=(i % 2 == 0),
         network=net(distinct_agents=4, distinct_orgs=3, distinct_orgs_froze=2, first_seen_network_ts=NOW - 20 * DAY,
                     clean_exec_bucket=3), base_tier="allow")
# confirmed-fraud elsewhere, normal-looking here
for i in range(3):
    case("network_known_mule", payee=f"fraud-{i}", amount=90.0, agent_typical=90.0, new_payee=False,
         network=net(confirmed_fraud=True, distinct_agents=5, distinct_orgs=4, clean_exec_bucket=10), base_tier="allow")
# fresh_scam_wallet: unseen across the ENTIRE network, large for this agent (per-agent might allow a new payee under ceiling)
for i in range(6):
    case("fresh_scam_wallet", payee=f"fresh-scam-{i}", amount=600.0, agent_typical=90.0, new_payee=True,
         network=net(), base_tier="allow")
# NOTE: a "network_unseen_known_payee" class (agent knows the payee, network never saw it, now large) was considered but
# NOT included: catching it needs a global network-population signal the store does not expose yet (without it, an empty
# network makes every payee "unseen" and the check would nag day-one payments). Documented as a future enhancement in
# PAYCANARY_CROSSAGENT_RESULTS.md, not faked with a case that only passes against an over-eager check.
# network_beneficiary_swap: a NEW destination wearing a network-trusted vendor's NAME
for i in range(6):
    case("network_beneficiary_swap", payee=f"AWS-Payouts-{i}", amount=90.0, agent_typical=90.0, new_payee=True,
         network=net(), base_tier="allow", name_skeleton="aws", recipient_key=f"EVIL{i}",
         known_high_rep={"aws": "AWS-REAL-KEY"})
# ring_buildup: a burst of brand-new orgs converging on one payee (the poisoning attempt itself)
for i in range(4):
    case("ring_buildup", payee=f"ring-mule-{i}", amount=90.0, agent_typical=90.0, new_payee=True,
         network=net(new_orgs_in_window=4, distinct_orgs=4, first_seen_network_ts=NOW - 2 * DAY), base_tier="allow")
# mule_fanout corroborated across orgs
for i in range(3):
    case("network_mule_fanout", payee=f"fanout-{i}", amount=90.0, agent_typical=90.0, new_payee=True,
         network=net(mule_fanout_hits=3, distinct_orgs=3), base_tier="allow")

# ===== BENIGN (must NOT be frozen; onboarding must be ALLOWED, proving the network reduces friction) =====
# trusted_vendor_onboarding: a network-trusted vendor, new to THIS agent, normal amount. Per-agent base = review (first-ever
# payee). Network must turn it into ALLOW. This is the precision/false-nag proof: the network cuts friction, not adds it.
for i in range(8):
    case("trusted_vendor_onboarding_BENIGN", payee=f"BigVendor-{i}", amount=90.0, agent_typical=90.0, new_payee=True,
         network=TRUSTED, base_tier="review")
# legit_new_small_vendor: genuinely new to BOTH the agent and the network, NORMAL amount. Must NOT be frozen or newly
# reviewed by the network (the one thing the design forbids). Per-agent base = review (new payee); network must leave it.
for i in range(8):
    case("legit_new_small_vendor_BENIGN", payee=f"newvendor-{i}", amount=80.0, agent_typical=90.0, new_payee=True,
         network=net(), base_tier="review")
# known_good_recurring: a payee this agent already pays, also broadly trusted on the network, normal amount -> allow stays allow
for i in range(6):
    case("known_good_recurring_BENIGN", payee=f"recurring-{i}", amount=90.0, agent_typical=90.0, new_payee=False,
         network=TRUSTED, base_tier="allow")

(HERE / "crossagent_corpus.json").write_text(json.dumps(cases, indent=0))
from collections import Counter
by = Counter(c["label"] for c in cases)
n_atk = sum(v for k, v in by.items() if not k.endswith("BENIGN"))
n_ben = sum(v for k, v in by.items() if k.endswith("BENIGN"))
print(f"cross-agent cases: {len(cases)}  attack: {n_atk}  benign: {n_ben}")
print("by class:", dict(by))
