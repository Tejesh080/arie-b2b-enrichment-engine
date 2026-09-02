"""Deterministic patterns over recommendation feedback — M7 Slice 7, Part A/T.

No database, no LLM anywhere in this file: `analyze_feedback_patterns` is a
pure function of `FeedbackOutcomeRow`s, and `derive_changes`
(`arie.intelligence.proposals`, already exhaustively tested on its own) is
reused unmodified once `build_outcome_dataset` has translated feedback into
the shape it already knows how to group and score.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from arie.intelligence.feedback_learning import (
    MIN_FEEDBACK_FOR_PROPOSAL,
    MIN_FEEDBACK_FOR_SUMMARY,
    FeedbackOutcomeRow,
    FeedbackSupport,
    analyze_feedback_patterns,
    build_outcome_dataset,
    feedback_support,
)
from arie.intelligence.outcomes import OutcomeLabel
from arie.intelligence.proposals import ChangeKind, derive_changes
from arie.intelligence.schemas import (
    BandPreference,
    BusinessProfileDraft,
    EmployeeBand,
)

LEAD = UUID("11111111-1111-1111-1111-111111111111")


def _row(
    *,
    sentiment: str = "negative",
    reason: str | None = "company_too_small",
    priority: str = "review",
    profile_version: int | None = 4,
    employee_count: int | None = 5,
    industry: str | None = None,
    lead_id: UUID = LEAD,
) -> FeedbackOutcomeRow:
    return FeedbackOutcomeRow(
        lead_id=lead_id,
        company="Acme",
        sentiment=sentiment,
        reason=reason,
        priority=priority,
        profile_version=profile_version,
        employee_count=employee_count,
        industry=industry,
    )


def _draft() -> BusinessProfileDraft:
    return BusinessProfileDraft(
        offering_summary="Widgets",
        plain_english_summary="We sell widgets to mid-sized companies.",
        employee_band_preferences={
            EmployeeBand.MICRO: BandPreference.PREFERRED,
            EmployeeBand.SMALL: BandPreference.ACCEPTABLE,
            EmployeeBand.MID: BandPreference.PREFERRED,
        },
    )


# ------------------------------------------------------------------ support --


@pytest.mark.parametrize(
    "total,expected",
    [
        (0, FeedbackSupport.INSUFFICIENT_DATA),
        (MIN_FEEDBACK_FOR_SUMMARY - 1, FeedbackSupport.INSUFFICIENT_DATA),
        (MIN_FEEDBACK_FOR_SUMMARY, FeedbackSupport.SUMMARY_ONLY),
        (MIN_FEEDBACK_FOR_PROPOSAL - 1, FeedbackSupport.SUMMARY_ONLY),
        (MIN_FEEDBACK_FOR_PROPOSAL, FeedbackSupport.ELIGIBLE),
        (MIN_FEEDBACK_FOR_PROPOSAL + 50, FeedbackSupport.ELIGIBLE),
    ],
)
def test_feedback_support_boundaries(total: int, expected: FeedbackSupport) -> None:
    assert feedback_support(total) is expected


def test_thresholds_are_ordered_constants() -> None:
    assert 0 < MIN_FEEDBACK_FOR_SUMMARY < MIN_FEEDBACK_FOR_PROPOSAL


# --------------------------------------------------------------- basic stats --


def test_insufficient_feedback_produces_no_outcome_analysis() -> None:
    rows = [_row() for _ in range(MIN_FEEDBACK_FOR_SUMMARY - 1)]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.support is FeedbackSupport.INSUFFICIENT_DATA
    assert analysis.outcome_analysis is None


def test_summary_only_tier_has_no_outcome_analysis_either() -> None:
    rows = [_row() for _ in range(MIN_FEEDBACK_FOR_SUMMARY)]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.support is FeedbackSupport.SUMMARY_ONLY
    assert analysis.outcome_analysis is None
    # The summary-only tier still gets real counts — it just isn't eligible
    # for a proposal yet.
    assert analysis.total == MIN_FEEDBACK_FOR_SUMMARY


def test_eligible_tier_produces_an_outcome_analysis() -> None:
    rows = [
        _row(sentiment="negative" if i % 2 else "positive")
        for i in range(MIN_FEEDBACK_FOR_PROPOSAL)
    ]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.support is FeedbackSupport.ELIGIBLE
    assert analysis.outcome_analysis is not None


def test_sentiment_by_priority() -> None:
    rows = [
        _row(sentiment="positive", priority="contact_first"),
        _row(sentiment="positive", priority="contact_first"),
        _row(sentiment="negative", priority="review"),
    ]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.by_priority["contact_first"] == {"positive": 2, "negative": 0}
    assert analysis.by_priority["review"] == {"positive": 0, "negative": 1}


def test_sentiment_by_profile_version() -> None:
    rows = [
        _row(sentiment="positive", profile_version=3),
        _row(sentiment="negative", profile_version=4),
        _row(sentiment="negative", profile_version=4),
    ]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.by_profile_version["3"] == {"positive": 1, "negative": 0}
    assert analysis.by_profile_version["4"] == {"positive": 0, "negative": 2}


def test_unknown_profile_version_buckets_separately() -> None:
    rows = [_row(sentiment="negative", profile_version=None)]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.by_profile_version["unknown"] == {"positive": 0, "negative": 1}


def test_negative_reason_counts_ignore_positive_rows() -> None:
    rows = [
        _row(sentiment="positive", reason=None),
        _row(sentiment="negative", reason="wrong_industry"),
        _row(sentiment="negative", reason="wrong_industry"),
        _row(sentiment="negative", reason="not_decision_maker"),
    ]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.negative_reason_counts == {"wrong_industry": 2, "not_decision_maker": 1}


def test_agreement_rate_is_none_with_zero_feedback() -> None:
    assert analyze_feedback_patterns([]).agreement_rate is None


def test_agreement_rate_math() -> None:
    rows = [_row(sentiment="positive") for _ in range(3)] + [_row(sentiment="negative")]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.agreement_rate == pytest.approx(0.75)


# ----------------------------------------------------- outcome translation --


def test_build_outcome_dataset_maps_sentiment_to_won_lost() -> None:
    rows = [_row(sentiment="positive"), _row(sentiment="negative")]
    dataset = build_outcome_dataset(rows)
    labels = [r.label for r in dataset.rows]
    assert labels == [OutcomeLabel.WON, OutcomeLabel.LOST]


def test_build_outcome_dataset_flags_available_dimensions() -> None:
    rows = [_row(employee_count=5, industry=None), _row(employee_count=None, industry="software")]
    dataset = build_outcome_dataset(rows)
    assert dataset.has_employee_counts is True
    assert dataset.has_industries is True


def test_build_outcome_dataset_every_row_is_labelled() -> None:
    """Unlike a CSV upload, feedback always carries a sentiment — there is no
    `UNKNOWN`-labelled row to exclude."""
    rows = [_row(sentiment="positive"), _row(sentiment="negative")]
    dataset = build_outcome_dataset(rows)
    assert len(dataset.labelled) == len(dataset.rows) == 2


# ------------------------------------------------- dominant-reason -> change --


def test_dominant_company_too_small_produces_a_demotion_candidate() -> None:
    """Success Example 1's shape: mostly negative feedback on very small
    companies, which the profile currently prefers."""
    # `arie.intelligence.outcomes.MODERATE_MIN_SAMPLE` is 12 — the group has
    # to clear that before `derive_changes` will act on it at all.
    negative_small = [
        _row(sentiment="negative", reason="company_too_small", employee_count=5) for _ in range(12)
    ]
    positive_other = [
        _row(sentiment="positive", reason=None, employee_count=100) for _ in range(15)
    ]
    analysis = analyze_feedback_patterns(negative_small + positive_other)
    assert analysis.support is FeedbackSupport.ELIGIBLE
    assert analysis.outcome_analysis is not None

    changes = derive_changes(analysis.outcome_analysis, _draft())
    micro_changes = [
        c for c in changes if c.kind is ChangeKind.EMPLOYEE_BAND and c.target == "employees_1_10"
    ]
    assert len(micro_changes) == 1
    assert micro_changes[0].from_value == str(BandPreference.PREFERRED)
    assert micro_changes[0].to_value == str(BandPreference.ACCEPTABLE)


def test_dominant_wrong_industry_produces_an_industry_candidate() -> None:
    negative_industry = [
        _row(sentiment="negative", reason="wrong_industry", industry="retail", employee_count=None)
        for _ in range(12)
    ]
    positive_other = [
        _row(sentiment="positive", reason=None, industry="software", employee_count=None)
        for _ in range(15)
    ]
    analysis = analyze_feedback_patterns(negative_industry + positive_other)
    assert analysis.outcome_analysis is not None

    draft = _draft().model_copy(update={"preferred_industries": ["retail"]})
    changes = derive_changes(analysis.outcome_analysis, draft)
    industry_changes = [
        c for c in changes if c.kind is ChangeKind.INDUSTRY and c.target == "retail"
    ]
    assert len(industry_changes) == 1
    assert industry_changes[0].to_value == "acceptable"


def test_mixed_weak_signal_produces_no_change() -> None:
    """Enough total feedback to be `ELIGIBLE`, but no group clears
    `arie.intelligence.outcomes`' own signal-strength bar — no proposal."""
    rows = [
        _row(sentiment="positive" if i % 2 == 0 else "negative", employee_count=5 + i)
        for i in range(MIN_FEEDBACK_FOR_PROPOSAL)
    ]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.outcome_analysis is not None
    changes = derive_changes(analysis.outcome_analysis, _draft())
    assert changes == []


