"""Policy profiles: named, versioned bundles of the per-agent guard knobs, and the single place a decision's effective policy is resolved.

Ratified shape (architect-board, 2026-11-03):
  * a profile bundles EXACTLY these 9 knobs (the 9th, probation_hold, is the opt-in stricter cooling-off posture, default off) and every profile must set ALL of them (a pin only freezes the fields a profile sets, so a partial
    profile would not be a provable policy). `mode`, `org_id`, `token_hash`, `approved_payees` are NOT profile fields. Unknown keys are refused.
  * resolution: per-agent explicit override > assigned profile field > system default, then the operator's platform_cap as a last-step min()
    on the resolved hard_cap (a profile can never lift an agent above the platform ceiling).
  * audit is a SNAPSHOT: the resolved policy body is content-addressed (policy_hash), written into the audit chain once per distinct body
    (`policy_resolved`), and every decision carries the hash. Reconstruction is deliberately not offered: platform cap, default mode and the
    gate code all move with the deployment, so only a snapshot proves what applied.
Pure functions, no I/O."""
import hashlib
import json
import math

from . import gate as G
from .schema import ValidationError

PROFILE_FIELDS = ("hard_cap", "active_hours", "require_intent", "new_payee_floor", "fanout_k", "cooldown_s", "probation_abs", "probation_mult", "probation_hold")
POSITIVE = ("hard_cap", "new_payee_floor", "fanout_k", "probation_abs", "probation_mult")


def finite(v, name, minimum=0.0, strict=False):
    """The one guard every numeric knob goes through (T6: float('nan') / float('inf') parse fine and silently disable a cap)."""
    if isinstance(v, bool) or v is None:
        raise ValidationError(f"{name} must be a number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a number")
    if not math.isfinite(f):
        raise ValidationError(f"{name} must be a finite number")
    if f < minimum or (strict and f <= minimum):
        raise ValidationError(f"{name} must be {'greater than' if strict else 'at least'} {minimum:g}")
    return f


def clean_hours(h):
    if not isinstance(h, (list, tuple)):
        raise ValidationError("active_hours must be a list of hours 0-23")
    out = []
    for x in h:
        if isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 23:
            raise ValidationError("active_hours must be a list of hours 0-23")
        out.append(x)
    return sorted(set(out))


def clean_field(name, v):
    if name == "active_hours":
        return clean_hours(v)
    if name in ("require_intent", "probation_hold"):
        if not isinstance(v, bool):
            raise ValidationError(f"{name} must be true or false")
        return v
    if name == "cooldown_s":
        return finite(v, name, 0.0)
    return finite(v, name, 0.0, strict=True)


def validate_profile(name, fields):
    """-> (clean name, clean fields). Refuses unknown keys, null/missing fields (all required), non-finite numbers."""
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64 or "@" in name:
        raise ValidationError("profile name must be 1-64 characters and must not contain '@'")
    if not isinstance(fields, dict):
        raise ValidationError("fields must be an object")
    extra = sorted(set(fields) - set(PROFILE_FIELDS))
    if extra:
        raise ValidationError("unknown profile field(s): " + ", ".join(extra) + " (a profile bundles only: " + ", ".join(PROFILE_FIELDS) + ")")
    missing = [f for f in PROFILE_FIELDS if f not in fields or fields[f] is None]
    if missing:
        raise ValidationError(f"a profile must set all {len(PROFILE_FIELDS)} fields; missing: " + ", ".join(missing))
    clean = {f: clean_field(f, fields[f]) for f in PROFILE_FIELDS}
    if clean["probation_hold"] and clean["cooldown_s"] == 0:
        raise ValidationError("probation_hold needs cooldown_s > 0 (it extends the cooling-off hold; with no cooling-off it would do nothing)")
    return name.strip(), clean


def system_defaults(default_hard_cap, default_require_intent):
    return {"hard_cap": default_hard_cap, "active_hours": None, "require_intent": default_require_intent, "new_payee_floor": G.NEW_PAYEE_FLOOR,
            "fanout_k": G.FANOUT_K, "cooldown_s": 0.0, "probation_abs": G.PROBATION_ABS, "probation_mult": G.PROBATION_MULT, "probation_hold": False}      # probation_hold defaults to False PERMANENTLY: flipping it would silently change every old pin


def resolve(cfg, profile_fields, defaults, platform_cap):
    """-> dict of the 8 resolved knobs. A per-agent explicit value (non-null in the agent config) wins, then the pinned profile's value, then the
    default; platform_cap is applied last to hard_cap. An empty profile active_hours means 'all hours' (explicitly set, not learned)."""
    out = {}
    for f in PROFILE_FIELDS:
        if cfg.get(f) is not None:
            out[f] = cfg[f]
        elif profile_fields is not None and profile_fields.get(f) is not None:
            out[f] = profile_fields[f] or None if f == "active_hours" else profile_fields[f]
        else:
            out[f] = defaults[f]
    if platform_cap is not None:
        out["hard_cap"] = platform_cap if out["hard_cap"] is None else min(out["hard_cap"], platform_cap)
    return out


# sha256 of gate.py's bytes, read ONCE when the process loads the gate (not per request, not re-read mid-run): it names the gate file that actually judged
# the payments of this process, and says nothing about service.py or the enrichers. Map a hash back to a release via the release notes.
with open(G.__file__, "rb") as _gate_file:
    GATE_SHA256 = hashlib.sha256(_gate_file.read()).hexdigest()


def policy_body(resolved, *, profile, mode, platform_cap, approved_payees):
    return {"profile": profile, "knobs": resolved, "mode": mode, "platform_cap": platform_cap, "approved_payees": sorted(approved_payees or []), "gate_sha256": GATE_SHA256}


def policy_hash(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


# Starter bundles with the numbers Marty signed off (2026-11-03). Nothing installs them automatically (operator calls POST /api/profiles/presets).
# standard differs from the bare engine defaults only by its 900 s cooling-off.
PRESETS = {
    "standard": {"hard_cap": 500.0, "active_hours": [], "require_intent": True, "new_payee_floor": 100.0, "fanout_k": 3.0,
                 "cooldown_s": 900.0, "probation_abs": 500.0, "probation_mult": 3.0, "probation_hold": False},
    "strict": {"hard_cap": 200.0, "active_hours": [], "require_intent": True, "new_payee_floor": 50.0, "fanout_k": 2.0,
               "cooldown_s": 1800.0, "probation_abs": 200.0, "probation_mult": 2.0, "probation_hold": False},
    "permissive": {"hard_cap": 2000.0, "active_hours": [], "require_intent": True, "new_payee_floor": 250.0, "fanout_k": 4.0,
                   "cooldown_s": 1800.0, "probation_abs": 1000.0, "probation_mult": 4.0, "probation_hold": False},
}
