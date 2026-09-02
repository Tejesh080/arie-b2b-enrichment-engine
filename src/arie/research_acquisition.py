"""Wires `arie.research`'s pure materiality/authorization rules to the real
database, the simulated provider catalogue, and (only when more than one
field is material) the LLM planner. M7 Slice 5, Parts C/G/H/I/K/R.

**No second acquisition pipeline.** Execution (:func:`execute_research`)
calls the exact same primitives the ordinary ingestion pipeline uses —
`arie.providers.simulated.SimulatedProvider.fetch`, `arie.evidence.store
.PostgresEvidenceStore`, `arie.ledger.store.PostgresCostLedger
.record_provider_call` — just for one field instead of running
`arie.jobs.handlers`' whole-lead VoI policy loop, which assumes a lead at
`LeadStatus.NEW` and cannot be re-entered once a lead has already reached
`DECISION`. A researched lead's frozen corpus row (if it had one) is not
replayed either, for the identical reason: `arie.jobs.handlers.build_runtime`
regenerates the whole evaluation dataset, which is a benchmark-scale
operation with no place in a synchronous API request. Execution instead
always synthesizes this lead's evidence deterministically
(`arie.providers.synthetic.synthesize_corpus_lead`, the exact fallback
`arie.jobs.handlers._run_simulated_acquisition` already uses for any
identity outside the frozen corpus) and calls the same
`SimulatedProvider.fetch` a normal acquisition run would have. See "Known
limitations" in the M7 Slice 5 handoff for what this trades away.

**The immutable Decision Receipt is never touched.** `decision_receipts` has
a unique index on `lead_id` (migration 0008) precisely because a receipt is
meant to be write-once history — `arie.api.receipt`'s whole module docstring
is about protecting that. Research adds new rows to the shared, mutable
`evidence` table (exactly what that table is for) and this module recomputes
a *preview* score from current evidence for the API response, but the lead's
original decision, status, and receipt are never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.api.receipt import build_receipt
from arie.billing.plans import is_live_provider_feature_allowed
from arie.config import APOLLO_PERSON, HUNTER, LIVE_PROVIDER
from arie.core.types import Entity, EntityType, Evidence, ProviderStatus
from arie.evidence.store import PostgresEvidenceStore
from arie.evidence.ttl_policy import ttl_for_field
from arie.icp_profiles import get_profile_by_version, resolve_scoring_config
from arie.intelligence.research_planning import propose_research_question
from arie.ledger.store import PostgresCostLedger
from arie.limits import get_usage_against_limits
from arie.live.outcome_cache import ProviderOutcomeGuard
from arie.live.provider_availability import resolve_organization_providers
from arie.llm.service import LLMService
from arie.organizations import SIMULATED
from arie.providers.apollo_contract import APOLLO_PROVIDER_NAME
from arie.providers.catalog import BY_NAME as SIMULATED_BY_NAME
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.providers.simulated import build_from_leads
from arie.providers.synthetic import synthesize_corpus_lead
from arie.research import (
    CANDIDATE_LIVE_PROVIDERS,
    CANDIDATE_SIMULATED_PROVIDERS,
    DETERMINISTIC_QUESTIONS,
    Materiality,
    MaterialityAnalysis,
    ResearchAuthorizationContext,
    ResearchReasonCode,
    ResearchTargetField,
    analyze_materiality,
    authorize_research,
    select_research_target,
)
from arie.scoring.engine import score_evidence
from arie.scoring.rules import use_scoring_config

__all__ = [
    "ResearchExecutionResult",
    "ResearchPlanResult",
    "ResearchPreview",
    "build_research_plan",
    "execute_research",
]

_LIVE_COST_USD: dict[str, Decimal] = {
    ABSTRACT_PROVIDER_NAME: Decimal(str(LIVE_PROVIDER.cost_usd_per_call)),
    HUNTER_PROVIDER_NAME: Decimal(str(HUNTER.cost_usd_per_success)),
    APOLLO_PROVIDER_NAME: Decimal(str(APOLLO_PERSON.cost_usd_per_success)),
}
"""Modelled prices from the same config singletons `arie.live.*` reads —
never a second, independently-typed copy of these figures."""

_ENTITY_TYPE_FOR_FIELD: dict[ResearchTargetField, EntityType] = {
    ResearchTargetField.EMPLOYEE_COUNT: "company",
    ResearchTargetField.INDUSTRY: "company",
    ResearchTargetField.TITLE_SENIORITY: "person",
    ResearchTargetField.TITLE_FUNCTION: "person",
}


@dataclass(frozen=True)
class _Identity:
    company_id: UUID | None
    person_id: UUID | None
    canonical_domain: str | None
    canonical_email: str | None
    full_name: str | None
    company_name: str | None


_SELECT_IDENTITY = """
    SELECT l.company_id, l.person_id, c.canonical_domain, p.canonical_email,
           p.full_name, c.name AS company_name
    FROM leads l
    LEFT JOIN companies c ON c.company_id = l.company_id
    LEFT JOIN persons p ON p.person_id = l.person_id
    WHERE l.lead_id = %(lead_id)s
