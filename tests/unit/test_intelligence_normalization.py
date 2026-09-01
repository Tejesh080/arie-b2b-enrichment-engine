"""The deterministic normaliser — the thing that makes a model safe to use here.

Every test in this file is really one assertion in different clothes: nothing a
model can say produces a scoring configuration ARIE would not accept, and the
same input produces the same output every time. The exhaustive sweep at the
bottom is the load-bearing one — it runs the entire preference space through
``build_scoring_config`` and ``validate_config``, so the exactly-100.0 guarantee
is checked against all 15,625 combinations rather than a handful someone thought
of.
"""

from __future__ import annotations

import itertools
from uuid import uuid4

import pytest

from arie.icp_profiles import (
    REFERENCE_CONFIG,
    WEIGHT_SUM_TARGET,
    InvalidICPConfigError,
    materialize_scoring_config,
    validate_config,
)
from arie.intelligence.normalization import (
    OBJECTIVE_THRESHOLDS,
    PREFERENCE_WEIGHTS,
    allocate_ceilings,
    build_scoring_config,
    describe_allocation,
)
from arie.intelligence.schemas import (
    EMPLOYEE_BANDS,
    SCORING_DIMENSIONS,
    BandPreference,
    BusinessProfileDraft,
    EmployeeBand,
    PreferenceLevel,
    ScoringDimension,
    TargetingObjective,
)

LEVELS = tuple(PreferenceLevel)


def _draft(**overrides: object) -> BusinessProfileDraft:
    base: dict[str, object] = {
        "offering_summary": "Wholesale sports supplements.",
        "plain_english_summary": "Gyms, supplement retailers and distributors.",
        "preferred_industries": ["retail", "ecommerce"],
        "acceptable_industries": ["hospitality"],
        "preferred_seniorities": ["c_level"],
        "acceptable_seniorities": ["director"],
        "preferred_functions": ["operations"],
        "acceptable_functions": ["sales"],
        "employee_band_preferences": {
            EmployeeBand.MICRO: BandPreference.AVOID,
            EmployeeBand.SMALL: BandPreference.PREFERRED,
            EmployeeBand.MID: BandPreference.PREFERRED,
        },
        "relative_preferences": {
            ScoringDimension.EMPLOYEE_COUNT: PreferenceLevel.HIGH,
            ScoringDimension.INDUSTRY: PreferenceLevel.HIGH,
            ScoringDimension.TITLE_SENIORITY: PreferenceLevel.CRITICAL,
            ScoringDimension.TITLE_FUNCTION: PreferenceLevel.HIGH,
            ScoringDimension.BUYING_INTENT: PreferenceLevel.MEDIUM,
            ScoringDimension.RECENT_TRIGGER_EVENT: PreferenceLevel.LOW,
        },
    }
    base.update(overrides)
    return BusinessProfileDraft.model_validate(base)


def _levels(*values: PreferenceLevel) -> dict[ScoringDimension, PreferenceLevel]:
    return dict(zip(SCORING_DIMENSIONS, values, strict=True))


# ------------------------------------------------------------- allocation --


def test_equal_preferences_split_a_hundred_as_evenly_as_whole_points_allow() -> None:
    ceilings = allocate_ceilings(_levels(*[PreferenceLevel.MEDIUM] * 6))
    assert sum(ceilings.values()) == 100
    # 100/6 = 16.67: four dimensions get 17, two get 16 — and which two is
    # fixed by declaration order, not by chance.
    assert sorted(ceilings.values()) == [16, 16, 17, 17, 17, 17]
    assert ceilings[ScoringDimension.EMPLOYEE_COUNT] == 17


def test_a_skewed_preference_dominates_but_never_takes_everything() -> None:
    ceilings = allocate_ceilings(
        _levels(
            PreferenceLevel.LOW,
            PreferenceLevel.LOW,
            PreferenceLevel.CRITICAL,
            PreferenceLevel.LOW,
            PreferenceLevel.LOW,
            PreferenceLevel.LOW,
        )
    )
    assert sum(ceilings.values()) == 100
    assert ceilings[ScoringDimension.TITLE_SENIORITY] == max(ceilings.values())
    assert all(value > 0 for value in ceilings.values())


def test_a_none_preference_really_is_zero() -> None:
    ceilings = allocate_ceilings(
        _levels(
            PreferenceLevel.NONE,
            PreferenceLevel.HIGH,
            PreferenceLevel.HIGH,
            PreferenceLevel.HIGH,
            PreferenceLevel.HIGH,
            PreferenceLevel.HIGH,
        )
    )
    assert ceilings[ScoringDimension.EMPLOYEE_COUNT] == 0
    assert sum(ceilings.values()) == 100


