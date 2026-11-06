import json, unittest, urllib.error, io
from app.paypal import PayPalClient, PayPalError, redact
from tests import close_when_done


class FakeResp:
    def __init__(self, status, body): self.status, self._b = status, json.dumps(body).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Net:
    """Scripted opener: pops (status, body) per call and records requests."""
    def __init__(self, script): self.script, self.calls = list(script), []
    def __call__(self, req, timeout=0):
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        status, body = self.script.pop(0)
        if status >= 400:
            raise close_when_done(urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(json.dumps(body).encode())))
        return FakeResp(status, body)


TOK = (200, {"access_token": "T1", "expires_in": 32400, "scope": "https://uri.paypal.com/services/payments/payouts"})


class T(unittest.TestCase):
    def test_refuses_non_sandbox(self):
        with self.assertRaises(ValueError): PayPalClient("a", "b", env="live")

    def test_requires_credentials(self):
        with self.assertRaises(ValueError): PayPalClient("", "")

    def test_token_cached_then_refreshed(self):
        now = [1000.0]; net = Net([TOK, TOK])
        c = PayPalClient("id", "sec", opener=net, clock=lambda: now[0])
        self.assertEqual(c.token(), "T1"); self.assertEqual(c.token(), "T1"); self.assertEqual(len(net.calls), 1)
        now[0] += 32400                      # past expiry minus margin
        c.token(); self.assertEqual(len(net.calls), 2)
        self.assertIn("payouts", c.scope.lower())

    def test_401_triggers_one_refresh_and_retry(self):
        net = Net([TOK, (401, {"name": "AUTHENTICATION_FAILURE"}), TOK, (200, {"ok": True})])
        c = PayPalClient("id", "sec", opener=net)
        status, body = c.request("GET", "/v1/x")
        self.assertEqual((status, body), (200, {"ok": True})); self.assertEqual(len(net.calls), 4)

    def test_token_failure_raises_without_leaking_secret(self):
        net = Net([(401, {"error": "invalid_client", "error_description": "Client Authentication failed"})])
        c = PayPalClient("id", "SUPERSECRET", opener=net)
        with self.assertRaises(PayPalError) as cm: c.token()
        self.assertNotIn("SUPERSECRET", str(cm.exception.body))

    def test_payout_body_and_idempotency_header(self):
        net = Net([TOK, (201, {"batch_header": {"payout_batch_id": "B1"}})])
        c = PayPalClient("id", "sec", opener=net)
        status, body = c.create_payout("a@b.co", "1.00", note="hi", batch_id="batch1")
        method, url, headers, data = net.calls[1]
        self.assertEqual((method, url.endswith("/v1/payments/payouts")), ("POST", True))
        sent = json.loads(data); self.assertEqual(sent["items"][0]["receiver"], "a@b.co"); self.assertEqual(sent["items"][0]["amount"], {"value": "1.00", "currency": "USD"})
        self.assertEqual(headers.get("Paypal-request-id"), "batch1")

    def test_redact(self):
        self.assertEqual(redact({"access_token": "x", "a": [{"client_secret": "y", "b": 1}]}), {"access_token": "[redacted]", "a": [{"client_secret": "[redacted]", "b": 1}]})


if __name__ == "__main__":
    unittest.main()
