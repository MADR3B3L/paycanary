"""The mock provider's own labelled test identities (disjoint from every other provider's)."""
NAMESPACE = "mock"
IDEMPOTENCY_PREFIX = "pc-mock-"
RECIPIENT_DOMAIN = "mock-sandbox.example"
PAYEES = [("Mock Cloud Co", "billing@mock-cloudco.mock-sandbox.example"), ("Mock Tools Inc", "billing@mock-tools.mock-sandbox.example"),
          ("Mock Supplies", "orders@mock-supplies.mock-sandbox.example"), ("Mock Contractor Bo", "bo@mock-sandbox.example")]


def recipient(payee):
    slug = "".join(c if c.isalnum() else "-" for c in str(payee).lower()).strip("-")[:40] or "payee"
    return "%s-%s@%s" % (NAMESPACE, slug, RECIPIENT_DOMAIN)
