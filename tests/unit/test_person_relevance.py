"""arie.live.person_relevance — Option C's core question, pure and offline.

No providers, no DB: every case builds a ScoringResult from real Evidence via
arie.scoring.engine.score_evidence, then asks whether a person-provider's
still-unknown fields could change the recommendation. The scoring math itself
is never duplicated here — every assertion is checked against what the real
scorer actually produced for that fixture, not against a hand-computed number.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from arie.core.types import Evidence
from arie.live.person_relevance import person_evidence_is_decision_relevant
from arie.scoring.engine import score_evidence
from arie.scoring.rules import QUALIFY_THRESHOLD, REJECT_THRESHOLD

NOW = datetime(2026, 8, 30, tzinfo=UTC)
CANDIDATE_FIELDS = ("title_seniority", "title_function")


def _company_evidence(**fields: object) -> list[Evidence]:
    company_id = uuid.uuid4()
    return [
        Evidence(
            entity_type="company",
            entity_id=company_id,
            field_name=field_name,
            value=value,
            source="abstract_company_enrichment",
            confidence=0.8,
            ttl_seconds=999_999,
            fetched_at=NOW,
        )
        for field_name, value in fields.items()
    ]


def test_a_lead_that_cannot_reach_qualify_even_at_best_case_skips_hunter() -> None:
    """A tiny nonprofit: even title_seniority=c_level + title_function=data
    (the maximum possible for both) cannot lift the score into contention —
    the recommendation is "reject" either way."""
    scoring = score_evidence(_company_evidence(employee_count=5, industry="nonprofit"), NOW)
    assert scoring.total_score < REJECT_THRESHOLD  # sanity: fixture is a clear reject already

    result = person_evidence_is_decision_relevant(scoring, CANDIDATE_FIELDS)

    assert result.should_call is False
    assert result.fields_considered == ("title_function", "title_seniority")
    assert result.best_case_score < REJECT_THRESHOLD
    assert "would not change the recommendation" in result.reason


def test_a_lead_where_best_case_title_would_cross_qualify_calls_hunter() -> None:
    """A mid-size software company sits below qualify on company evidence
    alone, but the best possible title_seniority/title_function would push
    it over QUALIFY_THRESHOLD — Hunter is worth its cost here."""
    scoring = score_evidence(_company_evidence(employee_count=150, industry="software"), NOW)
    assert scoring.total_score < QUALIFY_THRESHOLD  # sanity

    result = person_evidence_is_decision_relevant(scoring, CANDIDATE_FIELDS)

    assert result.should_call is True
    assert result.best_case_score >= QUALIFY_THRESHOLD
    assert "worth calling" in result.reason


def test_a_lead_where_best_case_title_would_cross_reject_into_escalate_calls_hunter() -> None:
    """The band between REJECT_THRESHOLD and QUALIFY_THRESHOLD is a real,
    distinct recommendation (escalate to a human) — moving into it from a
    reject is still a materially different outcome, not just "not qualify"."""
    scoring = score_evidence(_company_evidence(employee_count=90_000, industry="software"), NOW)
    assert scoring.total_score < REJECT_THRESHOLD  # sanity: currently a clear reject

    result = person_evidence_is_decision_relevant(scoring, CANDIDATE_FIELDS)

    assert result.should_call is True
    assert REJECT_THRESHOLD <= result.best_case_score < QUALIFY_THRESHOLD


def test_settled_bounds_skip_hunter_without_even_computing_a_best_case() -> None:
    """A disqualified lead's bounds are pinned to zero — the strongest,
    cheapest possible "no" — and this must be checked before the best-case
    simulation, not after."""
    scoring = score_evidence(_company_evidence(disqualifying_flag=True), NOW)
    assert scoring.bounds.is_settled

    result = person_evidence_is_decision_relevant(scoring, CANDIDATE_FIELDS)

    assert result.should_call is False
    assert result.best_case_score == result.current_score  # never simulated
    assert "settled" in result.reason


def test_fields_already_known_have_nothing_left_to_answer() -> None:
    """If title_seniority/title_function are already known (e.g. served from
    cache before this check runs), there is nothing left for the provider to
    usefully answer, independent of materiality."""
    scoring = score_evidence(
        _company_evidence(
            employee_count=150, industry="software", title_seniority="ic", title_function="other"
        ),
        NOW,
    )

    result = person_evidence_is_decision_relevant(scoring, CANDIDATE_FIELDS)

    assert result.should_call is False
    assert result.fields_considered == ()
    assert "already known" in result.reason


def test_only_the_still_unknown_candidate_fields_are_considered() -> None:
    """title_function already known, title_seniority is not — only the
    latter should appear in fields_considered, and its own best case alone
    (data function is already fixed, not re-simulated at its own ceiling) is
    what gets evaluated."""
    scoring = score_evidence(
        _company_evidence(employee_count=150, industry="software", title_function="other"), NOW
    )

    result = person_evidence_is_decision_relevant(scoring, CANDIDATE_FIELDS)

    assert result.fields_considered == ("title_seniority",)


def test_a_candidate_field_outside_the_scored_vocabulary_is_ignored() -> None:
    """A provider's provides_fields is passed through as-is by a caller — a
    field this scorer doesn't know about must not blow up or silently count
    as material."""
    scoring = score_evidence(_company_evidence(employee_count=150, industry="software"), NOW)

    result = person_evidence_is_decision_relevant(scoring, ("not_a_real_field",))

    assert result.fields_considered == ()
    assert result.should_call is False


@pytest.mark.parametrize("employee_count", [5, 150, 90_000])
def test_never_duplicates_the_scorers_own_arithmetic(employee_count: int) -> None:
    """Cross-check against arie.scoring.engine directly: best_case_score must
    equal what score_resolved actually computes for the same hypothetical
    facts, not a number this module derived independently."""
    from arie.scoring.engine import score_resolved
    from arie.scoring.rules import best_case_value

    scoring = score_evidence(
        _company_evidence(employee_count=employee_count, industry="software"), NOW
    )
    result = person_evidence_is_decision_relevant(scoring, CANDIDATE_FIELDS)

    if not result.fields_considered:
        pytest.skip("bounds already settled for this fixture")

    expected_facts = dict(scoring.facts)
    for field_name in result.fields_considered:
        expected_facts[field_name] = best_case_value(field_name)
    expected = score_resolved(expected_facts)

    assert result.best_case_score == expected.total_score
