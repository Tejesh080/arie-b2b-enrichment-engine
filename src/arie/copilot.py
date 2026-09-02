"""Ask ARIE — a safe, read-only interface to ARIE's own already-decided truth.

M7 Slice 6. Every other M7 slice produced a *deterministic* customer-facing
surface (a recommendation, a materiality analysis, a feedback aggregate) with
the model, when used at all, confined to wording or narrow selection —
never the decision itself. Copilot is the same discipline applied to
free-text questions:

    question -> intent + bounded filters -> one fixed, tenant-scoped query
    -> a small result set -> an optional, already-known-facts-only sentence

**The model never sees the database and never writes SQL.** `LeadListQueryPlan`
is the only thing a model produces for a list question, and it is a closed
Pydantic schema (`extra="forbid"`, enum-typed filters, a hard-capped `limit`)
— there is no field it could fill in that names a table, a column, or an
arbitrary predicate. `arie.copilot_service` is the only place a plan is
executed, through one fixed, statically-written query (see that module's own
docstring for why one bounded fetch plus Python-side filtering, rather than
per-intent dynamic SQL, is the deliberately narrower design).

**This module is pure — no database, no LLM client, no I/O.** Everything here
is directly unit-testable: the deterministic intent recognizers (Part E's
"reduce LLM cost" rule), the query-plan schema and its bounds, the
`WORK_TODAY` ranking rule, and the response shapes a caller assembles. Every
figure a `CopilotResponse`/`LeadCopilotResponse` carries is either read
straight off an already-computed `LeadSummary` (which itself is built from
`arie.recommendations`/`arie.research`, never re-decided here) or a fixed
template sentence — never a number a model invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from arie.feedback import FeedbackSentiment
from arie.recommendations import ConfidenceBand, CustomerPriority, NextAction, ResearchStatus
from arie.research import ResearchTargetField

__all__ = [
    "COMPARE_MAX_LEADS",
    "COMPARE_MIN_LEADS",
    "DEFAULT_LIST_LIMIT",
    "LEAD_INTENTS",
    "LIST_INTENTS",
    "MAX_LIST_LIMIT",
    "MAX_SUMMARIZED_RECORDS",
    "PRIORITY_RANK",
    "CopilotIntent",
    "CopilotLeadReference",
    "CopilotResponse",
    "LeadCopilotResponse",
    "LeadIntentChoice",
    "LeadListQueryPlan",
    "LeadSummary",
    "SortOption",
    "clamp_limit",
    "rank_work_today",
    "recognize_lead_intent",
    "recognize_list_intent",
    "to_reference",
    "unsupported_lead_answer",
    "unsupported_list_answer",
]


class CopilotIntent(StrEnum):
    """The closed set of questions Ask ARIE can answer. Nothing outside this
    enum reaches a query — an out-of-vocabulary value fails Pydantic
    validation on `LeadListQueryPlan.intent` before any query runs."""

    # -- list / org-scoped -----------------------------------------------
    TOP_LEADS = "top_leads"
    FILTER_LEADS = "filter_leads"
    NEEDS_RESEARCH = "needs_research"
    MISSING_DECISION_MAKER = "missing_decision_maker"
    LOW_CONFIDENCE = "low_confidence"
    COMPARE_LEADS = "compare_leads"
    FEEDBACK_SUMMARY = "feedback_summary"
    WORK_TODAY = "work_today"
    # -- single-lead -------------------------------------------------------
    LEAD_EXPLANATION = "lead_explanation"
    LEAD_MISSING_INFO = "lead_missing_info"
    LEAD_RESEARCHABILITY = "lead_researchability"
    LEAD_SCORE_DRIVERS = "lead_score_drivers"
    LEAD_IMPROVEMENT_PATH = "lead_improvement_path"


LIST_INTENTS = frozenset(
    {
        CopilotIntent.TOP_LEADS,
        CopilotIntent.FILTER_LEADS,
        CopilotIntent.NEEDS_RESEARCH,
        CopilotIntent.MISSING_DECISION_MAKER,
        CopilotIntent.LOW_CONFIDENCE,
        CopilotIntent.COMPARE_LEADS,
        CopilotIntent.FEEDBACK_SUMMARY,
        CopilotIntent.WORK_TODAY,
    }
)
"""Valid values for `LeadListQueryPlan.intent` — the model must never be
allowed to answer `POST /copilot/query` with a single-lead intent."""

LEAD_INTENTS = frozenset(
    {
        CopilotIntent.LEAD_EXPLANATION,
        CopilotIntent.LEAD_MISSING_INFO,
        CopilotIntent.LEAD_RESEARCHABILITY,
        CopilotIntent.LEAD_SCORE_DRIVERS,
        CopilotIntent.LEAD_IMPROVEMENT_PATH,
    }
)
"""Valid answers for `POST /leads/{lead_id}/copilot`'s intent classification."""


