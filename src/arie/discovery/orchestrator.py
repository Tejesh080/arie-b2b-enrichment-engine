"""The bounded discovery orchestration service — Discovery Pivot Phase 13.

`run_discovery` walks a `DiscoveryRun` through PLANNING -> DISCOVERING ->
SCREENING -> PROMOTING -> RESEARCHING -> COMPLETE, synchronously, inside one
call. No new workflow engine: promotion enqueues jobs on the exact same
Postgres queue every other lead uses, and this module drives them to
completion with `arie.jobs.worker.run_worker_cycle` — the identical
drive-to-completion pattern the batch-upload integration tests already use to
wait out asynchronous scoring in a synchronous test. A production deployment
with a standing worker fleet would let that fleet pick the jobs up instead;
this module runs its own cycles so a discovery run finishes inside the HTTP
request that started it, which is what today's one-page UX needs and what
the hard candidate caps below make affordable.

Every cap in this module is deliberate and bounded — see the constants — so
"1,000 candidates discovered" from the pivot brief becomes a documented,
smaller number here: a synchronous request that fans out to hundreds of
provider calls and LLM calls is a latency and cost problem a background job
queue should own, which is exactly the "Known limitations" / "Next step" this
slice's handoff calls out.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from psycopg_pool import ConnectionPool

from arie.discovery import repository
from arie.discovery.buyer_search import BuyerSearchFn, execute_buyer_search
from arie.discovery.dedupe import dedupe_raw_candidates
from arie.discovery.models import (
    DiscoveryCandidate,
    DiscoveryFunnel,
    DiscoveryRun,
    DiscoveryRunStatus,
    Opportunity,
    ScreeningClass,
    VerificationStatus,
)
from arie.discovery.opportunity import build_opportunity
from arie.discovery.promotion import promote_candidate
from arie.discovery.providers import (
    DiscoveryProvider,
    DiscoveryProviderError,
    build_discovery_provider,
)
from arie.discovery.repository import NewCandidate
from arie.discovery.screening import screen_candidates
from arie.discovery.search_planning import generate_search_plan
from arie.discovery.website_verification import WebsiteVerifierFn, verify_candidate
from arie.evidence.store import PostgresEvidenceStore
from arie.icp_profiles import ICPProfileRecord
from arie.identity.resolver import IdentityResolver
from arie.intelligence.targeting import stored_draft
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import run_worker_cycle
from arie.ledger.store import PostgresCostLedger
from arie.live.outcome_cache import ProviderOutcomeGuard
from arie.llm.service import LLMService
from arie.organizations import get_execution_mode

__all__ = ["MAX_CANDIDATES", "MAX_OPPORTUNITY_COUNT", "list_opportunities", "run_discovery"]

_LOGGER = logging.getLogger("arie.discovery.orchestrator")

MAX_OPPORTUNITY_COUNT = 50
MIN_OPPORTUNITY_COUNT = 1
MAX_CANDIDATES = 200
"""Hard cap on raw candidates per run. The pivot brief's own examples go up
to 1,000 — reduced here because this orchestrator runs synchronously inside
one HTTP request (see the module docstring); a queue-backed version could
raise this without changing anything else in this file."""
MIN_CANDIDATES = 10
_MAX_PER_QUERY = 25
_MAX_PROMOTIONS_MULTIPLIER = 3
"""Promote at most `requested_opportunity_count * this` survivors — enough
slack for screening to have been optimistic without promoting (and scoring)
every survivor of a generous screening pass."""
_MIN_PROMOTIONS_FLOOR = 10
_WORKER_DRIVE_MAX_CYCLES = 50
_WORKER_BATCH_SIZE = 10

_runtime_lock = threading.Lock()
_runtime_cache: SimulatedEnrichmentRuntime | None = None


def _shared_runtime() -> SimulatedEnrichmentRuntime:
    """Built once per process (dataset generation + confidence-model fit is
    not cheap) and reused — the same runtime every `arie.jobs.worker.main`
    process already builds once at startup."""
    global _runtime_cache
    with _runtime_lock:
        if _runtime_cache is None:
            _runtime_cache = build_runtime()
        return _runtime_cache


def _clamp(value: int, *, low: int, high: int) -> int:
    return max(low, min(high, value))


def _profile_summary(profile: ICPProfileRecord | None) -> tuple[str, str, tuple[str, ...]]:
    """`(offering_summary, target_summary, ideal_company_types)` from a
    confirmed targeting profile's own stored AI draft where one exists —
    never a re-ask of "what do you sell" the customer already answered.
    Falls back to the profile's name when it was created without a draft
    (a manually-configured profile, or none at all)."""
    if profile is None:
        return "", "", ()
    draft = stored_draft(profile.config)
    if draft is None:
        return profile.name, profile.name, ()
    return (
        draft.offering_summary or profile.name,
        draft.plain_english_summary or profile.name,
        tuple(draft.ideal_company_types),
    )


def _preferred_buyer_traits(
    profile: ICPProfileRecord | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(preferred_seniorities, preferred_functions)` for buyer ranking —
    Opportunity Activation Part 6. Same source as `_profile_summary`'s own
    read of the confirmed targeting draft; empty tuples (never scored, ranks
    on decision-maker/email-quality alone) when the profile has none."""
    if profile is None:
        return (), ()
    draft = stored_draft(profile.config)
    if draft is None:
        return (), ()
    return tuple(draft.preferred_seniorities), tuple(draft.preferred_functions)


