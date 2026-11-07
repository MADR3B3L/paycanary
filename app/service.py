"""PayCanary core service (pure Python, no web framework): intent registry, the /pay goalie, human release/deny,
and the PayPal webhook handler. The web layer (app/main.py) is a thin wrapper so this is unit-testable offline."""
import hashlib
import json
import re
import threading
import time
import uuid

from . import fx as fxmod
from . import gate as G
from . import policy as P
from .providers.base import Provider, ProviderError, SendRequest, SendResult
from .providers.registry import Registry
from .audit import AuditLog
from .schema import ValidationError, amount_bucket, clean_text, validate_request

INTENT_TTL_S = 15 * 60
RID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")      # opaque client-chosen key: no spaces, no PII-shaped free text


DEFAULT_HARD_CAP = 500.0       # safe-by-default: an agent nobody configured cannot move more than this in one payment


def fmt_amount(amount, currency):
    """PayPal wants whole numbers for zero-decimal currencies (JPY) and two decimals otherwise."""
    return "%d" % round(float(amount)) if str(currency).upper() in fxmod.ZERO_DECIMAL else "%.2f" % float(amount)


class Service:
    def __init__(self, store, paypal=None, clock=time.time, webhook_id="", default_require_intent=True, default_hard_cap=DEFAULT_HARD_CAP, platform_cap=None, dev_webhooks=False, use_reporting=False, default_mode="enforce", providers=None, default_provider=None, audit_key=None, audit_anchor=None):
        if default_mode not in ("shadow", "enforce"):
            raise ValueError("default_mode must be shadow or enforce")
        self.default_mode = default_mode
        self.notifier = None                                 # optional app.notify.Notifier: signed outbound webhook on freeze/review
        self.use_reporting = use_reporting                 # seed baselines from stored real-PayPal history (see app/reporting.py); optional
        self.platform_cap, self.dev_webhooks = platform_cap, dev_webhooks        # platform_cap: operator ceiling no per-agent cap can exceed
        self.default_require_intent, self.default_hard_cap = default_require_intent, default_hard_cap
        self._decide_lock = threading.Lock()            # serialises read-baseline -> classify -> record (no TOCTOU double-fire)
        self.store, self.paypal, self.clock, self.webhook_id = store, paypal, clock, webhook_id     # .paypal: the PayPal client, kept for the optional Reporting baseline
        if providers is None:                               # legacy construction: Service(store, <paypal client | Provider>) -> a registry of exactly that one provider
            from .providers.factory import wrap_legacy
            providers = Registry(*([wrap_legacy(paypal, webhook_id)] if paypal is not None else []))
            default_provider = default_provider or (paypal.name if isinstance(paypal, Provider) else "paypal")
        self.providers, self.default_provider = providers, default_provider or "paypal"
        if self.paypal is None and "paypal" in self.providers:                  # Reporting baseline needs the PayPal client; take it from the registered adapter
            self.paypal = getattr(self.providers.get("paypal"), "client", None)
        self.audit = AuditLog(store, clock, key=audit_key, anchor_path=audit_anchor)
        self.enrichers = None                               # optional app.enrichers.Enrichers (Tier-1 network evidence, async, held/new payees only)
        self.offline_evidence = False                       # demo/offline: attach Tier-0 evidence to held payments even with no network enrichers
        self.local_evidence = True                          # Tier-0 offline evidence checks (disposable inbox, business name at a freemail address): no I/O
        self.sync_enrich = False                            # tests: run the gather inline
        self.reputation = None                              # optional app.reputation_store.Reputation (shared, de-identified, cross-agent)
        self.llm = None                                  # optional app.llm.LLM: narration/flags only, never decides
        self.on_decided = None                           # optional callback(payment_id), fired AFTER the decision is final

    # ---- agents + intents -------------------------------------------------------------------------------------
    def set_agent(self, agent_id, hard_cap=None, active_hours=None, require_intent=None, approved_payees=None, new_payee_floor=None, mode=None, actor="operator", org_id=None, probation_abs=None, probation_mult=None, cooldown_s=None, fanout_k=None, probation_hold=None):
        """OPERATOR-ONLY (the HTTP layer enforces it): writes the agent's guard configuration."""
        agent_id = clean_text(agent_id, "agent_id", limit=64)
        num = lambda v, n, mn=0.0: P.finite(v, n, mn) if v is not None else None            # NaN / Infinity / negatives never reach the config (T6)
        prev = self.store.agent(agent_id)
        cfg = {"hard_cap": num(hard_cap, "hard_cap"),
               "active_hours": P.clean_hours(active_hours) if active_hours else None,
               "require_intent": None if require_intent is None else bool(require_intent),
               "approved_payees": [clean_text(p, "approved_payee", limit=80) for p in (approved_payees or [])][:200],
               "new_payee_floor": num(new_payee_floor, "new_payee_floor"),
               "mode": mode,
               "fanout_k": num(fanout_k, "fanout_k") or None,                                      # structuring fan-out multiple (default gate.FANOUT_K)
               "cooldown_s": num(cooldown_s, "cooldown_s"),                                        # O3 cooling-off window in seconds; 0/None = off
               "probation_abs": num(probation_abs, "probation_abs"),
               "probation_mult": num(probation_mult, "probation_mult"),
               "probation_hold": None if probation_hold is None else self._bool(probation_hold, "probation_hold"),
               "org_id": clean_text(org_id, "org_id", required=False, limit=64) or None,          # OPERATOR-set tenant id: the Sybil unit for reputation
               "token_hash": prev.get("token_hash"),                                               # never lose the agent's credential on a config edit
               "profile": prev.get("profile"),
               "provider": prev.get("provider")}                                                    # routing is its own verb (assign_provider), never reset by an edit                                                     # assignment is its own verb: an edit here never unpins it
        if mode not in (None, "shadow", "enforce"):
            raise ValidationError("mode must be shadow or enforce")
        self._write_config(agent_id, prev, cfg, actor)
        return cfg

    @staticmethod
    def _bool(v, name):
        if not isinstance(v, bool):
            raise ValidationError(f"{name} must be true or false")
        return v

    def _write_config(self, agent_id, before, cfg, actor, extra=None):
        """Every agent-config change goes through here: write, then audit; if the audit row cannot be written the change is rolled back and refused
        (the log swallows its own errors, so without this check a config edit could silently skip the ledger)."""
        self.store.set_agent(agent_id, cfg)
        shown = lambda c: {k: v for k, v in c.items() if k != "token_hash"}
        seq = self.audit.append("config_changed", actor, agent_id=agent_id, detail={"before": shown(before), "after": shown(cfg), **(extra or {})})
        if seq is None:
            self.store.set_agent(agent_id, before)
            raise ValidationError("audit log unavailable: configuration change refused")


    def modify_agent(self, agent_id, mode=None, approve=None, unapprove=None, actor="operator"):
        """OPERATOR-ONLY. Narrow edits (shadow/enforce mode, allowlist add/remove) as a read-modify-write on the existing config, ALWAYS through
        set_agent so the before/after `config_changed` audit row is written and no other knob is reset (set_agent itself replaces the whole config)."""
        agent_id = clean_text(agent_id, "agent_id", limit=64)
        cfg = self.store.agent(agent_id)
        if mode is not None and mode not in ("shadow", "enforce"):
            raise ValidationError("mode must be shadow or enforce")
        pay = [clean_text(p, "approved_payee", limit=80) for p in (approve or [])]
        gone = {clean_text(p, "approved_payee", limit=80) for p in (unapprove or [])}
        keep = [p for p in (cfg.get("approved_payees") or []) if p not in gone]
        approved = keep + [p for p in pay if p not in keep]
        return self.set_agent(agent_id, cfg.get("hard_cap"), cfg.get("active_hours"), cfg.get("require_intent"), approved, cfg.get("new_payee_floor"),
                              mode if mode is not None else cfg.get("mode"), actor=actor, org_id=cfg.get("org_id"), probation_abs=cfg.get("probation_abs"),
                              probation_mult=cfg.get("probation_mult"), cooldown_s=cfg.get("cooldown_s"), fanout_k=cfg.get("fanout_k"), probation_hold=cfg.get("probation_hold"))

    def _agent_warnings(self, agent_id):
        try:
            return self.policy_warnings(self.resolve_policy(agent_id))
        except Exception:
            return []

    def ruleset(self):
        """The firewall ruleset, three bands. (1) Always pass: each agent's approved_payees. (2) Always freeze: payees THIS operator marked via
        confirm_fraud (read from this deployment's own audit log: the shared reputation table is keyed hashes and cannot be reversed, and other orgs'
        entries are never shown). It only blocks at decision time when cross-agent reputation is enabled. (3) Everything else is scored."""
        agents = [{"agent_id": a, "mode": c.get("mode") or self.default_mode, "approved_payees": c.get("approved_payees") or [], "org_id": c.get("org_id"),
                   "hard_cap": c.get("hard_cap"), "profile": c.get("profile"), "warnings": self._agent_warnings(a)} for a, c in self.store.agents().items()]
        seen, deny = set(), []
        for r in reversed(self.audit.rows(0, 1000, "confirmed_fraud")):
            d = json.loads(r["detail"] or "{}")
            if d.get("payee") and d["payee"] not in seen:
                seen.add(d["payee"])
                deny.append({"payee": d["payee"], "agent_id": r["agent_id"], "payment_id": r["payment_id"], "ts": r["ts"], "by": r["actor"]})
        hist = []
        for r in reversed(self.audit.rows(0, 1000, "config_changed")):
            d = json.loads(r["detail"] or "{}")
            b, a = set((d.get("before") or {}).get("approved_payees") or []), set((d.get("after") or {}).get("approved_payees") or [])
            bm, am = (d.get("before") or {}).get("mode"), (d.get("after") or {}).get("mode")
            if a - b or b - a or bm != am:
                hist.append({"ts": r["ts"], "agent_id": r["agent_id"], "by": r["actor"], "allowlist_added": sorted(a - b), "allowlist_removed": sorted(b - a),
                             "mode_from": bm, "mode_to": am})
            if len(hist) >= 20:
                break
        return {"reputation_active": self.reputation is not None, "agents": agents, "denylist": deny, "history": hist}

    # ---- policy profiles ------------------------------------------------------------------------------------------
    def define_profile(self, name, fields, actor="operator"):
        """OPERATOR-ONLY. Creates a NEW VERSION of a named profile (versions are never edited in place). All 9 knobs required; unknown keys refused."""
        name, fields = P.validate_profile(name, fields)
        before = self.store.profile(name)
        v = self.store.put_profile(name, fields, self.clock())
        seq = self.audit.append("profile_changed", actor, detail={"name": name, "version": v, "before": before and before["fields"], "after": fields})
        if seq is None:
            raise ValidationError("audit log unavailable: profile change refused")           # the version row is harmless unassigned, but the operator is told
        return {"name": name, "version": v, "fields": fields}

    def install_presets(self, actor="operator"):
        """OPERATOR-ONLY. Defines the starter profiles (standard / strict / permissive) only where the name does not exist yet."""
        out = []
        for n, f in P.PRESETS.items():
            if self.store.profile(n) is None:
                out.append(self.define_profile(n, f, actor))
        return out

    def assign_profile(self, agent_id, profile, actor="operator"):
        """OPERATOR-ONLY. Pin an agent to 'name' (latest version at this moment) or 'name@version'; None unpins (fully custom, today's behaviour).
        Audited as config_changed naming the exact profile+version. Moving to a newer version is a new assignment, never implicit."""
        agent_id = clean_text(agent_id, "agent_id", limit=64)
        ref = None
        if profile:
            name, _, ver = str(profile).partition("@")
            pr = self.store.profile(name, int(ver) if ver.isdigit() else None) if (not ver or ver.isdigit()) else None
            if pr is None:
                raise ValidationError("unknown profile " + str(profile)[:80])
            ref = "%s@%d" % (pr["name"], pr["version"])
        before = self.store.agent(agent_id)
        cfg = dict(before)
        cfg["profile"] = ref
        self._write_config(agent_id, before, cfg, actor, extra={"profile_assigned": ref})
        return {"agent_id": agent_id, "profile": ref, "warnings": self.policy_warnings(self.resolve_policy(agent_id))}

    def resolve_policy(self, agent_id):
        """The ONE place a decision's effective policy is computed (override > pinned profile > default, then the platform ceiling).
        Pay resolves once and passes the result everywhere; nothing else reads raw knobs. Raises if the pinned profile is missing (the gate fails closed)."""
        cfg = self.store.agent(agent_id)
        ref, pf = cfg.get("profile"), None
        if ref:
            name, _, ver = ref.partition("@")
            pr = self.store.profile(name, int(ver))
            if pr is None:
                raise ValueError("pinned profile %s not found" % ref)
            pf = pr["fields"]
        knobs = P.resolve(cfg, pf, P.system_defaults(self.default_hard_cap, self.default_require_intent), self.platform_cap)
        mode = cfg.get("mode") or self.default_mode
        body = P.policy_body(knobs, profile=ref, mode=mode, platform_cap=self.platform_cap, approved_payees=cfg.get("approved_payees"))
        return {"knobs": knobs, "mode": mode, "profile": ref, "approved": cfg.get("approved_payees"), "body": body, "hash": P.policy_hash(body)}

    def policy_warnings(self, pol):
        """Non-blocking operator warnings about a resolved policy. Cooling-off with enrichment off means a held first payment never gets the network
        evidence that lets it release itself, so it waits for an operator indefinitely."""
        out = []
        if (pol["knobs"].get("cooldown_s") or 0) > 0 and self.enrichers is None:
            out.append("cooling-off is on but enrichment is off (PAYCANARY_ENRICHMENT=1): held first payments cannot auto-release and will wait for an operator indefinitely")
        if pol["knobs"].get("probation_hold") and not (pol["knobs"].get("cooldown_s") or 0) > 0:
            out.append("probation_hold is on but cooldown_s is 0: it only extends the cooling-off hold, so it has no effect")
        return out

    def _snapshot(self, pol):
        """Content-addressed snapshot: the full resolved body enters the audit chain ONCE per distinct policy; decisions then carry only the hash.
        If the snapshot cannot be recorded the decision is refused (fail-closed): a hash nobody can open would prove nothing."""
        if self.store.policy_known(pol["hash"]):
            return
        seq = self.audit.append("policy_resolved", "gate", detail={"policy_hash": pol["hash"], "policy": pol["body"]})
        if seq is None:
            raise RuntimeError("audit log unavailable: cannot snapshot policy")
        self.store.policy_put(pol["hash"], pol["body"], self.clock())

    def refresh_reporting(self, days=90):
        """OPERATOR-ONLY / out-of-band. Pulls real PayPal outflow history into the store; never raises; never on the /pay path."""
        from . import reporting
        return reporting.refresh(self.store, self.paypal, days=days, now=self.clock())

    def issue_agent_token(self, agent_id):
        """OPERATOR-ONLY. Returns a fresh per-agent credential ONCE (only its hash is stored) and invalidates any previous one."""
        import secrets
        from .auth import hash_token
        agent_id = clean_text(agent_id, "agent_id", limit=64)
        tok = secrets.token_urlsafe(32)
        cfg = self.store.agent(agent_id)
        cfg["token_hash"] = hash_token(tok)
        self.store.set_agent(agent_id, cfg)
        self.audit.append("token_issued", "operator", agent_id=agent_id, detail={})
        return tok

    def register_intent(self, agent_id, stated_payees, plan_text=""):
        agent_id = clean_text(agent_id, "agent_id", limit=64)
        if not isinstance(stated_payees, list) or not stated_payees or len(stated_payees) > 20:
            raise ValidationError("stated_payees must be a non-empty list (max 20)")
        payees = [clean_text(p, "stated_payee", limit=80) for p in stated_payees]
        iid = "int-" + uuid.uuid4().hex[:12]
        self.store.add_intent(iid, agent_id, self.clock(), payees, clean_text(plan_text, "plan_text", required=False, limit=500))
        return {"intent_id": iid, "stated_payees": payees}

    # ---- history ----------------------------------------------------------------------------------------------
    def _history(self, agent_id, now, hint=None, policy=None):
        pol = policy or self.resolve_policy(agent_id)
        k = pol["knobs"]
        h = G.History(hard_cap=k["hard_cap"], active_hours=k["active_hours"], approved_payees=pol["approved"],
                      new_payee_floor=k["new_payee_floor"], probation_abs=k["probation_abs"], probation_mult=k["probation_mult"],
                      cooldown_s=k["cooldown_s"] or 0.0, fanout_k=k["fanout_k"], probation_hold=k.get("probation_hold", False))
        cur = hint[2] if hint and len(hint) > 2 else "USD"
        for r in self.store.window_rows(agent_id, now - 7 * 86400):           # spend is tracked in USD-equivalent across ALL currencies (caps are USD)
            h.add_spend(r["ts"], r["payee"], r["amount"] * (fxmod.to_usd_rate(r["currency"]) or 1.0))
        if self.use_reporting:
            names = self.store.recipient_names()
            if hint:
                names.setdefault(hint[0], hint[1])                          # the payment in hand says which payee this destination belongs to
            cur = hint[2] if hint and len(hint) > 2 else "USD"
            def alias(row):
                rc, nm = row["recipient"], (row["payee_name"] or "")
                if rc in names:
                    return names[rc]                                  # a destination PayCanary has paid: the name we used for it wins
                if hint and rc == hint[0]:
                    return hint[1]                                    # the payment in hand goes to exactly this address: same payee
                # NEVER merge by name similarity: a lookalike ("AWS-Billing") must not inherit the history of the real "AWS"
                return nm or rc                                       # the real name PayPal has for it; the email only as a last resort
            ext = [(alias(r), r["amount"], r["ts"], r["recipient"]) for r in self.store.ext_rows(now - 90 * 86400)
                   if (r["currency"] or "USD") == cur]                                 # never pool another currency into this baseline
            own_first = min([r["ts"] for r in self.store.baseline_rows(agent_id)] or [now])
            h.seed_external([e for e in ext if e[2] < own_first])            # real history that PREDATES what we have observed; ours takes over after
        for r in self.store.baseline_rows(agent_id):
            if (r["currency"] or "USD") != cur:
                continue                                                          # one baseline per currency: amounts are never pooled across currencies
            h.observe(r["payee"], r["amount"], r["ts"], r["hour"], r["recipient"])
        for r in self.store.attempt_rows(agent_id, now - G.VELOCITY_WINDOW_S):
            h.note_attempt(r["ts"], r["payee"])
        return h

    # ---- the goalie -------------------------------------------------------------------------------------------
    def _evidence(self, payee, recipient, now):
        """Pre-resolved evidence for the gate: Tier 0 (pure, offline) plus anything ALREADY cached from an earlier gather. Never does network I/O."""
        from . import external_evidence as E
        ev = E.local_checks(payee, recipient) if self.local_evidence else {}
        try:
            cached = self.store.ev_get(str(recipient).rpartition("@")[2].lower(), now)
        except Exception:
            cached = None
        if cached:
            ev.update({k: v for k, v in cached.items() if k in ("domain_age_days", "mx_present", "blocklist_hit", "blocklist_source")})
        return ev

    def _payee_evidence(self, name, recipient, now, inline=False):
        """Evidence dict for one payee: Tier 0 (offline) plus Tier 1 from the cache, or gathered inline (bounded timeouts, fail-open) when asked."""
        from . import external_evidence as E
        if not recipient:
            return None
        ev = dict(E.local_checks(name, recipient))
        domain = str(recipient).rpartition("@")[2].lower()
        net = None
        try:
            net = self.store.ev_get(domain, now)
            if net is None and inline and self.enrichers is not None:
                net = self.enrichers.gather(domain)
        except Exception:
            net = None
        if net:
            ev.update({k: v for k, v in net.items() if k in ("domain_age_days", "mx_present", "blocklist_hit", "blocklist_source", "checked", "unavailable")})
        return ev

    def _fanout_evidence(self, hist, payee, recipient, now):
        """The SET of brand-new payees behind a tripped fan-out counter, each with its evidence (the gate asks fanout_verdict to decide). Only the current
        payee may be gathered inline (rare path: the counter just tripped); the others rely on what their own first-payee gather cached, else they are
        'unknown', which holds. Never raises."""
        out = {}
        try:
            for p in hist.fanout_payees(now):
                if p != payee:
                    rc = next(iter(sorted(hist.payee_recipients.get(p, ()))), None)
                    out[p] = self._payee_evidence(p, rc, now)
            out[payee] = self._payee_evidence(payee, recipient, now, inline=True)
        except Exception:
            return None
        return out

    def _wants_evidence(self, p):
        if p["status"] == "frozen":
            return True
        try:
            return bool(json.loads(p.get("event_json") or "{}").get("first_seen_payee"))
        except ValueError:
            return False

    def _gather(self, pid):
        """Tier 1, out of the pay path: fetch external evidence for the payee's DOMAIN, score it, and attach it to the payment so the human
        ask is informed. Evidence can only inform a held payment (or flag an executed one); it never releases anything."""
        from . import external_evidence as E
        p = self.store.payment(pid)
        if not p or (self.enrichers is None and not self.offline_evidence):
            return
        domain = str(p["recipient"]).rpartition("@")[2].lower()
        net = self.enrichers.gather(domain) if self.enrichers is not None else {}      # offline_evidence: Tier 0 only, nothing leaves the machine
        combined = {**(E.local_checks(p["payee"], p["recipient"]) if self.local_evidence else {}), **net}
        hint, reason, items = E.score_evidence(combined)
        unavailable = list(net.get("unavailable") or [])
        items = list(items)
        if unavailable:
            items.append({"signal": "unavailable", "detail": "not checked (unavailable): " + ", ".join(unavailable), "severity": "info"})
        detail = [i["detail"] for i in items]                                   # human lines for the dashboard / alert; `items` keeps the structure
        self.store.update_payment(pid, evidence_json=json.dumps({"hint": hint, "reason": reason, "items": items, "detail": detail, "domain": domain,
                                                                  "checked": net.get("checked", []), "unavailable": unavailable}))
        self.audit.append("evidence_gathered", "enricher", agent_id=p["agent_id"], payment_id=pid,
                          detail={"domain": domain, "hint": hint, "checked": net.get("checked", []), "unavailable": unavailable})
        if hint in ("freeze", "hold") and p["status"] in ("sent", "pending", "settled"):
            self.audit.append("adverse_evidence_after_execution", "enricher", agent_id=p["agent_id"], payment_id=pid, detail={"domain": domain, "hint": hint})

    # ---- O3 cooling-off: the hold releases itself, but only if the gather came back clean AND a fresh re-check still passes ----
    def release_due(self):
        """Server-side sweep of cooling-off holds whose window has passed. A hold auto-releases ONLY when (1) the Tier-1 gather completed and found
        nothing adverse (unknown is NOT clean: a gather that could not run keeps the hold for a human), and (2) the payment, re-classified NOW with the
        original plan, is no longer frozen by any rule (cap, cumulative windows, network reputation, evidence...). Otherwise it stays held for the
        operator. An operator denial always wins (the claim is an atomic frozen->pending flip). Safe to call repeatedly."""
        now, out = self.clock(), []
        for p in self.store.due_cooldowns(now):
            try:
                out.append((p["payment_id"],) + self._try_auto_release(p, now))
            except Exception as err:
                out.append((p["payment_id"], "error", type(err).__name__))
        return out

    def _hold_for_human(self, p, why):
        self.store.update_payment(p["payment_id"], release_at=None)                 # stop sweeping it: it now waits for the operator
        self.audit.append("cooldown_release_blocked", "gate", agent_id=p["agent_id"], payment_id=p["payment_id"], detail={"why": why[:200]})
        return "held", why

    def _try_auto_release(self, p, now):
        pid = p["payment_id"]
        try:
            ev = json.loads(p["evidence_json"]) if p.get("evidence_json") else None
        except ValueError:
            ev = None
        if ev is None:
            if now < p["release_at"] + 600:
                return "waiting", "evidence not gathered yet"
            return self._hold_for_human(p, "no evidence was gathered; unknown is not clean")
        checked = set(ev.get("checked") or [])
        if ev.get("hint") != "none" or not {"domain_age_days", "blocklist"} <= checked:
            return self._hold_for_human(p, "adverse or incomplete evidence (hint=%s, checked=%s)" % (ev.get("hint"), sorted(checked)))
        # re-check AND claim under ONE lock hold: the rolling-cap decision and the reservation must be atomic,
        # or a concurrent payment can slip in on the same window between the check and the claim.
        with self._decide_lock:
            cur = self.store.payment(pid)
            if not cur or cur["status"] != "frozen":
                return "skipped", "no longer held"
            agent = cur["agent_id"]
            old = json.loads(cur["event_json"] or "{}")
            hist = self._history(agent, now, (cur["recipient"], cur["payee"], cur["currency"]))
            txn = {k: old.get(k) for k in ("kind", "agent", "payee", "recipient", "amount", "currency", "memo", "hour", "stated_intent_payees", "intent_id", "require_intent")}
            txn.update(ts=now, cooloff_released=True,                                # judge it as a payment made now, with the ORIGINAL plan, minus the cooling-off rule itself
                       fx=fxmod.to_usd_rate(cur["currency"]) or 1.0)                 # re-resolve FX so caps are compared in USD, not treated as USD
            net = self._network(cur["recipient"], cur["payee"], now)
            if net:
                txn["network"] = net
            evn = self._evidence(cur["payee"], cur["recipient"], now)
            if evn:
                txn["evidence"] = evn
            tier, kind, reason = G.classify(txn, hist)
            cap_usd = float(cur["amount"]) * (txn["fx"])                             # the platform cap is a hard USD ceiling nothing crosses, not even a cooldown auto-release
            if tier == "freeze":
                return self._hold_for_human(p, "re-check now freezes it: " + reason)
            if self.platform_cap is not None and cap_usd > self.platform_cap:
                return self._hold_for_human(p, "re-check now exceeds the platform cap (%.2f USD): held for a human" % cap_usd)
            # the money moves NOW: flip the status to 'pending' AND book its spend at release time in ONE write, so a
            # concurrent payment can never see this release in flight while it still counts in the original (held) hour.
            # the original request time is preserved in the audit log below.
            if not self.store.claim_release(pid, "auto:cooldown", ts=now):
                return "skipped", "claimed elsewhere or denied"
        # record the release BEFORE sending: no money moves without a durable audit record.
        seq = self.audit.append("auto_released_after_cooldown", "gate", agent_id=agent, payment_id=pid, mode=cur.get("mode"),
                                detail={"amount": cur["amount"], "payee": cur["payee"], "evidence_checked": sorted(checked)})
        if seq is None:
            self.store.update_payment(pid, status="frozen", decided_by=None)
            return self._hold_for_human(p, "audit log unavailable: cooldown release not recorded, held for a human")
        res = self._send(pid, cur["recipient"], fmt_amount(cur["amount"], cur["currency"]), cur["currency"], cur["memo"] or "")
        self.audit.append("auto_released_after_cooldown_result", "gate", agent_id=agent, payment_id=pid, mode=cur.get("mode"),
                          detail={"payee": cur["payee"], "result": res.get("status")})
        if res.get("status") == "sent":
            self._rep_contribute(self.store.payment(pid), "executed")
        return "released", res.get("status", "")

    def _org(self, agent):
        return self.store.agent(agent).get("org_id") or "org:default"          # one deployment = one org unless the operator says otherwise

    def _rep_contribute(self, payment, event):
        """Best-effort and de-identified: a keyed payee hash, a keyed org hash, a timestamp and an event TYPE. Never blocks or fails a payment."""
        if self.reputation is None or not payment:
            return
        try:
            self.reputation.contribute(payment["recipient"], payment["payee"], self._org(payment["agent_id"]), event, payment["ts"] if event == "executed" else self.clock())
        except Exception:
            pass

    def confirm_fraud(self, payment_id, by="operator"):
        """OPERATOR-ONLY. A human states this payee is fraudulent: the strongest negative signal, shared (de-identified) with every agent.
        A still-frozen payment is denied in the same step."""
        p = self.store.payment(payment_id)
        if not p:
            return {"ok": False, "reason": "unknown payment"}
        if p["status"] == "frozen":
            self.store.claim_deny(payment_id, "human:" + clean_text(by, "by", limit=40))        # atomic; if a release won the race the payee is still recorded as fraud, the payment is not rewritten
        self.audit.append("confirmed_fraud", clean_text(by, "by", limit=40), agent_id=p["agent_id"], payment_id=payment_id, mode=p.get("mode"),
                          detail={"payee": p["payee"], "shared_with_network": self.reputation is not None})
        self._rep_contribute(p, "confirmed_fraud")
        return {"ok": True, "payment_id": payment_id, "shared": self.reputation is not None}

    def _network(self, recipient, payee, now):
        if self.reputation is None:
            return None
        try:
            return self.reputation.read(recipient, payee, now)               # None on any failure: the per-agent gate then stands alone
        except Exception:
            return None

    def _mode(self, agent):
        return self.resolve_policy(agent)["mode"]

    @staticmethod
    def _fingerprint(r):
        return hashlib.sha256(json.dumps([r["agent_id"], r["payee"], r["recipient_email"], str(r["amount"]), r["currency"], r["memo"], r["intent_id"]]).encode()).hexdigest()

    def pay(self, request, request_id=None):
        """In-path gate. allow/review -> call PayPal Payouts; freeze -> the PayPal call NEVER fires (held for a human).
        request_id = the PayPal-Request-Id header: the same key replays the FIRST result (verdict, status, any paypal_error), the same
        key with different parameters is an error, and a retry that arrives while the first is in flight gets `in_progress`.
        Shadow mode classifies and logs but lets the payment proceed. ANY internal failure before a verdict blocks the money move."""
        try:
            r = validate_request(request)
        except ValidationError as err:
            self.audit.append("rejected", "gate", detail={"reason": str(err)[:120]})
            return {"decision": "reject", "status": "rejected", "reason": str(err)}
        rid = request_id or r.get("idempotency_key") or None
        if rid and not RID_RE.match(rid):
            return {"decision": "reject", "status": "rejected", "reason": "PayPal-Request-Id must be 1-64 characters of letters, digits, _ . : -"}
        agent, fp = r["agent_id"], self._fingerprint(r)
        try:
            with self._decide_lock:
                now = self.clock()
                if rid:
                    prev = self.store.idem_get(agent, rid)
                    if prev:
                        if prev["fingerprint"] != fp:
                            return {"decision": "reject", "status": "idempotency_conflict", "reason": "this PayPal-Request-Id was already used with different parameters"}
                        if prev["response_json"]:
                            return {**json.loads(prev["response_json"]), "duplicate": True}
                        return {"decision": "in_progress", "status": "in_progress", "payment_id": prev["payment_id"], "duplicate": True}
                pol = self.resolve_policy(agent)                # resolved ONCE per decision; every read below uses it
                self._snapshot(pol)
                hist = self._history(agent, now, (r["recipient_email"], r["payee"], r["currency"]), policy=pol)
                hist.note_attempt(now, r["payee"])          # this attempt counts toward velocity even if frozen
                intent = self.store.intent(r["intent_id"]) if r["intent_id"] else self.store.latest_intent(agent, now - INTENT_TTL_S)
                if r["intent_id"] and (intent is None or intent["agent_id"] != agent):
                    return {"decision": "reject", "status": "rejected", "reason": "unknown intent_id for this agent"}
                stated = json.loads(intent["stated_payees_json"]) if intent else None
                amount = float(r["amount"])
                hour = time.gmtime(now).tm_hour
                txn = {"kind": "txn", "agent": agent, "payee": r["payee"], "recipient": r["recipient_email"], "amount": amount,
                       "currency": r["currency"], "memo": r["memo"], "ts": now, "hour": hour,
                       "stated_intent_payees": stated, "intent_id": intent["intent_id"] if intent else None,
                       "require_intent": pol["knobs"]["require_intent"], "fx": fxmod.to_usd_rate(r["currency"]) or 1.0}
                net = self._network(r["recipient_email"], r["payee"], now)
                if net:
                    txn["network"] = net
                ev = self._evidence(r["payee"], r["recipient_email"], now)
                if ev:
                    txn["evidence"] = ev
                if G.fanout_trips(txn, hist)[0]:
                    fe = self._fanout_evidence(hist, r["payee"], r["recipient_email"], now)
                    if fe is not None:
                        txn["fanout_evidence"] = fe
                crashed = False
                try:
                    tier, kind, reason = G.classify(txn, hist)
                except Exception as err:                     # the gate itself broke: that is a freeze, never an allow
                    tier, kind, reason, crashed = "freeze", "degradation", "gate error (%s): fail-closed" % type(err).__name__, True
                mode = pol["mode"]
                amount_usd = amount * (txn.get("fx") or 1.0)        # the platform cap is in USD; compare USD-normalized, like the gate's hard_cap
                platform_breach = self.platform_cap is not None and amount_usd > self.platform_cap
                # shadow mode suppresses enforcement for ordinary classifier freezes only.
                # the two hard safety stops are NEVER shadowed: the operator's platform cap, and a gate crash.
                hard_stop = platform_breach or crashed
                enforced = not (tier == "freeze" and mode == "shadow" and not hard_stop)
                if tier == "freeze" and mode == "shadow" and hard_stop:
                    reason += " (platform cap is enforced even in shadow mode)" if platform_breach else " (gate errors are enforced even in shadow mode)"
                try:
                    cadence = hist.cadence(r["payee"], now)
                    txn.update({"velocity_60s": len(hist.attempts_in_window(now)), "first_seen_payee": r["payee"] not in hist.payees,
                                "first_seen_amount_bucket": amount_bucket(amount), "cadence": cadence,
                                "intent_match": (r["payee"] in stated) if stated else None,
                                "verdict": tier, "score_kind": kind, "reason": reason, "mode": mode, "enforced": enforced})
                except Exception:
                    cadence = None
                held = tier == "freeze" and enforced
                pid = "pay-" + uuid.uuid4().hex[:14]
                row = {"payment_id": pid, "agent_id": agent, "ts": now, "payee": r["payee"], "recipient": r["recipient_email"],
                       "amount": amount, "currency": r["currency"], "memo": r["memo"], "hour": hour, "intent_id": txn["intent_id"],
                       "verdict": tier, "score_kind": kind, "reason": reason, "status": "frozen" if held else "pending",
                       "provider": self._provider_of(agent), "paypal_batch_id": None, "paypal_status": None, "decided_by": "goalie" if enforced else "goalie-shadow",
                       "event_json": json.dumps({**{k: v for k, v in txn.items() if k not in ("network", "fanout_evidence")},
                                                 "network": ({k: net[k] for k in ("distinct_orgs", "distinct_orgs_froze", "freeze_count", "confirmed_fraud", "seen")} if net else None)}),
                       "narration": None,
                       "risk": G.risk_score(tier, kind, on_cycle=bool((cadence or {}).get("on_cycle"))), "llm_flag": None,
                       "mode": mode, "enforced": 1 if enforced else 0, "policy_hash": pol["hash"],
                       "release_at": (now + (pol["knobs"]["cooldown_s"] or 0.0)) if (held and reason.startswith("cooling-off:")) else None}
                won_reservation = True
                if rid:
                    won_reservation = self.store.idem_put(agent, rid, fp, pid)
                    if not won_reservation:
                        # another caller already reserved this idempotency key: do not insert or send a second payment.
                        prev = self.store.idem_get(agent, rid)
                        if prev and prev.get("fingerprint") and prev["fingerprint"] != fp:
                            return {"decision": "reject", "status": "idempotency_conflict", "reason": "this PayPal-Request-Id was already used with different parameters"}
                        if prev and prev.get("response_json"):
                            return {**json.loads(prev["response_json"]), "duplicate": True}
                        return {"decision": "in_progress", "status": "in_progress", "payment_id": prev["payment_id"] if prev else None, "duplicate": True}
                try:
                    self.store.insert_payment(row)
                except Exception:
                    if rid and won_reservation:
                        self.store.idem_del(agent, rid, pid)       # the reservation points at a payment that does not exist: release it so a retry is not stranded
                    raise
                seq = self.audit.append("decision", "gate", agent_id=agent, payment_id=pid, mode=mode,
                                        detail={"verdict": tier, "enforced": enforced, "score_kind": kind, "reason": reason[:300], "payee": r["payee"],
                                                "amount": amount, "currency": r["currency"], "risk": row["risk"], "policy_hash": pol["hash"]})
                if seq is None and not held:
                    # the decision could not be written to the audit log: do not let money move without a record. fail closed.
                    reason = "audit log unavailable: fail-closed (nothing sent until the decision can be recorded)"
                    tier, kind, held, enforced = "freeze", "degradation", True, True
                    self.store.update_payment(pid, status="frozen", verdict="freeze", score_kind="degradation",
                                              reason=reason, enforced=1, decided_by="goalie")
                    row.update(status="frozen", verdict="freeze", score_kind="degradation", reason=reason, enforced=1)
                if held and G.is_identity_freeze(reason):               # payee-identity freezes only; cap/velocity/amount freezes are about the agent
                    self._rep_contribute(row, "identity_freeze")
        except Exception as err:                             # no verdict reached (store down, bug): block the money move
            self.audit.append("fail_closed", "gate", agent_id=agent, detail={"error": type(err).__name__})
            return {"decision": "error", "status": "fail_closed", "reason": "PayCanary could not reach a verdict, so nothing was sent (%s)" % type(err).__name__}
        decision = tier if enforced else ("review" if tier == "freeze" else tier)
        out = {"payment_id": pid, "decision": decision, "reasons": [reason], "status": row["status"], "paypal": None, "provider": row["provider"], "mode": mode}
        if not enforced:
            out.update(enforced=False, would_have="freeze")
        if not held:
            out.update(self._send(pid, r["recipient_email"], fmt_amount(r["amount"], r["currency"]), r["currency"], r["memo"]))
            if out.get("status") == "sent":
                self._rep_contribute(self.store.payment(pid), "executed")
        if rid:
            try:
                self.store.idem_set_response(agent, rid, json.dumps(out))
            except Exception:
                pass
        self._decided(pid)
        return out

    def enrich(self, pid):
        from .enrich import enrich_payment
        try:
            return enrich_payment(self, pid)
        except Exception:
            return None                                  # fail open: narration is optional

    def _require_intent(self, agent):
        return self.resolve_policy(agent)["knobs"]["require_intent"]

    def _alert(self, pid):
        n = self.notifier
        if not n:
            return
        p = self.store.payment(pid)
        if not p:
            return
        kind = ("freeze" if p["enforced"] else "shadow_freeze") if p["verdict"] == "freeze" else ("review" if p["verdict"] == "review" else None)
        try:
            ev = json.loads(p["evidence_json"]) if p.get("evidence_json") else None
        except ValueError:
            ev = None
        if kind is None and ev and ev.get("hint") in ("freeze", "hold"):
            kind = "review"                                  # an executed payment that external evidence now flags: tell the operator
        if kind and n.wants(kind):
            n.notify(kind, {"evidence": (ev or {}).get("detail"), "evidence_hint": (ev or {}).get("hint"),"payment_id": pid, "agent_id": p["agent_id"], "payee": p["payee"], "amount": p["amount"], "currency": p["currency"],
                            "verdict": p["verdict"], "reason": p["reason"], "risk": p["risk"], "mode": p["mode"], "enforced": bool(p["enforced"]),
                            "status": p["status"], "ts": p["ts"]})

    def _decided(self, pid):
        def alert():
            try:
                self._alert(pid)
            except Exception:
                pass                                     # an alert problem never touches the payment
        p = self.store.payment(pid) if (self.enrichers is not None or self.offline_evidence) else None
        if p and self._wants_evidence(p):
            def job():                                   # freeze-and-gather-then-ask: the alert carries the evidence (or says what could not be checked)
                try:
                    self._gather(pid)
                except Exception:
                    pass                                 # gather failures are fail-open: the hold stands and the ask goes out without evidence
                alert()
            if self.sync_enrich:
                job()
            else:
                threading.Thread(target=job, daemon=True).start()
        else:
            alert()
        if self.on_decided:
            try:
                self.on_decided(pid)
            except Exception:
                pass                                     # enrichment can never break a payment

    def _provider_of(self, agent):
        return self.store.agent(agent).get("provider") or self.default_provider

    def assign_provider(self, agent_id, name, actor="operator"):
        """OPERATOR-ONLY. Route this agent's FUTURE payments to a registered provider (exact name). Existing payments keep the provider they were created with.
        Audited as config_changed naming the provider."""
        agent_id = clean_text(agent_id, "agent_id", limit=64)
        if name not in self.providers:
            raise ValidationError("unknown provider %r (registered: %s)" % (str(name)[:30], ", ".join(self.providers.names()) or "none"))
        before = self.store.agent(agent_id)
        cfg = dict(before)
        cfg["provider"] = name
        self._write_config(agent_id, before, cfg, actor, extra={"provider_assigned": name})
        return {"agent_id": agent_id, "provider": name}

    def _send(self, pid, recipient, amount, currency, memo):
        """Hand an already-decided, not-frozen payment to the provider IT WAS ROUTED TO when it was created (the payment row's provider, never the agent's
        current setting). Exact-name dispatch, no fallback; any failure fails closed and is recorded."""
        row = self.store.payment(pid) or {}
        prov = row.get("provider") or "paypal"                                  # rows created before providers existed were PayPal payments
        req = SendRequest(prov, pid, recipient, str(amount), currency, memo or "", "pc-%s-%s" % (prov, pid[4:]))
        try:
            res = self.providers.get(prov).send(req)
        except ProviderError as err:
            res = SendResult(prov, False, "failed", error=type(err).__name__)
        except Exception as err:                                                # an adapter must never raise, but if it does: fail closed
            res = SendResult(prov, False, "failed", error=type(err).__name__)
        err_status = "paypal_error" if prov == "paypal" else "provider_error"
        if res.ok and res.provider_id:
            ref = "%s:%s" % (prov, res.provider_id)
            fields = {"status": "sent", "provider_ref": ref}
            if prov == "paypal":
                fields.update(paypal_batch_id=res.provider_id, paypal_status=res.raw_status)
            self.store.update_payment(pid, **fields)
            self.audit.append("provider_result", "gate", payment_id=pid, detail={"provider": prov, "status": "sent", "provider_ref": ref, "raw_status": res.raw_status})
            out = {"status": "sent", "provider": prov, "provider_ref": ref}
            if prov == "paypal":
                self.audit.append("paypal_result", "gate", payment_id=pid, detail={"status": "sent", "batch_id": res.provider_id, "batch_status": res.raw_status})
                out["paypal"] = {"batch_id": res.provider_id, "batch_status": res.raw_status}
            return out
        fields = {"status": err_status}
        if prov == "paypal":
            fields["paypal_status"] = str(res.http) if res.http is not None else res.error
        self.store.update_payment(pid, **fields)
        self.audit.append("provider_result", "gate", payment_id=pid, detail={"provider": prov, "status": err_status, "error": res.error, "http": res.http})
        out = {"status": err_status, "provider": prov}
        if prov == "paypal":
            legacy = {"error": res.error} if res.http is None else {"http": res.http, "name": None if str(res.error).startswith("http_") else res.error}
            self.audit.append("paypal_result", "gate", payment_id=pid, detail=({"status": "paypal_error", "error": res.error} if res.http is None else {"status": "paypal_error", "http": res.http}))
            out["paypal"] = legacy
        return out

    # ---- human decisions on a frozen payment ------------------------------------------------------------------
    def release(self, payment_id, by="operator"):
        """OPERATOR-ONLY (the HTTP layer authenticates; `by` is taken from the authenticated principal, never the request body)."""
        p = self.store.payment(payment_id)
        if not p or p["status"] != "frozen":
            return {"ok": False, "reason": "payment is not frozen"}
        # the operator's platform cap is a hard USD ceiling that NOTHING may cross, not even a manual release.
        cap_usd = p["amount"] * (fxmod.to_usd_rate(p["currency"]) or 1.0)
        if self.platform_cap is not None and cap_usd > self.platform_cap:
            self.audit.append("release_refused", clean_text(by, "by", limit=40), agent_id=p["agent_id"], payment_id=payment_id,
                              detail={"reason": "over platform cap", "amount_usd": round(cap_usd, 2), "platform_cap": self.platform_cap})
            return {"ok": False, "reason": "release would exceed the platform cap (%.2f USD > %.2f): raise PAYCANARY_PLATFORM_CAP to release this" % (cap_usd, self.platform_cap)}
        # The booking below changes rolling spend history, so it must be serialised against the decision path
        # (read-baseline -> classify -> insert). Under the SAME lock, an automatic payment can never read spend
        # history that is missing a release booked in the same instant, nor book one the release has not yet seen.
        with self._decide_lock:
            # atomic claim: only one of release / deny / sweep wins a frozen payment. the money moves NOW, so flip the
            # status to 'pending' AND book its spend at release time in ONE write — a concurrent payment can never see
            # this release in flight while it still counts in the original (held) hour. original ts kept in the audit log.
            if not self.store.claim_release(payment_id, "human:" + clean_text(by, "by", limit=40), ts=self.clock()):
                return {"ok": False, "reason": "payment is not frozen"}
        seq = self.audit.append("released", clean_text(by, "by", limit=40), agent_id=p["agent_id"], payment_id=payment_id, mode=p.get("mode"),
                                detail={"amount": p["amount"], "payee": p["payee"], "overridden_reason": (p.get("reason") or "")[:300]})
        if seq is None:
            # no durable record of the release: do not send. put the payment back on the operator's queue.
            self.store.update_payment(payment_id, status="frozen", decided_by=None)
            return {"ok": False, "reason": "audit log unavailable: release not recorded, so nothing was sent (payment stays frozen)"}
        res = self._send(payment_id, p["recipient"], fmt_amount(p["amount"], p["currency"]), p["currency"], p["memo"] or "")
        if res.get("status") == "sent":
            self._rep_contribute(self.store.payment(payment_id), "executed")
        return {"ok": True, "payment_id": payment_id, **res}

    def deny(self, payment_id, by="operator"):
        p = self.store.payment(payment_id)
        if not p or p["status"] != "frozen":
            return {"ok": False, "reason": "payment is not frozen"}
        if not self.store.claim_deny(payment_id, "human:" + clean_text(by, "by", limit=40)):
            return {"ok": False, "reason": "payment is not frozen"}
        self.audit.append("denied", clean_text(by, "by", limit=40), agent_id=p["agent_id"], payment_id=payment_id, mode=p.get("mode"),
                          detail={"amount": p["amount"], "payee": p["payee"], "overridden_reason": (p.get("reason") or "")[:300]})
        if G.is_identity_freeze(p["reason"]):                   # a human confirming an identity freeze is payee-attributable evidence
            self._rep_contribute(p, "denied")
        return {"ok": True, "payment_id": payment_id, "status": "denied"}

    # ---- webhooks ---------------------------------------------------------------------------------------------
    def handle_webhook(self, headers, body_bytes, provider="paypal"):
        """Log a provider webhook, let THAT provider's adapter verify it with ITS OWN secret, and update only payments that were routed to that same provider.
        Unverified events never change a payment unless the provider has no verification configured and PAYCANARY_DEV_WEBHOOKS=1 (local dev)."""
        try:
            adapter = self.providers.get(provider)
        except ProviderError:
            return {"ok": False, "reason": "unknown provider"}
        ev = adapter.verify_webhook(headers, body_bytes)
        if ev.parse_error:
            return {"ok": False, "reason": "invalid json"}
        self.store.add_webhook(self.clock(), ev.event_type, ev.provider_id, ev.verified, ev.raw or body_bytes.decode(errors="replace")[:20000], provider=provider)
        self.audit.append("webhook_received", provider if ev.verified else "unverified-sender",
                          detail={"provider": provider, "event_type": ev.event_type, "verified": ev.verified, "batch": ev.provider_id, "provider_id": ev.provider_id})
        if ev.provider_id and (ev.verified or (not ev.verification_configured and self.dev_webhooks)):
            p = self.store.payment_by_provider_ref(provider, ev.provider_id)
            if p is None and provider == "paypal":
                legacy = self.store.payment_by_batch(ev.provider_id)             # rows written before provider_ref existed
                p = legacy if legacy and (legacy.get("provider") or "paypal") == "paypal" else None
            if p:
                new = {"settled": "settled", "reversed": "reversed"}.get(ev.state, p["status"])
                fields = {"status": new}
                if provider == "paypal":
                    fields["paypal_status"] = str(ev.raw_status)[:40]
                self.store.update_payment(p["payment_id"], **fields)
        return {"ok": True, "verified": ev.verified}
