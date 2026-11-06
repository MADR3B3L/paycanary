"""Cross-agent payee reputation: the SHARED, de-identified store. (Scoring lives in app/reputation.py; this file is data + secret + wiring.)

PRIVACY INVARIANT (tested against the schema itself): the shared database holds only keyed hashes, timestamps and event TYPES.
No amounts, no memos, no payment ids, no agent ids, no plaintext emails or org ids. A reading agent learns pooled scalars about the
ONE payee it is about to pay and cannot enumerate who else pays whom.
  recipient_key = HMAC_k("rcpt|" + normalize(recipient))      org_hash = HMAC_k("org|" + org_id)
The key k is a proxy-side secret of the same class as the PayPal credentials: an agent that can read it can test guesses against the
table. Reputation is DISABLED, never weakened, when no key is configured.
Rows are events, one per (payee, org, time, type), so "as of" reads are exact (no future leakage), distinct-org counts are exact and
idempotent (a set, not a counter), and a customer's contribution can be removed (forget_org) without touching anyone else's.
Only PAYEE-ATTRIBUTABLE negative evidence is accepted: operator DENY, operator CONFIRMED fraud, and payee-identity freezes. Cap/velocity/
amount/cumulative freezes describe an AGENT's behaviour and must never become a payee's reputation."""
import hashlib
import hmac
import sqlite3

from .dbsafe import LockedDB
import threading
import time

