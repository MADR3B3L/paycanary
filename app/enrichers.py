"""Tier-1 external evidence gathering for new/unproven payees (the I/O half of option O9; the scoring is pure in app/external_evidence.py).
RULES (all tested): opt-in (PAYCANARY_ENRICHMENT=1); only the recipient DOMAIN ever leaves (never the address, amount, memo, agent or org);
fixed endpoints only (IANA bootstrap -> the registry's own RDAP host, Cloudflare DoH, public scam feeds downloaded and matched LOCALLY),
never an HTTP request to the payee's own domain, redirects refused, responses size-capped; every call has a hard timeout and FAILS OPEN
(a check that did not run is reported in `unavailable`, never as a clean result); results are cached per domain (24h, failures 5 min);
a per-minute budget stops an agent spamming /pay with random domains from burning feed quotas. No keys are needed for these sources; a
paid feed key would live in env / the 0600 file like the others and never in a commit.
Evidence dict keys (what app/external_evidence.score_evidence understands): domain_age_days, mx_present, blocklist_hit, blocklist_source,
plus `checked` and `unavailable` lists. sanctions / registry checks are NOT wired (a sanctions list is fuzzy name matching with a high false-positive
rate and there is no free registry API): they are roadmap, and absent keys are never penalized."""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
IANA_DNS = "https://data.iana.org/rdap/dns.json"
DOH = "https://cloudflare-dns.com/dns-query?name=%s&type=MX"
# Public domain-list feeds downloaded and matched LOCALLY. (OpenPhish now redirects and URLhaus text dumps are gone, both checked live;
# redirects stay refused.) Licenses: Phishing.Database is MIT; Phishing Army is CC BY-NC 4.0 (non-commercial: fine for the hackathon, swap for a
# licensed feed in production).
FEEDS = {"PhishingArmy": "https://phishing.army/download/phishing_army_blocklist.txt",
         "Phishing.Database": "https://raw.githubusercontent.com/mitchellkrogza/Phishing.Database/master/phishing-domains-ACTIVE.txt"}
MAX_BYTES = 16_000_000
# widely shared hosts that scam feeds list for the CONTENT they host, not because the host is a scam: never a "hit" on their own
SHARED_HOSTS = {"github.com", "githubusercontent.com", "google.com", "docs.google.com", "drive.google.com", "dropbox.com", "onedrive.live.com", "microsoft.com",
                "sharepoint.com", "amazonaws.com", "cloudfront.net", "azurewebsites.net", "wordpress.com", "weebly.com", "wixsite.com", "blogspot.com", "firebaseapp.com",
                "herokuapp.com", "netlify.app", "vercel.app", "pages.dev", "workers.dev", "notion.site", "gitbook.io", "t.me", "discord.com", "cdn.discordapp.com",
                "facebook.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "apple.com", "paypal.com", "stripe.com", "cloudflare.com", "bit.ly", "tinyurl.com"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _http(url, headers, timeout):
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"user-agent": "PayCanary-Enrichment/1", **(headers or {})})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(MAX_BYTES + 1)[:MAX_BYTES]
    except urllib.error.HTTPError as e:
        return e.code, b""


def valid_domain(d):
    return isinstance(d, str) and bool(DOMAIN_RE.match(d)) and not d.replace(".", "").isdigit()


