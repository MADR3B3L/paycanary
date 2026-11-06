"""Append-only, hash-chained, KEYED audit log: every decision, every human action, every config change, every alert, every denied
privileged call. Tamper-evident against anyone who can write the SQLite file but does NOT hold the audit key.

How it holds up (and where it stops):
  * SQLite triggers refuse UPDATE/DELETE, and each row's MAC covers the previous row's MAC, so editing, reordering, replaying or removing a past row
    breaks verify().
  * With a key (HMAC-SHA256, key held OUTSIDE the database: env var or a 0600 file in a separate directory) a forger who drops the triggers,
    rewrites a row and recomputes the chain cannot produce valid MACs. A keyless (legacy / :memory:) log is NOT tamper-evident against that attacker.
  * Every append is also written to an ANCHOR file (also outside the database, each line MAC'd): seq, row MAC, line MAC. verify() checks every anchored
    (seq, MAC) against the database, so truncating the tail, or replacing the file with a rebuilt chain, is detected.
  * Honest limit: whoever holds BOTH the key and the anchor file (i.e. owns the PayCanary host) can forge everything. For that threat, ship the anchor
    lines to storage the host cannot rewrite (a log service, WORM bucket, another machine) - the anchor format is line-oriented for exactly that.
Never put secrets, tokens or request bodies in `detail`."""
import hashlib
import hmac
import json
import os

GENESIS = "0" * 64
SEAL_EVENT = "audit_keyed"                       # the first row written under a key: it pins the legacy (unkeyed) chain head and switches the chain to HMAC
SCHEME = "hmac-sha256-v1"


def _canon(rec):
    return json.dumps(rec, sort_keys=True, separators=(",", ":"))


def _digest(prev, rec):
    """The legacy UNKEYED row hash (still used for rows written before a key was configured, and by keyless logs)."""
    return hashlib.sha256((prev + "|" + _canon(rec)).encode()).hexdigest()


def _mac(key, prev, rec):
    return hmac.new(key, (prev + "|" + _canon(rec)).encode(), hashlib.sha256).hexdigest()


def _anchor_mac(key, seq, h):
    return hmac.new(key, ("anchor|%d|%s" % (seq, h)).encode(), hashlib.sha256).hexdigest()


