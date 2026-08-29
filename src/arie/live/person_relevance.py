"""Option C's core question: is a person-provider's evidence worth buying?

**The case this module exists for.** ``optimized`` already stops the moment
overall score bounds settle (:data:`STOP_SETTLED` in
``arie.jobs.handlers``) or the calibrated confidence model clears its bar —
but the second of those is a probabilistic judgement about the *whole*
lead, not a claim about any one still-unbought field. A five-person
construction firm reads as a confident reject on firmographics alone before
a person-provider is even considered, which is a happy coincidence of that
lead's shape, not a rule that a person-provider's own fields were checked
for relevance. This module asks the narrower, deterministic question
directly: **could this specific provider's still-unknown fields, resolved
as favorably as they possibly could be, change the recommendation?** If not,
no calibrated model is needed to justify skipping it — the reachable score
range, computed the same way :func:`arie.scoring.engine.compute_bounds`
always has, already proves it.

**Reuses the scorer, never duplicates it.** The "as favorably as possible"
value for a still-unknown field comes from
:func:`arie.scoring.rules.best_case_value`, and the resulting hypothetical
fact bundle is scored by the real
:func:`arie.scoring.engine.score_resolved` — the exact function that scores
every other lead. This module contains no point tables and no threshold
comparisons of its own; it only asks "does the decision change" by comparing
two ``Decision`` values the scorer itself produced.

**Deliberately narrower than "is more evidence worth buying" in general.**
A field this module says "not material" for might still be worth having for
other reasons (audit completeness, cross-provider comparison) — this module
answers one question only: would *this acquisition* change *this
recommendation*. The caller (a live acquisition loop) decides what to do
with that answer; this module never calls a provider, never persists
anything, and never sees an API key.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from arie.scoring.engine import ScoringResult, score_resolved
from arie.scoring.rules import SCORED_FIELDS, best_case_value, is_known

__all__ = ["PersonEvidenceRelevance", "person_evidence_is_decision_relevant"]


@dataclass(frozen=True)
class PersonEvidenceRelevance:
    should_call: bool
    reason: str
    """One human-readable sentence — carried into a stop_reason's receipt
    explanation, the same way ``arie.identity.validation.IdentityValidation``
    carries its reasons for the same purpose."""
    fields_considered: tuple[str, ...]
    """The candidate fields that were actually still unknown and evaluated.
    Empty when every candidate field was already known — nothing left for
    the provider to usefully answer, independent of whether it would have
    mattered."""
    current_score: float
    best_case_score: float


def person_evidence_is_decision_relevant(
    scoring: ScoringResult, candidate_fields: Sequence[str]
) -> PersonEvidenceRelevance:
    """Would resolving ``candidate_fields`` — a person-provider's declared
    fields — to their most favorable possible values change the
    recommendation reached from ``scoring`` alone?

    ``candidate_fields`` is normally a provider's ``provides_fields`` (e.g.
    Hunter's ``("title_seniority", "title_function")``) — passed in rather
    than hardcoded so this isn't tied to one specific provider's catalogue.
    Only fields both scored (:data:`arie.scoring.rules.SCORED_FIELDS`) and
    still unknown in ``scoring.facts`` are considered; a field with no known
    "best" categorical value (:func:`arie.scoring.rules.best_case_value`
    returns ``None`` for it, e.g. a continuous field) cannot be improved on
    for this check and is left alone in the hypothetical bundle.
    """
    unresolved = tuple(
        sorted(
            field_name
            for field_name in candidate_fields
            if field_name in SCORED_FIELDS and not is_known(scoring.facts, field_name)
        )
    )

    if scoring.bounds.is_settled:
        return PersonEvidenceRelevance(
            should_call=False,
            reason=(
                f"score bounds are already settled at {scoring.bounds.settled_decision} "
                f"(reachable range {scoring.bounds.lower:.1f}-{scoring.bounds.upper:.1f} points); "
                "no further evidence, from this provider or any other, could change the "
                "recommendation"
            ),
            fields_considered=unresolved,
            current_score=scoring.total_score,
            best_case_score=scoring.total_score,
        )

    if not unresolved:
        return PersonEvidenceRelevance(
            should_call=False,
            reason="this provider's fields are already known; there is nothing left for it to answer",
            fields_considered=(),
            current_score=scoring.total_score,
            best_case_score=scoring.total_score,
        )

    best_case_facts = dict(scoring.facts)
    for field_name in unresolved:
        value = best_case_value(field_name)
        if value is not None:
            best_case_facts[field_name] = value
    best_case = score_resolved(best_case_facts)

    if best_case.decision == scoring.decision:
        return PersonEvidenceRelevance(
            should_call=False,
            reason=(
                f"even the most favorable possible {', '.join(unresolved)} would not change the "
                f"recommendation from {scoring.decision} (best case {best_case.total_score:.1f} vs "
                f"current {scoring.total_score:.1f} points)"
            ),
            fields_considered=unresolved,
            current_score=scoring.total_score,
            best_case_score=best_case.total_score,
        )

    return PersonEvidenceRelevance(
        should_call=True,
        reason=(
            f"the most favorable possible {', '.join(unresolved)} could move the score from "
            f"{scoring.total_score:.1f} to {best_case.total_score:.1f}, changing the recommendation "
            f"from {scoring.decision} to {best_case.decision} — worth calling"
        ),
        fields_considered=unresolved,
        current_score=scoring.total_score,
        best_case_score=best_case.total_score,
    )
