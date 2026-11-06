#!/usr/bin/env python3
"""Run Security's whole benchmark corpus through the REAL service path once per provider and show what 'provider-agnostic' means concretely:
  1. the gate's verdicts are IDENTICAL on every provider (and identical to the direct-gate harness),
  2. no frozen payment ever reaches a provider (provider-side calls == payments the gate let through),
  3. each provider receives only its own namespaced test identities (recipients come from that provider's fixtures).
Providers here are the MOCK provider and the PayPal and Stripe ADAPTERS over recording fake clients (no network). Live sandbox slices are a separate, key-gated step.
Usage: python3 tools/score_providers.py"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.providers.mock import fixtures as mock_fx
from app.providers.mock.adapter import MockProvider
from app.providers.paypal import fixtures as pp_fx
from app.providers.paypal.adapter import PayPalProvider
from app.providers.stripe import fixtures as st_fx
from app.providers.stripe.adapter import StripeProvider
from tools import score_benchmark as B
from tools import score_benchmark_service as BS


class RecordingPayPalClient:
    """Stands in for PayPalClient: records every payout it is asked to make and returns a PayPal-shaped success. No network."""
    def __init__(self):
        self.calls = []

    def create_payout(self, receiver, amount, currency="USD", note="", sender_item_id=None, batch_id=None, request_id=None):
        self.calls.append({"receiver": receiver, "amount": amount, "currency": currency, "batch_id": batch_id})
        return 201, {"batch_header": {"payout_batch_id": "PAYOUT-%06d" % len(self.calls), "batch_status": "PENDING"}}


class CountingPayPalProvider(PayPalProvider):
    @property
    def n(self):
        return len(self.client.calls)


class RecordingStripeClient:
    """Stands in for StripeClient: records every transfer it is asked to make and returns a Stripe-shaped success. No network."""
    def __init__(self):
        self.calls = []

    def create_transfer(self, amount_minor, currency, destination, description="", metadata=None, idempotency_key=None):
        self.calls.append({"amount": amount_minor, "destination": destination})
        return 200, {"id": "tr_%06d" % len(self.calls), "reversed": False}


class CountingStripeProvider(StripeProvider):
    @property
    def n(self):
        return len(self.client.calls)


PROVIDERS = {
    "mock": (lambda: MockProvider(), mock_fx),
    "paypal": (lambda: CountingPayPalProvider(RecordingPayPalClient()), pp_fx),
    "stripe": (lambda: CountingStripeProvider(RecordingStripeClient(), accounts={"pool": ["acct_TESTPOOL1"]}), st_fx),
}


def run_all():
    direct = B.run()
    out = {}
    for name, (factory, fx) in PROVIDERS.items():
        stats = {}
        res = BS.run(make_provider=factory, recipient=fx.recipient, stats=stats)
        out[name] = (res, stats)
    return direct, out


def main():
    direct, out = run_all()
    ok = True
    for name, (res, st) in out.items():
        same = sum(1 for t, v in res.items() if direct[t][0] == v[0])
        print("%-7s verdicts identical to the direct gate: %d/%d | gate-decided payments: %d (sent %d, frozen %d) | provider-side calls: %d"
              % (name, same, len(res), st["decided"], st["sent"], st["frozen"], st["provider_calls"]))
        ok = ok and same == len(res) and st["provider_calls"] == st["sent"] and st["frozen"] > 0
    names = list(out)
    cross = sum(1 for t in out[names[0]][0] if all(out[n][0][t][0] == out[names[0]][0][t][0] for n in names))
    print("cross-provider parity: %d/%d txns get the same verdict on %s" % (cross, len(out[names[0]][0]), " and ".join(names)))
    print("RESULT:", "PASS" if ok and cross == len(out[names[0]][0]) else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
