"""A payment routed to provider A can never be processed by provider B: routing is fixed on the payment row at creation, dispatch is exact-name with no
fallback, adapters refuse a request addressed to someone else, and webhooks only touch their own provider's payments."""
import json
import unittest

from app.providers.base import SendRequest, UnknownProvider, WrongProvider
from app.providers.mock.adapter import MockProvider
from app.providers.paypal.adapter import PayPalProvider
from app.providers.registry import Registry
from app.service import Service
from app.store import Store

NOW = 1_800_000_000.0


class FakeClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def create_payout(self, receiver, amount, currency="USD", note="", sender_item_id=None, batch_id=None, request_id=None):
        self.calls.append(batch_id)
        if self.fail:
            return 400, {"name": "INSUFFICIENT_FUNDS"}
        return 201, {"batch_header": {"payout_batch_id": "PAYOUT-%d" % len(self.calls), "batch_status": "PENDING"}}


def make(**kw):
    client, mock = FakeClient(kw.pop("fail", False)), MockProvider()
    svc = Service(Store(), None, providers=Registry(PayPalProvider(client), mock), clock=lambda: NOW, default_require_intent=False, **kw)
    svc.set_agent("a-pp", hard_cap=1000)
    svc.set_agent("a-mock", hard_cap=1000)
    svc.assign_provider("a-mock", "mock")
    return svc, client, mock


def pay(svc, agent, payee="Vendor", amount="20.00", **kw):
    return svc.pay({"agent_id": agent, "payee": payee, "recipient_email": payee.lower() + "@example.com", "amount": amount, "currency": "USD", "memo": "m"}, **kw)


