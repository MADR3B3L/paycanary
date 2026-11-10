"""PayCanary web layer (FastAPI). Thin wrapper over app.service.Service."""
import os
import threading

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config
from .auth import Auth, load_tokens
from .paypal import PayPalError
from .providers.factory import build_registry
from . import auditkey
from .schema import ValidationError
from .service import Service
from .store import Store

config.load_env()
app = FastAPI(title="PayCanary", version="0.1.0")
_store = Store(os.environ.get("PAYCANARY_DB", config.ROOT / "paycanary.sqlite3").__str__())
_registry = None
AUTH = Auth(*load_tokens(config.ROOT))


def _deny(request, who, why):
    try:
        service().audit.append("auth_denied", who, detail={"path": request.url.path[:80], "method": request.method, "why": why})
    except Exception:
        pass


def operator(request: Request, authorization: str = Header(None)):
    if not AUTH.operator_ok(authorization):
        # an agent credential knocking on the control plane is worth its own line in the log
        _deny(request, "agent-credential" if AUTH.looks_like_shared_agent_token(authorization) else "unknown", "operator credential required")
        raise HTTPException(401, "operator credential required")


def agent_auth(request, authorization, agent_id):
    if not AUTH.agent_ok(authorization, agent_id, _store.agent(agent_id) if isinstance(agent_id, str) else {}):
        _deny(request, "unknown", "agent credential invalid for this agent_id")
        raise HTTPException(401, "agent credential required (and valid for this agent_id)")


def demo_offline():
    """PAYCANARY_DEMO=1 + PAYCANARY_DEMO_OFFLINE=1: a self-contained demo with NO network (no PayPal, no Haiku, no enrichment lookups).
    Payouts go to the MOCK provider (ids labelled MOCK-...). Honoured only in demo mode, never in a normal run."""
    return config.get("PAYCANARY_DEMO") == "1" and config.get("PAYCANARY_DEMO_OFFLINE") == "1"


def registry():
    """The provider registry for this process, built from each provider's OWN config slot. Demo-offline: the mock provider only, no keys, no network.
    Otherwise PayPal (sandbox only; its client refuses anything else). A provider whose slot is not configured is simply not registered."""
    global _registry
    if _registry is None:
        if demo_offline():
            _registry = build_registry(os.environ, names=("mock", "demo_rails"))
        else:
            # Register every configured rail from its own slot: PayPal always, Stripe (test mode) when its key is set.
            # build_registry leaves out a provider whose slot is not configured, so a PayPal-only deployment is unchanged;
            # a live (non-test) Stripe key still raises in the Stripe slot rather than being silently skipped.
            _registry = build_registry(os.environ, names=("paypal", "stripe"))
            if "paypal" not in _registry:
                raise ValueError("PayPal sandbox credentials are required (PAYCANARY_PAYPAL_CLIENT_ID / PAYCANARY_PAYPAL_CLIENT_SECRET)")
    return _registry


def paypal():
    """The PayPal client (Reporting baseline and /api/paypal/check only). Raises if PayPal is not the configured provider."""
    return registry().get("paypal").client


_service = None


def audit_creds():
    """(key, anchor path) for the tamper-evident audit log: held outside the database (see app/auditkey.py). (None, None) for an in-memory DB."""
    return auditkey.load(os.environ, os.environ.get("PAYCANARY_DB", str(config.ROOT / "paycanary.sqlite3")))


