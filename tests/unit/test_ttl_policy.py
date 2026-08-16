"""Field-level TTL policy.

Every scored field must have a deliberate TTL — an unlisted field silently
falling back to "cache forever" would be the kind of bug this project's
culture explicitly guards against (see docs/ASSUMPTIONS.md).
"""

from __future__ import annotations

from arie.evidence.ttl_policy import DEFAULT_TTL_SECONDS, FIELD_TTL_SECONDS, ttl_for_field
from arie.scoring.rules import SCORED_FIELDS


def test_every_scored_field_has_an_explicit_ttl() -> None:
    missing = [f for f in SCORED_FIELDS if f not in FIELD_TTL_SECONDS]
    assert not missing, f"scored fields with no explicit TTL: {missing}"


def test_ttl_for_field_returns_the_table_value() -> None:
    for field_name, expected in FIELD_TTL_SECONDS.items():
        assert ttl_for_field(field_name) == expected


def test_unknown_field_falls_back_to_the_conservative_default() -> None:
    assert ttl_for_field("some_field_nobody_registered") == DEFAULT_TTL_SECONDS


def test_default_is_not_longer_than_any_explicit_ttl() -> None:
    """The fallback must never be the longest-lived option.

    Otherwise an unrecognised field would be cached *more* trustingly than
    fields someone deliberately reasoned about — a silent-failure shape.
    """
    assert max(FIELD_TTL_SECONDS.values()) >= DEFAULT_TTL_SECONDS


def test_volatile_signals_expire_faster_than_firmographic_identity() -> None:
    """Anchors the ordering the policy depends on, not just the raw numbers."""
    assert FIELD_TTL_SECONDS["buying_intent"] < FIELD_TTL_SECONDS["employee_count"]
    assert FIELD_TTL_SECONDS["recent_trigger_event"] < FIELD_TTL_SECONDS["industry"]


def test_all_ttls_are_positive() -> None:
    for field_name, ttl in FIELD_TTL_SECONDS.items():
        assert ttl > 0, f"{field_name} has a non-positive TTL"
