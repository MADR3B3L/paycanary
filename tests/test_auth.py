import json, os, unittest

os.environ["PAYCANARY_DB"] = ":memory:"
os.environ["PAYCANARY_OPERATOR_TOKEN"] = "op-token-for-tests"
os.environ["PAYCANARY_AGENT_TOKEN"] = "shared-agent-token-for-tests"
try:
    from fastapi.testclient import TestClient
    from app import main
    from app.service import Service
    from tests.test_service import FakePayPal
    HAVE = True
except Exception:                                              # fastapi/httpx live in .venv; the stdlib-only suite skips this file
    HAVE = False

OP = {"Authorization": "Bearer op-token-for-tests"}
AG = {"Authorization": "Bearer shared-agent-token-for-tests"}
PAY = {"agent_id": "v", "payee": "AttackerWallet", "recipient_email": "atk@example.com", "amount": "250000.00", "currency": "USD"}


@unittest.skipUnless(HAVE, "needs fastapi + httpx (run with .venv/bin/python)")
class Auth(unittest.TestCase):
    def setUp(self):
        from app.store import Store
        main._store = Store(":memory:")
        self.pp = FakePayPal()
        main._service = Service(main._store, self.pp, platform_cap=5000.0)
        self.c = TestClient(main.app)

    def test_every_mutating_or_reading_route_rejects_no_credential(self):
        for method, path, body in [("post", "/pay", PAY), ("post", "/intent", {"agent_id": "v", "stated_payees": ["A"]}),
                                   ("post", "/api/agents", {"agent_id": "v"}), ("post", "/api/payments/x/release", {}),
                                   ("post", "/api/payments/x/deny", {}), ("get", "/api/payments", None), ("get", "/api/webhooks", None),
                                   ("post", "/api/agents/v/token", {}), ("get", "/api/paypal/check", None), ("post", "/demo/seed", {})]:
            r = getattr(self.c, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 401, (path, r.status_code))
        self.assertEqual(self.pp.calls, [])

    def test_redteam_finding1_agent_cannot_reconfigure_its_own_guard(self):
        r = self.c.post("/api/agents", json={"agent_id": "v", "hard_cap": 5000000, "require_intent": False}, headers=AG)
        self.assertEqual(r.status_code, 401)                                      # the agent credential has no control-plane power
        out = self.c.post("/pay", json={**PAY, "amount": "999999.00"}, headers=AG).json()
        self.assertEqual(out["decision"], "freeze"); self.assertEqual(self.pp.calls, [])

    def test_redteam_finding2_agent_cannot_release_its_own_frozen_payment(self):
        out = self.c.post("/pay", json={**PAY, "amount": "5000.00"}, headers=AG).json()
        self.assertEqual(out["status"], "frozen")
        for h in ({}, AG, {"Authorization": "Bearer nope"}):
            self.assertEqual(self.c.post(f"/api/payments/{out['payment_id']}/release", json={"by": "anyone"}, headers=h).status_code, 401)
            self.assertEqual(self.c.post(f"/api/payments/{out['payment_id']}/deny", headers=h).status_code, 401)
        self.assertEqual(self.pp.calls, [])
        rel = self.c.post(f"/api/payments/{out['payment_id']}/release", json={"by": "spoofed-name"}, headers=OP).json()
        self.assertTrue(rel["ok"]); self.assertEqual(main._store.payment(out["payment_id"])["decided_by"], "human:operator")   # identity from the credential, body ignored

    def test_roles_do_not_overlap(self):
        self.assertEqual(self.c.post("/pay", json=PAY, headers=OP).status_code, 401)          # the operator credential is not an agent credential
        self.assertEqual(self.c.post("/intent", json={"agent_id": "v", "stated_payees": ["A"]}, headers=OP).status_code, 401)
        self.assertEqual(self.c.get("/api/payments", headers=AG).status_code, 401)           # agents cannot read the queue either

    def test_per_agent_tokens_bind_to_one_agent_and_retire_the_shared_one(self):
        tok = self.c.post("/api/agents/a1/token", headers=OP).json()["agent_token"]
        mine = {"Authorization": "Bearer " + tok}
        ok = self.c.post("/pay", json={**PAY, "agent_id": "a1", "amount": "10.00"}, headers=mine)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.c.post("/pay", json={**PAY, "agent_id": "a2", "amount": "10.00"}, headers=mine).status_code, 401)   # cannot pay as someone else
        self.assertEqual(self.c.post("/pay", json={**PAY, "agent_id": "a1", "amount": "10.00"}, headers=AG).status_code, 401)     # shared token retired for a1
        self.c.post("/api/agents", json={"agent_id": "a1", "hard_cap": 100}, headers=OP)                                            # a config edit keeps the credential
        self.assertEqual(self.c.post("/pay", json={**PAY, "agent_id": "a1", "amount": "10.00"}, headers=mine).status_code, 200)

    def test_whitespace_agent_id_cannot_bypass_a_per_agent_token(self):
        # Codex finding 1: a holder of the SHARED token must not act as a token-protected agent by padding the id with
        # whitespace. Auth looked up the raw " a1 " (no row -> empty cfg -> shared token accepted) while the payment path
        # stripped to "a1". Auth must normalize the id the same way before the lookup + decision.
        self.c.post("/api/agents/a1/token", headers=OP)                               # a1 now has its own token; shared is retired for it
        for padded in (" a1", "a1 ", " a1 ", "\ta1\t", "a1\n"):
            r = self.c.post("/pay", json={**PAY, "agent_id": padded, "amount": "10.00"}, headers=AG)   # shared token + padded id
            self.assertEqual(r.status_code, 401, "padded id %r with shared token must be rejected" % padded)
        self.assertEqual(self.pp.calls, [])                                           # nothing ever reached the provider

    def test_omitted_require_intent_does_not_switch_the_guard_off(self):
        self.c.post("/api/agents", json={"agent_id": "v", "hard_cap": 100}, headers=OP)         # the old code stored require_intent=False here
        out = self.c.post("/pay", json={**PAY, "amount": "20.00"}, headers=AG).json()
        self.assertEqual(out["decision"], "freeze"); self.assertIn("no live stated intent", out["reasons"][0])

    def test_webhook_route_is_public_but_cannot_settle_anything_unverified(self):
        r = self.c.post("/pay", json={**PAY, "amount": "10.00", "payee": "Tiny"}, headers=AG).json()
        ev = {"event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS", "resource": {"batch_header": {"payout_batch_id": "B-1", "batch_status": "SUCCESS"}}}
        self.assertEqual(self.c.post("/webhooks/paypal", json=ev).status_code, 200)
        self.assertNotEqual(main._store.payment(r["payment_id"])["status"], "settled")


