"""The evidence store — M1's replacement for the in-memory ``EvidenceCache``.

``arie.policy.base.EvidenceCache`` is an in-memory stand-in that exists so the
M0 benchmark can run offline: within one benchmark pass nothing expires, and a
hit just means "already fetched for an earlier lead in this run."

This module is what it stands in for. The ``evidence`` table
(``migrations/0001_init.sql``) already models the real thing: one row per
(entity, field, source), with ``expires_at`` generated from ``fetched_at +
ttl_seconds``. There is no separate cache subsystem to keep in sync — a cache
hit is exactly a row whose ``expires_at`` is still in the future.

Note this is a field-level cache, not a provider-level one like
``EvidenceCache``. That is a deliberate widening, not an accident: two
providers can supply the same field, and a second contact at an
already-enriched company should read every cached fact back for free regardless
of which provider originally supplied it — that cross-provider, cross-contact
sharing is where the real M1 cost saving lives. Wiring this store into the
policy execution path (replacing ``RunContext.cache``) is deliberately left to
the worker step — that wiring decides *when* to consult the store and *which*
provider to call next, which is policy logic, not persistence.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from arie.core.types import EntityType, Evidence

_SELECT_FRESH = """
    SELECT entity_type, entity_id, field_name, value, signal_description,
           source, confidence, effect_on_score, ttl_seconds, fetched_at
    FROM evidence
    WHERE organization_id = %(organization_id)s
      AND entity_type = %(entity_type)s AND entity_id = %(entity_id)s
      AND field_name = %(field_name)s AND expires_at > %(now)s
    ORDER BY fetched_at DESC
    LIMIT 1
"""

# DISTINCT ON (field_name, source) is the fix for a real bug: `evidence` has
# no uniqueness constraint on (entity_type, entity_id, field_name, source) —
# see put_many's own docstring for why one isn't added — so a provider whose
# declared fields have different TTLs can end up with two simultaneously-
# fresh rows for the same field from the same source. Concretely: a provider
# returning both a 90-day-TTL field and a 30-day-TTL field gets re-called
# once the short-TTL field expires, and re-writes *all* its fields,
# including the long-TTL one that was still fresh — the entity now has two
# fresh rows for that field, both from the same source. Without this, a
# reader gets both and mistakes one source disagreeing with its own earlier
# reading for two independent (and therefore contested) observations, which
# is exactly the inflated-conflict, depressed-confidence failure mode
# arie.scoring.merge exists to measure honestly. DISTINCT ON keeps only the
# newest row per (field_name, source) — Postgres requires its columns to be
# a prefix of ORDER BY, which is also what makes "newest" well-defined here.
# Every row older than that stays in the table untouched; this changes what
# a *read* considers current, not what's stored.
_SELECT_ALL_FRESH = """
    SELECT DISTINCT ON (field_name, source)
           entity_type, entity_id, field_name, value, signal_description,
           source, confidence, effect_on_score, ttl_seconds, fetched_at
    FROM evidence
    WHERE organization_id = %(organization_id)s
      AND entity_type = %(entity_type)s AND entity_id = %(entity_id)s
      AND expires_at > %(now)s
    ORDER BY field_name, source, fetched_at DESC
"""

_INSERT = """
    INSERT INTO evidence (
        organization_id, entity_type, entity_id, field_name, value, signal_description,
        source, confidence, effect_on_score, ttl_seconds, fetched_at
    ) VALUES (
        %(organization_id)s, %(entity_type)s, %(entity_id)s, %(field_name)s, %(value)s,
        %(signal_description)s, %(source)s, %(confidence)s,
        %(effect_on_score)s, %(ttl_seconds)s, %(fetched_at)s
    )