def service():
    """ONE Service per process: its decide-lock must be shared by every request (a per-request Service re-opens the race)."""
    global _service
    if _service is None:
        from . import llm as L
        _ak, _aa = audit_creds()
        _service = Service(_store, None, providers=registry(), default_provider="paypal", audit_key=_ak, audit_anchor=_aa,
                           webhook_id="",
                           platform_cap=float(config.get("PAYCANARY_PLATFORM_CAP", "5000") or 5000),
                           dev_webhooks=config.get("PAYCANARY_DEV_WEBHOOKS") == "1",
                           use_reporting=config.get("PAYCANARY_REPORTING_BASELINE") == "1",
                           default_mode=config.get("PAYCANARY_MODE", "enforce") or "enforce")
        offline = demo_offline()                                      # the offline demo makes NO outbound calls: enrichment (RDAP/DoH) and alerts (outbound webhook) are network, so they stay off even if .env turns them on
        if config.get("PAYCANARY_ENRICHMENT") == "1" and not offline:  # opt-in: sends only the recipient DOMAIN to RDAP / DoH; scam feeds are matched locally
            from .enrichers import Enrichers
            _service.enrichers = Enrichers(_store, feed_dir=config.ROOT / ".feeds")
            _service.audit.append("enrichment_enabled", "operator", detail={})
        if config.get("PAYCANARY_LOCAL_EVIDENCE") == "0":
            _service.local_evidence = False
        if config.get("PAYCANARY_REPUTATION") == "1":
            from .auth import load_reputation_key
            from .reputation_store import Reputation
            _service.reputation = Reputation(load_reputation_key(config.ROOT), os.environ.get("PAYCANARY_REPUTATION_DB", str(config.ROOT / "paycanary_reputation.sqlite3")))
            _service.audit.append("reputation_enabled", "operator", detail={})
        if config.get("PAYCANARY_ALERT_URL") and not offline:         # alerts are an outbound webhook: never fired in the offline demo
            from .notify import Notifier
            _service.notifier = Notifier(config.get("PAYCANARY_ALERT_URL"), config.get("PAYCANARY_ALERT_SECRET"),
                                         on=[x.strip() for x in (config.get("PAYCANARY_ALERT_ON", "freeze") or "freeze").split(",") if x.strip()],
                                         audit=_service.audit)
        client = None if demo_offline() else L.from_config(_store)
        if client:
            _service.llm = client
            _service.on_decided = lambda pid: threading.Thread(target=_service.enrich, args=(pid,), daemon=True).start()
    return _service


_seed_lock = threading.Lock()


def _seed(force=False):
    from . import seed as S
    with _seed_lock:
        svc = service()
        svc.offline_evidence = True                                  # demo: Tier-0 evidence on held payments (live /pay freezes too)
        # The seeded history always uses SIMULATED payouts (batch ids labelled SEED-OFFLINE): a real sandbox call that fails or runs dry would leave
        # the rows as paypal_error and the baseline empty (nothing would read as allowed), and it would spend sandbox funds on demo data.
        # Live /pay traffic after the seed still goes to the real sandbox. Haiku narration is skipped in the offline demo.
        return S.seed(_store, None, svc.llm, force=force, audit_key=svc.audit.key, audit_anchor=svc.audit.anchor_path)


def _sweeper():
    import time as _t
    while True:
        _t.sleep(30)
        try:
            service().release_due()
        except Exception:
            pass                                                     # the sweep never takes the service down; holds simply keep waiting


@app.on_event("startup")
def _start_sweeper():
    threading.Thread(target=_sweeper, daemon=True).start()


@app.on_event("startup")
def _maybe_seed():
    if config.get("PAYCANARY_DEMO") == "1":                      # demo mode only; never on by default
        _seed()


@app.post("/demo/seed", dependencies=[Depends(operator)])
def demo_seed(force: bool = False, body: dict = Body(default={})):
    """Demo only. force (query ?force=1 or JSON {"force": true}) RESETS the demo data (payments, intents, idempotency keys) and seeds again; the audit
    log is never deleted: a demo_reset row is appended."""
    if config.get("PAYCANARY_DEMO") != "1":
        raise HTTPException(404)
    res = _seed(bool(force or (body or {}).get("force")))
    return {k: v for k, v in res.items() if k != "rows"}


@app.get("/")
def dashboard():
    from fastapi.responses import FileResponse
    return FileResponse(str(config.ROOT / "app" / "static" / "index.html"))


@app.get("/healthz")
def healthz():
    return {"ok": True, "paypal_env": config.get("PAYPAL_ENV", "sandbox"), "providers": registry().names()}


@app.get("/api/paypal/check", dependencies=[Depends(operator)])
def paypal_check():
    """Booleans only — never the token or secret."""
    try:
        c = paypal()
        c.token(force=True)
        return {"token_ok": True, "scope_mentions_payouts": "payouts" in c.scope.lower(),
                "scope_mentions_reporting": "reporting" in c.scope.lower()}
    except (PayPalError, ValueError) as err:
        return {"token_ok": False, "error": str(err)}


@app.post("/api/agents", dependencies=[Depends(operator)])
def set_agent(body: dict = Body(...)):
    """OPERATOR-ONLY. Omitted require_intent/hard_cap inherit the safe defaults (they are NOT silently switched off)."""
    try:
        return service().set_agent(body.get("agent_id"), body.get("hard_cap"), body.get("active_hours"), body.get("require_intent"),
                                   body.get("approved_payees"), body.get("new_payee_floor"), body.get("mode"), org_id=body.get("org_id"),
                                   probation_abs=body.get("probation_abs"), probation_mult=body.get("probation_mult"),
                                   cooldown_s=body.get("cooldown_s"), fanout_k=body.get("fanout_k"), probation_hold=body.get("probation_hold"))
    except (ValidationError, ValueError, TypeError) as err:
        return {"ok": False, "reason": str(err)}


