"""Revision proposals: what gets suggested, and what a suggestion cannot do.

Every test here is a variation on one claim — a proposal is a suggestion. It is
derived from arithmetic, it is bounded to one step, it never fires on weak
evidence, and applying it goes through the same immutable-versioning path a
customer's own edit does. The storage and acceptance paths need a database and
are covered in ``tests/integration/test_intelligence_slice3_integration.py``;
everything deterministic is here.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.unit.test_intelligence_targeting import SUPPLEMENT_DRAFT

from arie.icp_profiles import validate_config
from arie.intelligence.normalization import build_scoring_config
from arie.intelligence.outcomes import (
    OutcomeInterpretation,
    SignalStrength,
    analyze_outcomes,
    parse_outcome_csv,
)
from arie.intelligence.proposals import (
    ACTIONABLE_SIGNALS,
    ChangeKind,
    ProposalStatus,
    apply_changes,
    build_revision_proposal,
    derive_changes,
)
from arie.intelligence.schemas import (
    BandPreference,
    BusinessProfileDraft,
    EmployeeBand,
    PreferenceLevel,
    ScoringDimension,
    TargetingObjective,
)


def _draft(**overrides: Any) -> BusinessProfileDraft:
    return BusinessProfileDraft.model_validate({**SUPPLEMENT_DRAFT, **overrides})


def _csv(rows: list[tuple[str, str, int]]) -> bytes:
    body = "".join(f"{c},{o},{e}\n" for c, o, e in rows)
    return ("company,outcome,employee_count\n" + body).encode()


def _analysis(mid_positive: int, mid_total: int, small_positive: int, small_total: int) -> Any:
    rows: list[tuple[str, str, int]] = []
    for i in range(mid_total):
        rows.append((f"Mid{i}", "won" if i < mid_positive else "lost", 120))
    for i in range(small_total):
        rows.append((f"Small{i}", "won" if i < small_positive else "lost", 20))
    return analyze_outcomes(parse_outcome_csv(_csv(rows)))


# ------------------------------------------------------------- derivation --


def test_a_moderate_positive_group_proposes_promoting_that_band() -> None:
    # The supplement draft already prefers 51-200, so use a band it does not.
    draft = _draft(
        employee_band_preferences={
            "employees_1_10": "avoid",
            "employees_11_50": "acceptable",
            "employees_51_200": "acceptable",
        }
    )
    changes = derive_changes(_analysis(16, 26, 5, 24), draft)
    band = next(c for c in changes if c.kind is ChangeKind.EMPLOYEE_BAND)
    assert band.target == "employees_51_200"
    assert band.from_value == "acceptable"
    assert band.to_value == "preferred"
    assert "in this dataset" in band.rationale.lower()


def test_a_weak_group_never_becomes_a_proposed_change() -> None:
    """The whole risk of this feature is somebody acting on noise."""
    analysis = _analysis(6, 10, 5, 10)
    assert all(g.signal not in ACTIONABLE_SIGNALS for g in analysis.groups)
    assert derive_changes(analysis, _draft()) == []


def test_an_insufficient_dataset_proposes_nothing() -> None:
    assert derive_changes(_analysis(2, 3, 1, 3), _draft()) == []


def test_a_change_is_not_proposed_when_the_profile_already_says_it() -> None:
    """A suggestion to keep doing what you are doing trains people to ignore
    every suggestion."""
    draft = _draft(
        employee_band_preferences={
            "employees_11_50": "acceptable",
            "employees_51_200": "preferred",
        }
    )
    bands = [
        c
        for c in derive_changes(_analysis(16, 26, 5, 24), draft)
        if c.kind is ChangeKind.EMPLOYEE_BAND
    ]
    assert bands == []


def test_a_negative_group_demotes_rather_than_excludes() -> None:
    """Turning a group off entirely is a bigger claim than one dataset carries."""
    draft = _draft(
        employee_band_preferences={"employees_11_50": "preferred", "employees_51_200": "preferred"}
    )
    # Small companies do badly: 2 of 26 positive against a much higher baseline.
    changes = derive_changes(_analysis(20, 24, 2, 26), draft)
    small = next(
        (c for c in changes if c.kind is ChangeKind.EMPLOYEE_BAND and "11-50" in c.target_label),
        None,
    )
    assert small is not None
    assert small.to_value == "acceptable"  # not "avoid"


def test_importance_moves_exactly_one_step_and_never_more() -> None:
    draft = _draft(
        employee_band_preferences={"employees_51_200": "acceptable"},
        relative_preferences={
            **SUPPLEMENT_DRAFT["relative_preferences"],
            "employee_count": "low",
        },
    )
    change = next(
        c
        for c in derive_changes(_analysis(16, 26, 5, 24), draft)
        if c.kind is ChangeKind.DIMENSION_IMPORTANCE
    )
    assert change.from_value == "low"
    assert change.to_value == "medium"  # not "critical"


def test_importance_at_the_ceiling_is_not_proposed_again() -> None:
    draft = _draft(
        employee_band_preferences={"employees_51_200": "preferred"},
        relative_preferences={
            **SUPPLEMENT_DRAFT["relative_preferences"],
            "employee_count": "critical",
        },
    )
    assert not [
        c
        for c in derive_changes(_analysis(16, 26, 5, 24), draft)
        if c.kind is ChangeKind.DIMENSION_IMPORTANCE
    ]


def test_derivation_is_deterministic() -> None:
    analysis, draft = _analysis(16, 26, 5, 24), _draft()
    first = derive_changes(analysis, draft)
    for _ in range(5):
        assert derive_changes(analysis, draft) == first


# ------------------------------------------------------------- applying --


def test_applying_changes_produces_a_draft_that_still_validates() -> None:
    draft = _draft(employee_band_preferences={"employees_51_200": "acceptable"})
    changes = derive_changes(_analysis(16, 26, 5, 24), draft)
    updated = apply_changes(draft, changes)
    assert updated.employee_band_preferences[EmployeeBand.MID] is BandPreference.PREFERRED
    config = build_scoring_config(updated, objective=TargetingObjective.BEST_PROSPECTS)
    validate_config(config)


def test_applying_changes_leaves_the_original_draft_alone() -> None:
    draft = _draft(employee_band_preferences={"employees_51_200": "acceptable"})
    before = draft.model_dump(mode="json")
    apply_changes(draft, derive_changes(_analysis(16, 26, 5, 24), draft))
    assert draft.model_dump(mode="json") == before


def test_applying_no_changes_is_a_no_op() -> None:
    draft = _draft()
    assert apply_changes(draft, []).model_dump() == draft.model_dump()


def test_applying_an_industry_promotion_moves_it_out_of_acceptable() -> None:
    from arie.intelligence.proposals import ProposedChange

    draft = _draft(preferred_industries=["retail"], acceptable_industries=["logistics"])
    updated = apply_changes(
        draft,
        [
            ProposedChange(
                kind=ChangeKind.INDUSTRY,
                dimension=str(ScoringDimension.INDUSTRY),
                target="logistics",
                target_label="logistics companies",
                from_value="acceptable",
                to_value="preferred",
                rationale="x",
            )
        ],
    )
    assert "logistics" in updated.preferred_industries
    assert "logistics" not in updated.acceptable_industries


def test_applying_a_change_naming_a_non_canonical_industry_is_refused() -> None:
    """A target that came out of a spreadsheet is revalidated, never cast."""
    from pydantic import ValidationError

    from arie.intelligence.proposals import ProposedChange

    with pytest.raises(ValidationError):
        apply_changes(
            _draft(),
            [
                ProposedChange(
                    kind=ChangeKind.INDUSTRY,
                    dimension=str(ScoringDimension.INDUSTRY),
                    target="artisanal_cheese",
                    target_label="cheese",
                    from_value="not listed",
                    to_value="preferred",
                    rationale="x",
                )
            ],
        )


# ------------------------------------------------------------- assembly --


def test_a_dataset_that_says_nothing_produces_no_proposal() -> None:
    assert build_revision_proposal(_analysis(6, 10, 5, 10), _draft()) is None


def test_a_proposal_without_a_model_is_complete_and_honest() -> None:
    draft = _draft(employee_band_preferences={"employees_51_200": "acceptable"})
    proposal = build_revision_proposal(_analysis(16, 26, 5, 24), draft)
    assert proposal is not None
    assert proposal.changes
    assert proposal.evidence_strength is SignalStrength.MODERATE
    assert proposal.sample_size == 26
    assert proposal.summary  # assembled from the statistics
    assert proposal.caveats  # never empty
    assert "not proof" in proposal.caveats[0]


def test_a_models_prose_replaces_the_summary_but_not_the_changes() -> None:
    draft = _draft(employee_band_preferences={"employees_51_200": "acceptable"})
    interpretation = OutcomeInterpretation.model_validate(
        json.loads(
            json.dumps(
                {
                    "summary": "Mid-sized companies did better in this data.",
                    "observations": ["51-200 people converted more often."],
                    "caveats": ["26 examples is not many."],
                    "suggested_changes": [],
                }
            )
        )
    )
    without = build_revision_proposal(_analysis(16, 26, 5, 24), draft)
    with_model = build_revision_proposal(
        _analysis(16, 26, 5, 24), draft, interpretation=interpretation
    )
    assert without is not None and with_model is not None
    assert with_model.summary == "Mid-sized companies did better in this data."
    assert with_model.caveats == ["26 examples is not many."]
    # The actionable part is identical either way.
    assert with_model.changes == without.changes


def test_a_proposal_keeps_the_aggregates_and_not_the_dataset() -> None:
    draft = _draft(employee_band_preferences={"employees_51_200": "acceptable"})
    proposal = build_revision_proposal(_analysis(16, 26, 5, 24), draft)
    assert proposal is not None
    stored = json.dumps(proposal.statistics)
    assert "Mid0" not in stored and "Small0" not in stored
    assert '"sample_size": 26' in stored
    assert '"baseline_rate"' in stored


def test_a_proposal_serialises_every_change_with_its_before_and_after() -> None:
    draft = _draft(employee_band_preferences={"employees_51_200": "acceptable"})
    proposal = build_revision_proposal(_analysis(16, 26, 5, 24), draft)
    assert proposal is not None
    payload = proposal.to_json()
    for change in payload["changes"]:
        assert set(change) == {
            "kind",
            "dimension",
            "target",
            "target_label",
            "from_value",
            "to_value",
            "rationale",
        }
        assert change["from_value"] != change["to_value"]


def test_a_proposal_starts_as_a_suggestion_and_nothing_else() -> None:
    """There is no field on a fresh proposal that says anything happened."""
    assert str(ProposalStatus.PROPOSED) == "proposed"
    draft = _draft(employee_band_preferences={"employees_51_200": "acceptable"})
    proposal = build_revision_proposal(_analysis(16, 26, 5, 24), draft)
    assert proposal is not None
    # Deriving and building touched neither the draft nor any config.
    assert draft.employee_band_preferences[EmployeeBand.MID] is BandPreference.ACCEPTABLE
    assert build_scoring_config(draft, objective=TargetingObjective.BEST_PROSPECTS) == (
        build_scoring_config(
            _draft(employee_band_preferences={"employees_51_200": "acceptable"}),
            objective=TargetingObjective.BEST_PROSPECTS,
        )
    )


def test_accepting_would_change_the_scoring_config_measurably() -> None:
    """The suggestion is worth something: applying it really does move points."""
    draft = _draft(
        employee_band_preferences={
            "employees_11_50": "acceptable",
            "employees_51_200": "acceptable",
        },
        relative_preferences={
            **SUPPLEMENT_DRAFT["relative_preferences"],
            "employee_count": "low",
        },
    )
    before = build_scoring_config(draft, objective=TargetingObjective.BEST_PROSPECTS)
    updated = apply_changes(draft, derive_changes(_analysis(16, 26, 5, 24), draft))
    after = build_scoring_config(updated, objective=TargetingObjective.BEST_PROSPECTS)

    validate_config(after)
    before_ceiling = max(b["points"] for b in before["employee_count_bands"])
    after_ceiling = max(b["points"] for b in after["employee_count_bands"])
    assert after_ceiling > before_ceiling
    assert (
        sum(
            [
                after_ceiling,
                max(after["industry_points"].values()),
                max(after["seniority_points"].values()),
                max(after["function_points"].values()),
                after["buying_intent_weight"],
                after["trigger_event_weight"],
            ]
        )
        == 100.0
    )


def test_a_preference_level_never_shifts_past_its_ends() -> None:
    from arie.intelligence.proposals import _shift

    assert _shift(PreferenceLevel.CRITICAL, 1) is PreferenceLevel.CRITICAL
    assert _shift(PreferenceLevel.NONE, -1) is PreferenceLevel.NONE
    assert _shift(PreferenceLevel.MEDIUM, 1) is PreferenceLevel.HIGH
