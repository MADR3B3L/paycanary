"""Two principals, two credentials, no overlap (red-team round 2, findings 1+2):
  OPERATOR  - the human: configure guards (/api/agents), approve payees, release/deny frozen payments, read the dashboard.
  AGENT     - the money-moving AI: /pay and /intent ONLY, and only as itself.
An agent must never hold the operator token. Tokens come from the environment or, on first run, a 0600 file next to the
code (.paycanary_tokens.json, gitignored). Comparison is constant-time over SHA-256 digests; nothing here ever logs a token."""
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path


def _digest(s):
    return hashlib.sha256((s or "").encode()).digest()


def _bearer(header):
    if not header or not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


def hash_token(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def load_tokens(root):
    """-> (operator_token, shared_agent_token_or_None). Env wins; otherwise create/read the 0600 file."""
    op, ag = os.environ.get("PAYCANARY_OPERATOR_TOKEN", ""), os.environ.get("PAYCANARY_AGENT_TOKEN", "")
    if op:
        return op, (ag or None)
    path = Path(root) / ".paycanary_tokens.json"
    if path.exists():
        d = json.loads(path.read_text())
    else:
        d = {"operator": secrets.token_urlsafe(32), "agent": secrets.token_urlsafe(32)}
        path.write_text(json.dumps(d))
        os.chmod(path, 0o600)
    return d["operator"], (ag or d.get("agent"))


class Auth:
    def __init__(self, operator_token, shared_agent_token=None):
        if not operator_token:
            raise ValueError("an operator token is required")
        self._op, self._shared = _digest(operator_token), (_digest(shared_agent_token) if shared_agent_token else None)
        if self._shared is not None and hmac.compare_digest(self._op, self._shared):
            raise ValueError("operator and agent tokens must differ")

    def operator_ok(self, header):
        tok = _bearer(header)
        return bool(tok) and hmac.compare_digest(_digest(tok), self._op)

    def looks_like_shared_agent_token(self, header):
        tok = _bearer(header)
        return bool(tok) and self._shared is not None and hmac.compare_digest(_digest(tok), self._shared)

    def agent_ok(self, header, agent_id, agent_cfg):
        """A per-agent token (issued by the operator) binds the caller to ONE agent_id. If that agent has one, the shared
        token no longer works for it. The shared token (dev convenience) is accepted only for agents without their own."""
        tok = _bearer(header)
        if not tok or not isinstance(agent_id, str):
            return False
        want = (agent_cfg or {}).get("token_hash")
        if want:
            return hmac.compare_digest(hash_token(tok), want)
        return self._shared is not None and hmac.compare_digest(_digest(tok), self._shared)


def load_reputation_key(root):
    """The HMAC key for the shared reputation table. Proxy-side secret (same handling as the PayPal credentials and the operator token):
    env PAYCANARY_REPUTATION_KEY, else a generated value kept in the 0600 tokens file. Never logged, never returned by any endpoint."""
    k = os.environ.get("PAYCANARY_REPUTATION_KEY", "")
    if k:
        return k
    path = Path(root) / ".paycanary_tokens.json"
    d = json.loads(path.read_text()) if path.exists() else {}
    if not d.get("reputation"):
        d["reputation"] = secrets.token_urlsafe(32)
        path.write_text(json.dumps(d))
        os.chmod(path, 0o600)
    return d["reputation"]
