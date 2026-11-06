#!/usr/bin/env python3
"""Run the benchmark and/or the persona x attacker corpus through the REAL Service.pay with a REAL provider SANDBOX / TEST-MODE behind it, then check what the
provider actually recorded. Sandbox/test only (the PayPal client refuses non-sandbox, the Stripe client refuses non-test keys). Keys come from .env and are never printed.
The gate decides on the TRUE recipients and amounts; at the provider boundary each recipient is rewritten to that provider's own labelled test identity and (optionally)
the amount is divided by --scale so a limited sandbox balance can carry the corpus (see tools/provider_wrappers.py).
  python3 tools/score_providers_live.py --provider paypal --scale 1000 [--corpus benchmark|persona|both] [--sample 20]
  python3 tools/score_providers_live.py --provider stripe [--corpus ...]"""
import argparse
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402
from app.providers.base import SendRequest  # noqa: E402
from app.providers.factory import build_registry  # noqa: E402
from tools import score_benchmark as B  # noqa: E402
from tools import score_benchmark_service as BS  # noqa: E402
from tools import score_persona_service as PS  # noqa: E402
from tools.provider_wrappers import Namespaced  # noqa: E402


def fixtures_for(name):
    import importlib
    return importlib.import_module("app.providers.%s.fixtures" % name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["paypal", "stripe"])
    ap.add_argument("--scale", type=float, default=None)
    ap.add_argument("--corpus", default="both", choices=["benchmark", "persona", "both"])
    ap.add_argument("--sample", type=int, default=20)
    a = ap.parse_args()
    config.load_env()
    reg = build_registry(os.environ, names=(a.provider,))
    if a.provider not in reg:
        print("provider %r is not configured in .env (its own slot)" % a.provider)
        return 2
    inner = reg.get(a.provider)
    fx = fixtures_for(a.provider)
    shared = Namespaced(inner, fx, scale=a.scale)
    print("provider: %s (SANDBOX/TEST) | corpus: %s | amount scale at the provider boundary: %s" % (a.provider, a.corpus, ("1/%g" % a.scale) if a.scale else "none (true amounts)"))
    total = Counter()
    refs = []
    for label, runner in (("benchmark", lambda st: (B.run(), BS.run(make_provider=lambda: shared, recipient=lambda p: p.lower() + "@example.com", stats=st))),
                          ("persona", lambda st: (None, PS.run(make_provider=lambda: shared, stats=st)))):
        if a.corpus not in ("both", label):
            continue
        st = {}
        direct, res = runner(st)
        line = "%-9s gate-decided %d | sent to provider %d | frozen (never sent) %d | provider errors (failed closed) %d" % (label, st["decided"], st["sent"], st["frozen"], st["errors"])
        if direct:
            same = sum(1 for t, v in res.items() if direct[t][0] == v[0])
            line += " | verdicts identical to the direct gate %d/%d" % (same, len(res))
        print(line)
        if st["errors"]:
            print("          provider error codes:", dict(st["error_codes"]))
        total.update(decided=st["decided"], sent=st["sent"], frozen=st["frozen"], errors=st["errors"])
        refs += st["refs"]
    print("TOTAL    decided %(decided)d | sent %(sent)d | frozen %(frozen)d | provider errors %(errors)d" % {k: total[k] for k in ("decided", "sent", "frozen", "errors")})
    for f in shared.failed[:10]:
        print("failed closed:", f)
    print("provider-side: the adapter made %d real sandbox sends (== payments the gate let through: %s)" % (shared.n, shared.n == total["sent"]))
    # what the provider itself reports for a sample of what we sent
    sample = [r.split(":", 1)[1] for r in refs if r.startswith(a.provider + ":")][: a.sample]
    states = Counter()
    for pid in sample:
        try:
            r = inner.status(pid)
            states[(r.state, r.raw_status)] += 1
        except Exception as err:
            states[("status-call-failed", type(err).__name__)] += 1
    print("provider-side status of %d sampled sends: %s" % (len(sample), dict(states)))
    # idempotent replay against the REAL provider: the same idempotency key must not create a second object
    if shared.sent:
        s0 = shared.sent[0]
        key = "pc-%s-replaycheck-%d" % (a.provider, int(__import__("time").time()))
        mk = lambda: SendRequest(a.provider, "pay-replaycheck", fx.recipient("replay check"), shared._amount(SendRequest(a.provider, "", "", "1.00", "USD", "", "")), "USD", "idempotency check", key)
        r1, r2 = inner.send(mk()), inner.send(mk())
        print("idempotent replay: first ok=%s id=%s | replay ok=%s id=%s error=%s | second provider object created: %s"
              % (r1.ok, r1.provider_id, r2.ok, r2.provider_id, r2.error, bool(r2.ok and r2.provider_id != r1.provider_id)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
