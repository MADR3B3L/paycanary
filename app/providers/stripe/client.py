"""Stripe REST client (TEST MODE ONLY): the two primitives PayCanary needs for a money-out test, Transfers to a connected account and reading one back, plus the
webhook signature check. Stdlib only. The client refuses any key that is not a test-mode key (sk_test_ / rk_test_), and never puts the key, or any part of
it, into an error, a log line or a return value."""
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.stripe.com"
TEST_PREFIXES = ("sk_test_", "rk_test_")


class StripeError(Exception):
    def __init__(self, status, code):
        super().__init__("Stripe HTTP %s %s" % (status, code))
        self.status, self.code = status, code


def _flatten(params, prefix=""):
    """Stripe's form encoding: nested dicts become metadata[key]=value."""
    out = []
    for k, v in (params or {}).items():
        key = "%s[%s]" % (prefix, k) if prefix else str(k)
        if isinstance(v, dict):
            out += _flatten(v, key)
        elif v is not None:
            out.append((key, "true" if v is True else "false" if v is False else str(v)))
    return out


class StripeClient:
    def __init__(self, secret_key, opener=None, timeout=25.0):
        if not isinstance(secret_key, str) or not secret_key.startswith(TEST_PREFIXES):
            raise ValueError("PayCanary only talks to Stripe in TEST mode: the key must be a test-mode key (sk_test_... or rk_test_...)")
        self._key = secret_key
        self._open = opener or urllib.request.urlopen
        self.timeout = float(timeout)

    def request(self, method, path, params=None, idempotency_key=None):
        data = urllib.parse.urlencode(_flatten(params)).encode() if params and method != "GET" else None
        url = BASE + path + ("?" + urllib.parse.urlencode(_flatten(params)) if params and method == "GET" else "")
        headers = {"Authorization": "Basic " + base64.b64encode((self._key + ":").encode()).decode(), "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return resp.status, _json(resp.read().decode() or "{}")
        except urllib.error.HTTPError as err:
            return err.code, _json(err.read().decode() or "{}")

    def request_json(self, method, path, body=None, idempotency_key=None, version=None):
        """JSON request (Stripe's v2 APIs, e.g. /v2/core/accounts). Same auth, same test-key-only guard, same never-leak-the-key rule."""
        headers = {"Authorization": "Bearer " + self._key, "Accept": "application/json", "Content-Type": "application/json"}
        if version:
            headers["Stripe-Version"] = version
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return resp.status, _json(resp.read().decode() or "{}")
        except urllib.error.HTTPError as err:
            return err.code, _json(err.read().decode() or "{}")

    def create_transfer(self, amount_minor, currency, destination, description="", metadata=None, idempotency_key=None):
        return self.request("POST", "/v1/transfers", {"amount": int(amount_minor), "currency": currency.lower(), "destination": destination,
                                                       "description": description[:100], "metadata": metadata or {}}, idempotency_key)

    def get_transfer(self, transfer_id):
        return self.request("GET", "/v1/transfers/" + urllib.parse.quote(str(transfer_id), safe=""))


def _json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw[:200]}


def error_code(body):
    """The Stripe error CODE/TYPE only. Never the message (it can echo part of an API key)."""
    e = body.get("error") if isinstance(body, dict) else None
    return (e.get("code") or e.get("type") or "error") if isinstance(e, dict) else "error"


def verify_signature(secret, header, payload, tolerance=300, now=None):
    """Stripe-Signature: t=<unix>,v1=<hex hmac_sha256(secret, "<t>.<payload>")>. Constant-time compare; rejects stale timestamps."""
    items = [tuple(p.strip().split("=", 1)) for p in (header or "").split(",") if "=" in p]
    ts = [v for k, v in items if k == "t"]
    sigs = [v for k, v in items if k == "v1"]
    try:
        t = int(ts[0])
    except (IndexError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - t) > tolerance:
        return False
    want = hmac.new(secret.encode(), ("%d." % t).encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, s) for s in sigs)
