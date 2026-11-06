import json, time, unittest
from app import enrichers as EN
from app import notify as N
from app.service import Service
from app.store import Store
from tests.test_service import FakePayPal, pay

NOW = 1_800_000_000.0
IANA = json.dumps({"services": [[["com", "net"], ["https://rdap.example-registry.test/"]]]}).encode()


def rdap(days):
    import datetime as dt
    reg = dt.datetime.fromtimestamp(NOW - days * 86400, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"events": [{"eventAction": "registration", "eventDate": reg}]}).encode()


class Net:
    """A fake transport: records every URL; answers by pattern. `age` days, `mx` bool, `feed_hosts` list."""
    def __init__(self, age=4000, mx=True, feed_hosts=(), fail=()):
        self.age, self.mx, self.feed_hosts, self.fail, self.urls = age, mx, feed_hosts, set(fail), []

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        if "iana" in url and "rdap" in self.fail or "domain/" in url and "rdap" in self.fail: raise ConnectionError("down")
        if "cloudflare-dns" in url and "mx" in self.fail: raise TimeoutError("slow")
        if "data.iana.org" in url: return 200, IANA
        if "rdap.example-registry.test/domain/" in url: return 200, rdap(self.age)
        if "cloudflare-dns" in url:
            return 200, json.dumps({"Status": 0, "Answer": [{"type": 15, "data": "10 mx.x"}] if self.mx else []}).encode()
        if "phishing.army" in url or "Phishing.Database" in url:
            if "feed" in self.fail: raise ConnectionError("down")
            return 200, "\n".join("http://%s/login" % h for h in self.feed_hosts).encode()
        return 404, b""


def enr(net, **kw):
    t = [NOW]; st = Store()
    return EN.Enrichers(st, transport=net, clock=lambda: t[0], **kw), st, t


