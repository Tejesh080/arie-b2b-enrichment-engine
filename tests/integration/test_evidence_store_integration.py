"""PostgresEvidenceStore against a real Postgres database.

Requires DATABASE_URL / DATABASE_DIRECT_URL; skipped otherwise (see
conftest.py). Mirrors the semantics tests/unit/test_evidence_decay.py already
pins for `Evidence.effective_confidence` — this file is about the *storage*
layer keeping those semantics intact through a round trip, not re-testing decay
arithmetic itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from arie.core.types import Evidence
from arie.evidence.store import PostgresEvidenceStore

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def _evidence(entity_id: UUID, **overrides: object) -> Evidence:
    defaults: dict[str, object] = {
        "entity_type": "company",
        "entity_id": entity_id,
        "field_name": "employee_count",
        "value": 250,
        "source": "firmographics_basic",
        "confidence": 0.9,
        "ttl_seconds": 3600,
        "fetched_at": NOW,
    }
    defaults.update(overrides)
    return Evidence(**defaults)  # type: ignore[arg-type]


def test_fresh_evidence_round_trips(
    evidence_store: PostgresEvidenceStore, cleanup_evidence: list[UUID]
) -> None:
    entity_id = uuid4()
    cleanup_evidence.append(entity_id)

    evidence_store.put(_evidence(entity_id))
    result = evidence_store.get_fresh(
        "company", entity_id, "employee_count", now=NOW + timedelta(minutes=1)
    )

    assert result is not None
    assert result.value == 250
    assert result.source == "firmographics_basic"
    assert result.confidence == 0.9


def test_expired_evidence_is_not_a_hit(
    evidence_store: PostgresEvidenceStore, cleanup_evidence: list[UUID]
) -> None:
    entity_id = uuid4()
    cleanup_evidence.append(entity_id)

    evidence_store.put(_evidence(entity_id, ttl_seconds=60))
    result = evidence_store.get_fresh(
        "company", entity_id, "employee_count", now=NOW + timedelta(minutes=5)
    )

    assert result is None


def test_get_fresh_ignores_a_different_entity(
    evidence_store: PostgresEvidenceStore, cleanup_evidence: list[UUID]
) -> None:
    entity_id = uuid4()
    other_entity_id = uuid4()
    cleanup_evidence.extend([entity_id, other_entity_id])

    evidence_store.put(_evidence(entity_id))
    result = evidence_store.get_fresh(
        "company", other_entity_id, "employee_count", now=NOW + timedelta(minutes=1)
    )

    assert result is None


def test_get_fresh_returns_the_most_recently_fetched_row(
    evidence_store: PostgresEvidenceStore, cleanup_evidence: list[UUID]
) -> None:
    entity_id = uuid4()
    cleanup_evidence.append(entity_id)

    evidence_store.put(_evidence(entity_id, value=100, fetched_at=NOW))
    evidence_store.put(_evidence(entity_id, value=300, fetched_at=NOW + timedelta(minutes=10)))

    result = evidence_store.get_fresh(
        "company", entity_id, "employee_count", now=NOW + timedelta(minutes=20)
    )

    assert result is not None
    assert result.value == 300


def test_get_all_fresh_returns_every_field_and_excludes_expired(
    evidence_store: PostgresEvidenceStore, cleanup_evidence: list[UUID]
) -> None:
    entity_id = uuid4()
    cleanup_evidence.append(entity_id)

    evidence_store.put_many(
        [
            _evidence(entity_id, field_name="employee_count", value=250, ttl_seconds=3600),
            _evidence(entity_id, field_name="industry", value="fintech", ttl_seconds=60),
        ]
    )

    fresh = evidence_store.get_all_fresh("company", entity_id, now=NOW + timedelta(minutes=5))

    field_names = {e.field_name for e in fresh}
    assert field_names == {"employee_count"}  # "industry" (ttl=60s) has expired by +5min


def test_get_all_fresh_excludes_other_entities(
    evidence_store: PostgresEvidenceStore, cleanup_evidence: list[UUID]
) -> None:
    entity_id = uuid4()
    other_entity_id = uuid4()
    cleanup_evidence.extend([entity_id, other_entity_id])

    evidence_store.put(_evidence(entity_id))
    evidence_store.put(_evidence(other_entity_id, field_name="industry", value="fintech"))

    fresh = evidence_store.get_all_fresh("company", entity_id, now=NOW + timedelta(minutes=1))

    assert {e.entity_id for e in fresh} == {entity_id}


def test_get_all_fresh_returns_one_row_per_field_and_source_after_a_mixed_ttl_refresh(
    evidence_store: PostgresEvidenceStore, cleanup_evidence: list[UUID]
) -> None:
    """Regression for the M1 audit's evidence-dedup finding: a provider
    declaring both a 90-day-TTL field (industry) and a 30-day-TTL field
    (employee_count) gets re-called once the short-TTL field expires, and
    re-writes *all* its fields -- including the long-TTL one that was still
    fresh. Before the DISTINCT ON fix, the entity ends up with two
    simultaneously-fresh 'industry' rows from the same source, and a reader
    (arie.scoring.merge.candidates_from_evidence, or any future direct
    consumer of get_all_fresh -- not just today's cache, which happens to
    dedup defensively on its own) sees them as two independent observations:
    one source disagreeing with its own earlier reading, not a real conflict.
    """
    entity_id = uuid4()
    cleanup_evidence.append(entity_id)
    day_zero = NOW - timedelta(days=31)
    ninety_days = 90 * 86400
    thirty_days = 30 * 86400

    # Day 0: the provider answers both fields.
    evidence_store.put_many(
        [
            _evidence(
                entity_id,
                field_name="industry",
                value="fintech",
                ttl_seconds=ninety_days,
                fetched_at=day_zero,
            ),
            _evidence(
                entity_id,
                field_name="employee_count",
                value=250,
                ttl_seconds=thirty_days,
                fetched_at=day_zero,
            ),
        ]
    )

    # Day 31: employee_count's TTL expired; the provider is re-called and
    # answers both fields again. industry's day-0 row is still fresh (90-day
    # TTL) when this lands, so the entity now holds two fresh industry rows
    # from the same source.
    evidence_store.put_many(
        [
            _evidence(
                entity_id,
                field_name="industry",
                value="fintech",
                ttl_seconds=ninety_days,
                fetched_at=NOW,
            ),
            _evidence(
                entity_id,
                field_name="employee_count",
                value=260,
                ttl_seconds=thirty_days,
                fetched_at=NOW,
            ),
        ]
    )

    fresh = evidence_store.get_all_fresh("company", entity_id, now=NOW + timedelta(minutes=1))

    by_field_and_source = [(e.field_name, e.source) for e in fresh]
    assert len(by_field_and_source) == len(set(by_field_and_source)), (
        f"more than one fresh row for the same field+source: {by_field_and_source}"
    )

    industry_rows = [e for e in fresh if e.field_name == "industry"]
    assert len(industry_rows) == 1
    assert industry_rows[0].fetched_at == NOW, "the newest row must win, not an arbitrary one"


def test_put_many_persists_every_item(
    evidence_store: PostgresEvidenceStore, cleanup_evidence: list[UUID]
) -> None:
    entity_id = uuid4()
    cleanup_evidence.append(entity_id)

    evidence_store.put_many(
        [
            _evidence(entity_id, field_name="employee_count", value=250),
            _evidence(entity_id, field_name="industry", value="fintech"),
            _evidence(entity_id, field_name="title_seniority", value="vp", entity_type="person"),
        ]
    )

    fresh = evidence_store.get_all_fresh("company", entity_id, now=NOW + timedelta(minutes=1))
    assert {e.field_name for e in fresh} == {"employee_count", "industry"}
