"""What a customer's own recommendation feedback says about their targeting.

M7 Slice 7, Part A. The same discipline as
``arie.intelligence.outcomes``, one level up: instead of a customer's
uploaded won/lost spreadsheet, the "outcome" here is whether they agreed
with ARIE's recommendation — a thumbs up or down on
``lead_recommendation_feedback``, joined to the same company-size/industry
evidence ``analyze_outcomes`` already knows how to group and score.

**Feedback IS an outcome dataset, so this module barely computes anything
new.** :func:`build_outcome_dataset` is the only translation step —
POSITIVE becomes :data:`~arie.intelligence.outcomes.OutcomeLabel.WON`,
NEGATIVE becomes :data:`~arie.intelligence.outcomes.OutcomeLabel.LOST` — and
everything downstream (grouping, signal-strength classification, one-step
candidate changes via
:func:`~arie.intelligence.proposals.derive_changes`) is the exact code
``arie.intelligence.outcomes``/``arie.intelligence.proposals`` already ship
and already have their own tests for. This module supplies only what
feedback needs and a CSV upload does not: a running "is there enough of it
yet" gate (:func:`feedback_support`), because feedback accumulates
continuously rather than arriving as one clearly-bounded upload.

**Association, never causation — same rule, same words.** See
``arie.intelligence.outcomes``'s own docstring; nothing here relaxes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from arie.intelligence.outcomes import (
    MIN_DATASET_ROWS,
    OutcomeAnalysis,
    OutcomeDataset,
    OutcomeLabel,
    OutcomeRow,
    analyze_outcomes,
)

__all__ = [
    "MIN_FEEDBACK_FOR_PROPOSAL",
    "MIN_FEEDBACK_FOR_SUMMARY",
    "FeedbackOutcomeRow",
    "FeedbackPatternAnalysis",
    "FeedbackSupport",
    "analyze_feedback_patterns",
    "build_outcome_dataset",
    "feedback_support",
]

MIN_FEEDBACK_FOR_SUMMARY = 5
"""Below this, feedback is `INSUFFICIENT_DATA` — not even a summary. A
handful of clicks describes nothing about a targeting pattern; showing
counts back to a customer at n=2 would just be noise dressed as a report."""

MIN_FEEDBACK_FOR_PROPOSAL = MIN_DATASET_ROWS
"""Below this, ARIE shows the summary (positive/negative counts, negative
reasons) but never a profile-revision proposal — Part A1's middle tier.
Deliberately equal to `arie.intelligence.outcomes.MIN_DATASET_ROWS`: once
this many feedback rows exist, `analyze_outcomes`'s own dataset-usability
gate (identical threshold) is what actually decides whether any *group*
within them is large enough to mean something, so this module's ceiling and
that module's floor are the same number for a reason, not by coincidence."""


class FeedbackSupport(StrEnum):
    INSUFFICIENT_DATA = "insufficient_data"
    SUMMARY_ONLY = "summary_only"
    ELIGIBLE = "eligible"


def feedback_support(total: int) -> FeedbackSupport:
    """Pure, total, three-way — see the module-level constants for the exact
    boundaries and why they are where they are."""
    if total < MIN_FEEDBACK_FOR_SUMMARY:
        return FeedbackSupport.INSUFFICIENT_DATA
    if total < MIN_FEEDBACK_FOR_PROPOSAL:
        return FeedbackSupport.SUMMARY_ONLY
    return FeedbackSupport.ELIGIBLE


@dataclass(frozen=True)
class FeedbackOutcomeRow:
    """One `lead_recommendation_feedback` row, joined (by the DB-touching
    caller) to just enough live evidence to group on — never the full
    evidence pool, never raw text a customer wrote elsewhere."""

    lead_id: UUID
    company: str
    sentiment: str
    """`"positive"` or `"negative"` — `arie.feedback.FeedbackSentiment`'s own
    values, kept as `str` here so this module never has to import a DB-facing
    enum for a pure comparison."""
    reason: str | None
    priority: str
    profile_version: int | None
    employee_count: int | None
    industry: str | None


