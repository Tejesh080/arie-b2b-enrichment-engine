"""Row <-> Evidence mapping in the Postgres evidence store.

These are pure-function tests — no database. They exist because the mapping is
where a DB-specific type (``Decimal`` for NUMERIC, in particular) could leak
into ``arie.core.types.Evidence``, which is shared with the DB-free benchmark
and declares ``confidence``/``effect_on_score`` as plain ``float``. A silent
``Decimal`` leak would not fail loudly — arithmetic against a ``float`` mostly
still works — it would just make ``Evidence`` instances built from the database
subtly different from ones built in-memory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb

from arie.core.types import Evidence
from arie.evidence.store import _evidence_from_row, _row_for_insert

FETCHED_AT = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "entity_type": "company",
        "entity_id": uuid4(),
        "field_name": "employee_count",
        "value": 250,
        "signal_description": None,
        "source": "firmographics_basic",
        "confidence": Decimal("0.93"),
        "effect_on_score": None,
        "ttl_seconds": 2_592_000,
        "fetched_at": FETCHED_AT,
    }
    row.update(overrides)
    return row


def test_decimal_confidence_becomes_a_plain_float() -> None:
    evidence = _evidence_from_row(_row(confidence=Decimal("0.93")))
    assert evidence.confidence == 0.93
    assert isinstance(evidence.confidence, float)


def test_null_effect_on_score_stays_none() -> None:
    evidence = _evidence_from_row(_row(effect_on_score=None))
    assert evidence.effect_on_score is None


def test_decimal_effect_on_score_becomes_a_plain_float() -> None:
    evidence = _evidence_from_row(_row(effect_on_score=Decimal("-1.50")))
    assert evidence.effect_on_score == -1.50
    assert isinstance(evidence.effect_on_score, float)


def test_round_trips_every_other_field_unchanged() -> None:
    entity_id = uuid4()
    row = _row(entity_id=entity_id, field_name="industry", value="fintech", source="dns_web")
    evidence = _evidence_from_row(row)

    assert evidence.entity_type == "company"
    assert evidence.entity_id == entity_id
    assert evidence.field_name == "industry"
    assert evidence.value == "fintech"
    assert evidence.source == "dns_web"
    assert evidence.ttl_seconds == 2_592_000
    assert evidence.fetched_at == FETCHED_AT


def test_row_for_insert_wraps_value_for_jsonb() -> None:
    evidence = Evidence(
        entity_type="person",
        entity_id=uuid4(),
        field_name="title_seniority",
        value="vp",
        source="contact_enrich",
        confidence=0.9,
        ttl_seconds=5_184_000,
        fetched_at=FETCHED_AT,
    )
    params = _row_for_insert(evidence)
    assert isinstance(params["value"], Jsonb)
    assert params["value"].obj == "vp"
    assert params["entity_id"] == evidence.entity_id
    assert params["confidence"] == 0.9
    assert params["effect_on_score"] is None