class Enrichers:
    def __init__(self, store, transport=None, clock=time.time, feed_dir=None, per_minute=30, ttl=86400.0, neg_ttl=300.0, timeout=2.5, feeds=None):
        self.store, self.get, self.clock = store, transport or _http, clock
        self.feed_dir = Path(feed_dir) if feed_dir else None
        self.per_minute, self.ttl, self.neg_ttl, self.timeout, self.feeds = per_minute, ttl, neg_ttl, timeout, (FEEDS if feeds is None else feeds)
        self._calls, self._lock, self._bootstrap, self._feed_sets = [], threading.Lock(), None, {}

    # ---- budget -------------------------------------------------------------------------------------------------------
    def _allow(self):
        with self._lock:
            now = self.clock()
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self.per_minute:
                return False
            self._calls.append(now)
            return True

    # ---- individual checks (each raises on failure; gather() turns that into `unavailable`) ----------------------------
    def _rdap_base(self, tld):
        if self._bootstrap is None or self.clock() - self._bootstrap[0] > 7 * 86400:
            st, body = self.get(IANA_DNS, {}, self.timeout)
            if st != 200:
                raise RuntimeError("iana bootstrap HTTP %s" % st)
            m = {}
            for urls_tlds in json.loads(body).get("services", []):
                for t in urls_tlds[0]:
                    m[t.lower()] = urls_tlds[1][0]
            self._bootstrap = (self.clock(), m)
        base = self._bootstrap[1].get(tld)
        if not base:
            raise RuntimeError("no RDAP service for .%s" % tld)
        return base if base.endswith("/") else base + "/"

    def domain_age_days(self, domain):
        base = self._rdap_base(domain.rsplit(".", 1)[1])
        st, body = self.get(base + "domain/" + domain, {"accept": "application/rdap+json"}, self.timeout)
        if st == 404:
            return None                                        # not registered / no record: no age, and not a clean result either
        if st != 200:
            raise RuntimeError("rdap HTTP %s" % st)
        for ev in json.loads(body).get("events", []):
            if ev.get("eventAction") == "registration" and ev.get("eventDate"):
                import datetime as dt
                reg = dt.datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00")).timestamp()
                return max(0, int((self.clock() - reg) // 86400))
        return None

    def mx_present(self, domain):
        st, body = self.get(DOH % domain, {"accept": "application/dns-json"}, self.timeout)
        if st != 200:
            raise RuntimeError("doh HTTP %s" % st)
        j = json.loads(body)
        if j.get("Status") == 3:
            return False                                       # NXDOMAIN: the domain does not exist
        return any(a.get("type") == 15 for a in j.get("Answer", []) or [])

    def _feed(self, name, url):
        """Hostnames in a public scam feed, downloaded at most every 12h and matched LOCALLY (the payee's domain never leaves)."""
        cached = self._feed_sets.get(name)
        if cached and self.clock() - cached[0] < 12 * 3600:
            return cached[1]
        path = self.feed_dir / (name + ".txt") if self.feed_dir else None
        text = None
        if path and path.exists() and self.clock() - path.stat().st_mtime < 12 * 3600:
            text = path.read_text(errors="replace")
        if text is None:
            st, body = self.get(url, {}, 20)
            if st != 200:
                raise RuntimeError("%s HTTP %s" % (name, st))
            text = body.decode("utf-8", "replace")
            if path:
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
        hosts = set()
        for line in text.splitlines():
            m = re.match(r"^\s*(?:[a-z]+://)?(?:[^/@\s]+@)?([a-z0-9.-]+)(?::\d+)?(?:[/?#]|$)", line.strip().lower())
            if m and not m.group(1).replace(".", "").isdigit():
                hosts.add(m.group(1).strip("."))
        self._feed_sets[name] = (self.clock(), hosts)
        return hosts

    def blocklist(self, domain):
        """(hit, source) for an EXACT host match in any feed; shared hosting domains never count."""
        if domain in SHARED_HOSTS or any(domain.endswith("." + h) for h in SHARED_HOSTS):
            return False, None
        ok, last = 0, None
        for name, url in self.feeds.items():
            try:
                if domain in self._feed(name, url):
                    return True, name + " feed"
                ok += 1
            except Exception as e:
                last = e
        if not ok and last is not None:
            raise last
        return False, None

    # ---- the one entry point ------------------------------------------------------------------------------------------
    def gather(self, domain):
        """Evidence dict for a domain (cached). Never raises."""
        domain = (domain or "").strip().lower()
        if not valid_domain(domain):
            return {"checked": [], "unavailable": ["invalid_domain"]}
        hit = self.store.ev_get(domain, self.clock())
        if hit is not None:
            return {**hit, "cached": True}
        if not self._allow():
            return {"checked": [], "unavailable": ["budget"]}
        ev, checked, unavailable = {}, [], []
        for key, fn in (("domain_age_days", self.domain_age_days), ("mx_present", self.mx_present)):
            try:
                v = fn(domain)
                if v is not None:
                    ev[key] = v
                checked.append(key)
            except Exception:
                unavailable.append(key)
        try:
            hitb, src = self.blocklist(domain)
            checked.append("blocklist")
            if hitb:
                ev["blocklist_hit"], ev["blocklist_source"] = True, src
        except Exception:
            unavailable.append("blocklist")
        ev["checked"], ev["unavailable"] = checked, unavailable
        self.store.ev_put(domain, ev, self.clock(), self.ttl if checked else self.neg_ttl)       # all-failed results are cached only briefly
        return ev
