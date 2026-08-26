"""Live-mode runtime concerns: the autonomy guard, spend caps, and what counts as live.

Everything in this package applies only when ``PROVIDER_MODE=live``. Simulated
mode — the frozen corpus, the M0 benchmark, and the public demo — is untouched
by all of it, deliberately: the simulated path's autonomy is validated against
an oracle on held-out data, and the live path's is not validated at all yet.
"""
