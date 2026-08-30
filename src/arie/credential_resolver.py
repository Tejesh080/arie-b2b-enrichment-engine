"""Per-organization provider credential resolution (Productization M4 Part
7) — the additive seam that lets a *future* live provider call ask "which
credential should Organization X's call to Provider Y use?" without ever
risking Organization A's credential reaching Organization B.

**This module is not wired into `arie.jobs.handlers` yet.** Today
`_default_live_providers` (`arie.jobs.handlers`) builds every live adapter
once, at process startup, from the process-wide env-var config singletons
(`LIVE_PROVIDER`, `HUNTER`, `APOLLO_PERSON` in `arie.config`) — one
credential for the whole process, used for every organization identically.
Actually routing a lead's provider calls through `resolve_provider_credential`
below is Productization M4's *next* increment, deliberately not done in this
milestone: `PROVIDER_MODE` stays `simulated` in production regardless (see
`arie.config.RuntimeConfig`), so no live call happens at all today, and
wiring this resolver into a worker that only ever runs simulated would be
change with no way to verify it end-to-end yet.

**Resolution has exactly one rule, with no silent fallback between
organizations:**

    organization has an enabled, configured provider
        -> that organization's own Vault credential
    otherwise
        -> `None` ("unavailable for this organization")

There is no "fall back to the global/system credential" branch here on
purpose. `LIVE_PROVIDER`/`HUNTER`/`APOLLO_PERSON`'s existing env-var
credentials remain exactly what they always were — the *system* credential
for internal/demo/simulated-adjacent operation, e.g. the one-command demo
script and n8n's own smoke-testing (see Productization M2C) — and this
resolver never reaches for them. A caller that wants "customer BYOK, else
the system credential" has to say so explicitly and separately; silently
blending the two here is exactly the failure mode Part 7 exists to make
structurally impossible: a bug that reads the wrong config would be caught
by `test_credential_resolver.py`'s tenant-isolation tests, not discovered in
production traffic.
"""

from __future__ import annotations

from uuid import UUID

import psycopg

from arie.provider_configs import get_provider_status
from arie.vault import resolve_secret

__all__ = ["resolve_provider_credential"]

_SELECT_VAULT_SECRET_ID = """
    SELECT vault_secret_id FROM organization_provider_configs
    WHERE organization_id = %(organization_id)s AND provider = %(provider)s AND enabled = true
"""


def resolve_provider_credential(
    conn: psycopg.Connection, *, organization_id: UUID, provider: str
) -> str | None:
    """The raw credential `organization_id` should use for `provider` right
    now, or `None` if that provider is unavailable for this organization
    (never configured, configured but disabled, or configured for a
    *different* organization — this query's own `organization_id` filter
    makes the last case unreachable, not merely checked).

    Deliberately re-queries `organization_provider_configs` directly rather
    than calling `arie.provider_configs.get_provider_status` first: that
    function's `ProviderStatus` never carries `vault_secret_id` (it is a
    public, API-response-safe shape by design), so resolving a credential
    needs its own query regardless. `get_provider_status` is still the
    right tool for validating `provider` is a real, supported name — reused
    here for exactly that, before this module's own query ever runs, so an
    unrecognised provider raises the same `InvalidProviderError` everywhere
    in this codebase rather than this function inventing a second way to
    report the same mistake.
    """
    get_provider_status(conn, organization_id=organization_id, provider=provider)

    with conn.cursor() as cur:
        cur.execute(
            _SELECT_VAULT_SECRET_ID, {"organization_id": organization_id, "provider": provider}
        )
        row = cur.fetchone()
    if row is None:
        return None
    return resolve_secret(conn, secret_id=row[0])
