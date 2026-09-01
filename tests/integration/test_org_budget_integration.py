"""Productization M5 Part 6 — `arie.live.org_budget.OrganizationSpendGuard`:
per-organization enforcement of `organizations.max_modeled_spend_usd_per_month`
(Productization M4 Part 9, previously report-only — see `arie.limits`).

**Provider names used here are deliberately fake** (`_PROVIDER =
"test_org_budget_provider"`, never a real registered name like
`abstract_company_enrichment`). `OrganizationSpendGuard` itself is provider-
name-agnostic (it sums `cost_usd` for the organization regardless of which
provider), but `arie.live.budget.LiveSpendGuard`'s *global* daily cap query
filters on `provider = ANY(arie.live.providers.LIVE_PROVIDER_NAMES)` — a
synthetic `provider_calls` row tagged with a real provider name would
inflate that shared, process-wide, date-scoped counter for every other test
(and, against a real deployment, every other organization) reading it that
same day. This bit a real end-to-end test once already (see the commit that
added this note) — keep it fake.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from arie.live.org_budget import ORGANIZATION_LIMIT_REACHED, OrganizationSpendGuard

pytestmark = pytest.mark.integration

_PROVIDER = "test_org_budget_provider"


def _insert_org(db_conn: psycopg.Connection, org_id: uuid.UUID, *, cap: float | None) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status, "
            "max_modeled_spend_usd_per_month) VALUES (%s, %s, %s, 'active', "
            "COALESCE(%s, 50.0))",
            (org_id, "Org Budget Test Org", f"ob-test-{org_id.hex[:10]}", cap),
        )
    db_conn.commit()


def _insert_call(
    db_conn: psycopg.Connection,
    *,
    organization_id: uuid.UUID,
    cost_usd: float,
    cache_hit: bool = False,
    provider: str = _PROVIDER,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO provider_calls (organization_id, provider, entity_type, entity_id, "
            "idempotency_key, completed_at, cost_usd, status, cache_hit) "
            "VALUES (%s, %s, 'company', gen_random_uuid(), %s, now(), %s, 'success', %s)",
            (organization_id, provider, str(uuid.uuid4()), cost_usd, cache_hit),
        )
    db_conn.commit()


@pytest.fixture
def pool(migrated_database: str) -> Iterator[ConnectionPool]:
    with ConnectionPool(migrated_database, min_size=1, max_size=4, open=True) as pool:
        yield pool


def test_a_fresh_organization_with_no_spend_is_permitted(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, cap=None)
    guard = OrganizationSpendGuard(pool)

    allowance = guard.allowance(organization_id=org_id, provider=_PROVIDER, estimated_cost_usd=0.01)

    assert allowance.permitted is True
    assert allowance.reason is None
    assert allowance.month_to_date_spent_usd == 0
    assert allowance.monthly_cap_usd == 50


def test_a_call_that_would_cross_the_cap_is_refused(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, cap=1.00)
    _insert_call(db_conn, organization_id=org_id, cost_usd=0.99)
    guard = OrganizationSpendGuard(pool)

    allowance = guard.allowance(organization_id=org_id, provider=_PROVIDER, estimated_cost_usd=0.02)

    assert allowance.permitted is False
    assert allowance.reason == ORGANIZATION_LIMIT_REACHED
    assert float(allowance.month_to_date_spent_usd) == pytest.approx(0.99)


def test_a_call_that_lands_exactly_on_the_cap_is_permitted(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """The boundary is inclusive — `spent + estimate <= cap`, not `<`."""
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, cap=1.00)
    _insert_call(db_conn, organization_id=org_id, cost_usd=0.98)
    guard = OrganizationSpendGuard(pool)

    allowance = guard.allowance(organization_id=org_id, provider=_PROVIDER, estimated_cost_usd=0.02)

    assert allowance.permitted is True


def test_cache_hit_rows_do_not_count_against_the_cap(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, cap=1.00)
    _insert_call(db_conn, organization_id=org_id, cost_usd=0.99, cache_hit=True)
    guard = OrganizationSpendGuard(pool)

    allowance = guard.allowance(organization_id=org_id, provider=_PROVIDER, estimated_cost_usd=0.99)

    assert allowance.permitted is True
    assert allowance.month_to_date_spent_usd == 0


def test_organizations_do_not_share_each_others_spend(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    _insert_org(db_conn, org_a, cap=1.00)
    _insert_org(db_conn, org_b, cap=1.00)
    _insert_call(db_conn, organization_id=org_a, cost_usd=0.99)
    guard = OrganizationSpendGuard(pool)

    allowance_a = guard.allowance(
        organization_id=org_a, provider=_PROVIDER, estimated_cost_usd=0.10
    )
    allowance_b = guard.allowance(
        organization_id=org_b, provider=_PROVIDER, estimated_cost_usd=0.10
    )

    assert allowance_a.permitted is False  # org A is nearly at its own cap
    assert allowance_b.permitted is True  # org B's cap is untouched by org A's spend


def test_spend_from_a_different_provider_still_counts_against_the_shared_org_cap(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """The cap is organization-wide, not per-provider — Part 6 asks for
    "organization remaining modeled allowance", a single ceiling covering
    every provider that organization calls."""
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, cap=1.00)
    _insert_call(
        db_conn, organization_id=org_id, cost_usd=0.99, provider="test_org_budget_provider_b"
    )
    guard = OrganizationSpendGuard(pool)

    allowance = guard.allowance(
        organization_id=org_id, provider="test_org_budget_provider_c", estimated_cost_usd=0.05
    )

    assert allowance.permitted is False


# ------------------------------------------------------------------- concurrency --


def test_the_advisory_lock_key_serializes_same_organization_same_provider(
    migrated_database: str,
) -> None:
    """Proves the lock primitive `OrganizationSpendGuard.allowance` takes
    (`pg_advisory_xact_lock(hashtext(organization_id || ':live_spend:' ||
    provider))`) actually serializes two holders of the *same* key, and does
    NOT block a different provider for the same organization — the exact
    scoping the module docstring claims. A true multi-worker race on the
    guard itself is not reproduced here (that would need real concurrent
    job processing); this isolates the lock primitive's own behavior, which
    is what makes that race bounded in the first place.
    """
    org_id = uuid.uuid4()
    lock_sql = (
        "SELECT pg_advisory_xact_lock(hashtext(%(organization_id)s::text || "
        "':live_spend:' || %(provider)s))"
    )
    try_lock_sql = (
        "SELECT pg_try_advisory_xact_lock(hashtext(%(organization_id)s::text || "
        "':live_spend:' || %(provider)s))"
    )

    holder = psycopg.connect(migrated_database)
    try:
        with holder.cursor() as cur:
            cur.execute(lock_sql, {"organization_id": org_id, "provider": _PROVIDER})
        # Still held (no commit/rollback yet) - a second session trying the
        # SAME (org, provider) key must not acquire it.
        with (
            psycopg.connect(migrated_database) as contender,
            contender.cursor() as cur,
        ):
            cur.execute(try_lock_sql, {"organization_id": org_id, "provider": _PROVIDER})
            same_key_row = cur.fetchone()
            assert same_key_row is not None
            assert same_key_row[0] is False

        # A different provider for the SAME organization is a different key
        # and must not be blocked.
        with (
            psycopg.connect(migrated_database) as contender,
            contender.cursor() as cur,
        ):
            cur.execute(
                try_lock_sql,
                {"organization_id": org_id, "provider": "hunter_combined_enrichment"},
            )
            different_provider_row = cur.fetchone()
            assert different_provider_row is not None
            assert different_provider_row[0] is True
    finally:
        holder.rollback()  # releases the advisory xact lock
        holder.close()


def test_realistic_sequential_processing_correctly_enforces_the_cap(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """The guarantee that actually matters for how `arie.jobs.handlers` calls
    this guard: check, THEN make the (comparatively slow) provider call,
    THEN record its cost — never a second check racing the first job's own
    write. One worker (or several, each fully finishing a call before its
    ledger row lands) never exceeds the cap; see
    `test_a_zero_delay_race_between_check_and_write_can_exceed_the_cap`
    below for the gap this does NOT close."""
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, cap=0.10)
    guard = OrganizationSpendGuard(pool)
    per_call_cost = 0.02  # exactly 5 calls fit in a $0.10 cap

    permitted_count = 0
    for _ in range(8):
        allowance = guard.allowance(
            organization_id=org_id, provider=_PROVIDER, estimated_cost_usd=per_call_cost
        )
        if not allowance.permitted:
            continue
        # Stand-in for the real provider HTTP call and its ledger write,
        # which in production land on a different connection only after the
        # call returns — the write is not folded into the check above.
        _insert_call(db_conn, organization_id=org_id, cost_usd=per_call_cost)
        permitted_count += 1

    assert permitted_count == 5


def test_a_zero_delay_race_between_check_and_write_can_exceed_the_cap(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """Pins the module docstring's documented, accepted gap rather than
    hiding it: `allowance()` releases its advisory lock before its caller
    has written anything, so concurrent checks with (near-)zero delay before
    their writes can all see the same pre-write headroom and all be
    permitted — this is the residual race the docstring's "Concurrency"
    section explains and bounds by real provider-call latency, which this
    test deliberately does not simulate. If a future change closes this gap
    (a reserve/settle protocol), this test's assumption breaks and should be
    updated alongside it — that is the point of pinning it explicitly rather
    than leaving the gap undocumented.
    """
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id, cap=0.10)
    guard = OrganizationSpendGuard(pool)
    per_call_cost = 0.02  # exactly 5 calls fit in a $0.10 cap

    def attempt(_: int) -> bool:
        allowance = guard.allowance(
            organization_id=org_id, provider=_PROVIDER, estimated_cost_usd=per_call_cost
        )
        if allowance.permitted:
            _insert_call(db_conn, organization_id=org_id, cost_usd=per_call_cost)
        return allowance.permitted

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))

    permitted_count = sum(results)
    # The documented gap, not a regression: more than the cap's 5 calls can
    # be permitted when nothing separates check from write (up to and
    # including all 8, in the worst case where every check races ahead of
    # every write) — never fewer than 5, since a write only ever follows a
    # genuinely permitted check, so the cap's own headroom is never
    # under-used by this race.
    assert 5 <= permitted_count <= 8
