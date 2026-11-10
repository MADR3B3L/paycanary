"""The ONE thing every provider shares: the interface (send / status / verify_webhook) and its plain data types. Imports nothing from any provider."""
import re
from dataclasses import dataclass, field

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,15}$")
STATES = ("sent", "pending", "settled", "failed", "reversed", "in_doubt")   # normalized across providers; the raw provider status is kept alongside, prefixed
# "in_doubt": the request left but its outcome is unknown (lost response, timeout, 5xx). The money MAY have moved, so it is
# reserved against the cap (counted like pending) until reconciliation (a status poll or webhook) resolves it to settled or failed.


class ProviderError(Exception):
    """Base for provider-layer refusals. Every one of these fails CLOSED: nothing is sent."""


class WrongProvider(ProviderError):
    """A request addressed to provider A reached provider B's adapter. Structurally refused."""


class UnknownProvider(ProviderError):
    """No adapter is registered under that exact name. There is no fallback to another provider."""


@dataclass(frozen=True)
class SendRequest:
    provider: str                 # the provider this payment is routed to (fixed when the payment row is created)
    payment_id: str
    recipient: str
    amount: str                   # decimal string in the currency's own units
    currency: str
    memo: str
    idempotency_key: str          # provider-scoped; the same key never creates a second provider object


@dataclass
class SendResult:
    provider: str
    ok: bool
    state: str                    # one of STATES when ok, else "failed"
    provider_id: str = None       # the provider's own id (unprefixed); the service stores it as "<provider>:<id>"
    raw_status: str = None        # the provider's own status string, unprefixed
    error: str = None             # exception class name or error name; never a secret
    http: int = None
    detail: dict = field(default_factory=dict)


@dataclass
class StatusResult:
    provider: str
    provider_id: str
    state: str
    raw_status: str = None


@dataclass
class WebhookEvent:
    provider: str
    event_type: str
    provider_id: str = ""
    state: str = None             # normalized state this event implies, if any
    raw_status: str = None        # the provider's own status string for this event, unprefixed
    verified: bool = False
    verification_configured: bool = False     # whether this provider has webhook verification set up at all
    parse_error: bool = False                 # the body was not valid JSON
    raw: str = ""


class Provider:
    """Subclass contract. `name` must match NAME_RE. An adapter only ever handles requests addressed to its own name."""
    name = ""

    def send(self, req):                                 # -> SendResult
        raise NotImplementedError

    def status(self, provider_id):                       # -> StatusResult
        raise NotImplementedError

    def verify_webhook(self, headers, body):             # -> WebhookEvent (verified or not); never raises
        raise NotImplementedError

    def _mine(self, req_or_name):
        n = getattr(req_or_name, "provider", req_or_name)
        if n != self.name:
            raise WrongProvider("request for provider %r reached the %r adapter" % (n, self.name))