def test_all_none_falls_back_to_an_even_split_rather_than_a_dead_profile() -> None:
    ceilings = allocate_ceilings(_levels(*[PreferenceLevel.NONE] * 6))
    assert sum(ceilings.values()) == 100
    assert all(value > 0 for value in ceilings.values())


def test_missing_dimensions_default_to_medium() -> None:
    assert allocate_ceilings({}) == allocate_ceilings(_levels(*[PreferenceLevel.MEDIUM] * 6))
    partial = allocate_ceilings({ScoringDimension.INDUSTRY: PreferenceLevel.CRITICAL})
    assert partial[ScoringDimension.INDUSTRY] == max(partial.values())
    assert sum(partial.values()) == 100


def test_allocation_is_deterministic_across_repeated_calls() -> None:
    preferences = _levels(
        PreferenceLevel.LOW,
        PreferenceLevel.CRITICAL,
        PreferenceLevel.HIGH,
        PreferenceLevel.MEDIUM,
        PreferenceLevel.NONE,
        PreferenceLevel.HIGH,
    )
    first = allocate_ceilings(preferences)
    assert all(allocate_ceilings(preferences) == first for _ in range(20))


def test_allocation_preserves_relative_ordering() -> None:
    """A dimension weighted above another never ends up worth less than it."""
    for combo in itertools.product(LEVELS, repeat=3):
        preferences = _levels(*combo, *combo)
        ceilings = allocate_ceilings(preferences)
        for a, b in itertools.permutations(SCORING_DIMENSIONS, 2):
            if PREFERENCE_WEIGHTS[preferences[a]] > PREFERENCE_WEIGHTS[preferences[b]]:
                assert ceilings[a] >= ceilings[b]


def test_the_remainder_tie_break_follows_declaration_order() -> None:
    """Documented behaviour, pinned so a refactor cannot quietly change it."""
    ceilings = allocate_ceilings(_levels(*[PreferenceLevel.MEDIUM] * 6))
    winners = [d for d in SCORING_DIMENSIONS if ceilings[d] == 17]
    assert winners == list(SCORING_DIMENSIONS[:4])


# ------------------------------------------------------------ full config --


def test_a_generated_config_passes_the_existing_domain_validator() -> None:
    config = build_scoring_config(_draft(), objective=TargetingObjective.BEST_PROSPECTS)
    validate_config(config)  # raises on failure


def test_ceilings_sum_to_exactly_one_hundred_point_zero() -> None:
    config = build_scoring_config(_draft(), objective=TargetingObjective.HIGH_VALUE)
    total = (
        max(band["points"] for band in config["employee_count_bands"])
        + max(config["industry_points"].values())
        + max(config["seniority_points"].values())
        + max(config["function_points"].values())
        + config["buying_intent_weight"]
        + config["trigger_event_weight"]
    )
    assert total == WEIGHT_SUM_TARGET  # exact equality, not almost-equal
    assert isinstance(total, float)


@pytest.mark.parametrize("objective", list(TargetingObjective))
def test_thresholds_come_from_the_objective_and_stay_ordered(
    objective: TargetingObjective,
) -> None:
    config = build_scoring_config(_draft(), objective=objective)
    qualify, reject = OBJECTIVE_THRESHOLDS[objective]
    assert config["qualify_threshold"] == qualify
    assert config["reject_threshold"] == reject
    assert reject < qualify  # validate_config's own rule


def test_the_default_objective_keeps_the_reference_decision_boundary() -> None:
    config = build_scoring_config(_draft(), objective=TargetingObjective.BEST_PROSPECTS)
    assert config["qualify_threshold"] == REFERENCE_CONFIG["qualify_threshold"]
    assert config["reject_threshold"] == REFERENCE_CONFIG["reject_threshold"]


def test_preferred_categories_sit_at_the_ceiling_and_acceptable_ones_below() -> None:
    config = build_scoring_config(_draft(), objective=TargetingObjective.BEST_PROSPECTS)
    ceiling = max(config["industry_points"].values())
    assert config["industry_points"]["retail"] == ceiling
    assert config["industry_points"]["ecommerce"] == ceiling
    assert 0 < config["industry_points"]["hospitality"] < ceiling


def test_every_band_is_emitted_including_avoided_ones_at_zero() -> None:
    config = build_scoring_config(_draft(), objective=TargetingObjective.BEST_PROSPECTS)
    bands = config["employee_count_bands"]
    assert len(bands) == len(EMPLOYEE_BANDS)
    assert bands[0]["points"] == 0.0  # MICRO was AVOID
    assert max(band["points"] for band in bands) > 0


def test_the_band_lattice_matches_the_reference_profiles_bands() -> None:
    generated = [
        (band["min_employees"], band["max_employees"])
        for band in build_scoring_config(_draft(), objective=TargetingObjective.BEST_PROSPECTS)[
            "employee_count_bands"
        ]
    ]
    reference = [
        (band["min_employees"], band["max_employees"])
        for band in REFERENCE_CONFIG["employee_count_bands"]
    ]
    assert generated == reference


