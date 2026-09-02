"""Projecting one promoted, decided lead into the customer-facing
`Opportunity` — Discovery Pivot Phases 8, 10, 11; extended by Opportunity
Activation Parts 3-13 with real buyer identification and next-action logic
that never claims a contact channel ARIE doesn't actually have.

No parallel scoring system. `arie.recommendations.build_recommendation` is
the exact same call `GET /leads/{lead_id}/recommendation` makes; this module
adds three things on top of it, all bounded and all reusing existing
machinery: one selective-research attempt (`arie.research_acquisition`,
Slice 5) when the receipt itself says a fact could still change the
decision, a real buyer search (`arie.discovery.buyer_search`) gated on the
recommendation actually being worth pursuing, and a discovery-specific
next-action projection that only ever claims a contact channel the buyer
search actually verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import psycopg

from arie.api.receipt import build_receipt
from arie.discovery.buyer_search import (
    BuyerSearchFn,
    BuyerSearchOutcome,
    buyer_search_eligible,
    execute_buyer_search,
)
from arie.discovery.models import (
    BuyerSignal,
    DiscoveryCandidate,
    DiscoverySuitability,
    EmailStatus,
    Opportunity,
    OpportunityNextAction,
)
from arie.discovery.suitability import assess_suitability
from arie.evidence.store import PostgresEvidenceStore
from arie.ledger.store import PostgresCostLedger
from arie.live.outcome_cache import ProviderOutcomeGuard
from arie.recommendations import (
    CustomerPriority,
    DecisionSignal,
    build_recommendation,
)
from arie.research import ResearchTargetField
from arie.research_acquisition import build_research_plan, execute_research

__all__ = ["OpportunityBuildResult", "build_opportunity"]

_PERSON_FIELDS = frozenset(
    {ResearchTargetField.TITLE_SENIORITY, ResearchTargetField.TITLE_FUNCTION}
)
_USABLE_EMAIL = frozenset({EmailStatus.VERIFIED, EmailStatus.LIKELY})

_PRIORITY_STRENGTH: dict[CustomerPriority, int] = {
    CustomerPriority.CONTACT_FIRST: 3,
    CustomerPriority.WORTH_PURSUING: 2,
    CustomerPriority.REVIEW: 1,
    CustomerPriority.SKIP: 0,
}

_SUITABILITY_CEILING: dict[DiscoverySuitability, CustomerPriority | None] = {
    DiscoverySuitability.SUPPORTED: None,
    DiscoverySuitability.UNCERTAIN: CustomerPriority.REVIEW,
    DiscoverySuitability.CONTRADICTED: CustomerPriority.SKIP,
}
"""Discovery Quality Fix 2/3: what real public evidence permits, as a ceiling
on what the scorer produced.

The scorer is untouched — it still runs exactly as it does for every other
lead, and in `simulated` provider mode it is still fed simulated
firmographics. What changes is that those simulated firmographics can no
longer *outrank* a real reading of the company's own website. `UNCERTAIN`
caps at `REVIEW` specifically because a discovery-origin company ARIE could
not verify has nothing but simulated evidence arguing for it: "contact this
company first" is a claim the run cannot support, while "worth a look" is.

Ceilings only, never lifts: a company the scorer disliked stays disliked no
matter how good its website looks."""


def _apply_ceiling(
    priority: CustomerPriority, suitability: DiscoverySuitability
) -> CustomerPriority:
    ceiling = _SUITABILITY_CEILING[suitability]
    if ceiling is None:
        return priority
    if _PRIORITY_STRENGTH[priority] <= _PRIORITY_STRENGTH[ceiling]:
        return priority
    return ceiling


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
    """Whether the *selective research* step above happened to target a
    person field — distinct from `buyer_search_*` below, which is the real
    Hunter Domain Search step."""
    research_cost_usd: Decimal
    buyer_search_eligible: bool
    buyer_search_performed: bool
    buyer_found: bool
    buyer_email_found: bool


def _simulated_buyer_signal(
    conn: psycopg.Connection,
    evidence_store: PostgresEvidenceStore,
    *,
    organization_id: UUID,
    person_id: UUID | None,
    now: datetime,
) -> BuyerSignal | None:
    """The existing pipeline's own simulated role signal — never a name, see
    `arie.discovery.promotion`'s module docstring for why that placeholder
    identity's display name must never surface as a customer-facing buyer."""
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


