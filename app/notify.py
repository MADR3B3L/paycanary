"""One configurable outbound webhook for time-sensitive events (a payment froze / would have frozen / needs review).
POSTs JSON signed like PayPal-style webhooks: X-PayCanary-Signature: t=<unix>,v1=<hex hmac_sha256(secret, "<t>.<body>")> so the
receiver can verify authenticity and reject replays by timestamp. Delivery is out-of-band with 3 tries and a 5s timeout; a dead
receiver can never delay or fail a payment, and every outcome is written to the audit log. The URL is operator-set only."""
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import urllib.request

EVENT_FOR = {"freeze": "payment.frozen", "shadow_freeze": "payment.would_freeze", "review": "payment.review"}


def sign(secret, ts, body):
    return "t=%d,v1=%s" % (ts, hmac.new(secret.encode(), ("%d." % ts).encode() + body, hashlib.sha256).hexdigest())


def verify(secret, header, body, tolerance=300, now=None):
    """For receivers (and our tests): True iff the signature matches and the timestamp is fresh."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError, AttributeError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, ts, body), "t=%d,v1=%s" % (ts, parts.get("v1", "")))


def _http(url, body, headers, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


class Notifier:
    def __init__(self, url, secret, on=("freeze",), audit=None, transport=None, sync=False, backoff=(0.5, 1.0), clock=time.time):
        u = urllib.parse.urlparse(url or "")
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError("alert url must be http(s)")
        if not secret:
            raise ValueError("an alert secret is required (receivers must be able to verify the signature)")
        self.url, self.secret, self.on, self.audit = url, secret, set(on), audit
        self.transport, self.sync, self.backoff, self.clock = transport or _http, sync, backoff, clock

    def wants(self, kind):
        return kind in self.on

    def notify(self, kind, payload):
        """kind: 'freeze' | 'shadow_freeze' | 'review'. Returns immediately (unless sync=True, for tests)."""
        if not self.wants(kind):
            return
        body = json.dumps({"event": EVENT_FOR[kind], **payload}, sort_keys=True, separators=(",", ":")).encode()
        if self.sync:
            self._deliver(kind, payload.get("payment_id"), body)
        else:
            threading.Thread(target=self._deliver, args=(kind, payload.get("payment_id"), body), daemon=True).start()

    def _deliver(self, kind, pid, body):
        last = "no attempt"
        for i in range(3):
            ts = int(self.clock())
            try:
                st = self.transport(self.url, body, {"content-type": "application/json", "user-agent": "PayCanary-Alert/1",
                                                     "x-paycanary-event": EVENT_FOR[kind], "x-paycanary-signature": sign(self.secret, ts, body)}, 5)
                if 200 <= int(st) < 300:
                    self._log("alert_sent", pid, {"kind": kind, "attempt": i + 1, "http": int(st)})
                    return True
                last = "HTTP %s" % st
            except Exception as err:
                last = type(err).__name__
            if i < 2 and self.backoff and not self.sync:
                time.sleep(self.backoff[min(i, len(self.backoff) - 1)])
        self._log("alert_failed", pid, {"kind": kind, "error": last})
        return False

    def _log(self, event, pid, detail):
        if self.audit:
            self.audit.append(event, "notifier", payment_id=pid, detail=detail)
