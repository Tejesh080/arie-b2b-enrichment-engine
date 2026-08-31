"""Organization-aware provider construction (Productization M5 Part 1) — the
seam that turns `arie.credential_resolver.resolve_provider_credential`'s raw
string into a real, working `EnrichmentProvider` adapter, scoped to exactly
one organization, and never anything else.

**Where this replaces.** Before this milestone, `arie.jobs.handlers`'
`_default_live_providers` built every live adapter once, at worker-process
startup, from the process-wide env-var singletons (`arie.config.LIVE_PROVIDER`
/`HUNTER`/`APOLLO_PERSON`) — one credential for the whole process, reused for
every organization's every lead. This module is what `arie.jobs.handlers`'
live handler calls instead, per job, after a lead's `organization_id` is
known — see `resolve_organization_providers`.

**No cross-tenant fallback, structurally.** Every adapter this module returns
is built from that one organization's own Vault-stored credential
(`arie.credential_resolver.resolve_provider_credential`), fetched fresh on
every call — never a shared, longer-lived object, never a different
organization's credential, and never the process-wide system credential.
Organization A's returned adapter and Organization B's are two entirely
separate `EnrichmentProvider` instances with two separate `httpx.Client`s;
nothing here retains state across calls that a second organization could
later observe. See `arie.jobs.handlers`' live handler for why per-job
construction (rather than a process-lifetime cache) is the deliberate
tradeoff: a cached adapter is a stale adapter the moment its organization
rotates or revokes the underlying credential, and this milestone's canary
scale does not need the throughput a cache would buy.

**A provider unavailable for this organization is reported, never
substituted.** `resolve_organization_providers` returns two things: the
adapters that could be built, and a `{provider_name: reason}` map for every
registered provider that could not — one of :data:`PROVIDER_NOT_CONFIGURED`,
:data:`PROVIDER_DISABLED`, :data:`CREDENTIAL_UNAVAILABLE`, or (when the whole
organization has not opted into live processing)
:data:`PROVIDER_MODE_DISALLOWS_LIVE`. Nothing here ever falls back to a
different provider, a different organization's credential, or the system
credential to paper over a gap — the caller decides what "fewer providers
than usual" means for acquisition (see `_acquire_live_evidence`, which
already treats a smaller `providers` tuple correctly: it just has less to
try), and the Decision Receipt is where the *reason* for the gap belongs
(Part 11), not a silent substitution here.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from uuid import UUID

import psycopg

from arie.config import APOLLO_PERSON, HUNTER, LIVE_PROVIDER
from arie.credential_resolver import resolve_provider_credential
from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES
from arie.organizations import SIMULATED
from arie.provider_configs import get_provider_status
from arie.providers.apollo_contract import APOLLO_PROVIDER_NAME
from arie.providers.base import EnrichmentProvider
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.providers.live_abstract import AbstractCompanyEnrichmentProvider
from arie.providers.live_apollo import ApolloPersonEnrichmentProvider
from arie.providers.live_hunter import HunterEnrichmentProvider

__all__ = [
    "CREDENTIAL_SOURCE_ORGANIZATION",
    "CREDENTIAL_UNAVAILABLE",
    "PROVIDER_DISABLED",
    "PROVIDER_MODE_DISALLOWS_LIVE",
    "PROVIDER_NOT_CONFIGURED",
    "UNAVAILABILITY_REASONS",
    "resolve_organization_providers",
]

PROVIDER_NOT_CONFIGURED = "provider_not_configured"
PROVIDER_DISABLED = "provider_disabled"
CREDENTIAL_UNAVAILABLE = "credential_unavailable"
PROVIDER_MODE_DISALLOWS_LIVE = "provider_mode_disallows_live"

UNAVAILABILITY_REASONS: frozenset[str] = frozenset(
    {
        PROVIDER_NOT_CONFIGURED,
        PROVIDER_DISABLED,
        CREDENTIAL_UNAVAILABLE,
        PROVIDER_MODE_DISALLOWS_LIVE,
    }
)
"""The structured vocabulary this module returns for a provider it could not
build — Part 4's list, minus the acquisition-time reasons
(`organization_limit_reached`, `lead_budget_reached`, provider-suppression)
that already have an established home in `arie.live.budget`/`org_budget`/
`outcome_cache`/`cooldown` and are reported through the acquisition outcome's
own `stop_reason`/`unavailable` fields instead of duplicated here."""

CREDENTIAL_SOURCE_ORGANIZATION = "organization"
"""The only `provider_calls.credential_source` value this module's adapters
ever produce — see `migrations/0028_provider_call_cost_and_credential_
provenance.sql`. The other documented value, `'system'`, is written only by
the explicit test/smoke-script injection path in `arie.jobs.handlers`, which
never calls this module."""


def _build_abstract(raw_credential: str) -> EnrichmentProvider:
    return AbstractCompanyEnrichmentProvider.build(
        config=dataclasses.replace(LIVE_PROVIDER, api_key=raw_credential)
    )


def _build_hunter(raw_credential: str) -> EnrichmentProvider:
    return HunterEnrichmentProvider.build(
        config=dataclasses.replace(HUNTER, api_key=raw_credential)
    )


def _build_apollo(raw_credential: str) -> EnrichmentProvider:
    return ApolloPersonEnrichmentProvider.build(
        config=dataclasses.replace(APOLLO_PERSON, api_key=raw_credential)
    )


_ADAPTER_BUILDERS: dict[str, Callable[[str], EnrichmentProvider]] = {
    ABSTRACT_PROVIDER_NAME: _build_abstract,
    HUNTER_PROVIDER_NAME: _build_hunter,
    APOLLO_PROVIDER_NAME: _build_apollo,
}
"""Every registered live provider's org-credentialed constructor — kept as an
explicit table, not a generic dispatch, so a new adapter must be added here
deliberately (mirrors `arie.live.providers.REGISTERED_LIVE_PROVIDER_NAMES`'s
own "no provider is reachable by accident" discipline)."""


def resolve_organization_providers(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    execution_mode: str,
    provider_names: Sequence[str] = REGISTERED_LIVE_PROVIDER_NAMES,
) -> tuple[tuple[EnrichmentProvider, ...], dict[str, str]]:
    """The adapters `organization_id` may use right now, and why every other
    registered provider is unavailable.

    Returns `(adapters, unavailable)` — `adapters` unsorted (the caller
    applies `arie.live.providers.acquisition_order`, same as every other
    provider set in this codebase) and built fresh from this call's own
    connection; `unavailable` maps every provider name in `provider_names`
    that isn't in `adapters` to one of :data:`UNAVAILABILITY_REASONS`.

    `execution_mode == arie.organizations.SIMULATED` short-circuits to "every
    provider unavailable, :data:`PROVIDER_MODE_DISALLOWS_LIVE`" without
    querying `organization_provider_configs` at all — an organization that
    has not opted into live processing gets zero real provider calls
    regardless of what BYOK credentials it has configured, which is the
    point of the setting.

    **Callers own the returned adapters' lifecycle** — each one owns a real
    `httpx.Client` and must be `.close()`-d after use (see
    `arie.jobs.handlers`' live `compute_score`, which does so in a
    `try/finally` around acquisition).
    """
    if execution_mode == SIMULATED:
        return (), {name: PROVIDER_MODE_DISALLOWS_LIVE for name in provider_names}

    adapters: list[EnrichmentProvider] = []
    unavailable: dict[str, str] = {}
    for name in provider_names:
        status = get_provider_status(conn, organization_id=organization_id, provider=name)
        if not status.configured:
            unavailable[name] = PROVIDER_NOT_CONFIGURED
            continue
        if not status.enabled:
            unavailable[name] = PROVIDER_DISABLED
            continue
        raw_credential = resolve_provider_credential(
            conn, organization_id=organization_id, provider=name
        )
        if raw_credential is None:
            unavailable[name] = CREDENTIAL_UNAVAILABLE
            continue
        build = _ADAPTER_BUILDERS.get(name)
        if build is None:  # pragma: no cover - defensive; every registered name has a builder
            unavailable[name] = PROVIDER_NOT_CONFIGURED
            continue
        adapters.append(build(raw_credential))

    return tuple(adapters), unavailable