class AuditLog:
    def __init__(self, store, clock, key=None, anchor_path=None):
        if isinstance(key, str):
            key = key.encode("utf-8")
        if key is not None and len(key) < 16:
            raise ValueError("the audit key must be at least 16 bytes")
        self.store, self.clock, self.key, self.anchor_path = store, clock, key, (anchor_path if key else None)
        self._sealed = False

    # ---- write ------------------------------------------------------------------------------------------------
    def _is_sealed(self):
        if not self._sealed:
            self._sealed = self.store.db.execute("SELECT 1 FROM audit WHERE event=? LIMIT 1", (SEAL_EVENT,)).fetchone() is not None
        return self._sealed

    def _anchor(self, seq, h):
        if not (self.key and self.anchor_path):
            return
        try:
            d = os.path.dirname(self.anchor_path)
            if d:
                os.makedirs(d, mode=0o700, exist_ok=True)
            fd = os.open(self.anchor_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, ("%d %s %s\n" % (seq, h, _anchor_mac(self.key, seq, h))).encode())
            finally:
                os.close(fd)
        except OSError:
            pass                                                        # best effort: a missed anchor only narrows what truncation can be detected; verify() still checks the chain

    def _insert(self, rec, prev, h):
        cur = self.store.db.execute("INSERT INTO audit (ts, event, actor, agent_id, payment_id, mode, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                                    (rec["ts"], rec["event"], rec["actor"], rec["agent_id"], rec["payment_id"], rec["mode"], json.dumps(rec["detail"], sort_keys=True, separators=(",", ":")), prev, h))
        self.store.db.commit()
        self._anchor(cur.lastrowid, h)
        return cur.lastrowid

    def append(self, event, actor, agent_id=None, payment_id=None, mode=None, detail=None, ts=None):
        """Best-effort for callers that must not fail (returns the new seq or None); the log itself never raises into a payment.
        A chain that has been sealed with a key can only be extended by a process that holds that key (otherwise it returns None)."""
        now = float(ts if ts is not None else self.clock())
        rec = {"ts": now, "event": event, "actor": actor, "agent_id": agent_id, "payment_id": payment_id, "mode": mode, "detail": detail or {}}
        try:
            with self.store.lock:
                last = self.store.db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
                prev = last["hash"] if last else GENESIS
                sealed = self._is_sealed()
                if sealed and not self.key:
                    return None                                         # refuse to write unkeyed rows into a keyed chain
                if self.key and not sealed:                             # first keyed write: pin the legacy head, then continue under HMAC
                    n = self.store.db.execute("SELECT count(*) FROM audit").fetchone()[0]
                    seal = {"ts": now, "event": SEAL_EVENT, "actor": "system", "agent_id": None, "payment_id": None, "mode": None,
                            "detail": {"scheme": SCHEME, "legacy_head": prev, "legacy_rows": n}}
                    sh = _mac(self.key, prev, seal)
                    self._insert(seal, prev, sh)
                    self._sealed, prev = True, sh
                h = _mac(self.key, prev, rec) if self.key else _digest(prev, rec)
                return self._insert(rec, prev, h)
        except Exception:
            return None

    def rows(self, since_seq=0, limit=200, event=None, payment_id=None):
        q, a = "SELECT * FROM audit WHERE seq>?", [since_seq]
        if event:
            q += " AND event=?"; a.append(event)
        if payment_id:
            q += " AND payment_id=?"; a.append(payment_id)
        q += " ORDER BY seq LIMIT ?"; a.append(int(limit))
        return [dict(r) for r in self.store.db.execute(q, a).fetchall()]

    def head(self):
        r = self.store.db.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return {"seq": r["seq"], "hash": r["hash"]} if r else {"seq": 0, "hash": GENESIS}

    # ---- verify -----------------------------------------------------------------------------------------------
    def verify(self):
        """-> {"ok", "rows", "first_bad_seq", "keyed", "sealed_at", "anchor": {"status", "entries"}, "reason"}.
        Detects edits, reordering, replays and holes; with a key also detects a recomputed chain; with an anchor also detects tail truncation and a swapped-in chain."""
        def out(ok, n, bad=None, reason=None, keyed=False, sealed_at=None, anchor=None):
            return {"ok": ok, "rows": n, "first_bad_seq": bad, "keyed": keyed, "sealed_at": sealed_at, "reason": reason,
                    "anchor": anchor or {"status": "not_configured" if not self.anchor_path else "unchecked", "entries": 0}}
        prev, expect, n, keyed, sealed_at, hashes = GENESIS, 1, 0, False, None, {}
        for r in self.store.db.execute("SELECT * FROM audit ORDER BY seq").fetchall():
            try:
                rec = {"ts": r["ts"], "event": r["event"], "actor": r["actor"], "agent_id": r["agent_id"], "payment_id": r["payment_id"], "mode": r["mode"], "detail": json.loads(r["detail"])}
            except ValueError:
                return out(False, n, r["seq"], "unreadable_row", keyed, sealed_at)
            is_seal = r["event"] == SEAL_EVENT and not keyed
            if keyed or is_seal:
                if not self.key:
                    return out(False, n, r["seq"], "key_unavailable", keyed, sealed_at)       # a keyed chain cannot be verified (or extended) without its key
                want = _mac(self.key, prev, rec)
            else:
                want = _digest(prev, rec)
            if r["seq"] != expect or r["prev_hash"] != prev or not hmac.compare_digest(str(r["hash"]), want):
                return out(False, n, r["seq"], "chain_broken", keyed, sealed_at)
            if is_seal:
                d = rec["detail"]
                if d.get("legacy_head") != prev or d.get("legacy_rows") != n:
                    return out(False, n, r["seq"], "seal_mismatch", keyed, sealed_at)
                keyed, sealed_at = True, r["seq"]
            prev, expect, n = r["hash"], expect + 1, n + 1
            hashes[r["seq"]] = r["hash"]
        if not (self.key and self.anchor_path):
            return out(True, n, None, None, keyed, sealed_at)
        # anchor: every (seq, hash) ever published must still be in the database, and no anchor line may have been forged or edited
        try:
            with open(self.anchor_path) as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except FileNotFoundError:
            lines = None
        except OSError:
            return out(False, n, None, "anchor_unreadable", keyed, sealed_at, {"status": "unreadable", "entries": 0})
        if lines is None:
            st = "missing" if n else "empty"
            return out(not n, n, None, "anchor_missing" if n else None, keyed, sealed_at, {"status": st, "entries": 0})
        for ln in lines:
            parts = ln.split(" ")
            try:
                seq, h, m = int(parts[0]), parts[1], parts[2]
                good = len(parts) == 3 and hmac.compare_digest(m, _anchor_mac(self.key, seq, h))
            except (ValueError, IndexError):
                good = False
            if not good:
                return out(False, n, None, "anchor_tampered", keyed, sealed_at, {"status": "tampered", "entries": len(lines)})
            if hashes.get(seq) != h:
                return out(False, n, seq, "truncated_or_rewritten" if seq not in hashes else "anchor_mismatch", keyed, sealed_at, {"status": "mismatch", "entries": len(lines)})
        return out(True, n, None, None, keyed, sealed_at, {"status": "ok", "entries": len(lines)})
