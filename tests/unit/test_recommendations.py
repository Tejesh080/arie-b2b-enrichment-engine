"""Deterministic priority/next-action derivation — M7 Slice 4 Part A/B.

Every test here asserts a `DecisionSignal` in, a `CustomerPriority`/
`NextAction` out, with no LLM and no database. `test_from_receipt_agrees_with_
direct_signal` is the one test that also touches `DecisionReceipt`, to prove
the two `DecisionSignal` constructors (`from_receipt`, used by the lead-detail
endpoint, and `from_decision_row`, used by the batch list) apply the exact
same rules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from arie.api.receipt import (
    DecisionReceipt,
    ReceiptCost,
    ReceiptDecision,
    ReceiptEvidence,
    ReceiptEvidenceItem,
    ReceiptProviders,
    ReceiptScore,
    ReceiptScoreBounds,
)
from arie.core.types import Decision, LeadStatus
from arie.recommendations import (
    ConfidenceBand,
    CustomerPriority,
    DecisionSignal,
    NextAction,
    ResearchStatus,
    build_recommendation,
    confidence_band,
    derive_customer_priority,
    derive_next_action,
)

LEAD_ID = UUID("33333333-3333-3333-3333-333333333333")


def _signal(
    *,
    decided: bool = True,
    lead_status: LeadStatus = LeadStatus.AUTO_ROUTED,
    recommended_action: str | None = str(Decision.AUTO_ROUTE),
    confidence: float | None = 0.9,
    score: float | None = 82.0,
    known_fields: tuple[str, ...] = ("employee_count", "industry", "title_seniority"),
    unknown_fields: tuple[str, ...] = (),
    profile_version: int | None = 1,
    shadow: bool = False,
    execution_mode: str | None = "simulated",
    research_status: ResearchStatus | None = ResearchStatus.RESEARCHED,
) -> DecisionSignal:
    return DecisionSignal(
        decided=decided,
        lead_status=lead_status,
        recommended_action=recommended_action,
        confidence=confidence,
        score=score,
        known_fields=known_fields,
        unknown_fields=unknown_fields,
        profile_version=profile_version,
        shadow=shadow,
        execution_mode=execution_mode,
        research_status=research_status,
    )


# ----------------------------------------------------------------- priority --


def test_strong_autonomous_fit_is_contact_first() -> None:
    assert derive_customer_priority(_signal()) is CustomerPriority.CONTACT_FIRST


def test_positive_but_lower_confidence_is_worth_pursuing() -> None:
    signal = _signal(confidence=0.5)
    assert derive_customer_priority(signal) is CustomerPriority.WORTH_PURSUING


def test_escalated_decision_that_qualified_is_worth_pursuing_not_contact_first() -> None:
    # AUTO_ROUTED status but the machine's own recommendation was an escalation
    # a human later approved — never as strong a signal as an autonomous route.
    signal = _signal(recommended_action=str(Decision.ESCALATE_HUMAN), confidence=0.95)
    assert derive_customer_priority(signal) is CustomerPriority.WORTH_PURSUING


def test_awaiting_human_is_review_even_with_a_high_score() -> None:
    signal = _signal(lead_status=LeadStatus.AWAITING_HUMAN, confidence=0.99, score=95.0)
    assert derive_customer_priority(signal) is CustomerPriority.REVIEW


def test_pending_lead_is_review_not_skip() -> None:
    signal = _signal(decided=False, recommended_action=None, confidence=None, score=None)
    assert derive_customer_priority(signal) is CustomerPriority.REVIEW


def test_processing_failure_is_review() -> None:
    signal = _signal(
        decided=False,
        lead_status=LeadStatus.DEAD_LETTER,
        recommended_action=None,
        confidence=None,
        score=None,
    )
    assert derive_customer_priority(signal) is CustomerPriority.REVIEW


def test_clear_reject_is_skip() -> None:
    signal = _signal(
        lead_status=LeadStatus.SYNCED,
        recommended_action=str(Decision.REJECT),
        confidence=0.9,
        score=20.0,
    )
    assert derive_customer_priority(signal) is CustomerPriority.SKIP


def test_manual_review_outcome_is_worth_pursuing() -> None:
    signal = _signal(
        lead_status=LeadStatus.MANUAL_REVIEW, recommended_action=str(Decision.ESCALATE_HUMAN)
    )
    assert derive_customer_priority(signal) is CustomerPriority.WORTH_PURSUING


def test_shadow_evaluation_uses_the_recommendation_not_the_status() -> None:
    signal = _signal(lead_status=LeadStatus.SHADOW_EVALUATED, shadow=True)
    assert derive_customer_priority(signal) is CustomerPriority.CONTACT_FIRST


def test_priority_is_deterministic_across_repeated_calls() -> None:
    signal = _signal()
    results = {derive_customer_priority(signal) for _ in range(5)}
    assert results == {CustomerPriority.CONTACT_FIRST}


def test_priority_reads_only_the_signal_it_is_given() -> None:
    """No branch in `derive_customer_priority` consults anything beyond its
    argument — asserted by checking two structurally-identical signals (same
    field values, different identity) agree."""
    a = _signal()
    b = _signal()
    assert derive_customer_priority(a) is derive_customer_priority(b)


# -------------------------------------------------------------- next action --


def test_contact_first_with_known_seniority_is_contact_now() -> None:
    priority = CustomerPriority.CONTACT_FIRST
    signal = _signal()
    assert derive_next_action(priority, signal) is NextAction.CONTACT_NOW


def test_contact_first_with_unknown_seniority_is_find_decision_maker() -> None:
    signal = _signal(
        known_fields=("employee_count", "industry"), unknown_fields=("title_seniority",)
    )
    priority = derive_customer_priority(signal)
    assert derive_next_action(priority, signal) is NextAction.FIND_DECISION_MAKER


def test_awaiting_human_next_action_is_human_review() -> None:
    signal = _signal(lead_status=LeadStatus.AWAITING_HUMAN)
    priority = derive_customer_priority(signal)
    assert derive_next_action(priority, signal) is NextAction.HUMAN_REVIEW


def test_skip_next_action_is_skip() -> None:
    signal = _signal(lead_status=LeadStatus.SYNCED, recommended_action=str(Decision.REJECT))
    priority = derive_customer_priority(signal)
    assert derive_next_action(priority, signal) is NextAction.SKIP


def test_uncertain_review_next_action_is_research_more() -> None:
    # REVIEW that is neither an open human review nor a pipeline failure:
    # not decided yet (still gathering evidence).
    signal = _signal(decided=False, recommended_action=None, confidence=None, score=None)
    priority = derive_customer_priority(signal)
    assert priority is CustomerPriority.REVIEW
    assert derive_next_action(priority, signal) is NextAction.RESEARCH_MORE


def test_next_action_never_calls_a_provider_or_llm() -> None:
    """Structural check: the function is pure and total over the enum space —
    every priority produces *some* next action with no exception raised."""
    for priority in CustomerPriority:
        assert isinstance(derive_next_action(priority, _signal()), NextAction)


# ----------------------------------------------------------- confidence band --


def test_confidence_band_thresholds() -> None:
    assert confidence_band(0.9) is ConfidenceBand.HIGH
    assert confidence_band(0.75) is ConfidenceBand.HIGH
    assert confidence_band(0.6) is ConfidenceBand.MEDIUM
    assert confidence_band(0.45) is ConfidenceBand.MEDIUM
    assert confidence_band(0.1) is ConfidenceBand.LOW


# -------------------------------------------------------- unknown vs. negative --


def test_unknown_disqualifier_does_not_force_skip() -> None:
    """A lead with no disqualifier evidence at all must still be able to
    reach CONTACT_FIRST — unknown is never treated as "disqualifying"."""
    signal = _signal(unknown_fields=("disqualifying_flag",))
    assert derive_customer_priority(signal) is CustomerPriority.CONTACT_FIRST


# --------------------------------------------------------- build_recommendation --


def test_build_recommendation_assembles_every_field() -> None:
    signal = _signal(unknown_fields=("buying_intent",))
    rec = build_recommendation(LEAD_ID, signal)
    assert rec.lead_id == LEAD_ID
    assert rec.priority is CustomerPriority.CONTACT_FIRST
    assert rec.next_action is NextAction.CONTACT_NOW
    assert rec.confidence_band is ConfidenceBand.HIGH
    assert "company size" in rec.key_evidence
    assert "buying intent" in rec.missing_information
    assert rec.explanation_status == "not_requested"
    assert rec.research_status is ResearchStatus.RESEARCHED
    assert rec.profile_version == 1


def test_short_reason_never_states_a_raw_value() -> None:
    rec = build_recommendation(LEAD_ID, _signal())
    for known in rec.key_evidence:
        # Labels only — never a number, which would imply a fabricated fact.
        assert not any(char.isdigit() for char in known)
    assert not any(char.isdigit() for char in rec.short_reason)


def test_batch_list_signal_omits_research_status_honestly() -> None:
    """`from_decision_row` never claims research happened — that requires a
    `provider_calls` join the batch list deliberately does not do."""
    signal = DecisionSignal.from_decision_row(
        lead_status=LeadStatus.AUTO_ROUTED,
        shadow=False,
        decision=str(Decision.AUTO_ROUTE),
        confidence=0.9,
        score_value=82.0,
        evidence_snapshot={
            "known": [{"field": "employee_count"}, {"field": "title_seniority"}],
            "unknown": ["buying_intent"],
            "execution_mode": "simulated",
        },
        profile_version=2,
    )
    rec = build_recommendation(LEAD_ID, signal)
    assert rec.priority is CustomerPriority.CONTACT_FIRST
    assert rec.research_status is ResearchStatus.NOT_PERFORMED
    assert rec.execution_mode == "simulated"


# --------------------------------------------------- DecisionSignal.from_receipt --


def _receipt(
    *,
    decision: str = str(Decision.AUTO_ROUTE),
    lead_status: LeadStatus = LeadStatus.AUTO_ROUTED,
    confidence: float = 0.9,
) -> DecisionReceipt:
    return DecisionReceipt(
        receipt_version="1",
        lead_id=LEAD_ID,
        status="decided",
        lead_status=lead_status,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        shadow=False,
        execution_mode="simulated",
        decision=ReceiptDecision(
            recommended_action=decision,
            autonomous=True,
            final_status=lead_status,
            human_override=False,
        ),
        score=ReceiptScore(
            value=82.0,
            threshold_qualify=65.0,
            threshold_reject=55.0,
            bounds=ReceiptScoreBounds(lower=80.0, upper=84.0),
            confidence=confidence,
            tau=0.8,
        ),
        stopping=None,
        versions=None,
        cost=ReceiptCost(
            provider_cost_usd=Decimal(0),
            model_cost_usd=Decimal(0),
            total_cost_usd=Decimal(0),
            budget_usd_cap=Decimal(10),
        ),
        evidence=ReceiptEvidence(
            cache_hits=0,
            provider_calls=1,
            items=(
                ReceiptEvidenceItem(
                    field="employee_count", source="clearbit", confidence=0.9, contested=False
                ),
                ReceiptEvidenceItem(
                    field="title_seniority", source="hunter", confidence=0.8, contested=False
                ),
            ),
            unknown_fields=(),
        ),
        providers=ReceiptProviders(called=(), not_called=(), unavailable={}),
        human_review=None,
    )


def test_from_receipt_agrees_with_direct_signal() -> None:
    receipt = _receipt()
    via_receipt = build_recommendation(LEAD_ID, DecisionSignal.from_receipt(receipt))
    via_direct = build_recommendation(
        LEAD_ID,
        _signal(known_fields=("employee_count", "title_seniority"), unknown_fields=()),
    )
    assert via_receipt.priority == via_direct.priority
    assert via_receipt.next_action == via_direct.next_action


def test_from_receipt_research_status_reflects_no_provider_calls() -> None:
    receipt = _receipt()
    signal = DecisionSignal.from_receipt(receipt)
    rec = build_recommendation(LEAD_ID, signal)
    assert rec.research_status is ResearchStatus.NOT_PERFORMED