def _drive_to_completion(pool: ConnectionPool, queue: PostgresJobQueue) -> None:
    handlers = build_handlers(pool, runtime=_shared_runtime(), provider_mode="simulated")
    for _ in range(_WORKER_DRIVE_MAX_CYCLES):
        results = run_worker_cycle(
            queue, pool, handlers, batch_size=_WORKER_BATCH_SIZE, job_types=["compute_score"]
        )
        if not results:
            return


def run_discovery(
    pool: ConnectionPool,
    *,
    resolver: IdentityResolver,
    queue: PostgresJobQueue,
    ledger: PostgresCostLedger,
    llm: LLMService | None,
    organization_id: UUID,
    profile: ICPProfileRecord | None,
    requested_opportunity_count: int,
    market: str | None,
    max_candidates: int,
    created_by_user_id: UUID | None,
    now: datetime,
    discovery_provider: DiscoveryProvider | None = None,
    website_verifier: WebsiteVerifierFn = verify_candidate,
    buyer_search_fn: BuyerSearchFn = execute_buyer_search,
) -> tuple[DiscoveryRun, list[Opportunity]]:
    """Run one discovery loop end to end and return the completed run
    (status COMPLETE or FAILED) and its ranked opportunities.

    `discovery_provider` lets a caller (tests) inject a fake provider;
    production leaves it `None` and gets `build_discovery_provider()`'s
    real-if-configured choice. `website_verifier` and `buyer_search_fn` are
    the identical seam for Opportunity Activation's website verification and
    buyer search steps — tests pass `arie.discovery.website_verification.
    fake_website_verifier` / `arie.discovery.buyer_search.fake_buyer_search`
    so a fake-provider run never makes a real Firecrawl or Hunter call.
    """
    requested_count = _clamp(
        requested_opportunity_count, low=MIN_OPPORTUNITY_COUNT, high=MAX_OPPORTUNITY_COUNT
    )
    candidate_cap = _clamp(max_candidates, low=MIN_CANDIDATES, high=MAX_CANDIDATES)

    with pool.connection() as conn:
        run = repository.create_run(
            conn,
            organization_id=organization_id,
            profile_version=profile.version if profile is not None else None,
            requested_opportunity_count=requested_count,
            market=market,
            max_candidates=candidate_cap,
            created_by_user_id=created_by_user_id,
        )
        conn.commit()

    funnel = DiscoveryFunnel()
    try:
        run, funnel = _run_stages(
            pool,
            resolver=resolver,
            queue=queue,
            ledger=ledger,
            llm=llm,
            organization_id=organization_id,
            profile=profile,
            run=run,
            market=market,
            candidate_cap=candidate_cap,
            discovery_provider=discovery_provider,
            website_verifier=website_verifier,
            now=now,
        )
    except Exception as exc:
        _LOGGER.exception("discovery run %s failed", run.run_id)
        with pool.connection() as conn:
            repository.update_run_status(
                conn,
                run_id=run.run_id,
                organization_id=organization_id,
                status=DiscoveryRunStatus.FAILED,
                funnel=funnel,
                error_detail=str(exc)[:500],
                now=now,
            )
            conn.commit()
        with pool.connection() as conn:
            fetched = repository.get_run(conn, run_id=run.run_id, organization_id=organization_id)
        assert fetched is not None
        return fetched, []

    opportunities, funnel = _build_opportunities(
        pool,
        ledger=ledger,
        organization_id=organization_id,
        run_id=run.run_id,
        requested_count=requested_count,
        funnel=funnel,
        profile=profile,
        buyer_search_fn=buyer_search_fn,
        now=now,
    )

    with pool.connection() as conn:
        repository.update_run_status(
            conn,
            run_id=run.run_id,
            organization_id=organization_id,
            status=DiscoveryRunStatus.COMPLETE,
            funnel=funnel,
            error_detail=None,
            now=now,
        )
        conn.commit()
        fetched = repository.get_run(conn, run_id=run.run_id, organization_id=organization_id)
    assert fetched is not None

    return fetched, opportunities