EVENTS = ("executed", "denied", "identity_freeze", "confirmed_fraud")      # mule_hit has no honest source (PayPal does not show onward transfers)
BUCKETS = (0, 1, 2, 5, 10, 25, 50, 100)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rep_event (recipient_key TEXT NOT NULL, org_hash TEXT NOT NULL, ts REAL NOT NULL, event TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS rep_event_key ON rep_event(recipient_key, ts);
CREATE INDEX IF NOT EXISTS rep_event_org ON rep_event(org_hash);
CREATE TABLE IF NOT EXISTS rep_skeleton (recipient_key TEXT NOT NULL, skeleton TEXT NOT NULL, PRIMARY KEY (recipient_key, skeleton));
CREATE INDEX IF NOT EXISTS rep_skeleton_s ON rep_skeleton(skeleton);
"""


def normalize(recipient):
    """Canonical destination: trimmed, lower-cased, +tag stripped from an email's local part."""
    r = str(recipient or "").strip().lower()
    if "@" in r:
        local, _, domain = r.rpartition("@")
        r = local.split("+", 1)[0] + "@" + domain
    return r


def bucket(n):
    """Coarsened clean-payment volume (so the shared table never carries an exact count)."""
    b = 0
    for lo in BUCKETS:
        if n >= lo:
            b = lo
    return b


class Reputation:
    def __init__(self, key, path=":memory:"):
        if not key or len(key) < 16:
            raise ValueError("a reputation HMAC key of at least 16 characters is required (reputation stays disabled without one)")
        self._key = key.encode()
        self.lock = threading.RLock()
        self.db = LockedDB(path, self.lock)
        self.db.executescript(SCHEMA)

    # ---- keyed identities (the only way a payee or org is ever written) ----
    def _h(self, tag, value):
        return hmac.new(self._key, (tag + "|" + value).encode(), hashlib.sha256).hexdigest()

    def recipient_key(self, recipient):
        return self._h("rcpt", normalize(recipient))

    def org_hash(self, org_id):
        return self._h("org", str(org_id))

    # ---- writes ----
    def contribute(self, recipient, name, org_id, event, ts=None):
        if event not in EVENTS:
            raise ValueError("unknown reputation event %r (allowed: %s)" % (event, ", ".join(EVENTS)))
        from .gate import skeleton
        rk, oh, ts = self.recipient_key(recipient), self.org_hash(org_id), float(ts if ts is not None else time.time())
        with self.lock:
            self.db.execute("INSERT INTO rep_event VALUES (?,?,?,?)", (rk, oh, ts, event))
            if name:
                self.db.execute("INSERT OR IGNORE INTO rep_skeleton VALUES (?,?)", (rk, self._h("name", skeleton(name))))      # hashed too: no plaintext names in the shared table
            self.db.commit()

    def forget_org(self, org_id):
        """Right to be forgotten: remove ONE customer's contributions; pooled views over everyone else are unaffected."""
        with self.lock:
            n = self.db.execute("DELETE FROM rep_event WHERE org_hash=?", (self.org_hash(org_id),)).rowcount
            self.db.execute("DELETE FROM rep_skeleton WHERE recipient_key NOT IN (SELECT DISTINCT recipient_key FROM rep_event)")
            self.db.commit()
        return n

    # ---- reads (one payee in hand, as of a moment) ----
    RING_WINDOW_S, RING_ORG_AGE_S = 7 * 86400, 30 * 86400       # same values as app/reputation.py (ring-buildup): brand-new orgs first-paying a payee inside a week

    def _new_orgs(self, paid, as_of):
        n = 0
        for org in {r["org_hash"] for r in paid}:
            first_here = min(r["ts"] for r in paid if r["org_hash"] == org)
            first_anywhere = self.db.execute("SELECT MIN(ts) m FROM rep_event WHERE org_hash=? AND ts<=?", (org, as_of)).fetchone()["m"]
            if first_here >= as_of - self.RING_WINDOW_S and first_anywhere is not None and first_anywhere >= as_of - self.RING_ORG_AGE_S:
                n += 1
        return n

    def _record(self, rk, as_of):
        rows = self.db.execute("SELECT org_hash, ts, event FROM rep_event WHERE recipient_key=? AND ts<=?", (rk, as_of)).fetchall()
        paid = [r for r in rows if r["event"] == "executed"]
        neg = [r for r in rows if r["event"] in ("denied", "identity_freeze")]
        fraud = [r for r in rows if r["event"] == "confirmed_fraud"]
        skel = {r["skeleton"] for r in self.db.execute("SELECT skeleton FROM rep_skeleton WHERE recipient_key=?", (rk,)).fetchall()}
        return {"recipient_key": rk, "name_skeletons": skel, "distinct_orgs": len({r["org_hash"] for r in paid}),
                "distinct_agents": len({r["org_hash"] for r in paid}),     # LOWER BOUND (>= one agent per org): agent identifiers are deliberately not stored; orgs are the Sybil unit
                "first_seen_network_ts": min((r["ts"] for r in rows), default=None), "clean_exec_bucket": bucket(len(paid)),
                "freeze_count": len(neg), "distinct_orgs_froze": len({r["org_hash"] for r in neg}),
                "confirmed_fraud": bool(fraud), "distinct_orgs_confirmed": len({r["org_hash"] for r in fraud}),
                "new_orgs_in_window": self._new_orgs(paid, as_of),
                "mule_fanout_hits": 0,                                    # no honest source: see module note
                "last_activity_ts": max((r["ts"] for r in rows), default=None), "seen": bool(rows)}

    def read(self, recipient, name, as_of):
        """Pooled, de-identified view of ONE payee as of `as_of`, plus the records of OTHER keys that share its name skeleton
        (so a network lookalike / beneficiary-swap check needs no second read). Never raises: on any error returns None."""
        try:
            from .gate import skeleton
            rk = self.recipient_key(recipient)
            rec = self._record(rk, as_of)
            sk = self._h("name", skeleton(name)) if name else None
            rec["skeleton_hash"] = sk
            peers = []
            if sk:
                for r in self.db.execute("SELECT DISTINCT recipient_key FROM rep_skeleton WHERE skeleton=? AND recipient_key<>?", (sk, rk)).fetchall():
                    p = self._record(r["recipient_key"], as_of)
                    if p["seen"]:
                        peers.append(p)
            rec["skeleton_peers"] = peers
            rec["enabled"] = True
            return rec
        except Exception:
            return None
