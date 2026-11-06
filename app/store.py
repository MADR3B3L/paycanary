"""SQLite persistence (stdlib). One connection per Store; callers serialize via a lock (single-process demo service)."""
import json
import threading

from .dbsafe import LockedDB

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (name TEXT NOT NULL, version INTEGER NOT NULL, fields_json TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY (name, version));
CREATE TABLE IF NOT EXISTS policies (hash TEXT PRIMARY KEY, body_json TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agents (agent_id TEXT PRIMARY KEY, config_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intents (intent_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, ts REAL NOT NULL,
  stated_payees_json TEXT NOT NULL, plan_text TEXT);
CREATE TABLE IF NOT EXISTS payments (payment_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, ts REAL NOT NULL, payee TEXT,
  recipient TEXT, amount REAL, currency TEXT, memo TEXT, hour INTEGER, intent_id TEXT, verdict TEXT, score_kind TEXT,
  reason TEXT, status TEXT NOT NULL, paypal_batch_id TEXT, paypal_status TEXT, decided_by TEXT, event_json TEXT,
  narration TEXT, risk REAL, llm_flag TEXT, mode TEXT, enforced INTEGER, evidence_json TEXT, release_at REAL);
CREATE TABLE IF NOT EXISTS external_history (ref TEXT PRIMARY KEY, ts REAL NOT NULL, recipient TEXT NOT NULL, amount REAL NOT NULL,
  currency TEXT, payee_name TEXT, memo TEXT, category TEXT);
CREATE TABLE IF NOT EXISTS enrichment_cache (domain TEXT PRIMARY KEY, evidence_json TEXT NOT NULL, fetched_ts REAL NOT NULL, ttl REAL NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency (agent_id TEXT NOT NULL, idem_key TEXT NOT NULL, payment_id TEXT NOT NULL, fingerprint TEXT, response_json TEXT, PRIMARY KEY (agent_id, idem_key));
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event TEXT NOT NULL, actor TEXT NOT NULL, agent_id TEXT, payment_id TEXT,
  mode TEXT, detail TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TABLE IF NOT EXISTS llm_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS payments_agent_ts ON payments(agent_id, ts);
CREATE TABLE IF NOT EXISTS webhook_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event_type TEXT,
  resource_id TEXT, verified INTEGER, raw TEXT);
"""


class Store:
    def __init__(self, path=":memory:"):
        self.lock = threading.RLock()
        self.db = LockedDB(path, self.lock)                  # one shared connection; every statement runs under self.lock (see app/dbsafe.py)
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(payments)")}
        for c, t in (("narration", "TEXT"), ("risk", "REAL"), ("llm_flag", "TEXT"), ("mode", "TEXT"), ("enforced", "INTEGER"), ("evidence_json", "TEXT"), ("release_at", "REAL"), ("policy_hash", "TEXT"), ("provider", "TEXT"), ("provider_ref", "TEXT")):      # migrate older db files
            if c not in cols:
                self.db.execute(f"ALTER TABLE payments ADD COLUMN {c} {t}")
        wcols = {r["name"] for r in self.db.execute("PRAGMA table_info(webhook_events)")}
        if "provider" not in wcols:
            self.db.execute("ALTER TABLE webhook_events ADD COLUMN provider TEXT")
        icols = {r["name"] for r in self.db.execute("PRAGMA table_info(idempotency)")}
        for c in ("fingerprint", "response_json"):
            if c not in icols:
                self.db.execute(f"ALTER TABLE idempotency ADD COLUMN {c} TEXT")
        ecols = {r["name"] for r in self.db.execute("PRAGMA table_info(external_history)")}
        for c in ("currency", "payee_name", "memo", "category"):
            if c not in ecols:
                self.db.execute(f"ALTER TABLE external_history ADD COLUMN {c} TEXT")
        self.db.commit()

    def set_agent(self, agent_id, config):
        with self.lock:
            self.db.execute("INSERT INTO agents VALUES (?,?) ON CONFLICT(agent_id) DO UPDATE SET config_json=excluded.config_json",
                            (agent_id, json.dumps(config)))
            self.db.commit()

    def put_profile(self, name, fields, ts):
        """Append a NEW version (never edits one in place). -> version."""
        with self.lock:
            v = (self.db.execute("SELECT MAX(version) m FROM profiles WHERE name=?", (name,)).fetchone()["m"] or 0) + 1
            self.db.execute("INSERT INTO profiles VALUES (?,?,?,?)", (name, v, json.dumps(fields, sort_keys=True), ts))
            self.db.commit()
            return v

    def profile(self, name, version=None):
        q, a = "SELECT * FROM profiles WHERE name=?", [name]
        if version is not None:
            q += " AND version=?"; a.append(int(version))
        r = self.db.execute(q + " ORDER BY version DESC LIMIT 1", a).fetchone()
        return {"name": r["name"], "version": r["version"], "fields": json.loads(r["fields_json"]), "ts": r["ts"]} if r else None

    def profiles(self):
        return [{"name": r["name"], "version": r["version"], "fields": json.loads(r["fields_json"]), "ts": r["ts"]}
                for r in self.db.execute("SELECT * FROM profiles ORDER BY name, version").fetchall()]

    def policy_known(self, h):
        return self.db.execute("SELECT 1 FROM policies WHERE hash=?", (h,)).fetchone() is not None

    def policy_put(self, h, body, ts):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO policies VALUES (?,?,?)", (h, json.dumps(body, sort_keys=True), ts))
            self.db.commit()

    def reset_demo_data(self):
        """DEMO ONLY (called by a forced re-seed): clears payments, intents, idempotency keys, cached enrichment and webhook events so the seed starts clean.
        The audit log is append-only and is never touched; agents, profiles and policy snapshots are kept. -> row counts removed."""
        out = {}
        with self.lock:
            for t in ("payments", "intents", "idempotency", "enrichment_cache", "webhook_events"):
                out[t] = self.db.execute("DELETE FROM %s" % t).rowcount
            self.db.commit()
        return out

    def agents(self):
        return {r["agent_id"]: json.loads(r["config_json"]) for r in self.db.execute("SELECT agent_id, config_json FROM agents ORDER BY agent_id").fetchall()}

    def agent(self, agent_id):
        r = self.db.execute("SELECT config_json FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        return json.loads(r["config_json"]) if r else {}

    def add_intent(self, intent_id, agent_id, ts, payees, plan_text):
        with self.lock:
            self.db.execute("INSERT INTO intents VALUES (?,?,?,?,?)", (intent_id, agent_id, ts, json.dumps(payees), plan_text))
            self.db.commit()

    def intent(self, intent_id):
        r = self.db.execute("SELECT * FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        return dict(r) if r else None

    def latest_intent(self, agent_id, since_ts):
        r = self.db.execute("SELECT * FROM intents WHERE agent_id=? AND ts>=? ORDER BY ts DESC LIMIT 1", (agent_id, since_ts)).fetchone()
        return dict(r) if r else None

    def insert_payment(self, row):
        cols = ",".join(row)
        with self.lock:
            self.db.execute(f"INSERT INTO payments ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
            self.db.commit()

    def update_payment(self, payment_id, **fields):
        if "provider" in fields:
            raise ValueError("a payment's provider is fixed when it is created and can never be changed")          # routing cannot be rewritten after the decision
        sets = ",".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE payments SET {sets} WHERE payment_id=?", (*fields.values(), payment_id))
            self.db.commit()

    def payment(self, payment_id):
        r = self.db.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
        return dict(r) if r else None

    def payment_by_batch(self, batch_id):
        r = self.db.execute("SELECT * FROM payments WHERE paypal_batch_id=?", (batch_id,)).fetchone()
        return dict(r) if r else None

    def payment_by_provider_ref(self, provider, raw_id):
        """The payment routed to `provider` whose provider-side id is `raw_id` (the stored ref is '<provider>:<raw_id>'). Never matches another provider's payment."""
        r = self.db.execute("SELECT * FROM payments WHERE provider=? AND provider_ref=?", (provider, provider + ":" + str(raw_id))).fetchone()
        return dict(r) if r else None

    def payments(self, agent_id=None, status=None, limit=200):
        q, args = "SELECT * FROM payments WHERE 1=1", []
        if agent_id:
            q += " AND agent_id=?"; args.append(agent_id)
        if status:
            q += " AND status=?"; args.append(status)
        q += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
        return [dict(r) for r in self.db.execute(q, args).fetchall()]

    def baseline_rows(self, agent_id):
        """Executed payments that may teach the baseline: verdict allow/review only (never frozen, even if later released)."""
        return self.db.execute("SELECT payee, amount, ts, hour, recipient, currency FROM payments WHERE agent_id=? AND verdict IN ('allow','review') "
                               "AND status IN ('sent','settled') ORDER BY ts", (agent_id,)).fetchall()

    def attempt_times(self, agent_id, since_ts):
        return [r["ts"] for r in self.db.execute("SELECT ts FROM payments WHERE agent_id=? AND ts>=?", (agent_id, since_ts)).fetchall()]

    def ext_add(self, rows):
        n = 0
        with self.lock:
            for r in rows:
                cur = self.db.execute("INSERT OR IGNORE INTO external_history (ref, ts, recipient, amount, currency, payee_name, memo, category) VALUES (?,?,?,?,?,?,?,?)",
                                      (r["ref"], r["ts"], r["recipient"], r["amount"], r.get("currency") or "USD", r.get("payee_name") or "", r.get("memo") or "", r.get("category") or ""))
                n += cur.rowcount
            self.db.commit()
        return n

    def ext_rows(self, since_ts):
        return self.db.execute("SELECT ts, recipient, amount, currency, payee_name, memo, category FROM external_history WHERE ts>=? ORDER BY ts", (since_ts,)).fetchall()

    def recipient_names(self):
        """recipient email -> the payee name PayCanary has used for it (so external history lines up with our payee names)."""
        return {r["recipient"]: r["payee"] for r in self.db.execute("SELECT recipient, payee FROM payments GROUP BY recipient").fetchall()}

    def idem_get(self, agent_id, key):
        r = self.db.execute("SELECT payment_id, fingerprint, response_json FROM idempotency WHERE agent_id=? AND idem_key=?", (agent_id, key)).fetchone()
        return dict(r) if r else None

    def idem_put(self, agent_id, key, fingerprint, payment_id):
        """Reserve this idempotency key. Returns True if THIS caller won the reservation, False if another
        caller already holds it (across processes sharing the DB). The caller must only send when it won."""
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO idempotency (agent_id, idem_key, payment_id, fingerprint) VALUES (?,?,?,?)", (agent_id, key, payment_id, fingerprint))
            self.db.commit()
            return cur.rowcount == 1

    def idem_set_response(self, agent_id, key, response_json):
        with self.lock:
            self.db.execute("UPDATE idempotency SET response_json=? WHERE agent_id=? AND idem_key=?", (response_json, agent_id, key))
            self.db.commit()

    def llm_usage(self, day):
        r = self.db.execute("SELECT calls, tokens FROM llm_usage WHERE day=?", (day,)).fetchone()
        return (r["calls"], r["tokens"]) if r else (0, 0)

    def llm_bump(self, day, calls=0, tokens=0):
        with self.lock:
            self.db.execute("INSERT INTO llm_usage (day, calls, tokens) VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET "
                            "calls=calls+excluded.calls, tokens=tokens+excluded.tokens", (day, calls, tokens))
            self.db.commit()

    def recent_payments(self, agent_id, limit=10):
        return [dict(r) for r in self.db.execute("SELECT payee, amount, verdict, ts FROM payments WHERE agent_id=? "
                                                 "ORDER BY ts DESC LIMIT ?", (agent_id, limit)).fetchall()]

    def window_rows(self, agent_id, since_ts):
        """Money that moved or is in flight since `since_ts`, whatever the original verdict (a released freeze still moved money)."""
        return self.db.execute("SELECT ts, payee, amount, currency FROM payments WHERE agent_id=? AND ts>=? AND status IN ('pending','sent','settled') "
                               "ORDER BY ts", (agent_id, since_ts)).fetchall()

    def attempt_rows(self, agent_id, since_ts):
        return self.db.execute("SELECT ts, payee FROM payments WHERE agent_id=? AND ts>=?", (agent_id, since_ts)).fetchall()

    def due_cooldowns(self, now):
        return [dict(r) for r in self.db.execute("SELECT * FROM payments WHERE status='frozen' AND release_at IS NOT NULL AND release_at<=? ORDER BY release_at", (now,)).fetchall()]

    def claim_release(self, payment_id, by):
        """Atomically take a still-frozen payment for release: exactly one caller wins, so a sweep can never send twice or override a denial."""
        with self.lock:
            n = self.db.execute("UPDATE payments SET status='pending', decided_by=?, release_at=NULL WHERE payment_id=? AND status='frozen'", (by, payment_id)).rowcount
            self.db.commit()
        return n == 1

    def claim_deny(self, payment_id, by):
        """Atomically take a still-frozen payment for denial: exactly one of release / deny / sweep can win a frozen payment; a loser changes nothing."""
        with self.lock:
            n = self.db.execute("UPDATE payments SET status='denied', decided_by=?, release_at=NULL WHERE payment_id=? AND status='frozen'", (by, payment_id)).rowcount
            self.db.commit()
        return n == 1

    def ev_get(self, domain, now):
        r = self.db.execute("SELECT evidence_json, fetched_ts, ttl FROM enrichment_cache WHERE domain=?", (domain,)).fetchone()
        return json.loads(r["evidence_json"]) if r and now - r["fetched_ts"] < r["ttl"] else None

    def ev_put(self, domain, evidence, now, ttl):
        with self.lock:
            self.db.execute("INSERT INTO enrichment_cache VALUES (?,?,?,?) ON CONFLICT(domain) DO UPDATE SET evidence_json=excluded.evidence_json, fetched_ts=excluded.fetched_ts, ttl=excluded.ttl",
                            (domain, json.dumps(evidence), now, ttl))
            self.db.commit()

    def known_payee(self, agent_id, payee):
        return self.db.execute("SELECT 1 FROM payments WHERE agent_id=? AND payee=? AND verdict IN ('allow','review') "
                               "AND status IN ('sent','settled') LIMIT 1", (agent_id, payee)).fetchone() is not None

    def add_webhook(self, ts, event_type, resource_id, verified, raw, provider="paypal"):
        with self.lock:
            self.db.execute("INSERT INTO webhook_events (ts,event_type,resource_id,verified,raw,provider) VALUES (?,?,?,?,?,?)",
                            (ts, event_type, resource_id, int(verified), raw, provider))
            self.db.commit()

    def webhooks(self, limit=100):
        return [dict(r) for r in self.db.execute("SELECT * FROM webhook_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
