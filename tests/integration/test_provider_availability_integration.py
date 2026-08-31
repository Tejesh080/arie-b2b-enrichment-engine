"""Productization M5 Part 1/4 — `arie.live.provider_availability.
resolve_organization_providers`: the org-aware provider factory that
replaces `arie.jobs.handlers`' old "build every adapter once from the
process-wide system credential" default.

Reuses `test_provider_configs_integration.py`'s local Vault stub
(`vault_stub`/`app_state_with_vault`) and org-insert helper — this module's
whole job is building on top of `arie.provider_configs`/
`arie.credential_resolver`, so it needs the exact same local substitute for
Supabase's real `vault` schema.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from tests.integration.test_provider_configs_integration import (
    _insert_org,
)
from tests.integration.test_provider_configs_integration import (
    app_state_with_vault as app_state_with_vault,
)
from tests.integration.test_provider_configs_integration import (
    vault_stub as vault_stub,
)

from arie.api.main import AppState
from arie.live.provider_availability import (
    CREDENTIAL_UNAVAILABLE,
    PROVIDER_DISABLED,
    PROVIDER_MODE_DISALLOWS_LIVE,
    PROVIDER_NOT_CONFIGURED,
    UNAVAILABILITY_REASONS,
    resolve_organization_providers,
)
from arie.organizations import LIVE_HUMAN_ONLY, LIVE_SHADOW, SIMULATED
from arie.provider_configs import set_provider_credential, set_provider_enabled
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.providers.live_apollo import APOLLO_PROVIDER_NAME

pytestmark = pytest.mark.integration

_ALL_THREE = (ABSTRACT_PROVIDER_NAME, HUNTER_PROVIDER_NAME, APOLLO_PROVIDER_NAME)


def _org(db_conn: psycopg.Connection) -> uuid.UUID:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    return org_id


# --------------------------------------------------------------- execution mode --


def test_simulated_execution_mode_makes_every_provider_unavailable_without_a_query(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    """The org-level killswitch: even a fully configured+enabled+credentialed
    provider is unavailable when the organization hasn't opted into live
    processing — checked before this touches `organization_provider_
    configs` at all."""
    org_id = _org(db_conn)
    actor = uuid.uuid4()
    set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential="sk-real-and-valid",
        actor_user_id=actor,
    )

    adapters, unavailable = resolve_organization_providers(
        db_conn, organization_id=org_id, execution_mode=SIMULATED
    )

    assert adapters == ()
    assert unavailable == {name: PROVIDER_MODE_DISALLOWS_LIVE for name in _ALL_THREE}


@pytest.mark.parametrize("execution_mode", [LIVE_SHADOW, LIVE_HUMAN_ONLY])
def test_live_execution_modes_with_nothing_configured_report_not_configured(
    app_state_with_vault: AppState, db_conn: psycopg.Connection, execution_mode: str
) -> None:
    org_id = _org(db_conn)

    adapters, unavailable = resolve_organization_providers(
        db_conn, organization_id=org_id, execution_mode=execution_mode
    )

    assert adapters == ()
    assert unavailable == {name: PROVIDER_NOT_CONFIGURED for name in _ALL_THREE}


# ----------------------------------------------------------- structured reasons --


def test_a_disabled_provider_is_reported_disabled_not_not_configured(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = _org(db_conn)
    actor = uuid.uuid4()
    set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential="sk-abc",
        actor_user_id=actor,
    )
    set_provider_enabled(
        db_conn,
        organization_id=org_id,
        provider=ABSTRACT_PROVIDER_NAME,
        enabled=False,
        actor_user_id=actor,
    )

    adapters, unavailable = resolve_organization_providers(
        db_conn, organization_id=org_id, execution_mode=LIVE_SHADOW
    )

    assert adapters == ()
    assert unavailable[ABSTRACT_PROVIDER_NAME] == PROVIDER_DISABLED


def test_a_vault_secret_gone_out_from_under_a_config_row_is_credential_unavailable(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    """Configured + enabled, but the underlying Vault secret no longer
    resolves (deleted independently of `delete_provider_config` — the
    scenario `arie.credential_resolver.resolve_provider_credential` already
    handles by returning `None`; this proves the factory reports it as
    `credential_unavailable` rather than crashing or treating it as
    `provider_not_configured`."""
    org_id = _org(db_conn)
    actor = uuid.uuid4()
    record = set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential="sk-abc",
        actor_user_id=actor,
    )
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM vault.secrets WHERE id = %s", (record.vault_secret_id,))
    db_conn.commit()

    adapters, unavailable = resolve_organization_providers(
        db_conn, organization_id=org_id, execution_mode=LIVE_HUMAN_ONLY
    )

    assert adapters == ()
    assert unavailable[ABSTRACT_PROVIDER_NAME] == CREDENTIAL_UNAVAILABLE


def test_every_reported_reason_is_in_the_documented_vocabulary(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = _org(db_conn)
    _, unavailable = resolve_organization_providers(
        db_conn, organization_id=org_id, execution_mode=LIVE_SHADOW
    )
    assert unavailable  # every provider unconfigured -> non-empty
    assert set(unavailable.values()) <= UNAVAILABILITY_REASONS


# ------------------------------------------------------------ adapters, and tenancy --


def test_a_fully_configured_provider_yields_a_real_adapter_with_that_orgs_own_credential(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = _org(db_conn)
    set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential="sk-org-own-key",
        actor_user_id=uuid.uuid4(),
    )

    adapters, unavailable = resolve_organization_providers(
        db_conn, organization_id=org_id, execution_mode=LIVE_HUMAN_ONLY
    )

    assert len(adapters) == 1
    (adapter,) = adapters
    assert adapter.name == ABSTRACT_PROVIDER_NAME
    assert adapter.config.api_key == "sk-org-own-key"  # type: ignore[attr-defined]
    assert unavailable[HUNTER_PROVIDER_NAME] == PROVIDER_NOT_CONFIGURED
    assert unavailable[APOLLO_PROVIDER_NAME] == PROVIDER_NOT_CONFIGURED
    adapter.close()  # type: ignore[attr-defined]


def test_two_organizations_configuring_the_same_provider_never_cross_credentials(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    """The critical tenant-isolation guarantee, at the adapter-construction
    layer (test_provider_configs_integration.py already proves it at the
    raw-credential layer) — Organization A's *adapter instance* must carry
    only Organization A's credential, never Organization B's, however the
    two calls interleave."""
    org_a, org_b = _org(db_conn), _org(db_conn)
    set_provider_credential(
        db_conn,
        organization_id=org_a,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential="ORG-A-SECRET",
        actor_user_id=uuid.uuid4(),
    )
    set_provider_credential(
        db_conn,
        organization_id=org_b,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential="ORG-B-SECRET",
        actor_user_id=uuid.uuid4(),
    )

    (adapter_a,), _ = resolve_organization_providers(
        db_conn, organization_id=org_a, execution_mode=LIVE_SHADOW
    )
    (adapter_b,), _ = resolve_organization_providers(
        db_conn, organization_id=org_b, execution_mode=LIVE_SHADOW
    )

    assert adapter_a.config.api_key == "ORG-A-SECRET"  # type: ignore[attr-defined]
    assert adapter_b.config.api_key == "ORG-B-SECRET"  # type: ignore[attr-defined]
    assert adapter_a is not adapter_b
    assert adapter_a.client is not adapter_b.client  # type: ignore[attr-defined]
    adapter_a.close()  # type: ignore[attr-defined]
    adapter_b.close()  # type: ignore[attr-defined]


def test_resolving_twice_for_the_same_organization_builds_two_independent_adapters(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    """Per-job construction, not a cached singleton — see the module
    docstring's rationale (a cached adapter would be a stale credential the
    moment it's rotated)."""
    org_id = _org(db_conn)
    set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=ABSTRACT_PROVIDER_NAME,
        raw_credential="sk-abc",
        actor_user_id=uuid.uuid4(),
    )

    (first,), _ = resolve_organization_providers(
        db_conn, organization_id=org_id, execution_mode=LIVE_SHADOW
    )
    (second,), _ = resolve_organization_providers(
        db_conn, organization_id=org_id, execution_mode=LIVE_SHADOW
    )

    assert first is not second
    assert first.client is not second.client  # type: ignore[attr-defined]
    first.close()  # type: ignore[attr-defined]
    second.close()  # type: ignore[attr-defined]
