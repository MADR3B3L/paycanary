"""Exact-name provider dispatch. No default-on-miss, no 'closest' match, no fallback: an unknown or unregistered name is an error and nothing is sent."""
from .base import NAME_RE, Provider, UnknownProvider


class Registry:
    def __init__(self, *providers):
        self._p = {}
        for p in providers:
            self.register(p)

    def register(self, provider):
        if not isinstance(provider, Provider):
            raise TypeError("a provider must subclass app.providers.base.Provider")
        if not NAME_RE.match(provider.name or ""):
            raise ValueError("invalid provider name %r" % (provider.name,))
        if provider.name in self._p:
            raise ValueError("provider %r is already registered" % provider.name)
        self._p[provider.name] = provider

    def get(self, name):
        try:
            return self._p[name]
        except (KeyError, TypeError):
            raise UnknownProvider("no provider registered as %r" % (name,)) from None

    def names(self):
        return sorted(self._p)

    def __contains__(self, name):
        return name in self._p
