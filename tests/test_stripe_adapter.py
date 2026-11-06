"""The Stripe (test mode) adapter, with a fake transport: no network, no real key."""
import hashlib
import hmac
import io
import json
import time
import unittest
import urllib.error

from app.providers.base import SendRequest, WrongProvider
from tests import close_when_done
from app.providers.stripe import config as sc
from app.providers.stripe.adapter import StripeProvider, to_minor
from app.providers.stripe.client import StripeClient, verify_signature

FAKE_KEY = "sk_test_" + "A" * 24


class Resp:
    def __init__(self, status, body):
        self.status, self._b = status, json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """urlopen stand-in: records requests, replays queued responses (a non-2xx status raises HTTPError like urllib does)."""
    def __init__(self, *responses):
        self.reqs, self.q = [], list(responses)

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        status, body = self.q.pop(0) if self.q else (200, {"id": "tr_%d" % len(self.reqs), "reversed": False})
        if status >= 400:
            raise close_when_done(urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(json.dumps(body).encode())))
        return Resp(status, body)


def prov(net=None, accounts=None, webhook_secret=""):
    net = net or FakeNet()
    return StripeProvider(StripeClient(FAKE_KEY, opener=net), webhook_secret=webhook_secret, accounts=accounts if accounts is not None else {"pool": ["acct_TESTPOOL1", "acct_TESTPOOL2"]}), net


def req(**kw):
    d = dict(provider="stripe", payment_id="pay-abc", recipient="st-vendor@st-sandbox.example", amount="12.34", currency="USD", memo="invoice", idempotency_key="pc-stripe-abc")
    d.update(kw)
    return SendRequest(**d)


class KeyGuard(unittest.TestCase):
    def test_only_test_mode_keys_are_accepted_and_the_key_never_appears_in_an_error(self):
        for bad in ("sk_live_" + "B" * 24, "rk_live_xxxxxxxxxxxx", "pk_test_xxxxxxxxxxxx", "", None, "whsec_xxxxxxxx"):
            with self.assertRaises(ValueError) as cm:
                StripeClient(bad)
            if bad:
                self.assertNotIn(str(bad), str(cm.exception))
        StripeClient(FAKE_KEY)
        StripeClient("rk_test_" + "C" * 24)

    def test_the_config_module_refuses_a_live_key_and_reads_only_its_slot(self):
        with self.assertRaises(ValueError):
            sc.build({"PAYCANARY_STRIPE_SECRET_KEY": "sk_live_" + "D" * 24})
        p = sc.build({"PAYCANARY_STRIPE_SECRET_KEY": FAKE_KEY}, accounts={})
        self.assertEqual(p.name, "stripe")
        self.assertFalse(sc.configured({"PAYCANARY_PAYPAL_CLIENT_ID": "x"}))

    def test_a_failed_call_never_leaks_the_key_or_the_provider_message(self):
        p, net = prov(FakeNet((401, {"error": {"type": "invalid_request_error", "code": "api_key_invalid", "message": "Invalid API Key provided: " + FAKE_KEY[:12] + "***"}})))
        r = p.send(req())
        self.assertFalse(r.ok)
        self.assertEqual((r.error, r.http), ("api_key_invalid", 401))
        self.assertNotIn(FAKE_KEY[:8], json.dumps(r.__dict__))
        self.assertNotIn("Invalid API Key", json.dumps(r.__dict__))


class JsonRequests(unittest.TestCase):
    def test_v2_json_requests_carry_the_version_idempotency_and_bearer_auth_without_leaking_the_key(self):
        net = FakeNet((200, {"id": "acct_V2TEST1234"}))
        c = StripeClient(FAKE_KEY, opener=net)
        st, body = c.request_json("POST", "/v2/core/accounts", {"display_name": "x"}, idempotency_key="k1", version="2026-10-28.endive")
        rq = net.reqs[0]
        self.assertEqual((st, body["id"]), (200, "acct_V2TEST1234"))
        self.assertEqual((rq.get_header("Stripe-version"), rq.get_header("Idempotency-key"), rq.get_header("Content-type")), ("2026-10-28.endive", "k1", "application/json"))
        self.assertEqual(json.loads(rq.data.decode()), {"display_name": "x"})
        self.assertTrue(rq.full_url.endswith("/v2/core/accounts"))


