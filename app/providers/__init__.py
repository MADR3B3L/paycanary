"""Payment providers. EVERY provider is a sealed box: one folder (app/providers/<name>/), its own config slot, its own fixtures, no imports from
another provider's folder. The only shared code is base.py (the Provider interface) and registry.py (exact-name dispatch). The decision core
(gate, policy, service, store) depends on base + registry only; app/providers/factory.py and the composition roots (main, seed) are the only
places that know the concrete providers. tests/test_providers_boundary.py enforces all of this structurally."""
