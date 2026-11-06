"""Boots the real app in demo-offline mode (lifespan + startup seed) and asserts the dashboard data: Frozen=3 with evidence, then a live /pay
freeze. Runs in a subprocess so the module-level env/Store of app.main is isolated. Needs fastapi + httpx (skipped on the bare stdlib)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    import fastapi, httpx  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

SCRIPT = r'''
import json, time
from fastapi.testclient import TestClient
from app.main import app
OP, AG = {"Authorization": "Bearer optok"}, {"Authorization": "Bearer agtok"}
t0 = time.time()
with TestClient(app) as c:
    boot = time.time() - t0
    rows = c.get("/api/payments", headers=OP).json()["payments"]
    frozen = [r for r in rows if r["status"] == "frozen"]
    aw = next(r for r in frozen if r["payee"] == "QuickTools LLC")
    c.post("/intent", headers=AG, json={"agent_id": "ops-bot", "stated_payees": ["AWS", "GitHub"], "plan_text": "aws and github"})
    live = c.post("/pay", headers={**AG, "PayPal-Request-Id": "live-demo-1"}, json={"agent_id": "ops-bot", "payee": "NovaPay LLC",
              "recipient_email": "billing@mailinator.com", "amount": "40.00", "currency": "USD", "memo": "urgent invoice"}).json()
    time.sleep(0.2)
    after = c.get("/api/payments", headers=OP).json()["payments"]
    q = next(r for r in after if r["payment_id"] == live["payment_id"])
    rel = c.post("/api/payments/%s/release" % q["payment_id"], headers=OP).json()
    noauth = c.get("/api/ruleset").status_code
    c.post("/api/agents/ops-bot/approved_payees", headers=OP, json={"add": ["Zendesk"]})
    c.post("/api/agents/ops-bot/mode", headers=OP, json={"mode": "shadow"})
    rs = c.get("/api/ruleset", headers=OP).json()
    bad = c.post("/api/agents/ops-bot/mode", headers=OP, json={"mode": "x"}).json()
    nan = c.post("/api/agents", headers={**OP, "content-type": "application/json"}, content='{"agent_id":"nanbot","hard_cap":NaN}').json()
    inf = c.post("/api/agents", headers={**OP, "content-type": "application/json"}, content='{"agent_id":"nanbot","hard_cap":Infinity}').json()
    pres = c.post("/api/profiles/presets", headers=OP).json()
    asg = c.post("/api/agents/ops-bot/profile", headers=OP, json={"profile": "strict"}).json()
    pol = c.get("/api/agents/ops-bot/policy", headers=OP).json()
    prof_agent = c.post("/api/profiles", headers=AG, json={"name": "x", "fields": {}}).status_code
    partial = c.post("/api/profiles", headers=OP, json={"name": "x", "fields": {"hard_cap": 5}}).json()
    plist = c.get("/api/profiles", headers=OP).json()
    c.post("/api/agents", headers=OP, json={"agent_id": "hold-bot", "hard_cap": 500, "cooldown_s": 900, "probation_hold": True})
    hb = c.get("/api/agents/hold-bot/policy", headers=OP).json()
    c.post("/api/agents", headers=OP, json={"agent_id": "nohold-bot", "hard_cap": 500})
    nb = c.get("/api/agents/nohold-bot/policy", headers=OP).json()
    dny = c.post("/api/payments/%s/deny" % next(r for r in after if r["status"] == "frozen")["payment_id"], headers=OP).json()
    rs1 = c.post("/demo/seed?force=1", headers=OP).json()
    rows2 = c.get("/api/payments", headers=OP).json()["payments"]
    prov_bad = c.post("/api/agents/ops-bot/provider", headers=OP, json={"provider": "nope"}).json()
    prov_ok = c.post("/api/agents/ops-bot/provider", headers=OP, json={"provider": "mock"}).json()
    prov_noauth = c.post("/api/agents/ops-bot/provider", headers=AG, json={"provider": "mock"}).status_code
    wh_unknown = c.post("/webhooks/nope", content=b"{}").json()
    agent_try = c.post("/api/agents/ops-bot/mode", headers=AG, json={"mode": "shadow"}).status_code
print(json.dumps({"prov": [prov_bad.get("ok"), prov_ok.get("provider"), prov_noauth, wh_unknown.get("ok"), live.get("provider")], "reseed": [rs1.get("seeded"), len(rows2), sorted({r["status"] for r in rows2})], "hold_on": hb["knobs"]["probation_hold"], "hold_default": nb["knobs"]["probation_hold"], "alias": plist.get("provisional_presets") == plist.get("presets") and bool(plist.get("presets")), "nan_ok": nan.get("ok"), "inf_ok": inf.get("ok"), "presets": len(pres["installed"]), "asg": asg, "pol_cap": pol["knobs"]["hard_cap"], "pol_hash": bool(pol["policy_hash"]),
                  "prof_agent": prof_agent, "partial_ok": partial.get("ok"), "noauth": noauth, "agent_try": agent_try, "bad_ok": bad.get("ok"), "rs_agent": next(a for a in rs["agents"] if a["agent_id"] == "ops-bot"), "rs_hist": len(rs["history"]),"boot": boot, "n": len(rows), "frozen": sorted(r["payee"] for r in frozen), "aw_ev": json.loads(aw["evidence_json"])["items"],
                  "live": live["decision"], "live_ev": bool(q["evidence_json"]), "frozen_after": sum(r["status"] == "frozen" for r in after), "release": rel.get("ok")}))
'''


@unittest.skipUnless(HAVE, "needs fastapi + httpx (.venv)")
class DemoBoot(unittest.TestCase):
    def test_boots_offline_with_four_frozen_heroes_and_live_freeze(self):
        d = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYCANARY_", "PAYPAL_", "ANTHROPIC"))}
        env.update(PAYCANARY_DB=os.path.join(d, "demo.sqlite3"), PAYCANARY_DEMO="1", PAYCANARY_DEMO_OFFLINE="1",
                   PAYCANARY_OPERATOR_TOKEN="optok", PAYCANARY_AGENT_TOKEN="agtok")
        env["HOME"] = d                                                  # no ~/.env style surprises; keys must not be needed
        out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        r = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertLess(r["boot"], 10)
        self.assertEqual(r["frozen"], ["Priya Raman", "QuickTools LLC", "Veloxa Payments", "Veloxa Payments"])
        self.assertTrue(any(i["signal"] == "in_disposable" for i in r["aw_ev"]))
        self.assertEqual((r["live"], r["live_ev"], r["frozen_after"]), ("freeze", True, 5))
        self.assertTrue(r["release"])
        self.assertEqual((r["noauth"], r["agent_try"], r["bad_ok"]), (401, 401, False))     # operator-only, input validated
        self.assertEqual(r["prov"], [False, "mock", 401, False, "paypal"])                      # demo registry = simulated PayPal + Stripe + mock; unknown name refused; operator-only; live payment uses the default simulated PayPal rail
        self.assertEqual(r["reseed"], [True, 72, ["frozen", "sent"]])                          # ?force=1 (query form) really resets: nothing stays denied
        self.assertEqual((r["hold_on"], r["hold_default"]), (True, False))                   # the override works through POST; default stays off
        self.assertTrue(r["alias"])                                                          # provisional_presets kept as an alias for one release
        self.assertEqual((r["nan_ok"], r["inf_ok"]), (False, False))                         # T6 over real HTTP
        self.assertEqual((r["presets"], r["asg"]["profile"], r["pol_cap"], r["prof_agent"], r["partial_ok"]), (3, "strict@1", 2500.0, 401, False))
        self.assertTrue(r["pol_hash"])                       # (ops-bot has an explicit 1000 cap, which beats the strict profile: override > profile)
        self.assertIn("Zendesk", r["rs_agent"]["approved_payees"])
        self.assertEqual(r["rs_agent"]["mode"], "shadow")
        self.assertEqual(r["rs_agent"]["hard_cap"], 2500)                                   # a narrow edit never resets the other knobs
        self.assertGreaterEqual(r["rs_hist"], 2)


if __name__ == "__main__":
    unittest.main()