@app.get("/api/ruleset", dependencies=[Depends(operator)])
def ruleset():
    return service().ruleset()


@app.post("/api/agents/{agent_id}/approved_payees", dependencies=[Depends(operator)])
def approved_payees(agent_id: str, body: dict = Body(...)):
    """OPERATOR-ONLY. Add/remove always-pass payees; goes through set_agent so it is audited (config_changed, before/after)."""
    try:
        return service().modify_agent(agent_id, approve=body.get("add"), unapprove=body.get("remove"))
    except (ValidationError, ValueError, TypeError) as err:
        return {"ok": False, "reason": str(err)}


@app.post("/api/agents/{agent_id}/mode", dependencies=[Depends(operator)])
def agent_mode(agent_id: str, body: dict = Body(...)):
    """OPERATOR-ONLY. shadow = Shadow (log only), enforce = Enforce."""
    try:
        if body.get("mode") not in ("shadow", "enforce"):
            return {"ok": False, "reason": "mode must be shadow or enforce"}
        return service().modify_agent(agent_id, mode=body.get("mode"))
    except (ValidationError, ValueError, TypeError) as err:
        return {"ok": False, "reason": str(err)}


@app.get("/api/profiles", dependencies=[Depends(operator)])
def profiles():
    from . import policy as P
    return {"profiles": service().store.profiles(), "presets": P.PRESETS, "provisional_presets": P.PRESETS}   # provisional_presets: deprecated alias kept for one release


@app.post("/api/profiles", dependencies=[Depends(operator)])
def define_profile(body: dict = Body(...)):
    """OPERATOR-ONLY. Creates the next VERSION of a named profile; all 9 knobs required, unknown keys refused, audited (profile_changed)."""
    try:
        return service().define_profile(body.get("name"), body.get("fields"))
    except (ValidationError, ValueError, TypeError) as err:
        return {"ok": False, "reason": str(err)}


@app.post("/api/profiles/presets", dependencies=[Depends(operator)])
def install_presets():
    return {"installed": service().install_presets()}


@app.post("/api/agents/{agent_id}/profile", dependencies=[Depends(operator)])
def assign_profile(agent_id: str, body: dict = Body(...)):
    """OPERATOR-ONLY. Pin the agent to 'name' / 'name@version', or null to unpin. Audited as config_changed naming the exact version."""
    try:
        return service().assign_profile(agent_id, body.get("profile"))
    except (ValidationError, ValueError, TypeError) as err:
        return {"ok": False, "reason": str(err)}


@app.get("/api/agents/{agent_id}/policy", dependencies=[Depends(operator)])
def agent_policy(agent_id: str):
    """The effective policy the gate would apply to this agent right now (override > profile > default, then the platform ceiling) and its hash."""
    try:
        pol = service().resolve_policy(agent_id)
    except ValueError as err:
        return {"ok": False, "reason": str(err)}
    return {"profile": pol["profile"], "mode": pol["mode"], "knobs": pol["knobs"], "policy_hash": pol["hash"], "policy": pol["body"],
            "warnings": service().policy_warnings(pol)}


@app.post("/api/agents/{agent_id}/provider", dependencies=[Depends(operator)])
def agent_provider(agent_id: str, body: dict = Body(...)):
    """OPERATOR-ONLY. Route this agent's future payments to a registered provider by exact name. Audited."""
    try:
        return service().assign_provider(agent_id, body.get("provider"))
    except (ValidationError, ValueError, TypeError) as err:
        return {"ok": False, "reason": str(err)}


@app.post("/api/agents/{agent_id}/token", dependencies=[Depends(operator)])
def agent_token(agent_id: str):
    """OPERATOR-ONLY. Issues a credential bound to this agent_id; shown once."""
    try:
        return {"agent_id": agent_id, "agent_token": service().issue_agent_token(agent_id)}
    except ValidationError as err:
        return {"ok": False, "reason": str(err)}