def test_change_is_one_step_never_preferred_to_avoid() -> None:
    """`derive_changes`' own rule (already tested in
    `tests/unit/test_intelligence_proposals.py`), reconfirmed reachable
    through the feedback path: a demotion never jumps past `acceptable`."""
    negative_small = [
        _row(sentiment="negative", reason="company_too_small", employee_count=5) for _ in range(20)
    ]
    positive_other = [
        _row(sentiment="positive", reason=None, employee_count=100) for _ in range(20)
    ]
    analysis = analyze_feedback_patterns(negative_small + positive_other)
    assert analysis.outcome_analysis is not None
    draft = _draft().model_copy(
        update={
            "employee_band_preferences": {
                EmployeeBand.MICRO: BandPreference.PREFERRED,
            }
        }
    )
    changes = derive_changes(analysis.outcome_analysis, draft)
    micro = next(c for c in changes if c.target == "employees_1_10")
    assert micro.to_value == str(BandPreference.ACCEPTABLE)  # never "avoid" in one step


def test_analysis_never_mutates_the_draft() -> None:
    draft = _draft()
    original = draft.model_copy(deep=True)
    rows = [
        _row(sentiment="negative", reason="company_too_small", employee_count=5) for _ in range(9)
    ] + [_row(sentiment="positive", reason=None, employee_count=100) for _ in range(15)]
    analysis = analyze_feedback_patterns(rows)
    assert analysis.outcome_analysis is not None
    derive_changes(analysis.outcome_analysis, draft)
    assert draft == original