"""


def _evidence_from_row(row: dict[str, Any]) -> Evidence:
    """Map one ``evidence`` row to the pure domain type.

    Postgres NUMERIC columns arrive as ``Decimal`` via psycopg's default type
    map; ``Evidence`` declares ``confidence``/``effect_on_score`` as ``float``,
    so both are cast explicitly rather than leaking a DB-specific type into
    code shared with the (DB-free) benchmark.
    """
    effect_on_score = row["effect_on_score"]
    return Evidence(
        entity_type=cast(EntityType, row["entity_type"]),
        entity_id=row["entity_id"],
        field_name=row["field_name"],
        value=row["value"],
        source=row["source"],
        confidence=float(row["confidence"]),
        ttl_seconds=row["ttl_seconds"],
        fetched_at=row["fetched_at"],
        signal_description=row["signal_description"],
        effect_on_score=float(effect_on_score) if effect_on_score is not None else None,
    )


def _row_for_insert(evidence: Evidence, organization_id: UUID) -> dict[str, Any]:
    return {
        "organization_id": organization_id,
        "entity_type": evidence.entity_type,
        "entity_id": evidence.entity_id,
        "field_name": evidence.field_name,
        "value": Jsonb(evidence.value),
        "signal_description": evidence.signal_description,
        "source": evidence.source,
        "confidence": evidence.confidence,
        "effect_on_score": evidence.effect_on_score,
        "ttl_seconds": evidence.ttl_seconds,
        "fetched_at": evidence.fetched_at,
    }


class PostgresEvidenceStore:
    """TTL/cache semantics over the ``evidence`` table.

    Answers exactly one question — "is there a still-fresh fact for this entity
    and field?" — and records new facts when the answer is no. It does not
    decide *whether* to fetch (the policy's job) or *which* value wins when
    sources disagree (``arie.scoring.merge``); keeping those separate is what
    let the M0 merge logic stay pure and DB-free and still apply unchanged here.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @classmethod
    def connect(
        cls, conninfo: str, *, min_size: int = 1, max_size: int = 10
    ) -> PostgresEvidenceStore:
        """Open a pooled store. Use the *pooled* connection string (``DATABASE_URL``),
        not the direct one — this is the runtime read/write path, not migrations."""
        pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size, open=True)
        return cls(pool)

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> PostgresEvidenceStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_fresh(
        self,
        entity_type: EntityType,
        entity_id: UUID,
        field_name: str,
        *,
        organization_id: UUID,
        now: datetime | None = None,
    ) -> Evidence | None:
        """The single-field cache lookup: freshest non-expired row, or ``None``.

        `organization_id` is required — Productization M1 made `evidence`
        tenant-owned even for `company`-entity rows (see
        `migrations/0012_organizations_and_members.sql`'s tenancy-boundary
        note), so a lookup with no organization scope would silently read
        across tenants for a shared `company_id`.
        """
        now = now or datetime.now(UTC)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _SELECT_FRESH,
                {
                    "organization_id": organization_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "field_name": field_name,
                    "now": now,
                },
            )
            row = cur.fetchone()
            return _evidence_from_row(row) if row is not None else None

    def get_all_fresh(
        self,
        entity_type: EntityType,
        entity_id: UUID,
        *,
        organization_id: UUID,
        now: datetime | None = None,
    ) -> tuple[Evidence, ...]:
        """Every still-fresh fact known about one entity, across all sources and fields.

        This is the shape a policy actually wants before deciding what to buy:
        the full known-facts bundle for one company or person, ready to feed
        into ``arie.scoring.merge.candidates_from_evidence``.

        `organization_id` scopes the read even when `entity_type == "company"`
        — see `get_fresh`'s docstring.
        """
        now = now or datetime.now(UTC)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _SELECT_ALL_FRESH,
                {
                    "organization_id": organization_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "now": now,
                },
            )
            return tuple(_evidence_from_row(row) for row in cur.fetchall())

    def put(self, evidence: Evidence, *, organization_id: UUID) -> None:
        self.put_many((evidence,), organization_id=organization_id)

    def put_many(self, items: Iterable[Evidence], *, organization_id: UUID) -> None:
        """Persist a batch of facts, e.g. every field one provider call returned.

        A single ``executemany`` in one transaction: a provider call either
        lands as evidence in full or not at all, never partially cached.

        `organization_id` is required and applies to every row in the batch —
        callers never mix entities from two organizations in one call. It is
        a plain parameter, not a field on `Evidence` itself: `Evidence`
        (``arie.core.types``) is shared with the DB-free M0 benchmark and
        ``arie.scoring``/``arie.policy``, none of which has any notion of a
        tenant, so tenancy stays a persistence-layer concern rather than
        leaking into the frozen domain type.

        Deliberately a plain append, not an upsert: there is no uniqueness
        constraint on ``(entity_type, entity_id, field_name, source)``, so a
        provider re-called after a *different* field's TTL expired writes a
        fresh row for every field it returns, including ones still fresh from
        an earlier call — the table can and does hold more than one
        simultaneously-fresh row per field/source. That is intentional
        history, not a bug to prevent here: it is what "evidence IS the
        cache" (this module's own docstring) means to preserve for audit —
        deleting or upserting over an older-but-still-fresh row would erase a
        real observation the system actually made. ``_SELECT_ALL_FRESH``'s
        ``DISTINCT ON`` is where "current" gets decided instead, at read
        time, once, rather than every caller needing to remember to dedup.
        """
        rows = [_row_for_insert(item, organization_id) for item in items]
        if not rows:
            return
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(_INSERT, rows)
            conn.commit()
