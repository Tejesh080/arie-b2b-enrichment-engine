"""Projecting one promoted, decided lead into the customer-facing
`Opportunity` — Discovery Pivot Phases 8, 10, 11.

No parallel scoring system. `arie.recommendations.build_recommendation` is
the exact same call `GET /leads/{lead_id}/recommendation` makes; this module
adds exactly two things on top of it, both bounded and both reusing existing
machinery: one selective-research attempt (`arie.research_acquisition`,
already Slice 5) when the receipt itself says a fact could still change the
decision, and a read of whatever contact-role evidence exists so far — never
a fabricated buyer name, see `arie.discovery.models.BuyerSignal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import psycopg

from arie.api.receipt import build_receipt
from arie.discovery.models import BuyerSignal, DiscoveryCandidate, Opportunity
from arie.evidence.store import PostgresEvidenceStore
from arie.ledger.store import PostgresCostLedger
from arie.live.outcome_cache import ProviderOutcomeGuard
from arie.recommendations import (
    DecisionSignal,
    LeadRecommendation,
    NextAction,
    build_recommendation,
)
from arie.research import ResearchTargetField
from arie.research_acquisition import build_research_plan, execute_research

__all__ = ["OpportunityBuildResult", "build_opportunity"]

_PERSON_FIELDS = frozenset(
    {ResearchTargetField.TITLE_SENIORITY, ResearchTargetField.TITLE_FUNCTION}
)


@dataclass(frozen=True)
class OpportunityBuildResult:
    opportunity: Opportunity | None
    """`None` only if the promoted lead has vanished — never expected in
    practice, since promotion just created it in the same run."""
    research_attempted: bool
    research_performed: bool
    """Whether a provider was actually called (as opposed to the plan being
    refused by budget/materiality) — the funnel's `research_calls`."""
    was_buyer_lookup: bool
    research_cost_usd: Decimal


def _read_buyer_signal(
    conn: psycopg.Connection,
    evidence_store: PostgresEvidenceStore,
    *,
    organization_id: UUID,
    person_id: UUID | None,
    now: datetime,
) -> BuyerSignal | None:
    if person_id is None:
        return None
    seniority = evidence_store.get_fresh(
        "person",
        person_id,
        str(ResearchTargetField.TITLE_SENIORITY),
        organization_id=organization_id,
        now=now,
    )
    function = evidence_store.get_fresh(
        "person",
        person_id,
        str(ResearchTargetField.TITLE_FUNCTION),
        organization_id=organization_id,
        now=now,
    )
    if seniority is None and function is None:
        return None
    source = seniority or function
    confidence = source.confidence if source is not None else None
    return BuyerSignal(
        seniority=str(seniority.value) if seniority is not None else None,
        function=str(function.value) if function is not None else None,
        name_known=False,
        source=source.source if source is not None else None,
        confidence=confidence,
    )


def _effective_next_action(
    recommendation: LeadRecommendation, buyer: BuyerSignal | None
) -> NextAction:
    """`recommendation.next_action` is frozen at the receipt's own decision
    time. If research performed *after* that (this module's own selective
    research step) found a contact's seniority, "find a decision maker" is
    stale — the customer-facing next action should say so, without rewriting
    the immutable receipt itself."""
    if (
        recommendation.next_action is NextAction.FIND_DECISION_MAKER
        and buyer is not None
        and buyer.seniority
    ):
        return NextAction.EMAIL_FIRST
    return recommendation.next_action


def build_opportunity(
    conn: psycopg.Connection,
    ledger: PostgresCostLedger,
    evidence_store: PostgresEvidenceStore,
    *,
    organization_id: UUID,
    candidate: DiscoveryCandidate,
    lead_id: UUID,
    execution_mode: str,
    now: datetime,
    outcome_guard: ProviderOutcomeGuard | None = None,
) -> OpportunityBuildResult:
    receipt = build_receipt(conn, ledger, lead_id, organization_id=organization_id)
    if receipt is None:
        return OpportunityBuildResult(
            opportunity=None,
            research_attempted=False,
            research_performed=False,
            was_buyer_lookup=False,
            research_cost_usd=Decimal(0),
        )

    recommendation = build_recommendation(lead_id, DecisionSignal.from_receipt(receipt))

    research_attempted = False
    research_performed = False
    was_buyer_lookup = False
    research_cost = Decimal(0)

    if receipt.status == "decided":
        plan = build_research_plan(
            conn,
            ledger,
            organization_id=organization_id,
            lead_id=lead_id,
            execution_mode=execution_mode,
            llm=None,  # deterministic top pick — Phase 5's cost discipline: no LLM spend just to rank one candidate
            now=now,
            outcome_guard=outcome_guard,
        )
        if plan is not None and plan.approved and plan.target_field is not None:
            research_attempted = True
            was_buyer_lookup = plan.target_field in _PERSON_FIELDS
            execution = execute_research(
                conn,
                ledger,
                evidence_store,
                organization_id=organization_id,
                lead_id=lead_id,
                target_field=plan.target_field,
                execution_mode=execution_mode,
                now=now,
                outcome_guard=outcome_guard,
            )
            if execution is not None and execution.approved:
                research_performed = True
                research_cost = execution.cost_usd

    buyer = _read_buyer_signal(
        conn,
        evidence_store,
        organization_id=organization_id,
        person_id=_person_id(conn, lead_id),
        now=now,
    )

    opportunity = Opportunity(
        candidate_id=candidate.candidate_id,
        lead_id=lead_id,
        company_name=candidate.company_name,
        domain=candidate.domain,
        priority=str(recommendation.priority),
        next_action=str(_effective_next_action(recommendation, buyer)),
        score=recommendation.score,
        confidence=recommendation.confidence,
        short_reason=recommendation.short_reason,
        key_evidence=recommendation.key_evidence,
        missing_information=recommendation.missing_information,
        buyer=buyer,
        research_performed=research_performed,
        discovery_source=candidate.source_provider,
        source_url=candidate.source_url,
        search_query=candidate.search_query,
    )
    return OpportunityBuildResult(
        opportunity=opportunity,
        research_attempted=research_attempted,
        research_performed=research_performed,
        was_buyer_lookup=was_buyer_lookup,
        research_cost_usd=research_cost,
    )


def _person_id(conn: psycopg.Connection, lead_id: UUID) -> UUID | None:
    with conn.cursor() as cur:
        cur.execute("SELECT person_id FROM leads WHERE lead_id = %(lead_id)s", {"lead_id": lead_id})
        row = cur.fetchone()
    return row[0] if row is not None else None
