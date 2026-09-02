"""Ask ARIE's pure domain rules — M7 Slice 6, Parts A-E/I.

No database, no LLM client anywhere in this file: the deterministic intent
recognizers, `LeadListQueryPlan`'s bounds, and `rank_work_today`'s ordering
are all pure functions of primitive/dataclass inputs.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from arie.copilot import (
    LEAD_INTENTS,
    LIST_INTENTS,
    MAX_LIST_LIMIT,
    CopilotIntent,
    LeadListQueryPlan,
    LeadSummary,
    clamp_limit,
    rank_work_today,
    recognize_lead_intent,
    recognize_list_intent,
    to_reference,
)
from arie.recommendations import ConfidenceBand, CustomerPriority, NextAction, ResearchStatus

LEAD_A = UUID("11111111-1111-1111-1111-111111111111")
LEAD_B = UUID("22222222-2222-2222-2222-222222222222")
LEAD_C = UUID("33333333-3333-3333-3333-333333333333")
LEAD_D = UUID("44444444-4444-4444-4444-444444444444")


def _summary(
    lead_id: UUID,
    *,
    priority: CustomerPriority,
    next_action: NextAction,
    score: float | None = 70.0,
    confidence: float | None = 0.8,
    confidence_band: ConfidenceBand | None = ConfidenceBand.HIGH,
) -> LeadSummary:
    return LeadSummary(
        lead_id=lead_id,
        company="Acme",
        contact="Nadia",
        priority=priority,
        next_action=next_action,
        score=score,
        confidence=confidence,
        confidence_band=confidence_band,
        short_reason="Strong match.",
        industry="software",
        research_status=ResearchStatus.NOT_PERFORMED,
        missing_information=(),
        feedback_sentiment=None,
        profile_version=1,
        created_at_iso="2026-09-01T00:00:00+00:00",
    )


# --------------------------------------------------------------- intent enum --


def test_list_and_lead_intents_are_disjoint_and_exhaustive() -> None:
    assert LIST_INTENTS.isdisjoint(LEAD_INTENTS)
    assert set(CopilotIntent) == LIST_INTENTS | LEAD_INTENTS


# ------------------------------------------------------- deterministic list --


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What should I work on today?", CopilotIntent.WORK_TODAY),
        ("Which leads need more research?", CopilotIntent.NEEDS_RESEARCH),
        (
            "Which promising leads are missing decision makers?",
            CopilotIntent.MISSING_DECISION_MAKER,
        ),
        ("Show me good companies with low confidence.", CopilotIntent.LOW_CONFIDENCE),
        ("Which leads did I mark as bad recommendations?", CopilotIntent.FEEDBACK_SUMMARY),
        ("Show me my 20 best leads.", CopilotIntent.TOP_LEADS),
    ],
)
def test_recognize_list_intent_matches_brief_examples(
    question: str, expected: CopilotIntent
) -> None:
    assert recognize_list_intent(question) is expected


def test_recognize_list_intent_returns_none_for_ambiguous_question() -> None:
    assert recognize_list_intent("Why is Acme ranked above Beta?") is None
    assert recognize_list_intent("Which Australian distributors should I contact first?") is None


def test_recognize_list_intent_returns_none_for_empty_question() -> None:
    assert recognize_list_intent("   ") is None


# ------------------------------------------------------- deterministic lead --


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Why is this a good lead?", CopilotIntent.LEAD_EXPLANATION),
        ("What is missing?", CopilotIntent.LEAD_MISSING_INFO),
        ("Would more research help?", CopilotIntent.LEAD_RESEARCHABILITY),
        ("Why did ARIE skip it?", CopilotIntent.LEAD_EXPLANATION),
        (
            "What would need to change for this to become Contact First?",
            CopilotIntent.LEAD_IMPROVEMENT_PATH,
        ),
        ("What evidence affected the score most?", CopilotIntent.LEAD_SCORE_DRIVERS),
    ],
)
def test_recognize_lead_intent_matches_brief_examples(
    question: str, expected: CopilotIntent
) -> None:
    assert recognize_lead_intent(question) is expected


def test_recognize_lead_intent_returns_none_for_empty_question() -> None:
    assert recognize_lead_intent("") is None


# ---------------------------------------------------------- query plan bounds --


def test_query_plan_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        LeadListQueryPlan.model_validate({"intent": "top_leads", "organization_id": "x"})


def test_query_plan_rejects_raw_sql_field() -> None:
    with pytest.raises(ValidationError):
        LeadListQueryPlan.model_validate({"intent": "top_leads", "raw_sql": "DROP TABLE leads"})


def test_query_plan_rejects_huge_limit() -> None:
    with pytest.raises(ValidationError):
        LeadListQueryPlan.model_validate({"intent": "top_leads", "limit": 10_000})


def test_query_plan_rejects_unknown_intent() -> None:
    with pytest.raises(ValidationError):
        LeadListQueryPlan.model_validate({"intent": "delete_everything"})


def test_query_plan_rejects_unknown_priority_value() -> None:
    with pytest.raises(ValidationError):
        LeadListQueryPlan.model_validate({"intent": "top_leads", "priorities": ["urgent"]})


def test_query_plan_defaults_are_bounded() -> None:
    plan = LeadListQueryPlan(intent=CopilotIntent.TOP_LEADS)
    assert plan.limit == 20
    assert plan.limit <= MAX_LIST_LIMIT


def test_clamp_limit_never_exceeds_max() -> None:
    assert clamp_limit(1000) == MAX_LIST_LIMIT
    assert clamp_limit(0) == 1
    assert clamp_limit(5) == 5


# --------------------------------------------------------------- work today --


def test_work_today_orders_contact_first_before_worth_pursuing_before_review() -> None:
    review = _summary(
        LEAD_A,
        priority=CustomerPriority.REVIEW,
        next_action=NextAction.HUMAN_REVIEW,
        score=None,
        confidence=None,
        confidence_band=None,
    )
    worth_pursuing = _summary(
        LEAD_B, priority=CustomerPriority.WORTH_PURSUING, next_action=NextAction.EMAIL_FIRST
    )
    contact_first = _summary(
        LEAD_C, priority=CustomerPriority.CONTACT_FIRST, next_action=NextAction.CONTACT_NOW
    )
    skip = _summary(LEAD_D, priority=CustomerPriority.SKIP, next_action=NextAction.SKIP)

    ranked = rank_work_today([review, worth_pursuing, contact_first, skip])

    assert [s.lead_id for s in ranked] == [LEAD_C, LEAD_B, LEAD_A]


def test_work_today_excludes_review_with_no_actionable_next_step() -> None:
    non_actionable_review = _summary(
        LEAD_A, priority=CustomerPriority.REVIEW, next_action=NextAction.RESEARCH_MORE
    )
    ranked = rank_work_today([non_actionable_review])
    assert ranked == []


def test_work_today_includes_review_needing_a_decision_maker() -> None:
    actionable_review = _summary(
        LEAD_A, priority=CustomerPriority.REVIEW, next_action=NextAction.FIND_DECISION_MAKER
    )
    ranked = rank_work_today([actionable_review])
    assert [s.lead_id for s in ranked] == [LEAD_A]


def test_work_today_orders_contact_first_by_score_descending() -> None:
    low = _summary(
        LEAD_A,
        priority=CustomerPriority.CONTACT_FIRST,
        next_action=NextAction.CONTACT_NOW,
        score=60.0,
    )
    high = _summary(
        LEAD_B,
        priority=CustomerPriority.CONTACT_FIRST,
        next_action=NextAction.CONTACT_NOW,
        score=90.0,
    )
    ranked = rank_work_today([low, high])
    assert [s.lead_id for s in ranked] == [LEAD_B, LEAD_A]


def test_work_today_is_not_a_plain_creation_date_sort() -> None:
    """A weak, low-score Contact First lead must still outrank a stronger
    Worth Pursuing lead, regardless of which was created more recently."""
    weak_contact_first = _summary(
        LEAD_A,
        priority=CustomerPriority.CONTACT_FIRST,
        next_action=NextAction.CONTACT_NOW,
        score=10.0,
    )
    strong_worth_pursuing = _summary(
        LEAD_B,
        priority=CustomerPriority.WORTH_PURSUING,
        next_action=NextAction.EMAIL_FIRST,
        score=99.0,
    )
    ranked = rank_work_today([strong_worth_pursuing, weak_contact_first])
    assert [s.lead_id for s in ranked] == [LEAD_A, LEAD_B]


# ------------------------------------------------------------- to_reference --


def test_to_reference_carries_only_the_compact_fields() -> None:
    summary = _summary(
        LEAD_A, priority=CustomerPriority.CONTACT_FIRST, next_action=NextAction.CONTACT_NOW
    )
    ref = to_reference(summary)
    assert ref.lead_id == LEAD_A
    assert ref.company == "Acme"
    assert ref.why == summary.short_reason
    assert ref.priority is CustomerPriority.CONTACT_FIRST
