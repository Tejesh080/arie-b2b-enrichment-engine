"""Productization M5 corrective fix (Issue 2) — `arie.live.budget.
LiveSpendGuard`'s daily cap and `arie.live.cooldown.ProviderCooldownGuard`'s
quota cooldown are now organization-scoped. Before this fix both queries
were keyed process-wide: one organization's spend or quota exhaustion could
silently suppress a *different*, independently BYOK-credentialed
organization's use of the very same provider name. This file proves the
fix directly against a real database — the unit suite
(`tests/unit/test_live_budget.py`) drives the arithmetic through a fake pool
and cannot exercise the `organization_id` filter's SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from tests.integration.test_provider_configs_integration import _insert_org

from arie.config import LiveBudgetConfig, LiveStrategyConfig
from arie.live.budget import LiveSpendGuard
from arie.live.cooldown import ProviderCooldownGuard

pytestmark = pytest.mark.integration

_HUNTER = "hunter_combined_enrichment"


def _org(db_conn: psycopg.Connection) -> uuid.UUID:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    return org_id


def _insert_call(
    db_conn: psycopg.Connection,
    *,
    organization_id: uuid.UUID,
    provider: str,
    cost_usd: float,
    error_kind: str | None = None,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO provider_calls (organization_id, provider, entity_type, entity_id, "
            "idempotency_key, completed_at, cost_usd, status, cache_hit, error_kind) "
            "VALUES (%s, %s, 'person', gen_random_uuid(), %s, now(), %s, %s, false, %s)",
            (
                organization_id,
                provider,
                str(uuid.uuid4()),
                cost_usd,
                "error" if error_kind else "success",
                error_kind,
            ),
        )
    db_conn.commit()


@pytest.fixture
def pool(migrated_database: str) -> Iterator[ConnectionPool]:
    with ConnectionPool(migrated_database, min_size=1, max_size=4, open=True) as pool:
        yield pool


# ------------------------------------------------------------------- LiveSpendGuard --


def test_org_as_daily_spend_does_not_consume_org_bs_daily_allowance(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    org_a, org_b = _org(db_conn), _org(db_conn)
    guard = LiveSpendGuard(pool, LiveBudgetConfig(daily_usd=1.00, per_lead_usd=1.00))

    # Org A spends right up to (a shared-value) $1.00/day cap.
    _insert_call(db_conn, organization_id=org_a, provider=_HUNTER, cost_usd=0.999)

    allowance_a = guard.allowance(
        organization_id=org_a, lead_id=uuid.uuid4(), estimated_cost_usd=0.01
    )
    allowance_b = guard.allowance(
        organization_id=org_b, lead_id=uuid.uuid4(), estimated_cost_usd=0.01
    )

    assert allowance_a.permitted is False  # org A is at its own daily cap
    assert allowance_b.permitted is True  # org B's own daily allowance is untouched
    assert allowance_b.daily_spent_usd == 0


def test_daily_spent_usd_is_scoped_to_one_organization(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    org_a, org_b = _org(db_conn), _org(db_conn)
    guard = LiveSpendGuard(pool, LiveBudgetConfig())
    _insert_call(db_conn, organization_id=org_a, provider=_HUNTER, cost_usd=0.5)
    _insert_call(db_conn, organization_id=org_b, provider=_HUNTER, cost_usd=0.3)

    assert float(guard.daily_spent_usd(org_a)) == pytest.approx(0.5)
    assert float(guard.daily_spent_usd(org_b)) == pytest.approx(0.3)


# --------------------------------------------------------------- ProviderCooldownGuard --


def test_org_as_quota_exhaustion_does_not_cool_down_hunter_for_org_b(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    org_a, org_b = _org(db_conn), _org(db_conn)
    guard = ProviderCooldownGuard(pool, LiveStrategyConfig(quota_cooldown_seconds=3600))

    # Org A's own Hunter account just reported a quota error.
    _insert_call(
        db_conn, organization_id=org_a, provider=_HUNTER, cost_usd=0.0, error_kind="quota_exhausted"
    )

    cooling_a = guard.cooling_down_until(_HUNTER, organization_id=org_a)
    cooling_b = guard.cooling_down_until(_HUNTER, organization_id=org_b)

    assert cooling_a is not None  # org A's own Hunter is cooling down
    assert cooling_b is None  # org B's independently credentialed Hunter is untouched


def test_cooldown_check_is_also_scoped_by_provider_within_one_organization(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """Not a new invariant (the pre-M5 query already filtered by provider
    name) — pinned here alongside the new organization scoping so both axes
    are proven in the same place."""
    org_id = _org(db_conn)
    guard = ProviderCooldownGuard(pool, LiveStrategyConfig(quota_cooldown_seconds=3600))
    _insert_call(
        db_conn,
        organization_id=org_id,
        provider=_HUNTER,
        cost_usd=0.0,
        error_kind="quota_exhausted",
    )

    assert guard.cooling_down_until(_HUNTER, organization_id=org_id) is not None
    assert guard.cooling_down_until("apollo_person_enrichment", organization_id=org_id) is None