def build_outcome_dataset(rows: list[FeedbackOutcomeRow]) -> OutcomeDataset:
    """Translate feedback into the exact shape
    `arie.intelligence.outcomes.analyze_outcomes` already groups and scores.

    Every row is labelled — feedback always carries a sentiment, unlike a
    customer's CSV where a blank or unrecognised outcome is common — so
    `OutcomeDataset.labelled` is always the full set, and unlike a CSV upload
    there is no "unrecognised label" case to report here.
    """
    outcome_rows = [
        OutcomeRow(
            row_number=index,
            company=row.company,
            label=OutcomeLabel.WON if row.sentiment == "positive" else OutcomeLabel.LOST,
            raw_label=row.sentiment,
            employee_count=row.employee_count,
            industry=row.industry,
        )
        for index, row in enumerate(rows, start=1)
    ]
    return OutcomeDataset(
        rows=outcome_rows,
        has_employee_counts=any(r.employee_count is not None for r in outcome_rows),
        has_industries=any(r.industry is not None for r in outcome_rows),
    )


@dataclass(frozen=True)
class FeedbackPatternAnalysis:
    """Everything deterministic ARIE can say about a customer's feedback so far."""

    total: int
    positive: int
    negative: int
    support: FeedbackSupport
    by_priority: dict[str, dict[str, int]] = field(default_factory=dict)
    """`{priority: {"positive": n, "negative": n}}` — Part A2's "sentiment by
    priority"."""
    by_profile_version: dict[str, dict[str, int]] = field(default_factory=dict)
    """`{profile_version: {"positive": n, "negative": n}}` — Part A2's
    "sentiment by profile version". Keys are strings (not every profile has a
    version, and a JSON-safe key has to be one type)."""
    negative_reason_counts: dict[str, int] = field(default_factory=dict)
    outcome_analysis: OutcomeAnalysis | None = None
    """`None` below `ELIGIBLE` support — there is no point grouping eight
    rows by company size. Populated (via `analyze_outcomes` on
    `build_outcome_dataset`'s translation) once `total >=
    MIN_FEEDBACK_FOR_PROPOSAL`, which is the same threshold
    `arie.intelligence.proposals.build_revision_proposal` needs an
    `OutcomeAnalysis` to even consider a change."""

    @property
    def agreement_rate(self) -> float | None:
        return (self.positive / self.total) if self.total else None


def analyze_feedback_patterns(rows: list[FeedbackOutcomeRow]) -> FeedbackPatternAnalysis:
    """Pure. No database, no model, no cost — the whole point, per the module
    docstring: statistics first, always, before anything is proposed."""
    total = len(rows)
    support = feedback_support(total)

    by_priority: dict[str, dict[str, int]] = {}
    by_profile_version: dict[str, dict[str, int]] = {}
    negative_reasons: dict[str, int] = {}
    positive = 0
    for row in rows:
        bucket = by_priority.setdefault(row.priority, {"positive": 0, "negative": 0})
        bucket[row.sentiment] = bucket.get(row.sentiment, 0) + 1

        version_key = str(row.profile_version) if row.profile_version is not None else "unknown"
        version_bucket = by_profile_version.setdefault(version_key, {"positive": 0, "negative": 0})
        version_bucket[row.sentiment] = version_bucket.get(row.sentiment, 0) + 1

        if row.sentiment == "positive":
            positive += 1
        elif row.reason:
            negative_reasons[row.reason] = negative_reasons.get(row.reason, 0) + 1

    outcome_analysis = (
        analyze_outcomes(build_outcome_dataset(rows))
        if support is FeedbackSupport.ELIGIBLE
        else None
    )

    return FeedbackPatternAnalysis(
        total=total,
        positive=positive,
        negative=total - positive,
        support=support,
        by_priority=by_priority,
        by_profile_version=by_profile_version,
        negative_reason_counts=negative_reasons,
        outcome_analysis=outcome_analysis,
    )
