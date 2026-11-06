"""The same behavioural checks run once per provider. 'Provider-agnostic' is only a claim until every provider passes the identical suite:
frozen payments never reach the provider, an idempotent replay creates one provider object, a failing provider fails closed (never 'sent'),
webhooks settle only their own payments, and the gate's verdicts do not depend on which provider is behind it (whole benchmark corpus)."""
import json
import unittest

from app.providers.base import Provider, SendResult
from app.providers.mock import fixtures as mock_fx
from app.providers.mock.adapter import MockProvider
from app.providers.paypal import fixtures as pp_fx
from app.providers.stripe import fixtures as st_fx
from app.providers.stripe.adapter import StripeProvider
from app.providers.paypal.adapter import PayPalProvider
from app.service import Service
from app.store import Store
from tests.test_providers_routing import FakeClient

NOW = 1_800_000_000.0


def mk_mock(fail=False):
    return MockProvider(fail="PROVIDER_DOWN" if fail else None), mock_fx


def mk_paypal(fail=False):
    return PayPalProvider(FakeClient(fail)), pp_fx


class FakeStripeClient:
    """Stands in for StripeClient (no network): records transfers, honours Idempotency-Key like Stripe does, can be made to fail."""
    def __init__(self, fail=False):
        self.calls, self.fail, self._by_key = [], fail, {}

    def create_transfer(self, amount_minor, currency, destination, description="", metadata=None, idempotency_key=None):
        if self.fail:
            return 402, {"error": {"code": "balance_insufficient", "type": "invalid_request_error"}}
        if idempotency_key in self._by_key:
            return 200, self._by_key[idempotency_key]
        self.calls.append({"amount": amount_minor, "currency": currency, "destination": destination, "metadata": metadata, "key": idempotency_key})
        body = {"id": "tr_%06d" % len(self.calls), "reversed": False}
        self._by_key[idempotency_key] = body
        return 200, body

    def get_transfer(self, tid):
        return 200, {"id": tid, "reversed": False}


def mk_stripe(fail=False):
    return StripeProvider(FakeStripeClient(fail), accounts={"pool": ["acct_TESTPOOL1", "acct_TESTPOOL2"]}), st_fx


MAKERS = {"mock": mk_mock, "paypal": mk_paypal, "stripe": mk_stripe}


def calls(p):
    return p.n if hasattr(p, "n") else len(p.client.calls)


def fresh(name, fail=False, cap=1000):
    prov, fx = MAKERS[name](fail)
    svc = Service(Store(), prov, clock=lambda: NOW, default_require_intent=False)
    svc.dev_webhooks = True
    svc.set_agent("agent", hard_cap=cap)
    return svc, prov, fx


def pay(svc, fx, payee="Vendor", amount="20.00", **kw):
    return svc.pay({"agent_id": "agent", "payee": payee, "recipient_email": fx.recipient(payee), "amount": amount, "currency": "USD", "memo": "m"}, **kw)


class Matrix(unittest.TestCase):
    def each(self):
        for name in MAKERS:
            with self.subTest(provider=name):
                yield name

    def test_frozen_payments_never_reach_the_provider(self):
        for name in self.each():
            svc, prov, fx = fresh(name)
            ok = pay(svc, fx)                                         # review: proceeds
            over = pay(svc, fx, "Big", "5000.00")                     # over the cap: frozen
            self.assertEqual((ok["status"], over["status"]), ("sent", "frozen"))
            self.assertEqual(calls(prov), 1)                           # exactly the payment the gate let through

    def test_idempotent_replay_creates_one_provider_object(self):
        for name in self.each():
            svc, prov, fx = fresh(name)
            a = pay(svc, fx, request_id="order-1")
            b = pay(svc, fx, request_id="order-1")
            self.assertEqual({k: a[k] for k in ("payment_id", "status", "provider")}, {k: b[k] for k in ("payment_id", "status", "provider")})
            self.assertEqual(calls(prov), 1)

    def test_a_failing_provider_fails_closed_and_is_recorded_never_sent(self):
        for name in self.each():
            svc, prov, fx = fresh(name, fail=True)
            out = pay(svc, fx)
            self.assertEqual(out["status"], "paypal_error" if name == "paypal" else "provider_error")                 # statuses name their provider family
            self.assertNotEqual(svc.store.payment(out["payment_id"])["status"], "sent")
            ev = [json.loads(r["detail"]) for r in svc.audit.rows(0, 100, "provider_result")][-1]
            self.assertEqual((ev["provider"], ev["status"]), (name, out["status"]))
            self.assertIsNone(svc.store.payment(out["payment_id"])["provider_ref"])

    def test_a_provider_that_raises_is_contained(self):
        class Boom(Provider):
            name = "boom"

            def send(self, req):
                raise RuntimeError("adapter bug")
        svc = Service(Store(), Boom(), clock=lambda: NOW, default_require_intent=False)
        svc.set_agent("agent", hard_cap=1000)
        out = svc.pay({"agent_id": "agent", "payee": "V", "recipient_email": "v@x.example", "amount": "20.00", "currency": "USD", "memo": ""})
        self.assertEqual((out["status"], out["provider"]), ("provider_error", "boom"))

    def test_webhook_settles_the_payment_through_its_own_provider_only(self):
        for name in self.each():
            svc, prov, fx = fresh(name)
            out = pay(svc, fx)
            row = svc.store.payment(out["payment_id"])
            raw_id = row["provider_ref"].split(":", 1)[1]
            body = {"mock": lambda: json.dumps({"event_type": "x", "provider_id": raw_id, "state": "settled"}),
                    "paypal": lambda: json.dumps({"event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS", "resource": {"batch_header": {"payout_batch_id": raw_id, "batch_status": "SUCCESS"}}}),
                    "stripe": lambda: json.dumps({"type": "transfer.created", "data": {"object": {"id": raw_id, "reversed": False}}})}[name]().encode()
            self.assertTrue(svc.handle_webhook({}, body, provider=name)["ok"])
            self.assertEqual(svc.store.payment(out["payment_id"])["status"], "settled")

    def test_status_query_through_the_interface(self):
        svc, prov, fx = fresh("mock")
        out = pay(svc, fx)
        raw = svc.store.payment(out["payment_id"])["provider_ref"].split(":", 1)[1]
        self.assertEqual(prov.status(raw).state, "sent")
        self.assertEqual(prov.status("MOCK-999999").state, "failed")

    def test_the_whole_benchmark_gets_identical_verdicts_on_every_provider_and_no_frozen_payment_reaches_one(self):
        from tools import score_providers as SP
        direct, out = SP.run_all()
        for name, (res, st) in out.items():
            self.assertEqual(sum(direct[t][0] == v[0] for t, v in res.items()), len(res), name)       # 371/371, same as the direct gate
            self.assertEqual(st["provider_calls"], st["sent"], name)                                  # provider saw exactly the payments the gate allowed
            self.assertGreater(st["frozen"], 100, name)
        runs = [{t: v[0] for t, v in out[n][0].items()} for n in out]
        self.assertEqual(len(runs), len(MAKERS))
        self.assertTrue(all(r == runs[0] for r in runs))                                              # and identical across all providers


if __name__ == "__main__":
    unittest.main()
