"""The mock provider: records "would have sent $X to Y" in a ledger and returns deterministic fake ids. For volume and the attacker corpus through the RULES
with zero API calls, and for the demo. It can be told to fail (to prove fail-closed) and to emit webhook-shaped events. Nothing here talks to a network."""
import json
import threading

from ..base import Provider, SendResult, StatusResult, WebhookEvent


class MockProvider(Provider):
    name = "mock"
    id_prefix = "MOCK"

    def __init__(self, fail=None):
        self.ledger = []                                          # [{"provider_id", "payment_id", "recipient", "amount", "currency", "memo", "idempotency_key"}]
        self._by_key = {}                                         # idempotency_key -> provider_id (the same key never creates a second ledger entry)
        self._state = {}
        self.fail = fail                                          # None | callable(req)->error-name or None | an error-name string (fail every send)
        self._lock = threading.Lock()

    @property
    def n(self):
        return len(self.ledger)

    def send(self, req):
        self._mine(req)
        with self._lock:
            if req.idempotency_key in self._by_key:               # replay: same provider object, no second send
                pid = self._by_key[req.idempotency_key]
                return SendResult(self.name, True, "sent", provider_id=pid, raw_status="SUCCESS", detail={"replayed": True})
            err = self.fail(req) if callable(self.fail) else self.fail
            if err:
                return SendResult(self.name, False, "failed", error=str(err), http=503)
            pid = "%s-%06d" % (self.id_prefix, len(self.ledger) + 1)
            self.ledger.append({"provider_id": pid, "payment_id": req.payment_id, "recipient": req.recipient, "amount": req.amount,
                                "currency": req.currency, "memo": req.memo, "idempotency_key": req.idempotency_key})
            self._by_key[req.idempotency_key] = pid
            self._state[pid] = "sent"
            return SendResult(self.name, True, "sent", provider_id=pid, raw_status="SUCCESS")

    def status(self, provider_id):
        return StatusResult(self.name, provider_id, self._state.get(provider_id, "failed"), "SUCCESS" if provider_id in self._state else "UNKNOWN")

    def verify_webhook(self, headers, body):
        """Mock events are shaped {"event_type", "provider_id", "state"} and are always 'verified' (there is nothing to verify against)."""
        try:
            e = json.loads(body.decode() or "{}")
            if not isinstance(e, dict):
                raise ValueError("webhook body must be a JSON object")
        except ValueError:
            return WebhookEvent(self.name, "", parse_error=True)
        return WebhookEvent(self.name, str(e.get("event_type", ""))[:80], provider_id=str(e.get("provider_id", "")), state=e.get("state"),
                            raw_status=str(e.get("state") or ""), verified=True, verification_configured=True, raw=body.decode()[:20000])


class SimulatedRail(MockProvider):
    """DEMO ONLY (offline dashboard): a mock ledger that carries a rail name ("paypal" / "stripe") so the multi-provider dashboard has both rails to show.
    Nothing is sent anywhere; every id is labelled SIM-<CODE>-NNNNNN and the dashboard says payouts are simulated. Never built by a normal run."""

    def __init__(self, rail, code):
        super().__init__()
        self.name, self.id_prefix = rail, "SIM-" + code
