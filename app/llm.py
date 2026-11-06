"""Thin Claude Haiku client with a hard daily budget. AUTHORITY = NONE: this module returns text or None, nothing else.
It never raises (LLM down / no key / over budget / junk => None), so callers fail OPEN for narration while the deterministic
gate (app/gate.py) keeps failing CLOSED on hard limits. Transport is injectable so every failure mode is testable offline."""
import json
import os
import time
import urllib.request

MODEL = "claude-haiku-5-5"
API_URL = "https://api.anthropic.com/v1/messages"


class Budget:
    """Per-UTC-day caps on calls and tokens, persisted in SQLite. A corrupt/unreadable counter counts as EXHAUSTED (we never
    spend blind)."""

    def __init__(self, store, max_calls=200, max_tokens=60000, clock=time.time):
        self.store, self.max_calls, self.max_tokens, self.clock = store, int(max_calls), int(max_tokens), clock

    def day(self):
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def try_spend(self):
        try:
            calls, tokens = self.store.llm_usage(self.day())
            if calls >= self.max_calls or tokens >= self.max_tokens:
                return False
            self.store.llm_bump(self.day(), calls=1)
            return True
        except Exception:
            return False

    def add_tokens(self, n):
        try:
            self.store.llm_bump(self.day(), tokens=max(0, int(n)))
        except Exception:
            pass

    def remaining(self):
        try:
            calls, tokens = self.store.llm_usage(self.day())
            return {"calls": self.max_calls - calls, "tokens": self.max_tokens - tokens}
        except Exception:
            return {"calls": 0, "tokens": 0}


def _http_transport(api_key):
    def call(system, user, max_tokens, timeout):
        body = json.dumps({"model": MODEL, "max_tokens": max_tokens, "system": system,
                           "messages": [{"role": "user", "content": user}],
                           "thinking": {"type": "disabled"}}).encode()      # the model thinks by default and a small max_tokens is eaten before any text
        req = urllib.request.Request(API_URL, data=body, headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                                                  "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage", {})
        return text, int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
    return call


class LLM:
    def __init__(self, budget, transport, timeout=20.0, max_chars=2000):
        self.budget, self.transport, self.timeout, self.max_chars = budget, transport, timeout, max_chars
        self.calls = self.failures = self.skipped_budget = 0

    def complete(self, system, user, max_tokens=200):
        return self.complete_ex(system, user, max_tokens)[0]

    def complete_ex(self, system, user, max_tokens=200):
        """-> (text|None, reason|None). reason is why there is no text: "budget" (daily cap), "timeout" (transport/timeout/HTTP error), "junk" (empty/oversize)."""
        if not self.budget.try_spend():
            self.skipped_budget += 1
            return None, "budget"
        self.calls += 1
        try:
            text, tokens = self.transport(system, user[: self.max_chars * 3], max_tokens, self.timeout)
            self.budget.add_tokens(tokens)
            if not isinstance(text, str) or len(text) > self.max_chars:
                self.failures += 1
                return None, "junk"
            return text, None
        except Exception:
            self.failures += 1
            return None, "timeout"


def from_config(store):
    from . import config
    key = config.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return LLM(Budget(store, int(config.get("LLM_MAX_CALLS_PER_DAY", "200") or 200), int(config.get("LLM_MAX_TOKENS_PER_DAY", "60000") or 60000)),
               _http_transport(key))