class Routing(unittest.TestCase):
    def test_each_agent_is_routed_to_its_own_provider_and_never_the_other(self):
        svc, client, mock = make()
        a, b = pay(svc, "a-pp"), pay(svc, "a-mock", "Other")
        self.assertEqual((a["provider"], a["status"]), ("paypal", "sent"))
        self.assertEqual((b["provider"], b["status"]), ("mock", "sent"))
        self.assertEqual(len(client.calls), 1)                     # PayPal saw only the PayPal-routed payment
        self.assertEqual([e["payment_id"] for e in mock.ledger], [b["payment_id"]])
        ra, rb = svc.store.payment(a["payment_id"]), svc.store.payment(b["payment_id"])
        self.assertTrue(ra["provider_ref"].startswith("paypal:") and rb["provider_ref"].startswith("mock:"))
        self.assertTrue(ra["paypal_batch_id"] and rb["paypal_batch_id"] is None)               # legacy PayPal columns are PayPal-only

    def test_idempotency_keys_are_provider_scoped(self):
        svc, client, mock = make()
        pay(svc, "a-pp"); pay(svc, "a-mock", "Other")
        self.assertTrue(client.calls[0].startswith("pc-paypal-"))
        self.assertTrue(mock.ledger[0]["idempotency_key"].startswith("pc-mock-"))

    def test_a_payments_provider_is_fixed_at_creation_even_if_the_agent_is_rerouted_afterwards(self):
        svc, client, mock = make()
        held = pay(svc, "a-pp", "Big", "5000.00")                                  # over the $1000 cap: frozen, routed to paypal
        self.assertEqual(held["status"], "frozen")
        svc.assign_provider("a-pp", "mock")                                         # operator re-routes the agent
        svc.store.update_payment(held["payment_id"], status="frozen")
        svc.set_agent("a-pp", hard_cap=100000, require_intent=False)                # (set_agent keeps the routing)
        res = svc.release(held["payment_id"])
        self.assertEqual(res["provider"], "paypal")                                 # released through the provider it was created with
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(mock.n, 0)

    def test_update_payment_refuses_to_rewrite_the_provider(self):
        svc, *_ = make()
        out = pay(svc, "a-pp")
        with self.assertRaises(ValueError):
            svc.store.update_payment(out["payment_id"], provider="mock")

    def test_set_agent_and_modify_agent_keep_the_routing(self):
        svc, *_ = make()
        svc.set_agent("a-mock", hard_cap=500)
        svc.modify_agent("a-mock", approve=["X"])
        self.assertEqual(svc.store.agent("a-mock")["provider"], "mock")

    def test_unknown_provider_fails_closed_with_no_fallback_to_another_adapter(self):
        svc, client, mock = make()
        svc.set_agent("ghost", hard_cap=1000)
        cfg = svc.store.agent("ghost"); cfg["provider"] = "stripe"; svc.store.set_agent("ghost", cfg)     # a provider nobody registered
        out = pay(svc, "ghost")
        self.assertEqual((out["status"], out["provider"]), ("provider_error", "stripe"))
        self.assertEqual(len(client.calls), 0)
        self.assertEqual(mock.n, 0)                                                 # nothing was diverted to the other adapters
        with self.assertRaises(Exception):
            svc.assign_provider("ghost", "stripe")                                  # cannot route to an unregistered provider

    def test_an_adapter_refuses_a_request_addressed_to_another_provider(self):
        mock, pp = MockProvider(), PayPalProvider(FakeClient())
        req_for_paypal = SendRequest("paypal", "pay-1", "a@b.c", "1.00", "USD", "m", "k")
        req_for_mock = SendRequest("mock", "pay-2", "a@b.c", "1.00", "USD", "m", "k2")
        with self.assertRaises(WrongProvider):
            mock.send(req_for_paypal)
        with self.assertRaises(WrongProvider):
            pp.send(req_for_mock)
        self.assertEqual(mock.n, 0)

    def test_the_registry_has_exact_dispatch_only(self):
        reg = Registry(MockProvider())
        self.assertEqual(reg.get("mock").name, "mock")
        for bad in ("Mock", "mock ", "mo", None, "paypal"):
            with self.assertRaises(UnknownProvider):
                reg.get(bad)
        with self.assertRaises(ValueError):
            reg.register(MockProvider())                                            # no silent replacement of a registered name

    def test_webhooks_only_touch_payments_of_their_own_provider(self):
        svc, client, mock = make()
        svc.dev_webhooks = True
        a, b = pay(svc, "a-pp"), pay(svc, "a-mock", "Other")
        pp_id = svc.store.payment(a["payment_id"])["paypal_batch_id"]
        mock_id = mock.ledger[0]["provider_id"]
        # a mock-shaped event carrying the PAYPAL payment's id must not move the PayPal payment
        ev = json.dumps({"event_type": "x", "provider_id": pp_id, "state": "settled"}).encode()
        self.assertTrue(svc.handle_webhook({}, ev, provider="mock")["ok"])
        self.assertEqual(svc.store.payment(a["payment_id"])["status"], "sent")
        # ...and the matching mock event settles the mock payment
        svc.handle_webhook({}, json.dumps({"event_type": "x", "provider_id": mock_id, "state": "settled"}).encode(), provider="mock")
        self.assertEqual(svc.store.payment(b["payment_id"])["status"], "settled")
        # a PayPal-shaped event for the PayPal payment settles only it (dev webhooks, no verification configured)
        pev = json.dumps({"event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS", "resource": {"batch_header": {"payout_batch_id": pp_id, "batch_status": "SUCCESS"}}}).encode()
        svc.handle_webhook({}, pev, provider="paypal")
        self.assertEqual(svc.store.payment(a["payment_id"])["status"], "settled")
        self.assertFalse(svc.handle_webhook({}, b"{}", provider="nope")["ok"])        # an unknown provider route is refused

    def test_every_audit_record_of_a_send_names_its_provider(self):
        svc, *_ = make()
        pay(svc, "a-pp"); pay(svc, "a-mock", "Other")
        rows = [json.loads(r["detail"]) for r in svc.audit.rows(0, 500, "provider_result")]
        self.assertEqual(sorted(r["provider"] for r in rows), ["mock", "paypal"])
        self.assertTrue(svc.audit.verify())

    def test_assigning_a_provider_is_audited(self):
        svc, *_ = make()
        d = json.loads(svc.audit.rows(0, 500, "config_changed")[-1]["detail"])
        self.assertEqual(d["provider_assigned"], "mock")


if __name__ == "__main__":
    unittest.main()
