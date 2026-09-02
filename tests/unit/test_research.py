"""Deterministic materiality and authorization — M7 Slice 5, Parts C/D/I.

No database, no provider, no LLM anywhere in this file — every rule
`arie.research` states is arithmetic and lookups over primitive inputs, and
every reason code in `ResearchReasonCode` is exercised at least once.
"""

from __future__ import annotations

from decimal import Decimal

from arie.organizations import LIVE_HUMAN_ONLY, SIMULATED
from arie.research import (
    CANDIDATE_SIMULATED_PROVIDERS,
    Materiality,
    MaterialityAnalysis,
    ResearchAuthorizationContext,
    ResearchReasonCode,
    ResearchTargetField,
    analyze_materiality,
    authorize_research,
    select_research_target,
)

# ------------------------------------------------------------- materiality --


def test_high_confidence_clear_positive_is_decision_already_clear() -> None:
    analysis = analyze_materiality(
        score_value=88.0,
        threshold_qualify=65.0,
        threshold_reject=55.0,
        bounds_lower=88.0,
        bounds_upper=90.0,
        known_fields=frozenset({"industry", "employee_count", "title_seniority", "title_function"}),
        field_ceilings={"recent_trigger_event": 10.0},
    )
    assert analysis.decision_already_clear is True
    assert analysis.material_fields == ()


def test_high_confidence_clear_reject_is_decision_already_clear() -> None:
    analysis = analyze_materiality(
        score_value=20.0,
        threshold_qualify=65.0,
        threshold_reject=55.0,
        bounds_lower=20.0,
        bounds_upper=30.0,  # even every unknown resolved best-case can't reach 55
        known_fields=frozenset(),
        field_ceilings={"employee_count": 20.0, "industry": 15.0},
    )
    assert analysis.decision_already_clear is True


def test_borderline_score_with_high_impact_unknown_is_material() -> None:
    # Part C's own worked example: score=60, qualify=65, employee_count
    # unknown with ceiling 20 -> 60+20=80 crosses qualify.
    analysis = analyze_materiality(
        score_value=60.0,
        threshold_qualify=65.0,
        threshold_reject=55.0,
        bounds_lower=60.0,
        bounds_upper=80.0,
        known_fields=frozenset({"industry", "title_seniority", "title_function"}),
        field_ceilings={"employee_count": 20.0},
    )
    field = next(f for f in analysis.fields if f.field is ResearchTargetField.EMPLOYEE_COUNT)
    assert field.materiality is Materiality.MATERIAL
    assert analysis.decision_already_clear is False


def test_unknown_low_impact_field_that_cannot_cross_threshold_is_non_material() -> None:
    analysis = analyze_materiality(
        score_value=40.0,
        threshold_qualify=65.0,
        threshold_reject=55.0,
        bounds_lower=40.0,
        bounds_upper=44.0,  # ceiling too small to reach reject_threshold=55
        known_fields=frozenset({"employee_count", "industry", "title_function"}),
        field_ceilings={"title_seniority": 4.0},
    )
    field = next(f for f in analysis.fields if f.field is ResearchTargetField.TITLE_SENIORITY)
    assert field.materiality is Materiality.NON_MATERIAL


def test_known_field_is_already_resolved() -> None:
    analysis = analyze_materiality(
        score_value=60.0,
        threshold_qualify=65.0,
        threshold_reject=55.0,
        bounds_lower=60.0,
        bounds_upper=80.0,
        known_fields=frozenset({"employee_count"}),
        field_ceilings={"employee_count": 20.0},
    )
    field = next(f for f in analysis.fields if f.field is ResearchTargetField.EMPLOYEE_COUNT)
    assert field.materiality is Materiality.ALREADY_RESOLVED


def test_multiple_unknowns_are_ranked_deterministically_by_ceiling() -> None:
    analysis = analyze_materiality(
        score_value=50.0,
        threshold_qualify=65.0,
        threshold_reject=55.0,
        bounds_lower=50.0,
        bounds_upper=100.0,
        known_fields=frozenset(),
        field_ceilings={
            "employee_count": 20.0,
            "industry": 15.0,
            "title_seniority": 20.0,  # tied with employee_count -> enum order breaks tie
            "title_function": 10.0,
        },
    )
    target = select_research_target(analysis)
    assert (
        target is ResearchTargetField.EMPLOYEE_COUNT
    )  # earlier in ResearchTargetField's own order


def test_unknown_disqualifier_semantics_preserved() -> None:
    """`bounds_lower < score_value` signals an unresolved disqualifier pinning
    the floor to zero (`arie.scoring.engine.compute_bounds`) — none of the
    four candidate fields can independently resolve the decision then, no
    matter how large their own ceiling."""
    analysis = analyze_materiality(
        score_value=80.0,
        threshold_qualify=65.0,
        threshold_reject=55.0,
        bounds_lower=0.0,  # disqualifier unknown pins the floor
        bounds_upper=95.0,
        known_fields=frozenset(),
        field_ceilings={"employee_count": 20.0},
    )
    field = next(f for f in analysis.fields if f.field is ResearchTargetField.EMPLOYEE_COUNT)
    assert field.materiality is Materiality.NON_MATERIAL
    assert select_research_target(analysis) is None


def test_materiality_is_deterministic_across_repeated_calls() -> None:
    kwargs = dict(
        score_value=60.0,
        threshold_qualify=65.0,
        threshold_reject=55.0,
        bounds_lower=60.0,
        bounds_upper=80.0,
        known_fields=frozenset(),
        field_ceilings={"employee_count": 20.0},
    )
    first = analyze_materiality(**kwargs)  # type: ignore[arg-type]
    second = analyze_materiality(**kwargs)  # type: ignore[arg-type]
    assert first == second