class SortOption(StrEnum):
    PRIORITY = "priority"
    SCORE = "score"
    CONFIDENCE = "confidence"
    RECENT = "recent"
    COMPANY_NAME = "company_name"


DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 50
"""Part C's hard ceiling — a model can never request more than this many
structured lead references, regardless of what the question asks for."""

COMPARE_MIN_LEADS = 2
COMPARE_MAX_LEADS = 5

MAX_SUMMARIZED_RECORDS = 20
"""Part Z / the LLM input budget: even when a query legitimately returns up
to `MAX_LIST_LIMIT` structured references, at most this many are ever
serialized into a prompt for narrative summarization."""


class LeadListQueryPlan(BaseModel):
    """The only thing a model may produce for a list question — see this
    module's own docstring. `extra="forbid"` is load-bearing: it is what
    makes `organization_id`, `raw_sql`, `tool`, or any other field the model
    might try to add fail validation rather than silently pass through.

    Country is deliberately absent: ARIE's data model has no country field
    anywhere (companies/persons/evidence all lack one), so a country filter
    here would either silently match nothing or, worse, invite the model to
    invent a value. `industries`/`company_names` stay free-form bounded
    strings because those *do* have a real backing source (evidence's
    `industry` field, and `companies.name`).
    """

    model_config = ConfigDict(extra="forbid")

    intent: CopilotIntent
    priorities: Annotated[list[CustomerPriority], Field(default_factory=list, max_length=4)]
    confidence_bands: Annotated[list[ConfidenceBand], Field(default_factory=list, max_length=3)]
    research_states: Annotated[list[ResearchStatus], Field(default_factory=list, max_length=5)]
    feedback_sentiment: FeedbackSentiment | None = None
    industries: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=100)]],
        Field(default_factory=list, max_length=5),
    ]
    company_names: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=200)]],
        Field(default_factory=list, max_length=5),
    ]
    limit: Annotated[int, Field(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_LIST_LIMIT
    sort: SortOption = SortOption.PRIORITY


class LeadIntentChoice(BaseModel):
    """The only thing a model may produce for an ambiguous single-lead
    question — just which of the five lead-scoped intents applies. The
    schema allows any `CopilotIntent` value (Pydantic has no "enum subset"
    constraint), so the caller additionally rejects an answer outside
    `LEAD_INTENTS` — the same narrowing
    `arie.intelligence.research_planning.propose_research_question` already
    applies to `target_field`."""

    model_config = ConfigDict(extra="forbid")

    intent: CopilotIntent


def clamp_limit(limit: int) -> int:
    """Defense in depth alongside `LeadListQueryPlan.limit`'s own schema
    bound — a deterministically-built plan (never passed through Pydantic)
    must be clamped the same way a model-produced one already is."""
    return max(1, min(limit, MAX_LIST_LIMIT))


@dataclass(frozen=True)
class LeadSummary:
    """The compact, per-lead record `arie.copilot_service`'s executor
    produces and every list/ranking function below consumes — Part H's
    "do not send raw receipt/evidence/provider history for list questions"
    rule, expressed as a type. Never carries evidence rows, provider-call
    history, or anything a full Decision Receipt would."""

    lead_id: UUID
    company: str | None
    contact: str | None
    priority: CustomerPriority
    next_action: NextAction
    score: float | None
    confidence: float | None
    confidence_band: ConfidenceBand | None
    short_reason: str
    industry: str | None
    research_status: ResearchStatus
    missing_information: tuple[str, ...]
    feedback_sentiment: str | None
    profile_version: int | None
    created_at_iso: str


def to_reference(summary: LeadSummary) -> CopilotLeadReference:
    return CopilotLeadReference(
        lead_id=summary.lead_id,
        company=summary.company,
        contact=summary.contact,
        priority=summary.priority,
        score=summary.score,
        why=summary.short_reason,
        next_action=summary.next_action,
    )


@dataclass(frozen=True)
class CopilotLeadReference:
    """Part U's minimal per-lead row for a list answer — company/contact
    names, priority, score, a one-line why, and the next action. Never the
    full recommendation payload `GET /leads/{lead_id}/recommendation`
    already exists for."""

    lead_id: UUID
    company: str | None
    contact: str | None
    priority: CustomerPriority
    score: float | None
    why: str
    next_action: NextAction


@dataclass(frozen=True)
class CopilotResponse:
    answer: str
    leads: tuple[CopilotLeadReference, ...]
    intent: CopilotIntent
    result_count: int
    filters_applied: dict[str, object]
    llm_used: bool


@dataclass(frozen=True)
class LeadCopilotResponse:
    """Part O-T's single-lead answer shape — one plain-language `answer`,
    which fact-family question it resolved to, and, only where the question
    is about missing information, the same field list
    `LeadRecommendation.missing_information` already exposes elsewhere."""

    lead_id: UUID
    intent: CopilotIntent
    answer: str
    missing_information: tuple[str, ...]
    researchable_field: ResearchTargetField | None


_UNSUPPORTED_LIST_ANSWER = (
    "Ask ARIE can help with your leads, targeting, and recommendations — try asking for your "
    "top leads, leads needing research, leads missing a decision-maker, or what to work on today."
)
_UNSUPPORTED_LEAD_ANSWER = (
    "Ask ARIE can explain this lead's recommendation, what's missing, whether more research "
    "would help, what affects its score, or what would need to change."
)


# ------------------------------------------------ deterministic recognizers --
#
# Part E — a small, deliberately narrow set of obvious phrasings answered
# with zero LLM calls. Anything that doesn't match falls through to LLM
# classification (or, if that's unavailable, the controlled "couldn't
# interpret that" response) — this is a cost optimization, not the only path
# to an intent.

_WORD = r"\b{}\b"


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(patterns))


_WORK_TODAY_RE = _any(r"work on today", r"what should i (work on|do)( today)?", r"today'?s priorit")
_NEEDS_RESEARCH_RE = _any(r"need(s)? (more )?research", r"needs? more (information|info|data)")
_MISSING_DM_RE = _any(
    r"missing (a )?decision.?maker", r"without (a )?decision.?maker", r"no decision.?maker"
)
_LOW_CONFIDENCE_RE = _any(r"low confidence")
_FEEDBACK_RE = _any(
    r"\bfeedback\b",
    r"disliked",
    r"marked as bad",
    r"bad recommendation",
    r"rejected recommendation",
)
_TOP_LEADS_RE = _any(r"top \d*\s*leads?", r"best leads?")

_MISSING_INFO_RE = _any(r"what'?s missing", r"what is missing", r"missing information")
_RESEARCHABILITY_RE = _any(r"research", r"more (information|info|evidence)")
_RESEARCHABILITY_HELP_RE = _any(r"help", r"worth", r"would.*change")
_SCORE_DRIVERS_RE = _any(r"affect", r"score driver", r"drove the score", r"what.*score")
_IMPROVEMENT_RE = _any(
    r"what would (need to )?change", r"become contact first", r"improve (the|this) (lead|score)"
)
_WHY_RE = _any(r"\bwhy\b")


def recognize_list_intent(question: str) -> CopilotIntent | None:
    """A deterministic shortcut for the handful of obviously-phrased list
    questions in the M7 Slice 6 brief. Returns `None` — never a guess — for
    anything else, which routes the question to LLM classification instead.
    """
    q = question.strip().lower()
    if not q:
        return None
    if _WORK_TODAY_RE.search(q):
        return CopilotIntent.WORK_TODAY
    if _NEEDS_RESEARCH_RE.search(q):
        return CopilotIntent.NEEDS_RESEARCH
    if _MISSING_DM_RE.search(q):
        return CopilotIntent.MISSING_DECISION_MAKER
    if _LOW_CONFIDENCE_RE.search(q):
        return CopilotIntent.LOW_CONFIDENCE
    if _FEEDBACK_RE.search(q):
        return CopilotIntent.FEEDBACK_SUMMARY
    if _TOP_LEADS_RE.search(q):
        return CopilotIntent.TOP_LEADS
    return None


def recognize_lead_intent(question: str) -> CopilotIntent | None:
    """The single-lead equivalent of :func:`recognize_list_intent` — Part P's
    seven example questions, matched deterministically wherever the phrasing
    is unambiguous. Order matters: more specific patterns are checked before
    the generic "why" catch-all."""
    q = question.strip().lower()
    if not q:
        return None
    if _MISSING_INFO_RE.search(q):
        return CopilotIntent.LEAD_MISSING_INFO
    if _RESEARCHABILITY_RE.search(q) and _RESEARCHABILITY_HELP_RE.search(q):
        return CopilotIntent.LEAD_RESEARCHABILITY
    if _IMPROVEMENT_RE.search(q):
        return CopilotIntent.LEAD_IMPROVEMENT_PATH
    if _SCORE_DRIVERS_RE.search(q):
        return CopilotIntent.LEAD_SCORE_DRIVERS
    if _WHY_RE.search(q):
        return CopilotIntent.LEAD_EXPLANATION
    return None


# ---------------------------------------------------------------- ranking --
#
# Part I. Deterministic — no LLM involved in deciding order, only (optionally)
# in narrating the result.

PRIORITY_RANK: dict[CustomerPriority, int] = {
    CustomerPriority.CONTACT_FIRST: 0,
    CustomerPriority.WORTH_PURSUING: 1,
    CustomerPriority.REVIEW: 2,
    CustomerPriority.SKIP: 3,
}

_ACTIONABLE_REVIEW_ACTIONS = frozenset({NextAction.HUMAN_REVIEW, NextAction.FIND_DECISION_MAKER})


def rank_work_today(summaries: list[LeadSummary]) -> list[LeadSummary]:
    """ "What should I work on today?" — Contact First (highest score/
    confidence first), then Worth Pursuing, then a Review that actually needs
    a human action, in that order. A plain Review with no actionable next
    step, and every Skip, is excluded — never simply "sorted by created_at"."""

    def included(summary: LeadSummary) -> bool:
        if summary.priority is CustomerPriority.SKIP:
            return False
        if summary.priority is CustomerPriority.REVIEW:
            return summary.next_action in _ACTIONABLE_REVIEW_ACTIONS
        return True

    def sort_key(summary: LeadSummary) -> tuple[int, float, float]:
        return (
            PRIORITY_RANK[summary.priority],
            -(summary.score if summary.score is not None else 0.0),
            -(summary.confidence if summary.confidence is not None else 0.0),
        )

    return sorted((s for s in summaries if included(s)), key=sort_key)


def unsupported_list_answer() -> str:
    return _UNSUPPORTED_LIST_ANSWER


def unsupported_lead_answer() -> str:
    return _UNSUPPORTED_LEAD_ANSWER
