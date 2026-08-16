"""Evidence staleness decay.

This behaviour is small but load-bearing: it is what lets zero-cost cached
evidence compete *honestly* against a fresh paid call inside the EVoI
calculation. Treating cache hits as either fully trustworthy or worthless would
both be wrong, and both would distort the stopping decision.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from arie.core.types import Evidence

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def _evidence(fetched_at: datetime, ttl_seconds: int = 86_400, confidence: float = 0.9) -> Evidence:
    return Evidence(
        entity_type="company",
        entity_id=uuid4(),
        field_name="employee_count",
        value=250,
        source="test_provider",
        confidence=confidence,
        ttl_seconds=ttl_seconds,
        fetched_at=fetched_at,
    )


def test_fresh_evidence_retains_full_confidence() -> None:
    ev = _evidence(fetched_at=NOW)
    assert ev.effective_confidence(NOW) == pytest.approx(0.9)


def test_confidence_decays_linearly_with_age() -> None:
    ev = _evidence(fetched_at=NOW - timedelta(hours=12))  # half of a 24h TTL
    assert ev.effective_confidence(NOW) == pytest.approx(0.45)


def test_expired_evidence_has_zero_effective_confidence() -> None:
    ev = _evidence(fetched_at=NOW - timedelta(days=2))
    assert ev.is_expired(NOW)
    assert ev.effective_confidence(NOW) == 0.0


def test_decay_never_goes_negative() -> None:
    """A very stale fact is worthless, never actively misleading in the arithmetic."""
    ev = _evidence(fetched_at=NOW - timedelta(days=365))
    assert ev.effective_confidence(NOW) == 0.0


def test_longer_ttl_decays_more_slowly() -> None:
    """Field-specific TTLs are the mechanism: a domain outlives a funding signal."""
    age = timedelta(days=7)
    short = _evidence(fetched_at=NOW - age, ttl_seconds=14 * 86_400)  # news-like
    long = _evidence(fetched_at=NOW - age, ttl_seconds=365 * 86_400)  # identity-like
    assert long.effective_confidence(NOW) > short.effective_confidence(NOW)


def test_zero_ttl_is_treated_as_worthless_not_divide_by_zero() -> None:
    ev = _evidence(fetched_at=NOW, ttl_seconds=0)
    assert ev.effective_confidence(NOW) == 0.0