def test_a_draft_naming_no_preferred_category_still_produces_a_reachable_ceiling() -> None:
    """Points with nothing to attach them to would make the dimension dead."""
    config = build_scoring_config(
        _draft(
            preferred_industries=[],
            acceptable_industries=[],
            preferred_seniorities=[],
            acceptable_seniorities=[],
            preferred_functions=[],
            acceptable_functions=[],
            employee_band_preferences={},
        ),
        objective=TargetingObjective.BEST_PROSPECTS,
    )
    validate_config(config)
    assert max(config["industry_points"].values()) > 0
    assert max(config["seniority_points"].values()) > 0
    assert max(config["function_points"].values()) > 0
    assert max(band["points"] for band in config["employee_count_bands"]) > 0


def test_avoiding_every_band_still_leaves_the_size_dimension_reachable() -> None:
    config = build_scoring_config(
        _draft(employee_band_preferences={band: BandPreference.AVOID for band in EMPLOYEE_BANDS}),
        objective=TargetingObjective.BEST_PROSPECTS,
    )
    validate_config(config)
    assert max(band["points"] for band in config["employee_count_bands"]) > 0


def test_a_zeroed_dimension_emits_zeros_rather_than_an_empty_map() -> None:
    config = build_scoring_config(
        _draft(
            relative_preferences={
                **_levels(*[PreferenceLevel.HIGH] * 6),
                ScoringDimension.INDUSTRY: PreferenceLevel.NONE,
            }
        ),
        objective=TargetingObjective.BEST_PROSPECTS,
    )
    validate_config(config)
    assert config["industry_points"]  # present, so a reviewer sees the intent
    assert max(config["industry_points"].values()) == 0.0


def test_geography_is_carried_through_but_never_scored() -> None:
    config = build_scoring_config(
        _draft(preferred_geographies=["Australia", "New Zealand"]),
        objective=TargetingObjective.BEST_PROSPECTS,
    )
    assert config["target_geographies"] == ["Australia", "New Zealand"]
    scoring = materialize_scoring_config(config, profile_id=uuid4(), version=1)
    assert not hasattr(scoring, "target_geographies")


def test_a_generated_config_materializes_into_a_usable_scoring_config() -> None:
    config = build_scoring_config(_draft(), objective=TargetingObjective.BEST_PROSPECTS)
    scoring = materialize_scoring_config(config, profile_id=uuid4(), version=3)
    assert scoring.profile_version == 3
    assert scoring.qualify_threshold == 65.0
    assert scoring.seniority_points["c_level"] == max(config["seniority_points"].values())
    assert len(scoring.size_bands) == len(EMPLOYEE_BANDS)


def test_generation_is_reproducible() -> None:
    draft = _draft()
    first = build_scoring_config(draft, objective=TargetingObjective.HIGH_VALUE)
    for _ in range(10):
        assert build_scoring_config(draft, objective=TargetingObjective.HIGH_VALUE) == first


# ------------------------------------------------------------ exhaustive --


def test_every_preference_combination_produces_a_valid_config() -> None:
    """5^6 = 15,625 combinations. The exactly-100.0 guarantee, exhaustively.

    Runs against the *empty* draft — no preferred categories anywhere — because
    that is the case where the fallbacks have to work, and a draft that names
    categories can only make each dimension easier to satisfy.
    """
    empty = BusinessProfileDraft(offering_summary="x", plain_english_summary="y")
    failures: list[tuple[PreferenceLevel, ...]] = []
    for combo in itertools.product(LEVELS, repeat=len(SCORING_DIMENSIONS)):
        draft = empty.model_copy(update={"relative_preferences": _levels(*combo)})
        config = build_scoring_config(draft, objective=TargetingObjective.BEST_PROSPECTS)
        try:
            validate_config(config)
        except InvalidICPConfigError:
            failures.append(combo)
    assert not failures, f"{len(failures)} preference combinations produced invalid configs"


# ------------------------------------------------------------- describe --


def test_the_allocation_summary_ranks_dimensions_by_points() -> None:
    config = build_scoring_config(_draft(), objective=TargetingObjective.BEST_PROSPECTS)
    rows = describe_allocation(config)
    assert [row["rank"] for row in rows] == [1, 2, 3, 4, 5, 6]
    assert rows[0]["dimension"] == "title_seniority"  # the CRITICAL one
    assert [row["points"] for row in rows] == sorted((row["points"] for row in rows), reverse=True)
    assert sum(row["points"] for row in rows) == WEIGHT_SUM_TARGET
    assert all(row["label"] and not row["label"].islower() for row in rows)