class Send(unittest.TestCase):
    def test_success_maps_to_a_stripe_transfer_with_minor_units_idempotency_and_metadata(self):
        p, net = prov()
        r = p.send(req())
        self.assertEqual((r.ok, r.state, r.provider), (True, "sent", "stripe"))
        self.assertTrue(r.provider_id.startswith("tr_"))
        rq = net.reqs[0]
        body = rq.data.decode()
        self.assertEqual(rq.get_header("Idempotency-key"), "pc-stripe-abc")
        self.assertIn("amount=1234", body)
        self.assertIn("currency=usd", body)
        self.assertIn("metadata%5Bpaycanary_provider%5D=stripe", body)
        self.assertIn("metadata%5Bpaycanary_payment_id%5D=pay-abc", body)
        self.assertTrue(rq.full_url.endswith("/v1/transfers"))

    def test_amounts_use_minor_units_including_zero_decimal_currencies(self):
        self.assertEqual((to_minor("12.34", "USD"), to_minor("0.005", "USD"), to_minor("1500", "JPY"), to_minor("1500.4", "JPY"), to_minor("1000000.00", "EUR")),
                         (1234, 1, 1500, 1500, 100000000))

    def test_recipient_resolution_direct_account_email_map_pool_and_fail_closed(self):
        p, net = prov(accounts={"emails": {"known@st-sandbox.example": "acct_KNOWN123"}, "pool": []})
        self.assertEqual(p.resolve("acct_DIRECT1234"), "acct_DIRECT1234")
        self.assertEqual(p.resolve("KNOWN@st-sandbox.example"), "acct_KNOWN123")
        self.assertIsNone(p.resolve("stranger@st-sandbox.example"))
        r = p.send(req(recipient="stranger@st-sandbox.example"))
        self.assertEqual((r.ok, r.error), (False, "recipient_not_onboarded"))
        self.assertEqual(net.reqs, [])                                            # nothing was sent anywhere
        q, _ = prov()
        a, b = q.resolve("x@st-sandbox.example"), q.resolve("x@st-sandbox.example")
        self.assertEqual(a, b)                                                    # deterministic pool pick

    def test_http_errors_become_code_only_failures(self):
        p, _ = prov(FakeNet((400, {"error": {"code": "balance_insufficient", "type": "invalid_request_error", "message": "..."}})))
        r = p.send(req())
        self.assertEqual((r.ok, r.state, r.error, r.http), (False, "failed", "balance_insufficient", 400))

    def test_a_reversed_or_idless_response_is_not_a_success(self):
        for body in ({"id": "tr_1", "reversed": True}, {"object": "transfer"}, {"id": "po_notatransfer"}):
            p, _ = prov(FakeNet((200, body)))
            self.assertFalse(p.send(req()).ok, body)

    def test_a_network_exception_is_contained(self):
        def boom(req, timeout=None):
            raise OSError("down")
        p = StripeProvider(StripeClient(FAKE_KEY, opener=boom), accounts={"pool": ["acct_TESTPOOL1"]})
        r = p.send(req())
        self.assertEqual((r.ok, r.error), (False, "OSError"))

    def test_refuses_a_request_for_another_provider(self):
        p, net = prov()
        with self.assertRaises(WrongProvider):
            p.send(req(provider="paypal"))
        self.assertEqual(net.reqs, [])


class StatusAndWebhooks(unittest.TestCase):
    def test_status_maps_created_and_reversed(self):
        p, _ = prov(FakeNet((200, {"id": "tr_1", "reversed": False})))
        self.assertEqual(p.status("tr_1").state, "settled")
        p, _ = prov(FakeNet((200, {"id": "tr_1", "reversed": True})))
        self.assertEqual(p.status("tr_1").state, "reversed")
        p, _ = prov(FakeNet((404, {"error": {"code": "resource_missing"}})))
        self.assertEqual(p.status("tr_nope").state, "failed")

    def sign(self, secret, body, t=None):
        t = int(t or time.time())
        return "t=%d,v1=%s" % (t, hmac.new(secret.encode(), ("%d." % t).encode() + body, hashlib.sha256).hexdigest())

    def test_signature_verification_accepts_good_rejects_bad_stale_and_malformed(self):
        body = b'{"type":"transfer.created"}'
        good = self.sign("whsec_test", body)
        self.assertTrue(verify_signature("whsec_test", good, body))
        self.assertFalse(verify_signature("whsec_other", good, body))
        self.assertFalse(verify_signature("whsec_test", good, body + b" "))
        self.assertFalse(verify_signature("whsec_test", self.sign("whsec_test", body, time.time() - 3600), body))
        for junk in ("", "garbage", "t=abc,v1=zz", "v1=abc"):
            self.assertFalse(verify_signature("whsec_test", junk, body))

    def test_webhook_events_are_normalized_and_only_verified_when_a_secret_is_configured(self):
        body = json.dumps({"type": "transfer.created", "data": {"object": {"id": "tr_77", "reversed": False}}}).encode()
        p, _ = prov(webhook_secret="whsec_test")
        ev = p.verify_webhook({"Stripe-Signature": self.sign("whsec_test", body)}, body)
        self.assertEqual((ev.provider, ev.provider_id, ev.state, ev.verified, ev.verification_configured), ("stripe", "tr_77", "settled", True, True))
        self.assertFalse(p.verify_webhook({"Stripe-Signature": "t=1,v1=00"}, body).verified)
        rev = json.dumps({"type": "transfer.reversed", "data": {"object": {"id": "tr_77"}}}).encode()
        self.assertEqual(p.verify_webhook({}, rev).state, "reversed")
        q, _ = prov()
        self.assertFalse(q.verify_webhook({}, body).verification_configured)
        self.assertTrue(q.verify_webhook({}, b"{nope").parse_error)
        other = json.dumps({"type": "charge.succeeded", "data": {"object": {"id": "ch_1"}}}).encode()
        self.assertEqual(q.verify_webhook({}, other).provider_id, "")              # only transfer ids are ever acted on


if __name__ == "__main__":
    unittest.main()