def test_select_research_target_returns_none_with_no_material_fields() -> None:
    analysis = MaterialityAnalysis(decision_already_clear=False, fields=())
    assert select_research_target(analysis) is None


# ------------------------------------------------------------ authorization --


def _ctx(**overrides: object) -> ResearchAuthorizationContext:
    base: dict[str, object] = dict(
        target_field=ResearchTargetField.EMPLOYEE_COUNT,
        materiality=Materiality.MATERIAL,
        decision_already_clear=False,
        candidate_providers=CANDIDATE_SIMULATED_PROVIDERS[ResearchTargetField.EMPLOYEE_COUNT],
        unavailable_providers={},
        suppressed_providers=frozenset(),
        execution_mode=SIMULATED,
        entitled_live=True,
        estimated_cost_usd=Decimal("0.01"),
        lead_spent_usd=Decimal("0.10"),
        lead_budget_cap_usd=Decimal("0.50"),
        org_modeled_spend_remaining_usd=Decimal("10.00"),
    )
    base.update(overrides)
    return ResearchAuthorizationContext(**base)  # type: ignore[arg-type]


def test_research_approved_in_simulated_mode() -> None:
    decision = authorize_research(_ctx())
    assert decision.approved is True
    assert decision.reason_code is ResearchReasonCode.RESEARCH_APPROVED
    assert (
        decision.chosen_provider
        in CANDIDATE_SIMULATED_PROVIDERS[ResearchTargetField.EMPLOYEE_COUNT]
    )


def test_decision_already_clear_refuses_regardless_of_everything_else() -> None:
    decision = authorize_research(_ctx(decision_already_clear=True))
    assert decision.approved is False
    assert decision.reason_code is ResearchReasonCode.DECISION_ALREADY_CLEAR


def test_field_already_known_is_refused() -> None:
    decision = authorize_research(_ctx(materiality=Materiality.ALREADY_RESOLVED))
    assert decision.reason_code is ResearchReasonCode.FIELD_ALREADY_KNOWN


def test_non_material_field_is_refused() -> None:
    decision = authorize_research(_ctx(materiality=Materiality.NON_MATERIAL))
    assert decision.reason_code is ResearchReasonCode.MISSING_FIELD_CANNOT_CHANGE_DECISION


def test_no_supported_source_is_refused() -> None:
    decision = authorize_research(_ctx(candidate_providers=()))
    assert decision.reason_code is ResearchReasonCode.NO_SUPPORTED_SOURCE


def test_provider_not_configured_in_live_mode() -> None:
    decision = authorize_research(
        _ctx(
            execution_mode=LIVE_HUMAN_ONLY,
            candidate_providers=("abstract_company_enrichment",),
            unavailable_providers={"abstract_company_enrichment": "provider_not_configured"},
        )
    )
    assert decision.reason_code is ResearchReasonCode.PROVIDER_NOT_CONFIGURED


def test_provider_unavailable_for_a_different_reason() -> None:
    decision = authorize_research(
        _ctx(
            execution_mode=LIVE_HUMAN_ONLY,
            candidate_providers=("abstract_company_enrichment",),
            unavailable_providers={"abstract_company_enrichment": "credential_unavailable"},
        )
    )
    assert decision.reason_code is ResearchReasonCode.PROVIDER_UNAVAILABLE


def test_over_budget_lead_cap_is_refused() -> None:
    decision = authorize_research(
        _ctx(
            lead_spent_usd=Decimal("0.499"),
            lead_budget_cap_usd=Decimal("0.50"),
            estimated_cost_usd=Decimal("0.01"),
        )
    )
    assert decision.reason_code is ResearchReasonCode.OVER_BUDGET


def test_over_budget_org_modeled_spend_is_refused() -> None:
    decision = authorize_research(_ctx(org_modeled_spend_remaining_usd=Decimal("0.00")))
    assert decision.reason_code is ResearchReasonCode.OVER_BUDGET


def test_entitlement_blocked_for_a_live_mode_without_the_plan_feature() -> None:
    decision = authorize_research(
        _ctx(
            execution_mode=LIVE_HUMAN_ONLY,
            candidate_providers=("abstract_company_enrichment",),
            entitled_live=False,
        )
    )
    assert decision.reason_code is ResearchReasonCode.ENTITLEMENT_BLOCKED


def test_suppressed_recent_failure_is_refused() -> None:
    candidates = ("internal_crm",)
    decision = authorize_research(
        _ctx(candidate_providers=candidates, suppressed_providers=frozenset(candidates))
    )
    assert decision.reason_code is ResearchReasonCode.SUPPRESSED_RECENT_FAILURE


def test_execution_mode_blocked_for_an_otherwise_approvable_live_plan() -> None:
    decision = authorize_research(
        _ctx(
            execution_mode=LIVE_HUMAN_ONLY,
            candidate_providers=("abstract_company_enrichment",),
            entitled_live=True,
        )
    )
    assert decision.approved is False
    assert decision.reason_code is ResearchReasonCode.EXECUTION_MODE_BLOCKED


def test_authorization_is_deterministic_for_fixed_state() -> None:
    ctx = _ctx()
    assert authorize_research(ctx) == authorize_research(ctx)


def test_approved_plan_names_no_provider_when_refused() -> None:
    decision = authorize_research(_ctx(decision_already_clear=True))
    assert decision.chosen_provider is None
    assert decision.estimated_cost_usd is None
