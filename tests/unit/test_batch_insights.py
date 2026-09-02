"""Deterministic batch-insights math — M7 Slice 7, Part D/U. Pure only; the
DB-touching aggregation is covered live in
`tests/integration/test_batch_insights_and_export_integration.py`.
"""

from __future__ import annotations

from arie.batch_insights import MIN_BATCH_FEEDBACK_FOR_APPROVAL, compute_unknown_data_rate
from arie.intelligence.schemas import SCORING_DIMENSIONS


def test_scoring_dimension_count_is_six() -> None:
    """Pins the denominator basis Part D4 depends on — a change here is a
    scorer change, not something this test should silently absorb."""
    assert len(SCORING_DIMENSIONS) == 6


def test_zero_decided_leads_gives_none_not_zero() -> None:
    decided, unknown, expected, rate = compute_unknown_data_rate([])
    assert (decided, unknown, expected, rate) == (0, 0, 0, None)


def test_fully_known_batch_has_zero_rate() -> None:
    decided, unknown, expected, rate = compute_unknown_data_rate([[], [], []])
    assert decided == 3
    assert unknown == 0
    assert expected == 18  # 3 leads * 6 dimensions
    assert rate == 0.0


def test_fully_unknown_batch_has_rate_one() -> None:
    six_unknown = [
        "employee_count",
        "industry",
        "title_seniority",
        "title_function",
        "buying_intent",
        "recent_trigger_event",
    ]
    decided, unknown, expected, rate = compute_unknown_data_rate([six_unknown])
    assert decided == 1
    assert unknown == 6
    assert expected == 6
    assert rate == 1.0


def test_disqualifying_flag_is_excluded_from_the_count() -> None:
    """The gate is not a scored observation — including it would inflate
    both the numerator and (if it were also counted per lead) contradict the
    six-dimension denominator this module pins by test."""
    _decided, unknown, expected, rate = compute_unknown_data_rate([["disqualifying_flag"]])
    assert unknown == 0
    assert expected == 6
    assert rate == 0.0


def test_worked_example_from_the_slice_7_brief() -> None:
    """Success Example 2: 184 unknown observations out of 1200 expected ->
    15.3%. Modelled here as 200 decided leads (200*6=1200 expected)."""
    per_lead: list[list[str]] = [[] for _ in range(200)]
    remaining = 184
    for lead_fields in per_lead:
        if remaining <= 0:
            break
        lead_fields.append("employee_count")
        remaining -= 1
    decided, unknown, expected, rate = compute_unknown_data_rate(per_lead)
    assert decided == 200
    assert unknown == 184
    assert expected == 1200
    assert rate is not None
    assert round(rate, 3) == 0.153


def test_min_batch_feedback_for_approval_is_a_small_positive_constant() -> None:
    assert 0 < MIN_BATCH_FEEDBACK_FOR_APPROVAL < 10
