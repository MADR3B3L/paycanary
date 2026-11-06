"""SERVICE-LEVEL adversarial tests (distinct from gate-logic tests): the tamper-evident audit log under attack, hostile/malformed input to the HTTP surface,
and concurrency/races. Tests named test_KNOWN_GAP_* assert the CURRENT (weak) behaviour on purpose: they document a real limitation so nobody oversells it,
and they fail loudly the day the limitation is fixed (then flip them into guards)."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from app.audit import AuditLog, _digest, GENESIS
from app.providers.mock.adapter import MockProvider
from app.service import Service
from app.store import Store
from tests import close_when_done, read_file

try:
    os.environ.setdefault("PAYCANARY_DB", ":memory:")
    os.environ["PAYCANARY_OPERATOR_TOKEN"] = "op-token-adversarial"
    os.environ["PAYCANARY_AGENT_TOKEN"] = "agent-token-adversarial"
    from fastapi.testclient import TestClient
    from app import main
    HAVE_HTTP = True
except Exception:
    HAVE_HTTP = False

OP = {"Authorization": "Bearer op-token-adversarial"}
AG = {"Authorization": "Bearer agent-token-adversarial"}


def file_store():
    path = os.path.join(tempfile.mkdtemp(), "a.sqlite3")
    return Store(path), path


KEY = b"unit-test-audit-key-0123456789abcdef"


def seeded_log(n=6, keyed=True):
    st, path = file_store()
    anchor = os.path.join(os.path.dirname(path), "anchors", "a.log") if keyed else None
    log = AuditLog(st, lambda: 1000.0, key=KEY if keyed else None, anchor_path=anchor)
    for i in range(n):
        log.append("evt%d" % i, "actor", agent_id="a", payment_id="pay_%d" % i, detail={"i": i, "amount": 10.0 * i})
    log.anchor_file = anchor
    return st, path, log


def raw(path):
    c = close_when_done(sqlite3.connect(path))
    c.row_factory = sqlite3.Row
    return c


def recompute(path, key=None, upto_seal=False):
    """Play the forger: drop the triggers, rewrite row 4 and recompute every later hash (keyless digest, or with `key`)."""
    c = raw(path)
    c.execute("DROP TRIGGER IF EXISTS audit_no_update")
    rows = [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY seq").fetchall()]
    target = [r for r in rows if r["event"] == "evt2"][0]
    target["detail"] = json.dumps({"i": 2, "amount": 0.0}, sort_keys=True, separators=(",", ":"))
    prev = GENESIS
    for r in rows:
        rec = {"ts": r["ts"], "event": r["event"], "actor": r["actor"], "agent_id": r["agent_id"], "payment_id": r["payment_id"], "mode": r["mode"], "detail": json.loads(r["detail"])}
        if key:
            import hmac, hashlib
            h = hmac.new(key, (prev + "|" + json.dumps(rec, sort_keys=True, separators=(",", ":"))).encode(), hashlib.sha256).hexdigest()
        else:
            h = _digest(prev, rec)
        c.execute("UPDATE audit SET detail=?, prev_hash=?, hash=? WHERE seq=?", (r["detail"], prev, h, r["seq"]))
        prev = h
    c.commit()


class AuditUnderAttack(unittest.TestCase):
    def test_clean_keyed_log_verifies_and_reports_keyed_and_anchored(self):
        _, _, log = seeded_log()
        v = log.verify()
        self.assertTrue(v["ok"], v)
        self.assertTrue(v["keyed"])
        self.assertEqual(v["anchor"]["status"], "ok")
        self.assertGreaterEqual(v["anchor"]["entries"], 7)                         # six rows plus the seal row

    def test_update_and_delete_are_refused_by_triggers(self):
        st, path, log = seeded_log()
        c = raw(path)
        for sql in ("UPDATE audit SET actor='x' WHERE seq=2", "DELETE FROM audit WHERE seq=2", "UPDATE audit SET hash='0' WHERE seq=3"):
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute(sql)
        self.assertTrue(log.verify()["ok"])

    def test_editing_a_past_row_with_triggers_dropped_is_detected(self):
        st, path, log = seeded_log()
        c = raw(path)
        c.execute("DROP TRIGGER audit_no_update")
        c.execute("UPDATE audit SET detail='{\"i\":99,\"amount\":1.0}' WHERE seq=4")
        c.commit()
        v = log.verify()
        self.assertFalse(v["ok"])
        self.assertEqual(v["first_bad_seq"], 4)

    def test_deleting_a_middle_row_is_detected(self):
        st, path, log = seeded_log()
        c = raw(path)
        c.execute("DROP TRIGGER audit_no_delete")
        c.execute("DELETE FROM audit WHERE seq=4")
        c.commit()
        self.assertFalse(log.verify()["ok"])

    def test_swapping_two_rows_is_detected(self):
        st, path, log = seeded_log()
        c = raw(path)
        c.execute("DROP TRIGGER audit_no_update")
        c.execute("UPDATE audit SET seq=-1 WHERE seq=3")
        c.execute("UPDATE audit SET seq=3 WHERE seq=4")
        c.execute("UPDATE audit SET seq=4 WHERE seq=-1")
        c.commit()
        self.assertFalse(log.verify()["ok"])

    def test_replaying_a_row_as_a_duplicate_append_is_detected(self):
        st, path, log = seeded_log()
        c = raw(path)
        r = c.execute("SELECT * FROM audit WHERE seq=3").fetchone()
        c.execute("INSERT INTO audit (ts,event,actor,agent_id,payment_id,mode,detail,prev_hash,hash) VALUES (?,?,?,?,?,?,?,?,?)",
                  (r["ts"], r["event"], r["actor"], r["agent_id"], r["payment_id"], r["mode"], r["detail"], r["prev_hash"], r["hash"]))
        c.commit()
        self.assertFalse(log.verify()["ok"])

    def test_FIXED_tail_truncation_is_detected_by_the_anchor(self):
        st, path, log = seeded_log()
        c = raw(path)
        c.execute("DROP TRIGGER audit_no_delete")
        c.execute("DELETE FROM audit WHERE seq>=5")
        c.commit()
        v = log.verify()
        self.assertFalse(v["ok"], v)                                                 # was a KNOWN GAP: verify() used to say OK
        self.assertIn(v["reason"], ("truncated_or_rewritten", "anchor_mismatch"))

    def test_FIXED_full_chain_recompute_forgery_without_the_key_fails(self):
        st, path, log = seeded_log()
        recompute(path)                                                              # forger recomputes with the public algorithm (no key)
        v = log.verify()
        self.assertFalse(v["ok"], v)                                                 # was a KNOWN GAP: verify() used to pass
        self.assertEqual(v["reason"], "chain_broken")

    def test_FIXED_forgery_with_a_wrong_key_fails(self):
        st, path, log = seeded_log()
        recompute(path, key=b"attacker-guess-of-the-key-xxxxxxxxxxxx")
        self.assertFalse(log.verify()["ok"])

    def test_forgery_by_someone_holding_the_key_but_not_the_anchor_is_caught_by_the_anchor(self):
        st, path, log = seeded_log()
        recompute(path, key=KEY)                                                     # the chain now verifies on its own...
        v = log.verify()
        self.assertFalse(v["ok"], v)                                                 # ...but every anchored (seq, hash) from before no longer matches
        self.assertEqual(v["anchor"]["status"], "mismatch")

    def test_anchor_line_edits_and_deletion_are_detected(self):
        st, path, log = seeded_log()
        with open(log.anchor_file) as fh: lines = fh.read().splitlines()
        with open(log.anchor_file, "w") as fh: fh.write("\n".join(lines[:-1] + [lines[-1][:-3] + "abc"]) + "\n")
        self.assertEqual(log.verify()["reason"], "anchor_tampered")
        os.remove(log.anchor_file)
        v = log.verify()
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "anchor_missing")

    def test_a_keyed_chain_cannot_be_verified_or_extended_without_its_key(self):
        st, path, log = seeded_log()
        nokey = AuditLog(st, lambda: 1000.0)
        self.assertEqual(nokey.verify()["reason"], "key_unavailable")
        self.assertIsNone(nokey.append("sneaky", "x"))                               # refuses to write unkeyed rows into a keyed chain
        self.assertTrue(log.verify()["ok"])

    def test_migration_from_a_legacy_unkeyed_log_pins_the_old_head(self):
        st, path, legacy = seeded_log(keyed=False)
        keyed = AuditLog(st, lambda: 1000.0, key=KEY, anchor_path=os.path.join(os.path.dirname(path), "anchors", "m.log"))
        keyed.append("after_key", "x")
        v = keyed.verify()
        self.assertTrue(v["ok"], v)
        self.assertTrue(v["keyed"])
        self.assertIsNotNone(v["sealed_at"])
        c = raw(path)                                                                # now tamper with a LEGACY row and recompute the legacy digests
        c.execute("DROP TRIGGER audit_no_update")
        rows = [dict(r) for r in c.execute("SELECT * FROM audit WHERE seq<=6 ORDER BY seq").fetchall()]
        rows[1]["detail"] = json.dumps({"i": 1, "amount": 0.0}, sort_keys=True, separators=(",", ":"))
        prev = GENESIS
        for r in rows:
            rec = {"ts": r["ts"], "event": r["event"], "actor": r["actor"], "agent_id": r["agent_id"], "payment_id": r["payment_id"], "mode": r["mode"], "detail": json.loads(r["detail"])}
            h = _digest(prev, rec)
            c.execute("UPDATE audit SET detail=?, prev_hash=?, hash=? WHERE seq=?", (r["detail"], prev, h, r["seq"]))
            prev = h
        c.commit()
        self.assertFalse(keyed.verify()["ok"])                                       # the keyed seal row pinned the real legacy head

    def test_KEYLESS_log_is_still_forgeable_and_says_so(self):
        """No key = no tamper-evidence claim against someone who can write the file. This is why main.py always keys a file-backed database, and why verify()
        reports keyed:false so nobody mistakes a keyless log for a protected one."""
        st, path, log = seeded_log(keyed=False)
        recompute(path)
        v = log.verify()
        self.assertTrue(v["ok"])
        self.assertFalse(v["keyed"])
        self.assertEqual(v["anchor"]["status"], "not_configured")

    def test_append_is_best_effort_and_returns_none_on_failure_but_config_changes_refuse(self):
        st, path = file_store()
        svc = Service(st, MockProvider(), clock=lambda: 1000.0)
        st.db.execute("DROP TABLE audit")                            # make appends fail
        self.assertIsNone(svc.audit.append("x", "a"))
        with self.assertRaises(Exception):
            svc.set_agent("agent-z", hard_cap=100.0)                 # a config change whose audit append fails is refused, not applied silently
        self.assertFalse(st.agent("agent-z").get("hard_cap"))

    def test_the_key_never_lives_in_the_database_file(self):
        st, path, log = seeded_log()
        data = read_file(path, "rb")
        self.assertNotIn(KEY, data)


class HostileInput(unittest.TestCase):
    PAY = {"agent_id": "adv", "payee": "Acme", "recipient_email": "a@acme.example", "amount": "10.00", "currency": "USD"}

    def setUp(self):
        self.st = Store(":memory:")
        self.mock = MockProvider()
        self.svc = Service(self.st, self.mock, clock=lambda: 1000.0, platform_cap=5000.0)

    def test_malformed_pay_requests_fail_closed_and_never_reach_the_provider(self):
        bad = [{}, {"agent_id": None}, {**self.PAY, "amount": "NaN"}, {**self.PAY, "amount": "Infinity"}, {**self.PAY, "amount": "-5"},
               {**self.PAY, "amount": "0"}, {**self.PAY, "amount": "1e400"}, {**self.PAY, "amount": "9" * 400}, {**self.PAY, "amount": ["10"]},
               {**self.PAY, "amount": {"$gt": 0}}, {**self.PAY, "currency": "ZZZ"}, {**self.PAY, "currency": None}, {**self.PAY, "recipient_email": "not-an-email"},
               {**self.PAY, "recipient_email": "a@b.example\r\nBcc: evil@x.example"}, {**self.PAY, "payee": "x" * 100000}, {**self.PAY, "agent_id": "../../etc/passwd"},
               {**self.PAY, "memo": "\x00\x01\x02" * 100}, {**self.PAY, "amount": True}, {**self.PAY, "amount": 1e309}]
        for b in bad:
            try:
                out = self.svc.pay(b)
            except Exception as e:                                   # an exception is acceptable only if it is a clean validation error, never a send
                out = {"exc": type(e).__name__}
            self.assertNotEqual(out.get("status"), "sent", b)
            self.assertNotEqual(out.get("decision"), "allow") if out.get("decision") else None
        self.assertEqual(self.mock.n, 0, "hostile input reached the provider")

    def test_oversized_amount_cannot_overflow_into_a_send(self):
        out = self.svc.pay({**self.PAY, "amount": "99999999999999.99"})
        self.assertNotEqual(out.get("status"), "sent")
        self.assertEqual(self.mock.n, 0)

    def test_sql_injection_strings_are_data_not_sql(self):
        out = self.svc.pay({**self.PAY, "payee": "x'; DROP TABLE payments;--", "agent_id": "adv'; DELETE FROM audit;--"})
        self.assertTrue(self.st.db.execute("SELECT count(*) FROM payments").fetchone()[0] >= 0)
        self.assertTrue(self.svc.audit.verify()["ok"])

    @unittest.skipUnless(HAVE_HTTP, "needs fastapi + httpx (run with .venv/bin/python)")
    def test_http_surface_never_500s_on_garbage_and_never_sends(self):
        from tests.test_service import FakePayPal
        main._store = Store(":memory:")
        mock = MockProvider()
        main._service = Service(main._store, mock, platform_cap=5000.0)
        c = TestClient(main.app, raise_server_exceptions=False)
        bodies = [b"", b"not json", b"[]", b"null", b"\xff\xfe\x00", b"{" * 5000, b'{"agent_id":' + b"[" * 3000, json.dumps({"agent_id": "adv", "amount": "NaN"}).encode(), b"x" * 2_000_000]
        for path in ("/pay", "/intent", "/api/agents", "/webhooks/paypal", "/webhooks/stripe", "/webhooks/mock", "/webhooks/nope", "/webhooks/../etc"):
            for b in bodies:
                for h in ({}, AG, OP):
                    r = c.post(path, content=b, headers={**h, "content-type": "application/json"})
                    self.assertLess(r.status_code, 500, (path, b[:20], h, r.status_code))
        self.assertEqual(mock.n, 0)
        self.assertTrue(main.service().audit.verify()["ok"])

    @unittest.skipUnless(HAVE_HTTP, "needs fastapi + httpx (run with .venv/bin/python)")
    def test_token_edge_cases_are_all_rejected(self):
        main._store = Store(":memory:")
        main._service = Service(main._store, MockProvider(), platform_cap=5000.0)
        c = TestClient(main.app, raise_server_exceptions=False)
        for h in ({"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer "}, 
                  {"Authorization": "Bearer op-token-adversarial\x00"}, {"Authorization": "Basic b3A6"},
                  {"Authorization": "Bearer " + "A" * 100000}, {"Authorization": "Bearer op-token-adversariaL"}):
            try:
                r = c.get("/api/payments", headers=h)
            except Exception:
                continue                                              # an HTTP-layer rejection of an invalid header is also a rejection
            self.assertIn(r.status_code, (401, 400, 422, 431), (h, r.status_code))
            self.assertNotEqual(r.status_code, 200)


class Races(unittest.TestCase):
    def _frozen(self):
        st = Store(":memory:")
        mock = MockProvider()
        svc = Service(st, mock, clock=lambda: 1000.0, platform_cap=5000.0)
        out = svc.pay({"agent_id": "race", "payee": "Big", "recipient_email": "b@big.example", "amount": "4000.00", "currency": "USD", "memo": "m"})
        self.assertEqual(out["status"], "frozen", out)
        return svc, mock, out["payment_id"]

    def test_concurrent_double_release_sends_at_most_once(self):
        svc, mock, pid = self._frozen()
        res = []
        ts = [threading.Thread(target=lambda: res.append(svc.release(pid))) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(mock.n, 1, "money moved more than once: %s sends" % mock.n)         # idempotency key (derived from the payment id) holds even if the claim is not atomic

    def test_FIXED_release_and_deny_cannot_both_win_a_frozen_payment(self):
        """release()/deny()/confirm_fraud() now take the payment with an atomic UPDATE ... WHERE status='frozen'; exactly one wins. Was a KNOWN GAP (deny could
        overwrite a payment to 'denied' after a concurrent release had already sent the money)."""
        for _ in range(30):
            svc, mock, pid = self._frozen()
            res = {}
            ts = [threading.Thread(target=lambda: res.__setitem__("r", svc.release(pid))), threading.Thread(target=lambda: res.__setitem__("d", svc.deny(pid))),
                  threading.Thread(target=lambda: res.__setitem__("f", svc.confirm_fraud(pid)))]
            [t.start() for t in ts]
            [t.join() for t in ts]
            status = svc.store.payment(pid)["status"]
            if mock.n == 1:
                self.assertIn(status, ("sent", "pending", "paypal_error", "provider_error"), (status, res))        # money moved => the record never says denied
            else:
                self.assertEqual((mock.n, status), (0, "denied"), (status, res))
            self.assertLessEqual(mock.n, 1)
            self.assertTrue(svc.audit.verify()["ok"])

    def test_a_deny_that_loses_the_race_changes_nothing(self):
        svc, mock, pid = self._frozen()
        svc.release(pid)
        self.assertFalse(svc.deny(pid)["ok"])
        self.assertNotEqual(svc.store.payment(pid)["status"], "denied")


class Stress(unittest.TestCase):
    def test_parallel_pays_keep_audit_chain_valid_and_idempotency_holds(self):
        st = Store(":memory:")
        mock = MockProvider()
        svc = Service(st, mock, clock=lambda: 1000.0, platform_cap=5000.0)
        errs = []

        def worker(i):
            try:
                svc.pay({"agent_id": "s%d" % (i % 5), "payee": "V%d" % (i % 7), "recipient_email": "v%d@x.example" % (i % 7), "amount": "%d.00" % (10 + i), "currency": "USD", "memo": "m"},
                        request_id="same-key" if i % 10 == 0 else "k-%d" % i)
            except Exception as e:
                errs.append(repr(e))
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(80)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertTrue(svc.audit.verify()["ok"])
        keys = [e["idempotency_key"] for e in mock.ledger]
        self.assertEqual(len(keys), len(set(keys)), "a provider key was used twice (double send)")


if __name__ == "__main__":
    unittest.main()