"""


def _load_identity(conn: psycopg.Connection, lead_id: UUID) -> _Identity:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_IDENTITY, {"lead_id": lead_id})
        row = cur.fetchone()
    assert row is not None  # caller already proved this lead exists via build_receipt
    return _Identity(
        company_id=row["company_id"],
        person_id=row["person_id"],
        canonical_domain=row["canonical_domain"],
        canonical_email=row["canonical_email"],
        full_name=row["full_name"],
        company_name=row["company_name"],
    )


_SELECT_CALLED_PROVIDERS = (
    "SELECT DISTINCT provider FROM provider_calls WHERE lead_id = %(lead_id)s"
)


def _already_called_providers(conn: psycopg.Connection, lead_id: UUID) -> frozenset[str]:
    with conn.cursor() as cur:
        cur.execute(_SELECT_CALLED_PROVIDERS, {"lead_id": lead_id})
        return frozenset(row[0] for row in cur.fetchall())


def _lead_budget_cap(conn: psycopg.Connection, lead_id: UUID) -> Decimal:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT budget_usd_cap FROM leads WHERE lead_id = %(lead_id)s", {"lead_id": lead_id}
        )
        row = cur.fetchone()
    assert row is not None
    return Decimal(str(row[0]))


def _suppressed_providers(
    outcome_guard: ProviderOutcomeGuard | None,
    candidates: tuple[str, ...],
    *,
    entity_type: EntityType,
    entity_id: UUID | None,
    organization_id: UUID,
) -> frozenset[str]:
    """Which of `candidates` the existing M5 outcome-cache guard
    (`arie.live.outcome_cache.ProviderOutcomeGuard`) currently recognises as a
    recent, still-suppressed settled miss or uncertain (timeout/transport)
    outcome for this exact entity — the read this module's own "Known
    limitations" note flagged as missing: `authorize_research` has always
    accepted a `suppressed_providers` set, but nothing populated it. No new
    suppression *behavior* is introduced here; this only wires the read that
    `arie.jobs.handlers`' live acquisition path already performs for the
    identical reason (see `_acquire_live_evidence`).

    `()` (no suppression) whenever there is nothing to check against — no
    guard was supplied (simulated mode never needs one), no candidates, or no
    resolved entity yet.
    """
    if outcome_guard is None or not candidates or entity_id is None:
        return frozenset()
    return frozenset(
        provider
        for provider in candidates
        if outcome_guard.recent_miss(
            provider, entity_type, entity_id, organization_id=organization_id
        )
        is not None
        or outcome_guard.recent_uncertain_outcome(
            provider, entity_type, entity_id, organization_id=organization_id
        )
        is not None
    )


def _authorization_inputs(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    lead_id: UUID,
    target_field: ResearchTargetField,
    execution_mode: str,
    outcome_guard: ProviderOutcomeGuard | None = None,
) -> tuple[tuple[str, ...], dict[str, str], Decimal, bool, frozenset[str]]:
    """Everything `ResearchAuthorizationContext` needs beyond materiality and
    spend — one place both `build_research_plan` and `execute_research` read
    it from, so a plan and its later execution can never disagree about what
    was available. Returns `(candidates, unavailable, estimated_cost,
    entitled_live, suppressed)`.

    Provider *suppression* (`arie.live.outcome_cache`, live mode only) is
    resolved here via `outcome_guard` when the caller supplies one —
    `simulated` mode never checks it (there is nothing for it to answer: the
    guard only ever sees rows a *live* adapter wrote). Pass `None` (the
    default) to keep the pre-fix behaviour of an always-empty suppressed set,
    which every caller that hasn't been updated yet still gets.
    """
    if execution_mode == SIMULATED:
        called = _already_called_providers(conn, lead_id)
        candidates = (
            tuple(p for p in CANDIDATE_SIMULATED_PROVIDERS[target_field] if p not in called)
            or CANDIDATE_SIMULATED_PROVIDERS[target_field]
        )
        unavailable: dict[str, str] = {}
        estimated_cost = (
            Decimal(str(SIMULATED_BY_NAME[candidates[0]].base_cost_usd))
            if candidates
            else Decimal(0)
        )
        entitled_live = True  # not applicable in simulated mode; authorize_research ignores it
        return candidates, unavailable, estimated_cost, entitled_live, frozenset()

    candidates = CANDIDATE_LIVE_PROVIDERS[target_field]
    adapters, unavailable = resolve_organization_providers(
        conn,
        organization_id=organization_id,
        execution_mode=execution_mode,
        provider_names=candidates,
    )
    for adapter in adapters:
        # `EnrichmentProvider` declares no `close()` (arie.jobs.handlers'
        # own live builder uses the identical defensive getattr, for the
        # identical reason: simulated providers and test fakes share the
        # same minimal Protocol shape).
        close = getattr(adapter, "close", None)
        if close is not None:
            close()
    estimated_cost = _LIVE_COST_USD.get(candidates[0], Decimal(0)) if candidates else Decimal(0)
    entitled_live = is_live_provider_feature_allowed(conn, organization_id=organization_id)

    identity = _load_identity(conn, lead_id)
    entity_type = _ENTITY_TYPE_FOR_FIELD[target_field]
    entity_id = identity.company_id if entity_type == "company" else identity.person_id
    suppressed = _suppressed_providers(
        outcome_guard,
        candidates,
        entity_type=entity_type,
        entity_id=entity_id,
        organization_id=organization_id,
    )

    return candidates, unavailable, estimated_cost, entitled_live, suppressed


def _materiality_analysis(
    conn: psycopg.Connection, *, organization_id: UUID, lead_id: UUID, ledger: PostgresCostLedger
) -> tuple[MaterialityAnalysis, str | None] | None:
    """`(analysis, icp_profile_name)` for a decided lead, or `None` if the
    lead doesn't exist or hasn't reached a decision yet."""
    receipt = build_receipt(conn, ledger, lead_id, organization_id=organization_id)
    if receipt is None or receipt.status != "decided" or receipt.score is None:
        return None
    scoring_config = resolve_scoring_config(conn, organization_id=organization_id)
    known = frozenset(item.field for item in receipt.evidence.items)
    analysis = analyze_materiality(
        score_value=receipt.score.value,
        threshold_qualify=receipt.score.threshold_qualify,
        threshold_reject=receipt.score.threshold_reject,
        bounds_lower=receipt.score.bounds.lower,
        bounds_upper=receipt.score.bounds.upper,
        known_fields=known,
        field_ceilings=scoring_config.max_field_points,
    )
    profile_name: str | None = None
    if receipt.versions is not None and receipt.versions.icp_profile_version is not None:
        profile = get_profile_by_version(
            conn, organization_id=organization_id, version=receipt.versions.icp_profile_version
        )
        profile_name = profile.name if profile is not None else None
    return analysis, profile_name


@dataclass(frozen=True)
class ResearchPlanResult:
    """`POST /leads/{lead_id}/research-plan`'s domain shape — a proposal,
    never an action. See `build_research_plan`."""

    lead_exists: bool
    target_field: ResearchTargetField | None
    question: str | None
    rationale: str | None
    materiality: Materiality | None
    decision_already_clear: bool
    candidate_sources: tuple[str, ...]
    estimated_cost_usd: Decimal | None
    reason_code: ResearchReasonCode
    detail: str
    approved: bool
    llm_used: bool


_NOT_DECIDED = ResearchPlanResult(
    lead_exists=True,
    target_field=None,
    question=None,
    rationale=None,
    materiality=None,
    decision_already_clear=False,
    candidate_sources=(),
    estimated_cost_usd=None,
    reason_code=ResearchReasonCode.NO_RESEARCH_NEEDED,
    detail="ARIE hasn't finished evaluating this lead yet.",
    approved=False,
    llm_used=False,
)

_NO_USEFUL_QUESTION = ResearchPlanResult(
    lead_exists=True,
    target_field=None,
    question=None,
    rationale=None,
    materiality=None,
    decision_already_clear=False,
    candidate_sources=(),
    estimated_cost_usd=None,
    reason_code=ResearchReasonCode.NO_USEFUL_QUESTION,
    detail="No supported field could change this recommendation right now.",
    approved=False,
    llm_used=False,
)


def build_research_plan(
    conn: psycopg.Connection,
    ledger: PostgresCostLedger,
    *,
    organization_id: UUID,
    lead_id: UUID,
    execution_mode: str,
    llm: LLMService | None,
    now: datetime,
    outcome_guard: ProviderOutcomeGuard | None = None,
) -> ResearchPlanResult | None:
    """The single best next fact for this lead, or a reason there isn't one.

    Never executes a provider call — see `execute_research` for that. `llm`
    is consulted at most once, and only when more than one field is material
    (Part Y's cost-discipline rule); pass `None` to force the deterministic
    top pick regardless (still correct, just less explained).

    Returns `None` only when `lead_id` doesn't exist for `organization_id` —
    the caller 404s, matching every other lead-scoped endpoint.
    """
    materiality = _materiality_analysis(
        conn, organization_id=organization_id, lead_id=lead_id, ledger=ledger
    )
    if materiality is None:
        exists = build_receipt(conn, ledger, lead_id, organization_id=organization_id) is not None
        if not exists:
            return None
        return _NOT_DECIDED
    analysis, profile_name = materiality

    if analysis.decision_already_clear:
        return ResearchPlanResult(
            lead_exists=True,
            target_field=None,
            question=None,
            rationale=None,
            materiality=None,
            decision_already_clear=True,
            candidate_sources=(),
            estimated_cost_usd=None,
            reason_code=ResearchReasonCode.DECISION_ALREADY_CLEAR,
            detail="Given everything already known, no additional fact could change this recommendation.",
            approved=False,
            llm_used=False,
        )

    material = analysis.material_fields
    if not material:
        return _NO_USEFUL_QUESTION

    llm_used = False
    if len(material) == 1:
        target = material[0].field
        question = DETERMINISTIC_QUESTIONS[target]
        rationale = "The only remaining field that could change this recommendation."
    elif llm is not None:
        proposal = propose_research_question(
            llm,
            organization_id=organization_id,
            lead_id=lead_id,
            material_fields=material,
            profile_name=profile_name or "your targeting profile",
            recommendation_priority="review",
            now=now,
        )
        target, question, rationale = proposal.target_field, proposal.question, proposal.rationale
        llm_used = True
    else:
        deterministic_target = select_research_target(analysis)
        assert deterministic_target is not None
        target = deterministic_target
        question = DETERMINISTIC_QUESTIONS[target]
        rationale = "The largest-impact missing field."

    candidates, unavailable, cost, entitled_live, suppressed = _authorization_inputs(
        conn,
        organization_id=organization_id,
        lead_id=lead_id,
        target_field=target,
        execution_mode=execution_mode,
        outcome_guard=outcome_guard,
    )
    ledger_cost = ledger.lead_cost(lead_id)
    assert ledger_cost is not None
    usage = get_usage_against_limits(conn, organization_id=organization_id, now=now)

    ctx = ResearchAuthorizationContext(
        target_field=target,
        materiality=Materiality.MATERIAL,
        decision_already_clear=False,
        candidate_providers=candidates,
        unavailable_providers=unavailable,
        suppressed_providers=suppressed,
        execution_mode=execution_mode,
        entitled_live=entitled_live,
        estimated_cost_usd=cost,
        lead_spent_usd=ledger_cost.total_cost_usd,
        lead_budget_cap_usd=_lead_budget_cap(conn, lead_id),
        org_modeled_spend_remaining_usd=Decimal(str(usage.modeled_spend_remaining_usd)),
    )
    decision = authorize_research(ctx)

    return ResearchPlanResult(
        lead_exists=True,
        target_field=target,
        question=question,
        rationale=rationale,
        materiality=Materiality.MATERIAL,
        decision_already_clear=False,
        candidate_sources=candidates,
        estimated_cost_usd=cost,
        reason_code=decision.reason_code,
        detail=decision.detail,
        approved=decision.approved,
        llm_used=llm_used,
    )


@dataclass(frozen=True)
class ResearchPreview:
    """A recomputed score from current evidence — informational only. Never
    a claim that the lead's persisted decision changed; see this module's
    own docstring for why `decision_receipts` is never rewritten."""

    score: float
    bounds_lower: float
    bounds_upper: float
    likely_outcome: str
    """`"qualifies"`, `"borderline"`, or `"rejects"` against this
    organization's current thresholds — deliberately not a `CustomerPriority`,
    which also needs a calibrated confidence this preview does not compute."""


@dataclass(frozen=True)
class ResearchExecutionResult:
    approved: bool
    reason_code: ResearchReasonCode
    detail: str
    target_field: ResearchTargetField | None
    provider: str | None
    found_value: Any | None
    cost_usd: Decimal
    preview: ResearchPreview | None


def _preview(
    conn: psycopg.Connection,
    evidence_store: PostgresEvidenceStore,
    *,
    organization_id: UUID,
    identity: _Identity,
    now: datetime,
) -> ResearchPreview:
    evidence: list[Evidence] = []
    if identity.company_id is not None:
        evidence.extend(
            evidence_store.get_all_fresh(
                "company", identity.company_id, organization_id=organization_id, now=now
            )
        )
    if identity.person_id is not None:
        evidence.extend(
            evidence_store.get_all_fresh(
                "person", identity.person_id, organization_id=organization_id, now=now
            )
        )
    scoring_config = resolve_scoring_config(conn, organization_id=organization_id)
    with use_scoring_config(scoring_config):
        result = score_evidence(evidence, now)
        qualify, reject = scoring_config.qualify_threshold, scoring_config.reject_threshold
    if result.bounds.lower >= qualify:
        outcome = "qualifies"
    elif result.bounds.upper < reject:
        outcome = "rejects"
    else:
        outcome = "borderline"
    return ResearchPreview(
        score=result.total_score,
        bounds_lower=result.bounds.lower,
        bounds_upper=result.bounds.upper,
        likely_outcome=outcome,
    )


def execute_research(
    conn: psycopg.Connection,
    ledger: PostgresCostLedger,
    evidence_store: PostgresEvidenceStore,
    *,
    organization_id: UUID,
    lead_id: UUID,
    target_field: ResearchTargetField,
    execution_mode: str,
    now: datetime,
    outcome_guard: ProviderOutcomeGuard | None = None,
) -> ResearchExecutionResult | None:
    """Authorize and, if approved, perform one simulated provider call for
    `target_field`. Recomputes authorization from scratch — never trusts a
    client-supplied provider, cost, or approval; only `target_field` comes
    from the caller, and it is validated against this lead's own current
    materiality exactly as `build_research_plan` would.

    Returns `None` only when `lead_id` doesn't exist for `organization_id`.
    Live execution modes always come back refused with
    `EXECUTION_MODE_BLOCKED` (or a more specific reason first) — see
    `arie.research.authorize_research`'s own ordering.
    """
    materiality = _materiality_analysis(
        conn, organization_id=organization_id, lead_id=lead_id, ledger=ledger
    )
    if materiality is None:
        exists = build_receipt(conn, ledger, lead_id, organization_id=organization_id) is not None
        if not exists:
            return None
        return ResearchExecutionResult(
            approved=False,
            reason_code=ResearchReasonCode.NO_RESEARCH_NEEDED,
            detail=_NOT_DECIDED.detail,
            target_field=target_field,
            provider=None,
            found_value=None,
            cost_usd=Decimal(0),
            preview=None,
        )
    analysis, _ = materiality
    field_state = next(f for f in analysis.fields if f.field is target_field)

    candidates, unavailable, cost, entitled_live, suppressed = _authorization_inputs(
        conn,
        organization_id=organization_id,
        lead_id=lead_id,
        target_field=target_field,
        execution_mode=execution_mode,
        outcome_guard=outcome_guard,
    )
    ledger_cost = ledger.lead_cost(lead_id)
    assert ledger_cost is not None
    usage = get_usage_against_limits(conn, organization_id=organization_id, now=now)

    ctx = ResearchAuthorizationContext(
        target_field=target_field,
        materiality=field_state.materiality,
        decision_already_clear=analysis.decision_already_clear,
        candidate_providers=candidates,
        unavailable_providers=unavailable,
        suppressed_providers=suppressed,
        execution_mode=execution_mode,
        entitled_live=entitled_live,
        estimated_cost_usd=cost,
        lead_spent_usd=ledger_cost.total_cost_usd,
        lead_budget_cap_usd=_lead_budget_cap(conn, lead_id),
        org_modeled_spend_remaining_usd=Decimal(str(usage.modeled_spend_remaining_usd)),
    )
    decision = authorize_research(ctx)
    if not decision.approved:
        return ResearchExecutionResult(
            approved=False,
            reason_code=decision.reason_code,
            detail=decision.detail,
            target_field=target_field,
            provider=None,
            found_value=None,
            cost_usd=Decimal(0),
            preview=None,
        )

    identity = _load_identity(conn, lead_id)
    entity_type = _ENTITY_TYPE_FOR_FIELD[target_field]
    entity_id = identity.company_id if entity_type == "company" else identity.person_id
    assert entity_id is not None  # a decided lead always has both entities resolved
    assert decision.chosen_provider is not None and decision.estimated_cost_usd is not None

    # Already-answered check (Part Z) doubles as this call's idempotency
    # guard (Part L): a retry that lands after evidence was already written
    # finds it fresh here and never re-spends.
    existing = evidence_store.get_fresh(
        entity_type, entity_id, str(target_field), organization_id=organization_id, now=now
    )
    if existing is not None:
        preview = _preview(
            conn, evidence_store, organization_id=organization_id, identity=identity, now=now
        )
        return ResearchExecutionResult(
            approved=True,
            reason_code=ResearchReasonCode.RESEARCH_APPROVED,
            detail="This was already researched.",
            target_field=target_field,
            provider=existing.source,
            found_value=existing.value,
            cost_usd=Decimal(0),
            preview=preview,
        )

    assert identity.canonical_email is not None and identity.canonical_domain is not None
    corpus_lead = synthesize_corpus_lead(
        canonical_email=identity.canonical_email,
        canonical_domain=identity.canonical_domain,
        full_name=identity.full_name,
        company_name=identity.company_name,
    )
    _, registry = build_from_leads([corpus_lead])
    provider = registry.get(decision.chosen_provider)
    canonical_key = (
        corpus_lead.company.canonical_domain
        if entity_type == "company"
        else corpus_lead.person.email
    )
    entity = Entity(entity_type=entity_type, entity_id=entity_id, canonical_key=canonical_key)
    result = provider.fetch(entity)

    idempotency_key = f"research:{lead_id}:{target_field}:{decision.chosen_provider}"
    ledger.record_provider_call(
        idempotency_key=idempotency_key,
        provider=decision.chosen_provider,
        entity_type=entity_type,
        entity_id=entity_id,
        status=result.status,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        organization_id=organization_id,
        lead_id=lead_id,
    )

    found_value = result.fields.get(str(target_field))
    if result.status is ProviderStatus.SUCCESS and found_value is not None:
        evidence_store.put(
            Evidence(
                entity_type=entity_type,
                entity_id=entity_id,
                field_name=str(target_field),
                value=found_value,
                source=decision.chosen_provider,
                confidence=result.confidence,
                ttl_seconds=ttl_for_field(str(target_field)),
                fetched_at=now,
            ),
            organization_id=organization_id,
        )

    preview = _preview(
        conn, evidence_store, organization_id=organization_id, identity=identity, now=now
    )
    detail = (
        "New information was found and added to this lead's evidence."
        if found_value is not None
        else "This source had no answer for this question."
    )
    return ResearchExecutionResult(
        approved=True,
        reason_code=ResearchReasonCode.RESEARCH_APPROVED,
        detail=detail,
        target_field=target_field,
        provider=decision.chosen_provider,
        found_value=found_value,
        cost_usd=Decimal(str(result.cost_usd)),
        preview=preview,
    )
