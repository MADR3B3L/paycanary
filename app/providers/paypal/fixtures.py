"""PayPal's own clearly-labelled test identities. Nothing here is shared with any other provider (tests/test_providers_fixtures.py checks the sets are disjoint)."""
NAMESPACE = "pp"
IDEMPOTENCY_PREFIX = "pc-paypal-"
RECIPIENT_DOMAIN = "pp-sandbox.example"
PAYEES = [("PP Cloud Hosting", "billing@pp-cloudhosting.pp-sandbox.example"), ("PP Dev Tools", "billing@pp-devtools.pp-sandbox.example"),
          ("PP Office Supply", "orders@pp-officesupply.pp-sandbox.example"), ("PP Freelancer Ada", "ada@pp-sandbox.example")]


def recipient(payee):
    """A PayPal-namespaced test recipient for any payee name (so no PayPal sandbox record can be mistaken for another provider's)."""
    slug = "".join(c if c.isalnum() else "-" for c in str(payee).lower()).strip("-")[:40] or "payee"
    return "%s-%s@%s" % (NAMESPACE, slug, RECIPIENT_DOMAIN)