def _run_stages(
    pool: ConnectionPool,
    *,
    resolver: IdentityResolver,
    queue: PostgresJobQueue,
    ledger: PostgresCostLedger,
    llm: LLMService | None,
    organization_id: UUID,
    profile: ICPProfileRecord | None,
    run: DiscoveryRun,
    market: str | None,
    candidate_cap: int,
    discovery_provider: DiscoveryProvider | None,
    website_verifier: WebsiteVerifierFn,
    now: datetime,
) -> tuple[DiscoveryRun, DiscoveryFunnel]:
    funnel = DiscoveryFunnel()

    def _advance(status: DiscoveryRunStatus) -> None:
        with pool.connection() as conn:
            repository.update_run_status(
                conn,
                run_id=run.run_id,
                organization_id=organization_id,
                status=status,
                funnel=funnel,
                error_detail=None,
                now=now,
            )
            conn.commit()

    _advance(DiscoveryRunStatus.PLANNING)
    offering_summary, target_summary, ideal_types = _profile_summary(profile)
    plan_result = generate_search_plan(
        llm,
        organization_id=organization_id,
        offering_summary=offering_summary,
        target_summary=target_summary,
        ideal_company_types=ideal_types,
        market=market,
        now=now,
    )
    funnel = replace(
        funnel,
        search_queries=len(plan_result.plan.queries),
        llm_calls=funnel.llm_calls + (1 if plan_result.llm_used else 0),
        llm_cost_usd=funnel.llm_cost_usd + plan_result.cost_usd,
    )

    _advance(DiscoveryRunStatus.DISCOVERING)
    provider = discovery_provider or build_discovery_provider()
    per_query_limit = max(
        1, min(_MAX_PER_QUERY, candidate_cap // max(1, len(plan_result.plan.queries)))
    )
    raw = []
    for item in plan_result.plan.queries:
        try:
            raw.extend(provider.search(item.query, per_query_limit))
        except DiscoveryProviderError:
            _LOGGER.warning("discovery provider failed for query %r", item.query, exc_info=True)
            continue
    raw = raw[:candidate_cap]
    unique = dedupe_raw_candidates(raw)
    funnel = replace(funnel, raw_candidates=len(raw), unique_companies=len(unique))

    with pool.connection() as conn:
        candidates = repository.insert_candidates(
            conn,
            run_id=run.run_id,
            organization_id=organization_id,
            candidates=[
                NewCandidate(
                    company_name=item.company_name,
                    domain=domain,
                    source_url=item.url,
                    snippet=item.snippet,
                    source_provider=item.source_provider,
                    search_query=item.search_query,
                )
                for domain, item in unique
            ],
        )
        conn.commit()

    _advance(DiscoveryRunStatus.SCREENING)
    screening = screen_candidates(
        llm,
        organization_id=organization_id,
        candidates=candidates,
        target_summary=target_summary,
        now=now,
    )
    counts = {c: 0 for c in ScreeningClass}
    screened_candidates: list[DiscoveryCandidate] = []
    with pool.connection() as conn:
        for candidate in candidates:
            screening_class, reason = screening.screened.get(
                candidate.candidate_id, (ScreeningClass.INSUFFICIENT_INFO, "Not screened.")
            )
            counts[screening_class] += 1
            repository.update_candidate_screening(
                conn,
                candidate_id=candidate.candidate_id,
                organization_id=organization_id,
                screening_class=screening_class,
                screening_reason=reason,
            )
            screened_candidates.append(
                replace(candidate, screening_class=screening_class, screening_reason=reason)
            )
        conn.commit()

    funnel = replace(
        funnel,
        screened=len(screened_candidates),
        promising=counts[ScreeningClass.PROMISING],
        possible=counts[ScreeningClass.POSSIBLE],
        unlikely=counts[ScreeningClass.UNLIKELY],
        insufficient_info=counts[ScreeningClass.INSUFFICIENT_INFO],
        llm_calls=funnel.llm_calls + screening.llm_calls,
        llm_cost_usd=funnel.llm_cost_usd + screening.cost_usd,
    )

    survivors = [
        c
        for c in screened_candidates
        if c.screening_class in (ScreeningClass.PROMISING, ScreeningClass.POSSIBLE)
    ]
    # Promising first, so the promotion cap keeps the strongest survivors.
    survivors.sort(key=lambda c: 0 if c.screening_class is ScreeningClass.PROMISING else 1)
    promotion_cap = max(
        _MIN_PROMOTIONS_FLOOR, run.requested_opportunity_count * _MAX_PROMOTIONS_MULTIPLIER
    )
    to_promote = survivors[:promotion_cap]

    _advance(DiscoveryRunStatus.PROMOTING)
    promoted = 0
    website_verified = 0
    company_rejected_after_verification = 0
    website_calls = 0
    website_cost = Decimal(0)
    verification_llm_calls = 0
    verification_llm_cost = Decimal(0)
    for candidate in to_promote:
        # Opportunity Activation Part 1/17: only survivors of cheap
        # screening — already true of `to_promote` — and only the already-
        # capped promotion set, so website calls stay bounded to roughly
        # `promotion_cap`, never to every raw discovery result.
        verification = website_verifier(
            llm,
            organization_id=organization_id,
            domain=candidate.domain,
            target_summary=target_summary,
            now=now,
        )
        website_calls += verification.pages_fetched
        website_cost += verification.website_cost_usd
        if verification.llm_used:
            verification_llm_calls += 1
            verification_llm_cost += verification.llm_cost_usd

        with pool.connection() as conn:
            repository.update_candidate_verification(
                conn,
                candidate_id=candidate.candidate_id,
                organization_id=organization_id,
                status=verification.status,
                verified_facts=verification.facts.model_dump() if verification.facts else None,
                verified_at=now,
            )
            conn.commit()

        if verification.status in (VerificationStatus.VERIFIED, VerificationStatus.REJECTED):
            website_verified += 1

        if verification.status is VerificationStatus.REJECTED:
            company_rejected_after_verification += 1
            reason = (
                verification.facts.reasoning
                if verification.facts
                else "Rejected after website review."
            )
            with pool.connection() as conn:
                repository.update_candidate_screening(
                    conn,
                    candidate_id=candidate.candidate_id,
                    organization_id=organization_id,
                    screening_class=ScreeningClass.UNLIKELY,
                    screening_reason=f"Website review: {reason}",
                )
                conn.commit()
            continue  # never promoted — Part 12's "candidate may move promising -> poor fit"

        with pool.connection() as conn:
            result = promote_candidate(
                conn,
                resolver=resolver,
                queue=queue,
                organization_id=organization_id,
                run_id=run.run_id,
                candidate=candidate,
            )
            conn.commit()
        with pool.connection() as conn:
            repository.update_candidate_promoted_lead(
                conn,
                candidate_id=candidate.candidate_id,
                organization_id=organization_id,
                lead_id=result.ingest.lead_id,
            )
            conn.commit()
        promoted += 1

    funnel = replace(
        funnel,
        promoted_to_leads=promoted,
        website_verified=website_verified,
        company_rejected_after_verification=company_rejected_after_verification,
        website_calls=website_calls,
        website_cost_usd=website_cost,
        llm_calls=funnel.llm_calls + verification_llm_calls,
        llm_cost_usd=funnel.llm_cost_usd + verification_llm_cost,
    )

    # This "RESEARCHING" stage is the ordinary acquisition/scoring pipeline
    # every promoted lead goes through regardless of its origin — the
    # *selective* research pass (Phase 8) and buyer identification (Phase
    # 10) happen per-opportunity in `_build_opportunities`, after every
    # promoted lead has reached a decision.
    _advance(DiscoveryRunStatus.RESEARCHING)
    _drive_to_completion(pool, queue)

    return run, funnel


_PRIORITY_ORDER = {"contact_first": 0, "worth_pursuing": 1, "review": 2, "skip": 3}


def _build_opportunities(
    pool: ConnectionPool,
    *,
    ledger: PostgresCostLedger,
    organization_id: UUID,
    run_id: UUID,
    requested_count: int,
    funnel: DiscoveryFunnel,
    profile: ICPProfileRecord | None,
    now: datetime,
    buyer_search_fn: BuyerSearchFn = execute_buyer_search,
) -> tuple[list[Opportunity], DiscoveryFunnel]:
    with pool.connection() as conn:
        candidates = repository.list_candidates(
            conn, run_id=run_id, organization_id=organization_id
        )
        promoted = [c for c in candidates if c.promoted_lead_id is not None]
        if not promoted:
            return [], funnel
        execution_mode = get_execution_mode(conn, organization_id=organization_id)

    preferred_seniorities, preferred_functions = _preferred_buyer_traits(profile)
    evidence_store = PostgresEvidenceStore(pool)
    outcome_guard = ProviderOutcomeGuard(pool)

    opportunities: list[Opportunity] = []
    research_candidates = 0
    research_calls = 0
    buyer_lookup_eligible = 0
    buyer_lookups = 0
    buyer_found = 0
    buyer_email_found = 0

    for candidate in promoted:
        assert candidate.promoted_lead_id is not None
        with pool.connection() as conn:
            outcome = build_opportunity(
                conn,
                ledger,
                evidence_store,
                organization_id=organization_id,
                candidate=candidate,
                lead_id=candidate.promoted_lead_id,
                execution_mode=execution_mode,
                preferred_seniorities=preferred_seniorities,
                preferred_functions=preferred_functions,
                now=now,
                outcome_guard=outcome_guard,
                buyer_search_fn=buyer_search_fn,
            )
            conn.commit()
        if outcome.research_attempted:
            research_candidates += 1
        if outcome.research_performed:
            research_calls += 1
        if outcome.buyer_search_eligible:
            buyer_lookup_eligible += 1
        if outcome.buyer_search_performed:
            buyer_lookups += 1
        if outcome.buyer_found:
            buyer_found += 1
        if outcome.buyer_email_found:
            buyer_email_found += 1
        if outcome.opportunity is not None:
            opportunities.append(outcome.opportunity)

    opportunities.sort(
        key=lambda o: (
            _PRIORITY_ORDER.get(o.priority, 9),
            -(o.score if o.score is not None else -1.0),
        )
    )
    ranked = opportunities[:requested_count]
    contactable = sum(1 for o in ranked if o.is_contactable)

    provider_calls = 0
    provider_cost = Decimal(0)
    for candidate in promoted:
        assert candidate.promoted_lead_id is not None
        cost = ledger.lead_cost(candidate.promoted_lead_id)
        if cost is not None:
            provider_calls += cost.provider_calls
            provider_cost += Decimal(str(cost.provider_cost_usd))

    funnel = replace(
        funnel,
        research_candidates=research_candidates,
        research_calls=research_calls,
        buyer_lookup_eligible=buyer_lookup_eligible,
        buyer_lookups=buyer_lookups,
        buyer_found=buyer_found,
        buyer_email_found=buyer_email_found,
        final_opportunities=len(ranked),
        final_contactable_opportunities=contactable,
        provider_calls=provider_calls,
        provider_cost_usd=provider_cost,
    )
    return ranked, funnel


def list_opportunities(
    pool: ConnectionPool,
    *,
    ledger: PostgresCostLedger,
    organization_id: UUID,
    run_id: UUID,
    requested_count: int,
    profile: ICPProfileRecord | None,
    now: datetime,
    buyer_search_fn: BuyerSearchFn = execute_buyer_search,
) -> list[Opportunity]:
    """Re-derive a completed run's opportunities on demand —
    `GET /discovery/runs/{run_id}/opportunities`'s read model. Safe to poll
    repeatedly: both `build_opportunity`'s selective-research step and its
    buyer search are idempotent (each checks for fresh evidence before
    spending), so a second call here costs nothing beyond what the first one
    already spent.
    """
    opportunities, _ = _build_opportunities(
        pool,
        ledger=ledger,
        organization_id=organization_id,
        run_id=run_id,
        requested_count=requested_count,
        funnel=DiscoveryFunnel(),
        profile=profile,
        buyer_search_fn=buyer_search_fn,
        now=now,
    )
    return opportunities
