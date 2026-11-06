"""PayPal REST client (SANDBOX ONLY) — OAuth client-credentials token with cache + refresh, JSON requests with
idempotency ids, and the two money-out primitives we use: Payouts (primary) and Orders (fallback).
Stdlib only. The client refuses to run against anything but the sandbox. Secrets are never logged or returned."""
import base64
import json
import time
import urllib.error
import urllib.request
import uuid

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    def __init__(self, status, body):
        super().__init__(f"PayPal HTTP {status}")
        self.status = status
        self.body = body


class PayPalClient:
    def __init__(self, client_id, client_secret, env="sandbox", opener=None, clock=time.time, timeout=25.0):
        self.timeout = float(timeout)                     # PayPal-facing timeout; the AGENT's client timeout toward PayCanary must be LONGER (see README)
        if env != "sandbox":
            raise ValueError("PayCanary only talks to the PayPal SANDBOX (PAYPAL_ENV must be 'sandbox')")
        if not client_id or not client_secret:
            raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are required")
        self._id, self._secret = client_id, client_secret
        self._open = opener or urllib.request.urlopen
        self._clock = clock
        self._token, self._expires_at, self.scope = None, 0.0, ""

    # --- OAuth -----------------------------------------------------------------------------------------------
    def token(self, force=False):
        if not force and self._token and self._clock() < self._expires_at - 300:
            return self._token
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        req = urllib.request.Request(
            SANDBOX_BASE + "/v1/oauth2/token", data=b"grant_type=client_credentials", method="POST",
            headers={"Authorization": "Basic " + basic, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"})
        status, body = self._send(req)
        if status != 200 or "access_token" not in body:
            raise PayPalError(status, redact(body))
        self._token = body["access_token"]
        self._expires_at = self._clock() + float(body.get("expires_in", 3600))
        self.scope = str(body.get("scope", ""))
        return self._token

    def _send(self, req):
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode() or "{}"
                return resp.status, _json(raw)
        except urllib.error.HTTPError as err:
            return err.code, _json(err.read().decode() or "{}")

    # --- Generic JSON call (retries once on 401 with a fresh token) -------------------------------------------
    def request(self, method, path, body=None, request_id=None):
        for attempt in (0, 1):
            headers = {"Authorization": "Bearer " + self.token(force=bool(attempt)), "Accept": "application/json",
                       "Content-Type": "application/json", "PayPal-Request-Id": request_id or str(uuid.uuid4())}
            data = json.dumps(body).encode() if body is not None else None
            status, resp = self._send(urllib.request.Request(SANDBOX_BASE + path, data=data, method=method, headers=headers))
            if status == 401 and attempt == 0:
                continue
            return status, resp
        return status, resp

    # --- Money-out primitives ---------------------------------------------------------------------------------
    def create_payout(self, receiver_email, amount, currency="USD", note="", sender_item_id=None, batch_id=None, request_id=None):
        """Payouts: one item to an email recipient. amount is a decimal string like '1.00'."""
        batch_id = batch_id or "pc-" + uuid.uuid4().hex[:20]
        body = {"sender_batch_header": {"sender_batch_id": batch_id, "email_subject": "PayCanary sandbox payout"},
                "items": [{"recipient_type": "EMAIL", "receiver": receiver_email,
                           "amount": {"value": str(amount), "currency": currency}, "note": note[:80],
                           "sender_item_id": sender_item_id or "item-" + uuid.uuid4().hex[:16]}]}
        return self.request("POST", "/v1/payments/payouts", body, request_id=request_id or batch_id)

    def get_payout(self, batch_id):
        return self.request("GET", f"/v1/payments/payouts/{batch_id}")

    def create_order(self, amount, currency="USD", description="", request_id=None):
        """Orders (fallback 'agent pays' primitive): create a CAPTURE order. A buyer must approve before capture."""
        body = {"intent": "CAPTURE", "purchase_units": [{"description": description[:120],
                "amount": {"currency_code": currency, "value": str(amount)}}]}
        return self.request("POST", "/v2/checkout/orders", body, request_id=request_id)


def _json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw[:300]}


def redact(body):
    """Drop anything that looks like a credential before it can reach a log or an error."""
    if isinstance(body, dict):
        return {k: ("[redacted]" if k in {"access_token", "client_secret", "secret", "id_token"} else redact(v)) for k, v in body.items()}
    if isinstance(body, list):
        return [redact(v) for v in body]
    return body