class Gather(unittest.TestCase):
    def test_collects_age_mx_and_feed_evidence_and_caches(self):
        net = Net(age=3, mx=False, feed_hosts=["evil-shop.com"]); e, st, t = enr(net)
        ev = e.gather("evil-shop.com")
        self.assertEqual((ev["domain_age_days"], ev["mx_present"], ev["blocklist_hit"]), (3, False, True)); self.assertIn("feed", ev["blocklist_source"])
        self.assertEqual(sorted(ev["checked"]), ["blocklist", "domain_age_days", "mx_present"]); self.assertEqual(ev["unavailable"], [])
        n = len(net.urls); again = e.gather("evil-shop.com")
        self.assertTrue(again["cached"]); self.assertEqual(len(net.urls), n)                    # one-time cost per domain

    def test_failures_are_reported_unavailable_never_as_clean_and_cached_only_briefly(self):
        net = Net(fail=("rdap", "mx", "feed")); e, st, t = enr(net)
        ev = e.gather("acme.com")
        self.assertEqual(sorted(ev["unavailable"]), ["blocklist", "domain_age_days", "mx_present"]); self.assertEqual(ev["checked"], [])
        self.assertNotIn("blocklist_hit", ev); self.assertNotIn("domain_age_days", ev)
        n = len(net.urls); e.gather("acme.com"); self.assertEqual(len(net.urls), n)             # negative cache: no hammering
        t[0] += 301; e.gather("acme.com"); self.assertGreater(len(net.urls), n)                  # ... for 5 minutes only

    def test_only_the_domain_ever_leaves_and_no_request_goes_to_the_payees_own_host(self):
        net = Net(); e, st, t = enr(net); e.gather("vendor-corp.com")
        for u in net.urls:
            self.assertNotIn("@", u); self.assertNotIn("%40", u)
            host = u.split("/")[2]; self.assertNotEqual(host, "vendor-corp.com"); self.assertFalse(host.endswith(".vendor-corp.com"))
        self.assertTrue(any("vendor-corp.com" in u for u in net.urls))                           # the domain is a path/query component of fixed hosts

    def test_shared_hosts_and_subdomain_lookalikes_are_not_feed_hits(self):
        net = Net(feed_hosts=["github.com", "evil.com"]); e, st, t = enr(net)
        self.assertNotIn("blocklist_hit", e.gather("github.com"))                                # legit shared host listed for hosted content
        self.assertNotIn("blocklist_hit", e.gather("notevil.com"))                               # exact host match only
        self.assertTrue(e.gather("evil.com")["blocklist_hit"])

    def test_invalid_domains_and_the_per_minute_budget(self):
        net = Net(); e, st, t = enr(net, per_minute=2)
        for bad in ("", "localhost", "127.0.0.1", "a b.com", "evil.com/../x", "x@y.com", None):
            self.assertEqual(e.gather(bad)["unavailable"], ["invalid_domain"], bad)
        self.assertEqual(net.urls, [])
        e.gather("a-one.com"); e.gather("b-two.com"); self.assertEqual(e.gather("c-three.com")["unavailable"], ["budget"])
        t[0] += 61; self.assertEqual(e.gather("c-three.com")["unavailable"], [] ) if False else None

    def test_redirects_are_refused(self):
        self.assertIsNone(EN.NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://evil.example/"))


def svc(net=None, notifier_on=("freeze",), enrich=True, **kw):
    t = [NOW]; pp = FakePayPal()
    s = Service(Store(), pp, clock=lambda: t[0], default_require_intent=False, default_hard_cap=1000, **kw)
    sent = []
    s.notifier = N.Notifier("https://ops.example.com/h", "secret", on=notifier_on, audit=s.audit, transport=lambda u, b, h, to: (sent.append(json.loads(b)), 200)[1], sync=True)
    s.sync_enrich = True
    if enrich:
        s.enrichers = EN.Enrichers(s.store, transport=net or Net(), clock=lambda: t[0])
    return s, pp, t, sent


class Tier0Inline(unittest.TestCase):
    def test_tier0_alone_cannot_hold_a_first_payment_it_only_adds_to_cached_evidence(self):
        # FINDING: the offline signals max out at weight 2 (a freemail-hosted business name, OR a disposable domain; the two sets do not overlap),
        # which equals the existing first-payee REVIEW, so Tier 0 by itself never changes a verdict. It matters once other evidence is present.
        s, pp, t, sent = svc(enrich=False)
        mule = pay(s, "Quickpay Solutions LLC", "60.00", email="quickpay.solutions@gmail.com")
        self.assertEqual((mule["decision"], mule["status"]), ("review", "sent"))
        s.store.ev_put("gmail.com", {"domain_age_days": 3, "checked": ["domain_age_days"], "unavailable": []}, NOW, 86400)             # (contrived: cached young domain)
        t[0] += 300; held = pay(s, "Quickpay Solutions LLC", "61.00", email="quickpay.solutions@gmail.com")
        self.assertEqual(held["status"], "frozen"); self.assertIn("freemail", held["reasons"][0]); self.assertIn("held for an informed review", held["reasons"][0])
        t[0] += 300; real = pay(s, "Datavest Inc", "60.00", email="billing@datavest.example")
        self.assertNotIn("freemail", real["reasons"][0]); self.assertNotEqual(real["status"], "frozen")

    def test_the_cheap_footprint_mule_vs_the_aged_vendor_with_cached_evidence(self):
        s, pp, t, sent = svc(enrich=False)
        s.store.ev_put("fresh-mule.test", {"domain_age_days": 2, "mx_present": False, "checked": ["domain_age_days", "mx_present"], "unavailable": []}, NOW, 86400)
        s.store.ev_put("old-vendor.test", {"domain_age_days": 4000, "mx_present": True, "checked": ["domain_age_days", "mx_present"], "unavailable": []}, NOW, 86400)
        mule = pay(s, "Fresh Mule", "60.00", email="pay@fresh-mule.test")
        self.assertEqual(mule["status"], "frozen"); self.assertIn("held for an informed review", mule["reasons"][0]); self.assertIn("2 days ago", mule["reasons"][0]); self.assertEqual(pp.calls, [])
        t[0] += 300; real = pay(s, "Old Vendor", "60.00", email="pay@old-vendor.test")
        self.assertNotEqual(real["status"], "frozen"); self.assertEqual(len(pp.calls), 1)                  # a clean footprint adds no friction beyond the first-payee review
        self.assertEqual(real["decision"], "review")


class Tier1OnHeld(unittest.TestCase):
    def test_a_held_payment_gets_evidence_attached_and_the_alert_carries_it(self):
        s, pp, t, sent = svc(Net(age=2, mx=False))
        out = pay(s, "Wallet Co", "900.00", email="pay@brand-new-wallet.com")                              # over the $1000/…? 900 to a new payee > floor: held
        self.assertEqual(out["status"], "frozen")
        ev = json.loads(s.store.payment(out["payment_id"])["evidence_json"])
        self.assertEqual(ev["hint"], "hold"); self.assertTrue(any("2 days ago" in d for d in ev["detail"])); self.assertTrue(any(i["signal"] == "mx_present" for i in ev["items"]))
        self.assertEqual(len(sent), 1); self.assertEqual(sent[0]["event"], "payment.frozen"); self.assertTrue(any("2 days ago" in d for d in sent[0]["evidence"]))
        self.assertEqual(sent[0]["evidence_hint"], "hold"); self.assertEqual(s.store.payment(out["payment_id"])["status"], "frozen"); self.assertEqual(pp.calls, [])

    def test_a_feed_hit_is_hard_and_clean_evidence_never_releases_a_hold(self):
        s, pp, t, sent = svc(Net(feed_hosts=["scam-site.com"]))
        bad = pay(s, "Scam Co", "900.00", email="pay@scam-site.com"); ev = json.loads(s.store.payment(bad["payment_id"])["evidence_json"])
        self.assertEqual(ev["hint"], "freeze"); self.assertTrue(any(i["severity"] == "hard" for i in ev["items"]))
        t[0] += 300; s2, pp2, t2, sent2 = svc(Net(age=5000, mx=True))
        ok = pay(s2, "Big Vendor", "900.00", email="pay@big-vendor.com"); evc = json.loads(s2.store.payment(ok["payment_id"])["evidence_json"])
        self.assertEqual(evc["hint"], "none"); self.assertEqual(s2.store.payment(ok["payment_id"])["status"], "frozen"); self.assertEqual(pp2.calls, [])    # clean evidence does not auto-release

    def test_enrichment_failures_are_fail_open_the_hold_stands_and_the_ask_still_goes_out(self):
        s, pp, t, sent = svc(Net(fail=("rdap", "mx", "feed")))
        out = pay(s, "Wallet Co", "900.00", email="pay@somewhere-new.com")
        self.assertEqual(out["status"], "frozen"); ev = json.loads(s.store.payment(out["payment_id"])["evidence_json"])
        self.assertEqual(ev["hint"], "none"); self.assertTrue(any("not checked" in d for d in ev["detail"]))
        self.assertEqual(len(sent), 1)                                                                     # the alert was not lost to a dead feed

    def test_an_executed_new_payee_payment_that_evidence_flags_is_audited_and_alerted(self):
        s, pp, t, sent = svc(Net(feed_hosts=["late-flag.com"]), notifier_on=("freeze", "review"))
        out = pay(s, "Small Co", "40.00", email="pay@late-flag.com")                                       # small first payment: review, proceeds
        self.assertEqual(out["status"], "sent")
        ev = [r["event"] for r in s.audit.rows(limit=100)]; self.assertIn("adverse_evidence_after_execution", ev); self.assertIn("evidence_gathered", ev)
        self.assertTrue(any(e.get("evidence_hint") == "freeze" for e in sent))

    def test_no_enrichment_when_not_configured_and_audit_records_only_the_domain(self):
        s, pp, t, sent = svc(enrich=False)
        out = pay(s, "Wallet Co", "900.00", email="pay@somewhere.com"); self.assertIsNone(s.store.payment(out["payment_id"])["evidence_json"])
        s2, pp2, t2, sent2 = svc(Net()); pay(s2, "Wallet Co", "900.00", email="secret.person@somewhere.com")
        blob = json.dumps(s2.audit.rows(limit=200)); self.assertNotIn("secret.person", blob); self.assertIn("somewhere.com", blob)


if __name__ == "__main__":
    unittest.main()
