"""`arie.scoring.rules.ScoringConfig`/`use_scoring_config` — the dynamically
scoped override that lets an organization ICP profile (`arie.icp_profiles`)
drive the existing scorer without changing any of its callers' signatures.

The load-bearing property under test throughout: with no `use_scoring_config`
block active, every function's behaviour must be byte-identical to before
this mechanism existed — that is what keeps the frozen M0 benchmark frozen.
"""

from __future__ import annotations

from arie.core.types import Decision
from arie.scoring.engine import compute_bounds
from arie.scoring.rules import (
    QUALIFY_THRESHOLD,
    REJECT_THRESHOLD,
    ScoringConfig,
    active_config,
    best_case_value,
    decide,
    field_points,
    is_disqualified,
    score_facts,
    use_scoring_config,
)


def test_default_active_config_matches_reference_constants() -> None:
    config = active_config()
    assert config.qualify_threshold == QUALIFY_THRESHOLD
    assert config.reject_threshold == REJECT_THRESHOLD
    assert config.disqualifier_enabled is True


def test_decide_is_unaffected_with_no_override_active() -> None:
    assert decide(65.0) is Decision.AUTO_ROUTE
    assert decide(54.9) is Decision.REJECT
    assert decide(60.0) is Decision.ESCALATE_HUMAN


def test_use_scoring_config_overrides_thresholds_only_inside_the_block() -> None:
    custom = ScoringConfig(qualify_threshold=40.0, reject_threshold=20.0)
    assert decide(30.0) is Decision.REJECT  # reference config: 30 < REJECT_THRESHOLD (55)
    with use_scoring_config(custom):
        assert decide(30.0) is Decision.ESCALATE_HUMAN  # custom: 20 <= 30 < 40
        assert decide(41.0) is Decision.AUTO_ROUTE  # custom: 41 >= 40
        assert decide(19.0) is Decision.REJECT  # custom: 19 < 20
    assert decide(30.0) is Decision.REJECT  # reference config restored, after


def test_use_scoring_config_resets_even_if_the_block_raises() -> None:
    custom = ScoringConfig(qualify_threshold=1.0, reject_threshold=0.0)
    try:
        with use_scoring_config(custom):
            assert active_config().qualify_threshold == 1.0
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert active_config().qualify_threshold == QUALIFY_THRESHOLD


def test_custom_industry_points_change_field_points_and_score() -> None:
    custom = ScoringConfig(industry_points={"construction": 15.0})
    assert field_points("industry", "construction") == 0.0  # unmapped under reference config
    with use_scoring_config(custom):
        assert field_points("industry", "construction") == 15.0
        assert field_points("industry", "software") == 0.0  # not in the custom map at all
    assert field_points("industry", "construction") == 0.0  # restored


def test_disqualifier_can_be_disabled() -> None:
    facts = {"disqualifying_flag": True, "industry": "software", "employee_count": 100}
    assert is_disqualified(facts) is True
    assert score_facts(dict(facts)).total_score == 0.0

    with use_scoring_config(ScoringConfig(disqualifier_enabled=False)):
        assert is_disqualified(facts) is False
        breakdown = score_facts(dict(facts))
        assert breakdown.total_score > 0.0  # no longer nullified


def test_compute_bounds_disqualifier_floor_respects_disabled_toggle() -> None:
    facts = {"industry": "software", "employee_count": 100}  # disqualifying_flag unknown

    reference_bounds = compute_bounds(facts)
    assert reference_bounds.lower == 0.0  # unknown blocker pins the floor at zero

    with use_scoring_config(ScoringConfig(disqualifier_enabled=False)):
        disabled_bounds = compute_bounds(facts)
        assert disabled_bounds.lower == disabled_bounds.current  # floor no longer pinned to zero


def test_best_case_value_reflects_the_active_config() -> None:
    assert best_case_value("industry") == "software"  # reference config's highest (15.0)
    with use_scoring_config(ScoringConfig(industry_points={"widgets": 99.0, "gadgets": 1.0})):
        assert best_case_value("industry") == "widgets"
    assert best_case_value("industry") == "software"


def test_custom_employee_count_bands() -> None:
    custom = ScoringConfig(size_bands=((1, 5, 3.0), (6, 10**9, 25.0)))
    with use_scoring_config(custom):
        assert field_points("employee_count", 3) == 3.0
        assert field_points("employee_count", 500) == 25.0
        assert field_points("employee_count", 0) == 0.0  # below every band
