"""PayPal behind the Provider interface. Wraps the existing PayPalClient unchanged: Payouts for send/status, the verify-webhook-signature API for webhooks.
Failure handling is exactly what the service did inline before the seam existed (fail closed, never pretend it was sent)."""
import json

from ..base import Provider, SendResult, StatusResult, WebhookEvent


class PayPalProvider(Provider):
    name = "paypal"

    def __init__(self, client, webhook_id=""):
        self.client, self.webhook_id = client, webhook_id

    def send(self, req):
        self._mine(req)
        try:
            status, body = self.client.create_payout(req.recipient, req.amount, req.currency, note=req.memo, batch_id=req.idempotency_key, sender_item_id=req.payment_id)
        except Exception as err:
            # The request may have left and been accepted before the response was lost (timeout, reset, auth/network failure):
            # the outcome is UNKNOWN, not a confirmed non-send. Reserve it against the cap until reconciliation resolves it.
            return SendResult("paypal", False, "in_doubt", error=type(err).__name__)
        hdr = body.get("batch_header", {}) if isinstance(body, dict) else {}
        if status in (200, 201) and hdr.get("payout_batch_id"):
            return SendResult("paypal", True, "sent", provider_id=hdr["payout_batch_id"], raw_status=hdr.get("batch_status"), http=status)
        # A 5xx (or any non-4xx without a batch id) means the server may still have created the payout: outcome unknown -> in_doubt.
        # A 4xx is a client-side rejection PayPal recorded without moving money: a confirmed non-send -> failed.
        if isinstance(status, int) and 400 <= status < 500:
            return SendResult("paypal", False, "failed", http=status, error=(body.get("name") if isinstance(body, dict) else None) or "http_%s" % status)
        return SendResult("paypal", False, "in_doubt", http=status, error=(body.get("name") if isinstance(body, dict) else None) or "http_%s" % status)

    def status(self, provider_id):
        status, body = self.client.get_payout(provider_id)
        raw = ((body.get("batch_header") or {}).get("batch_status") if isinstance(body, dict) else None) or str(status)
        state = "settled" if str(raw).upper() in ("SUCCESS", "SUCCEEDED") else ("failed" if str(raw).upper() in ("DENIED", "CANCELED", "FAILED") else "pending")
        return StatusResult("paypal", provider_id, state, raw)

    def verify_webhook(self, headers, body):
        try:
            event = json.loads(body.decode() or "{}")
        except ValueError:
            return WebhookEvent("paypal", "", parse_error=True)
        res = event.get("resource") or {}
        batch = ((res.get("batch_header") or {}).get("payout_batch_id")) or res.get("payout_batch_id") or ""
        etype = str(event.get("event_type", ""))[:80]
        raw_status = (res.get("batch_header") or {}).get("batch_status") or res.get("transaction_status") or etype
        ev = WebhookEvent("paypal", etype, provider_id=batch, raw_status=str(raw_status)[:40], raw=body.decode()[:20000],
                          state="settled" if str(raw_status).upper() in ("SUCCESS", "SUCCEEDED") else None, verification_configured=bool(self.webhook_id))
        if self.webhook_id:
            h = {k.lower(): v for k, v in headers.items()}
            vb = {"auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"), "transmission_id": h.get("paypal-transmission-id"),
                  "transmission_sig": h.get("paypal-transmission-sig"), "transmission_time": h.get("paypal-transmission-time"),
                  "webhook_id": self.webhook_id, "webhook_event": event}
            try:
                st, resp = self.client.request("POST", "/v1/notifications/verify-webhook-signature", vb)
                ev.verified = st == 200 and resp.get("verification_status") == "SUCCESS"
            except Exception:
                ev.verified = False
        return ev
