"""Deterministic scorer over partial, missing, stale, and conflicting evidence.

Partial evidence is the normal case at runtime, not an edge case — the whole
point of the system is to decide *before* buying everything. These tests cover
the states a lead actually passes through as evidence accumulates.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from arie.core.types import Decision, Evidence
from arie.scoring.engine import (
    compute_bounds,
    score_evidence,
    score_resolved,
)
from arie.scoring.merge import (
    CONFLICT_EPSILON,
    Candidate,
    FieldResolution,
    facts_from,
    resolve_candidates,
)
from arie.scoring.rules import (
    MAX_TOTAL_SCORE,
    QUALIFY_THRESHOLD,
    REJECT_THRESHOLD,
    field_points,
    score_facts,
)

NOW = datetime(2026, 8, 16, 12, 0, 0)

STRONG_FACTS = {
    "employee_count": 120,
    "industry": "software",
    "title_seniority": "vp",
    "title_function": "data",
    "buying_intent": 0.9,
    "recent_trigger_event": "hired_vp_data",
    "disqualifying_flag": False,
}


def _ev(
    field_name: str,
    value: object,
    source: str = "firmographics_basic",
    confidence: float = 0.9,
    ttl_seconds: int = 86_400,
    fetched_at: datetime = NOW,
) -> Evidence:
    return Evidence(
        entity_type="company",
        entity_id=uuid4(),
        field_name=field_name,
        value=value,
        source=source,
        confidence=confidence,
        ttl_seconds=ttl_seconds,
        fetched_at=fetched_at,
    )


# --- missing evidence --------------------------------------------------------


def test_no_evidence_scores_zero_and_rejects() -> None:
    result = score_evidence([], NOW)
    assert result.total_score == 0.0
    assert result.decision is Decision.REJECT
    assert result.signals.completeness == 0.0


def test_missing_fields_contribute_zero_not_a_neutral_prior() -> None:
    """Unknown intent must not be credited as if it were positive.

    This is what makes stopping too early genuinely risky, rather than free.
    """
    partial = score_facts({"industry": "software"}).total_score
    assert partial == field_points("industry", "software")


def test_completeness_rises_as_fields_arrive() -> None:
    seen = []
    evidence: list[Evidence] = []
    for field_name, value in [
        ("industry", "software"),
        ("employee_count", 120),
        ("title_seniority", "vp"),
        ("buying_intent", 0.9),
    ]:
        evidence.append(_ev(field_name, value))
        seen.append(score_evidence(evidence, NOW).signals.completeness)

    assert seen == sorted(seen)
    assert all(0.0 < c < 1.0 for c in seen)


def test_full_evidence_reaches_complete() -> None:
    evidence = [_ev(name, value) for name, value in STRONG_FACTS.items()]
    assert score_evidence(evidence, NOW).signals.completeness == pytest.approx(1.0)


def test_unknown_disqualifier_prevents_full_completeness() -> None:
    """A lead is not fully known while the fact that can overturn it is missing.

    Weighting the disqualifier at zero would let a lead report as fully
    enriched with its most decision-relevant field still unchecked.
    """
    without = {k: v for k, v in STRONG_FACTS.items() if k != "disqualifying_flag"}
    evidence = [_ev(name, value) for name, value in without.items()]
    assert score_evidence(evidence, NOW).signals.completeness < 1.0


# --- score bounds and decision stability ------------------------------------


def test_bounds_collapse_when_disqualified() -> None:
    bounds = compute_bounds({**STRONG_FACTS, "disqualifying_flag": True})
    assert (bounds.lower, bounds.current, bounds.upper) == (0.0, 0.0, 0.0)
    assert bounds.settled_decision is Decision.REJECT


def test_unknown_disqualifier_drops_the_floor_to_zero() -> None:
    """The asymmetry that drives acquisition behaviour.

    However strong a lead looks, an unchecked blocker can nullify it — so
    auto-routing can never be *proven* safe until that flag is read.
    """
    facts = {k: v for k, v in STRONG_FACTS.items() if k != "disqualifying_flag"}
    bounds = compute_bounds(facts)
    assert bounds.current >= QUALIFY_THRESHOLD
    assert bounds.lower == 0.0
    assert bounds.settled_decision is None, "must not settle on auto-route unchecked"


def test_strong_lead_settles_once_blocker_is_cleared() -> None:
    bounds = compute_bounds(STRONG_FACTS)
    assert bounds.lower == bounds.current == bounds.upper
    assert bounds.settled_decision is Decision.AUTO_ROUTE


def test_hopeless_lead_settles_as_reject_before_full_enrichment() -> None:
    """Stopping early is safe when the ceiling cannot reach the threshold."""
    facts = {
        "employee_count": 4,  # 2 pts
        "industry": "nonprofit",  # 2 pts
        "title_seniority": "ic",  # 2 pts
        "title_function": "other",  # 2 pts
        "disqualifying_flag": False,
    }
    bounds = compute_bounds(facts)
    # Only intent (20) and trigger (10) remain unknown.
    assert bounds.upper == pytest.approx(bounds.current + 30.0)
    assert bounds.upper < REJECT_THRESHOLD
    assert bounds.settled_decision is Decision.REJECT


def test_bounds_can_settle_inside_the_borderline_band() -> None:
    """A lead can be provably one for a human, before buying anything more.

    Note `recent_trigger_event=False` — "checked, none found", which caps the
    score. `None` would mean "unchecked" and leave 10 points in play, so the
    lead would not settle.
    """
    facts = {
        "employee_count": 120,  # 20
        "industry": "software",  # 15
        "title_seniority": "director",  # 14
        "title_function": "operations",  # 9
        "recent_trigger_event": False,
        "buying_intent": 0.0,
        "disqualifying_flag": False,
    }
    bounds = compute_bounds(facts)
    assert bounds.lower >= REJECT_THRESHOLD
    assert bounds.upper < QUALIFY_THRESHOLD
    assert bounds.settled_decision is Decision.ESCALATE_HUMAN


def test_checked_absent_differs_from_unchecked() -> None:
    """`False` means "looked, found nothing"; `None` means "never looked".

    Conflating them would leave the trigger's 10 points permanently in play,
    keeping leads unsettled after they were in fact settled — which makes the
    policy buy more than it needs and understates achievable savings.
    """
    base = {
        "employee_count": 120,
        "industry": "software",
        "buying_intent": 0.1,
        "disqualifying_flag": False,
    }
    unchecked = compute_bounds({**base, "recent_trigger_event": None})
    checked_absent = compute_bounds({**base, "recent_trigger_event": False})

    assert unchecked.current == checked_absent.current
    assert unchecked.upper == pytest.approx(checked_absent.upper + 10.0)
    assert checked_absent.width < unchecked.width


def test_open_decision_is_reported_as_unsettled() -> None:
    facts = {"employee_count": 120, "industry": "software", "disqualifying_flag": False}
    bounds = compute_bounds(facts)
    assert bounds.settled_decision is None
    assert bounds.width > 0


def test_bounds_always_contain_the_current_score() -> None:
    for facts in (STRONG_FACTS, {"industry": "software"}, {}):
        bounds = compute_bounds(facts)
        assert bounds.lower <= bounds.current <= bounds.upper


def test_upper_bound_never_exceeds_the_maximum_score() -> None:
    assert compute_bounds({}).upper <= MAX_TOTAL_SCORE


def test_bounds_tighten_monotonically_as_evidence_arrives() -> None:
    """More evidence must never widen the reachable interval."""
    evidence: list[Evidence] = [_ev("disqualifying_flag", False)]
    widths = [score_evidence(evidence, NOW).bounds.width]
    for field_name, value in [
        ("industry", "software"),
        ("employee_count", 120),
        ("title_seniority", "vp"),
        ("title_function", "data"),
        ("buying_intent", 0.9),
        ("recent_trigger_event", "new_cto"),
    ]:
        evidence.append(_ev(field_name, value))
        widths.append(score_evidence(evidence, NOW).bounds.width)

    assert widths == sorted(widths, reverse=True)
    assert widths[-1] == pytest.approx(0.0)


# --- conflicting evidence ----------------------------------------------------


def test_highest_confidence_source_wins() -> None:
    resolutions = resolve_candidates(
        [
            Candidate("industry", "nonprofit", "dns_web", 0.6),
            Candidate("industry", "software", "firmographics_premium", 0.95),
        ]
    )
    assert resolutions["industry"].value == "software"
    assert resolutions["industry"].source == "firmographics_premium"


def test_conflict_is_measured_in_score_impact_not_raw_difference() -> None:
    """Disagreement only matters when it moves the score.

    120 vs 180 employees is a 60-unit gap that lands in the same band and
    changes nothing; counting it as conflict would swamp the confidence model
    with noise. (180 vs 210 *would* conflict — they straddle the 200 boundary.)
    """
    same_band = resolve_candidates(
        [
            Candidate("employee_count", 120, "a", 0.9),
            Candidate("employee_count", 180, "b", 0.8),
        ]
    )["employee_count"]
    assert same_band.candidate_count == 2
    assert not same_band.contested

    across_band = resolve_candidates(
        [
            Candidate("employee_count", 40, "a", 0.9),
            Candidate("employee_count", 120, "b", 0.8),
        ]
    )["employee_count"]
    assert across_band.contested
    assert across_band.conflict_points == pytest.approx(10.0)


def test_tiny_numeric_disagreement_is_not_conflict() -> None:
    resolution = resolve_candidates(
        [
            Candidate("buying_intent", 0.7001, "a", 0.9),
            Candidate("buying_intent", 0.7002, "b", 0.8),
        ]
    )["buying_intent"]
    assert resolution.conflict_points < CONFLICT_EPSILON
    assert not resolution.contested


def test_conflict_rate_uses_multi_source_fields_as_denominator() -> None:
    """Single-source fields cannot disagree and must not dilute the rate.

    Using every field as the denominator would drive conflict toward zero
    precisely when coverage is thin — exactly when it matters most.
    """
    result = score_resolved(
        *_resolved(
            [
                Candidate("industry", "software", "a", 0.9),
                Candidate("industry", "nonprofit", "b", 0.8),
                Candidate("title_seniority", "vp", "a", 0.9),
            ]
        )
    )
    assert result.signals.conflict_rate == pytest.approx(1.0)
    assert result.signals.contested_fields == ("industry",)


def test_agreeing_sources_produce_no_conflict() -> None:
    result = score_resolved(
        *_resolved(
            [
                Candidate("industry", "software", "a", 0.9),
                Candidate("industry", "software", "b", 0.8),
            ]
        )
    )
    assert result.signals.conflict_rate == 0.0
    assert result.signals.max_conflict_points == 0.0


def _resolved(
    candidates: list[Candidate],
) -> tuple[dict[str, Any], dict[str, FieldResolution]]:
    resolutions = resolve_candidates(candidates)
    return facts_from(resolutions), resolutions


# --- staleness ---------------------------------------------------------------


def test_expired_evidence_is_dropped() -> None:
    stale = _ev("industry", "software", fetched_at=NOW - timedelta(days=2), ttl_seconds=86_400)
    assert score_evidence([stale], NOW).total_score == 0.0


def test_fresh_evidence_outranks_stale_evidence_of_equal_confidence() -> None:
    """Decay is what lets a free cached value lose honestly to a fresh call."""
    result = score_evidence(
        [
            _ev("industry", "nonprofit", source="cache", fetched_at=NOW - timedelta(hours=20)),
            _ev("industry", "software", source="fresh", fetched_at=NOW),
        ],
        NOW,
    )
    assert result.facts["industry"] == "software"
    assert result.resolutions["industry"].source == "fresh"


def test_stale_fields_are_reported() -> None:
    result = score_evidence(
        [_ev("industry", "software", fetched_at=NOW - timedelta(hours=12))], NOW
    )
    assert result.signals.stale_fields == ("industry",)


# --- shared code path with the oracle ---------------------------------------


def test_runtime_scoring_matches_oracle_on_complete_facts() -> None:
    """Complete evidence must reproduce the oracle exactly.

    This is the invariant that makes 'agreement with the oracle' measure
    acquisition behaviour rather than rule drift between two scorers.
    """
    evidence = [_ev(name, value, confidence=1.0) for name, value in STRONG_FACTS.items()]
    from_evidence = score_evidence(evidence, NOW)
    from_facts = score_facts(STRONG_FACTS)

    assert from_evidence.total_score == from_facts.total_score
    assert from_evidence.breakdown.components == from_facts.components
    assert from_evidence.decision is Decision.AUTO_ROUTE


def test_scoring_is_order_independent() -> None:
    evidence = [_ev(name, value) for name, value in STRONG_FACTS.items()]
    forward = score_evidence(evidence, NOW)
    backward = score_evidence(list(reversed(evidence)), NOW)
    assert forward.facts == backward.facts
    assert forward.total_score == backward.total_score


def test_unscored_fields_are_ignored() -> None:
    result = score_evidence([_ev("industry", "software"), _ev("favourite_colour", "blue")], NOW)
    assert "favourite_colour" not in result.facts
    assert result.total_score == field_points("industry", "software")


def test_unrecognised_values_score_zero_rather_than_raising() -> None:
    """Providers return junk; the scorer must degrade, not crash."""
    result = score_evidence(
        [_ev("industry", "cryptozoology"), _ev("employee_count", "not-a-number")], NOW
    )
    assert result.total_score == 0.0
