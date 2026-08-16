"""`ExtractedSignal` is the entire interface between free text and the rest of
the system — see the module's docstring. These test the contract directly,
without going through HTTP, so a schema regression fails fast and locally.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arie.llm.schema import TRIGGER_CATEGORIES, ExtractedSignal


def _valid(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "has_buying_intent": True,
        "trigger_event_category": "funding_event",
        "trigger_event_detail": "raised a Series B",
        "disqualifying_signal": False,
        "confidence": 0.8,
        "rationale": "mentions closing a funding round",
    }
    payload.update(overrides)
    return payload


def test_a_well_formed_payload_validates() -> None:
    signal = ExtractedSignal(**_valid())  # type: ignore[arg-type]
    assert signal.has_buying_intent is True
    assert signal.trigger_event_category == "funding_event"


def test_trigger_event_category_and_detail_are_optional() -> None:
    signal = ExtractedSignal(
        **_valid(trigger_event_category=None, trigger_event_detail=None)  # type: ignore[arg-type]
    )
    assert signal.trigger_event_category is None
    assert signal.trigger_event_detail is None


def test_unrecognised_trigger_category_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedSignal(**_valid(trigger_event_category="alien_invasion"))  # type: ignore[arg-type]


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0, -1.0])
def test_confidence_outside_zero_to_one_is_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        ExtractedSignal(**_valid(confidence=confidence))  # type: ignore[arg-type]


def test_confidence_boundary_values_are_accepted() -> None:
    ExtractedSignal(**_valid(confidence=0.0))  # type: ignore[arg-type]
    ExtractedSignal(**_valid(confidence=1.0))  # type: ignore[arg-type]


def test_an_unexpected_field_is_rejected_not_silently_dropped() -> None:
    """This is what makes the schema a hard boundary rather than a
    best-effort one: a model that tries to smuggle an extra instruction
    through — an extra key, however named — fails validation instead of
    being quietly accepted with the key ignored."""
    with pytest.raises(ValidationError):
        ExtractedSignal(**_valid(recommended_action="auto_route"))  # type: ignore[arg-type]


def test_rationale_over_the_length_limit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedSignal(**_valid(rationale="x" * 281))  # type: ignore[arg-type]


def test_trigger_event_detail_over_the_length_limit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedSignal(**_valid(trigger_event_detail="x" * 201))  # type: ignore[arg-type]


def test_missing_required_field_is_rejected() -> None:
    payload = _valid()
    del payload["has_buying_intent"]
    with pytest.raises(ValidationError):
        ExtractedSignal(**payload)  # type: ignore[arg-type]


def test_the_schema_has_no_field_shaped_like_an_action() -> None:
    """A structural guarantee, checked by code rather than only asserted in
    prose: nothing in the schema could plausibly be read as "call this
    provider" or "set the lead to this status"."""
    forbidden_substrings = ("provider", "status", "route", "action", "tool", "call_")
    for field_name in ExtractedSignal.model_fields:
        lowered = field_name.lower()
        assert not any(bad in lowered for bad in forbidden_substrings), field_name


def test_trigger_categories_constant_matches_the_schemas_own_literal() -> None:
    """Pins the exact shape Pydantic emits for `TriggerCategory | None` today:
    inlined as an `anyOf` on the field, not lifted into `$defs` (that only
    happens for types Pydantic sees reused across more than one field)."""
    schema = ExtractedSignal.model_json_schema()
    literal_values = schema["properties"]["trigger_event_category"]["anyOf"][0]["enum"]
    assert set(literal_values) == set(TRIGGER_CATEGORIES)