@app.post("/intent")
def intent(request: Request, body: dict = Body(...), authorization: str = Header(None)):
    agent_auth(request, authorization, body.get("agent_id"))
    try:
        return service().register_intent(body.get("agent_id"), body.get("stated_payees"), body.get("plan_text", ""))
    except ValidationError as err:
        return {"ok": False, "reason": str(err)}


_HTTP_FOR = {"fail_closed": 503, "idempotency_conflict": 422, "in_progress": 409, "rejected": 400}


@app.post("/pay")
def pay(request: Request, body: dict = Body(...), authorization: str = Header(None), paypal_request_id: str = Header(None)):
    """AGENT credential. `PayPal-Request-Id` (optional, recommended): same key = replay of the first result; same key with different
    parameters = 422; retry while the first is in flight = 409. 503 means PayCanary reached no verdict and NOTHING was sent."""
    agent_auth(request, authorization, body.get("agent_id"))
    out = service().pay(body, request_id=paypal_request_id)
    headers = {"Idempotent-Replay": "true"} if out.get("duplicate") else {}
    return JSONResponse(out, status_code=_HTTP_FOR.get(out.get("status"), 200), headers=headers)


@app.post("/api/payments/{payment_id}/release", dependencies=[Depends(operator)])
def release(payment_id: str):
    return service().release(payment_id, "operator")           # identity comes from the credential, not from anything the caller typed


@app.post("/api/payments/{payment_id}/ask", dependencies=[Depends(operator)])
def ask_payment(payment_id: str, body: dict = Body(default={})):
    """Advisory Q&A about one payment. Read-only: the answer is text, never an action (app/ask.py)."""
    from . import ask as A
    out = A.ask(service(), payment_id, (body or {}).get("question"))
    if out is None:
        raise HTTPException(404, "unknown payment")
    return out


@app.post("/api/cooldown/sweep", dependencies=[Depends(operator)])
def cooldown_sweep():
    """OPERATOR-ONLY: run the cooling-off release sweep now (a background sweep also runs every 30 s)."""
    return {"results": service().release_due()}


@app.get("/pay/{payment_id}")
def pay_status(request: Request, payment_id: str, agent_id: str, authorization: str = Header(None)):
    """AGENT credential: the status of one of ITS OWN payments (so an agent can poll a held / cooling-off payment). Unknown or someone else's: 404."""
    agent_auth(request, authorization, agent_id)
    p = _store.payment(payment_id)
    if not p or p["agent_id"] != agent_id:
        raise HTTPException(404)
    return {"payment_id": payment_id, "status": p["status"], "decision": p["verdict"], "reason": p["reason"], "release_at": p.get("release_at"),
            "paypal": {"batch_id": p["paypal_batch_id"], "status": p["paypal_status"]} if p["paypal_batch_id"] else None}


@app.post("/api/payments/{payment_id}/confirm_fraud", dependencies=[Depends(operator)])
def confirm_fraud(payment_id: str):
    """OPERATOR-ONLY: a human asserts this payee is fraudulent; shared, de-identified, with every agent's reputation read."""
    return service().confirm_fraud(payment_id, "operator")


@app.post("/api/payments/{payment_id}/deny", dependencies=[Depends(operator)])
def deny(payment_id: str):
    return service().deny(payment_id, "operator")


@app.post("/api/reporting/refresh", dependencies=[Depends(operator)])
def reporting_refresh(body: dict = Body(default={})):
    """OPERATOR-ONLY. Pull the account's real PayPal outflow history (needs the Transaction Search scope; degrades gracefully)."""
    return service().refresh_reporting(int(body.get("days", 90)))


@app.get("/api/audit", dependencies=[Depends(operator)])
def audit_rows(since_seq: int = 0, limit: int = 200, event: str = None, payment_id: str = None):
    a = service().audit
    return {"head": a.head(), "rows": a.rows(since_seq, min(limit, 1000), event, payment_id)}


@app.get("/api/audit/verify", dependencies=[Depends(operator)])
def audit_verify():
    return {**service().audit.verify(), "head": service().audit.head()}


@app.get("/api/payments", dependencies=[Depends(operator)])
def payments(agent_id: str = None, status: str = None):
    return {"payments": _store.payments(agent_id, status)}


@app.post("/webhooks/{provider}")
async def webhook(provider: str, request: Request):
    """One route per provider NAME; each provider's adapter verifies with its own secret and may only update payments routed to that same provider."""
    return service().handle_webhook(dict(request.headers), await request.body(), provider=provider)


@app.get("/api/webhooks", dependencies=[Depends(operator)])
def webhooks():
    return {"events": _store.webhooks()}