@unittest.skipUnless(HAVE, "needs fastapi + httpx (run with .venv/bin/python)")
class ProductLayerHTTP(unittest.TestCase):
    def setUp(self):
        from app.store import Store
        main._store = Store(":memory:")
        self.pp = FakePayPal()
        main._service = Service(main._store, self.pp, platform_cap=5000.0, default_require_intent=False)
        self.c = TestClient(main.app)
        self.body = {"agent_id": "v", "payee": "AWS", "recipient_email": "aws@example.com", "amount": "40.00", "currency": "USD"}

    def test_paypal_request_id_header_replays_conflicts_and_is_visible(self):
        h = {**AG, "PayPal-Request-Id": "order-1001"}
        a = self.c.post("/pay", json=self.body, headers=h); b = self.c.post("/pay", json=self.body, headers=h)
        self.assertEqual((a.status_code, b.status_code), (200, 200)); self.assertEqual(b.headers.get("Idempotent-Replay"), "true")
        self.assertEqual(a.json()["payment_id"], b.json()["payment_id"]); self.assertEqual(len(self.pp.calls), 1)
        c = self.c.post("/pay", json={**self.body, "amount": "41.00"}, headers=h)
        self.assertEqual(c.status_code, 422); self.assertEqual(c.json()["status"], "idempotency_conflict"); self.assertEqual(len(self.pp.calls), 1)
        self.assertEqual(self.c.post("/pay", json=self.body, headers={**AG, "PayPal-Request-Id": "bad key!"}).status_code, 400)

    def test_no_verdict_is_a_503_and_nothing_is_sent(self):
        main._service._history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store down"))
        r = self.c.post("/pay", json=self.body, headers=AG)
        self.assertEqual(r.status_code, 503); self.assertEqual(r.json()["status"], "fail_closed"); self.assertEqual(self.pp.calls, [])

    def test_audit_is_operator_only_and_logs_an_agent_knocking_on_the_control_plane(self):
        self.assertEqual(self.c.get("/api/audit", headers=AG).status_code, 401); self.assertEqual(self.c.get("/api/audit/verify").status_code, 401)
        self.c.post("/api/agents", json={"agent_id": "v", "hard_cap": 9999}, headers=AG)                  # the agent tries to reconfigure itself
        self.c.post("/pay", json=self.body, headers=AG)
        rows = self.c.get("/api/audit", headers=OP).json()["rows"]
        denied = [r for r in rows if r["event"] == "auth_denied" and r["actor"] == "agent-credential"]
        self.assertTrue(any("/api/agents" in d["detail"] for d in denied))
        v = self.c.get("/api/audit/verify", headers=OP).json(); self.assertTrue(v["ok"]); self.assertGreater(v["rows"], 1)
        self.assertNotIn("shared-agent-token-for-tests", json.dumps(rows)); self.assertNotIn("op-token-for-tests", json.dumps(rows))

    def test_confirm_fraud_is_operator_only_and_the_reputation_key_is_never_exposed(self):
        from app.reputation_store import Reputation
        from app import auth
        main._service.reputation = Reputation("test-reputation-key-0123456789")
        self.c.post("/api/agents", json={"agent_id": "v", "hard_cap": 10, "org_id": "acme"}, headers=OP)
        f = self.c.post("/pay", json={**self.body, "payee": "Shady", "recipient_email": "shady@evil.example", "amount": "400.00"}, headers=AG).json()
        for h in ({}, AG): self.assertEqual(self.c.post(f"/api/payments/{f['payment_id']}/confirm_fraud", headers=h).status_code, 401)
        r = self.c.post(f"/api/payments/{f['payment_id']}/confirm_fraud", headers=OP); self.assertTrue(r.json()["ok"])
        blob = json.dumps(self.c.get("/api/audit?limit=500", headers=OP).json()) + json.dumps(self.c.get("/api/payments", headers=OP).json())
        self.assertNotIn("test-reputation-key", blob)
        import tempfile, stat
        d = tempfile.mkdtemp(); k = auth.load_reputation_key(d); self.assertGreaterEqual(len(k), 32)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, ".paycanary_tokens.json")).st_mode), 0o600); self.assertEqual(auth.load_reputation_key(d), k)

    def test_agent_can_poll_only_its_own_payment_and_the_sweep_is_operator_only(self):
        f = self.c.post("/pay", json={**self.body, "payee": "Big", "recipient_email": "b@big-co.com", "amount": "9000.00"}, headers=AG).json()
        mine = self.c.get(f"/pay/{f['payment_id']}", params={"agent_id": "v"}, headers=AG)
        self.assertEqual(mine.status_code, 200); self.assertEqual(mine.json()["status"], "frozen")
        tok = self.c.post("/api/agents/w/token", headers=OP).json()["agent_token"]
        self.assertEqual(self.c.get(f"/pay/{f['payment_id']}", params={"agent_id": "w"}, headers={"Authorization": "Bearer " + tok}).status_code, 404)      # another agent's payment: 404
        self.assertEqual(self.c.get(f"/pay/{f['payment_id']}", params={"agent_id": "v"}).status_code, 401)
        self.assertEqual(self.c.post("/api/cooldown/sweep", headers=AG).status_code, 401); self.assertEqual(self.c.post("/api/cooldown/sweep").status_code, 401)
        self.assertEqual(self.c.post("/api/cooldown/sweep", headers=OP).json(), {"results": []})

    def test_operator_config_change_and_release_are_attributed(self):
        self.c.post("/api/agents", json={"agent_id": "v", "hard_cap": 10, "mode": "enforce"}, headers=OP)
        f = self.c.post("/pay", json={**self.body, "amount": "400.00"}, headers=AG).json(); self.assertEqual(f["status"], "frozen")
        self.c.post(f"/api/payments/{f['payment_id']}/release", headers=OP)
        ev = {r["event"]: r for r in self.c.get("/api/audit?limit=500", headers=OP).json()["rows"]}
        self.assertEqual((ev["config_changed"]["actor"], ev["released"]["actor"]), ("operator", "operator"))
        self.assertEqual(self.c.post("/api/agents", json={"agent_id": "v", "mode": "yolo"}, headers=OP).json().get("ok"), False)


if __name__ == "__main__":
    unittest.main()
