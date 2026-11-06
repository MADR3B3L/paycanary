"""Stripe's own clearly-labelled test identities ('st-'), disjoint from every other provider's."""
NAMESPACE = "st"
IDEMPOTENCY_PREFIX = "pc-stripe-"
RECIPIENT_DOMAIN = "st-sandbox.example"
PAYEES = [("ST Cloud Hosting", "billing@st-cloudhosting.st-sandbox.example"), ("ST Dev Tools", "billing@st-devtools.st-sandbox.example"),
          ("ST Office Supply", "orders@st-officesupply.st-sandbox.example"), ("ST Freelancer Kai", "kai@st-sandbox.example")]


def recipient(payee):
    slug = "".join(c if c.isalnum() else "-" for c in str(payee).lower()).strip("-")[:40] or "payee"
    return "%s-%s@%s" % (NAMESPACE, slug, RECIPIENT_DOMAIN)