def _next_action(priority: CustomerPriority, buyer: BuyerSignal | None) -> OpportunityNextAction:
    """Part 13's deterministic projection. Never claims a contact channel
    ARIE doesn't have: `CONTACT_NOW`/`EMAIL_FIRST` require a *named* buyer
    with a *usable* (verified or likely) email — a known role with no name,
    or a name with no usable email, both fall through to an honest
    intermediate state instead."""
    if priority is CustomerPriority.SKIP:
        return OpportunityNextAction.SKIP
    if priority is CustomerPriority.REVIEW:
        return OpportunityNextAction.RESEARCH_MORE
    # CONTACT_FIRST or WORTH_PURSUING.
    if buyer is None or not buyer.name_known:
        return OpportunityNextAction.FIND_DECISION_MAKER
    if buyer.email is not None and buyer.email_status in _USABLE_EMAIL:
        return (
            OpportunityNextAction.CONTACT_NOW
            if priority is CustomerPriority.CONTACT_FIRST
            else OpportunityNextAction.EMAIL_FIRST
        )
    return OpportunityNextAction.VERIFY_CONTACT_METHOD


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
    preferred_seniorities: tuple[str, ...] = (),
    preferred_functions: tuple[str, ...] = (),
    seller_offering: str = "",
    market: str | None = None,
    outcome_guard: ProviderOutcomeGuard | None = None,
    buyer_search_fn: BuyerSearchFn = execute_buyer_search,
) -> OpportunityBuildResult:
    receipt = build_receipt(conn, ledger, lead_id, organization_id=organization_id)
    if receipt is None:
        return OpportunityBuildResult(
            opportunity=None,
            research_attempted=False,
            research_performed=False,
            was_buyer_lookup=False,
            research_cost_usd=Decimal(0),
            buyer_search_eligible=False,
            buyer_search_performed=False,
            buyer_found=False,
            buyer_email_found=False,
        )

    recommendation = build_recommendation(lead_id, DecisionSignal.from_receipt(receipt))

    # The gate runs before anything is spent on this lead's buyer, and before
    # any priority is reported — see `_SUITABILITY_CEILING`.
    assessment = assess_suitability(
        domain=candidate.domain,
        verification_status=candidate.verification_status,
        verified_facts=candidate.verified_facts,
        seller_offering=seller_offering,
        market=market,
    )
    priority = _apply_ceiling(recommendation.priority, assessment.suitability)
    downgraded = priority is not recommendation.priority

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

    person_id = _person_id(conn, lead_id)
    buyer = _simulated_buyer_signal(
        conn, evidence_store, organization_id=organization_id, person_id=person_id, now=now
    )
    alternate_buyers: list[BuyerSignal] = []

    outcome: BuyerSearchOutcome | None = None
    # Discovery Quality Fix 8: the gate refuses here, at the call site,
    # rather than trusting the provider adapter to refuse for us. A company
    # real evidence contradicted — or never confirmed — cannot spend a buyer
    # lookup however well simulated firmographics scored it, and that holds
    # for *any* injected buyer-search function, not only the Hunter one that
    # happens to re-check the same gate.
    if person_id is not None and buyer_search_eligible(priority=priority, existing_buyer_name=None):
        outcome = buyer_search_fn(
            conn,
            ledger,
            evidence_store,
            organization_id=organization_id,
            lead_id=lead_id,
            person_id=person_id,
            domain=candidate.domain,
            priority=priority,
            preferred_seniorities=preferred_seniorities,
            preferred_functions=preferred_functions,
            now=now,
        )
        if outcome.best is not None:
            buyer = BuyerSignal.from_candidate(outcome.best)
            alternate_buyers = [BuyerSignal.from_candidate(c) for c in outcome.alternates]
        # A gated-out company keeps whatever role-only signal the ordinary
        # pipeline produced and nothing more: no lookup, and no named buyer
        # implying a confidence in the *company* that the evidence does not
        # support.

    next_action = _next_action(priority, buyer)
    short_reason = (
        f"{recommendation.short_reason} {assessment.reason}".strip()
        if downgraded
        else recommendation.short_reason
    )

    opportunity = Opportunity(
        candidate_id=candidate.candidate_id,
        lead_id=lead_id,
        company_name=candidate.company_name,
        domain=candidate.domain,
        priority=str(priority),
        next_action=str(next_action),
        score=recommendation.score,
        confidence=recommendation.confidence,
        short_reason=short_reason,
        key_evidence=recommendation.key_evidence,
        missing_information=recommendation.missing_information,
        buyer=buyer,
        alternate_buyers=alternate_buyers,
        research_performed=research_performed,
        discovery_source=candidate.source_provider,
        source_url=candidate.source_url,
        search_query=candidate.search_query,
        verification_status=candidate.verification_status,
        verified_facts=candidate.verified_facts,
        website_verified_at=candidate.website_verified_at,
        suitability=assessment.suitability,
        suitability_reason=assessment.reason,
    )
    return OpportunityBuildResult(
        opportunity=opportunity,
        research_attempted=research_attempted,
        research_performed=research_performed,
        was_buyer_lookup=was_buyer_lookup,
        research_cost_usd=research_cost,
        buyer_search_eligible=priority
        in (CustomerPriority.CONTACT_FIRST, CustomerPriority.WORTH_PURSUING),
        buyer_search_performed=outcome.provider_called if outcome is not None else False,
        buyer_found=buyer is not None and buyer.name_known,
        buyer_email_found=buyer is not None and buyer.name_known and buyer.email is not None,
    )


def _person_id(conn: psycopg.Connection, lead_id: UUID) -> UUID | None:
    with conn.cursor() as cur:
        cur.execute("SELECT person_id FROM leads WHERE lead_id = %(lead_id)s", {"lead_id": lead_id})
        row = cur.fetchone()
    return row[0] if row is not None else None
