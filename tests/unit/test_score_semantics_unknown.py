"""Unknown vs. known-negative, at the scoring layer (Live V1 Foundation, Phase 4).

The dangerous behaviour this file pins down: before the canonical taxonomy,
unrecognised real-world vocabulary reached the scorer and produced 0.0 points —
arithmetically identical to a deliberate "this lead is a poor fit". The two
must produce the same *score* and different *bounds, completeness, and
settledness*, because only one of them is a reason to stop buying evidence.
"""

from __future__ import annotations

import pytest

from arie.normalization.taxonomy import normalize_industry
from arie.scoring.engine import compute_bounds, compute_signals, score_resolved
from arie.scoring.rules import (
    MAX_FIELD_POINTS,
    UNKNOWN,
    field_points,
    is_known,
    is_unknown,
    score_facts,
)


def _facts(**overrides: object) -> dict[str, object]:
    """A complete, ordinary fact bundle; override one field per test."""
    base: dict[str, object] = {
        "employee_count": 120,
        "industry": "software",
        "title_seniority": "vp",
        "title_function": "operations",
        "buying_intent": 0.8,
        "recent_trigger_event": "series_b_funding",
        "disqualifying_flag": False,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------- the primitive --


@pytest.mark.parametrize("value", [None, UNKNOWN, "unknown"])
def test_is_unknown_covers_both_never_asked_and_asked_but_unmappable(value: object) -> None:
    assert is_unknown(value)


@pytest.mark.parametrize("value", ["software", "construction", "nonprofit", 0, False, 0.0, "other"])
def test_is_unknown_is_false_for_every_genuinely_observed_value(value: object) -> None:
    """Including the falsy ones. `0` employees and `False` for the
    disqualifier are *observations*, and a truthiness check here would have
    silently reclassified both as unknown."""
    assert not is_unknown(value)


def test_field_points_treats_the_sentinel_as_absent() -> None:
    assert field_points("industry", UNKNOWN) == 0.0
    assert field_points("title_seniority", UNKNOWN) == 0.0
    assert field_points("employee_count", UNKNOWN) == 0.0


# ------------------------------------------------ the same score, different state --


def test_known_poor_fit_and_unknown_score_identically() -> None:
    """They must. The score is the sum of what is known to be worth
    something, and neither is worth anything."""
    known_negative = score_facts(_facts(industry=normalize_industry("Construction")))
    unmappable = score_facts(_facts(industry=UNKNOWN))
    assert known_negative.total_score == unmappable.total_score


def test_but_only_the_unknown_one_leaves_the_upper_bound_open() -> None:
    """The whole point. A known-negative industry is settled at 0.0 points and
    can never rise; an unknown one could still turn out to be software and add
    15. Collapsing the second onto the first is what made unrecognised
    vocabulary behave like a confident rejection."""
    known_negative = compute_bounds(_facts(industry="construction"))
    unmappable = compute_bounds(_facts(industry=UNKNOWN))

    assert unmappable.upper == pytest.approx(known_negative.upper + MAX_FIELD_POINTS["industry"])
    assert unmappable.current == known_negative.current


def test_an_unknown_field_counts_against_completeness() -> None:
    known_negative = compute_signals(_facts(industry="construction"), {})
    unmappable = compute_signals(_facts(industry=UNKNOWN), {})

    assert "industry" in known_negative.known_fields
    assert "industry" in unmappable.unknown_fields
    assert unmappable.completeness < known_negative.completeness


def test_a_fully_unmappable_lead_reads_as_zero_percent_complete() -> None:
    """Not 100% complete with a zero score, which is what a `value is not None`
    completeness check produced for a response ARIE could not read at all."""
    all_unknown = dict.fromkeys(_facts(), UNKNOWN)
    signals = compute_signals(all_unknown, {})
    assert signals.completeness == 0.0
    assert signals.known_fields == ()


def test_settledness_distinguishes_the_two_states() -> None:
    """The operational consequence, and the reason this distinction is not
    academic.

    Both leads score 59. The first knows every field, so its reachable range is
    the single point 59 — provably inside the borderline band, settled, stop
    buying. The second differs in one respect: its industry came back as
    vocabulary ARIE could not read. That lifts its ceiling to 74, which crosses
    the qualify threshold, so the decision is genuinely still open and
    enrichment should continue. Treating the unreadable value as a known zero
    would have declared this lead finished on evidence nobody ever established.
    """
    fully_known = _facts(
        industry="construction",
        employee_count=5,
        title_seniority="director",
        title_function="data",
        buying_intent=0.9,
        recent_trigger_event="series_b_funding",
        disqualifying_flag=False,
    )
    same_but_unreadable_industry = {**fully_known, "industry": UNKNOWN}

    known_bounds = compute_bounds(fully_known)
    unreadable_bounds = compute_bounds(same_but_unreadable_industry)

    assert known_bounds.current == unreadable_bounds.current
    assert known_bounds.is_settled
    assert not unreadable_bounds.is_settled
    assert unreadable_bounds.upper == pytest.approx(
        known_bounds.upper + MAX_FIELD_POINTS["industry"]
    )


# ------------------------------------------- the Phase 4 acceptance variants --


@pytest.mark.parametrize(
    "raw",
    ["software", "computer software", "Computer Software", "software development", "SaaS"],
)
def test_every_software_variant_scores_identically_after_normalization(raw: str) -> None:
    """The regression the Live V1 audit found: these five strings describe one
    company and used to produce two different scores 15 points apart."""
    result = score_resolved(_facts(industry=normalize_industry(raw)))
    baseline = score_resolved(_facts(industry="software"))
    assert result.total_score == baseline.total_score
    assert result.decision is baseline.decision


def test_an_unknown_industry_does_not_score_as_a_bad_industry() -> None:
    normalized = normalize_industry("Pet Grooming Franchises")
    facts = _facts(industry=normalized)

    assert is_unknown(normalized)
    assert not is_known(facts, "industry")
    assert "industry" in compute_signals(facts, {}).unknown_fields


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("VP of Sales", "vp"), ("Head of Revenue Operations", "director"), ("CEO", "c_level")],
)
def test_seniority_variants_score_through_the_same_path(raw: str, expected: str) -> None:
    from arie.normalization.taxonomy import seniority_from_title

    assert seniority_from_title(raw) == expected
    assert field_points("title_seniority", seniority_from_title(raw)) == field_points(
        "title_seniority", expected
    )


# --------------------------------------------------- the merge-layer contract --


def test_an_unknown_valued_candidate_never_becomes_a_resolution() -> None:
    """A resolution is what the Decision Receipt renders as "this field is
    known, and this source won it". A value ARIE could not map is not a won
    field."""
    from arie.scoring.merge import Candidate, resolve_candidates

    resolutions = resolve_candidates(
        [
            Candidate("industry", UNKNOWN, "some_provider", 0.9),
            Candidate("employee_count", 120, "some_provider", 0.9),
        ]
    )
    assert set(resolutions) == {"employee_count"}


def test_a_real_value_still_wins_over_an_unknown_from_a_more_confident_source() -> None:
    from arie.scoring.merge import Candidate, resolve_candidates

    resolutions = resolve_candidates(
        [
            Candidate("industry", UNKNOWN, "confident_provider", 0.99),
            Candidate("industry", "software", "unsure_provider", 0.4),
        ]
    )
    assert resolutions["industry"].value == "software"
    assert resolutions["industry"].candidate_count == 1
    assert not resolutions["industry"].contested
