"""Harness-only wrappers around a Provider (the provider boxes themselves are never modified):
  Namespaced(inner, fixtures, scale=None): at the provider BOUNDARY, (a) rewrites the recipient to that provider's own labelled test identity (fixtures.recipient) so no
  sandbox record can be mistaken for another provider's, and (b) optionally divides the amount by `scale` so a limited sandbox balance can carry the corpus. The gate has
  ALREADY decided on the true recipient and amount by then; the true values are recorded in the memo. Both are disclosed in every report that uses them."""
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal

from app.providers.base import Provider

ZERO_DECIMAL = {"JPY"}


class Namespaced(Provider):
    def __init__(self, inner, fixtures, scale=None):
        self.inner, self.fx, self.scale = inner, fixtures, scale
        self.name = inner.name
        self.sent = []                                    # [(true_recipient, true_amount, sent_recipient, sent_amount, provider_id)]
        self.failed = []                                  # [(true_recipient, true_amount, currency, error, http)]

    @property
    def n(self):
        return len(self.sent)

    def _amount(self, req):
        if not self.scale:
            return req.amount
        d = Decimal(req.amount) / Decimal(self.scale)
        if req.currency.upper() in ZERO_DECIMAL:
            return str(max(1, int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))))
        return str(max(Decimal("0.01"), d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))

    def send(self, req):
        self._mine(req)
        new = replace(req, recipient=self.fx.recipient(req.recipient), amount=self._amount(req), memo=("%s | true %s %s -> %s" % (req.memo, req.amount, req.currency, req.recipient))[:80])
        res = self.inner.send(new)
        if res.ok:
            self.sent.append((req.recipient, req.amount, new.recipient, new.amount, res.provider_id))
        else:
            self.failed.append((req.recipient, req.amount, req.currency, res.error, res.http))
        return res

    def status(self, provider_id):
        return self.inner.status(provider_id)

    def verify_webhook(self, headers, body):
        return self.inner.verify_webhook(headers, body)
