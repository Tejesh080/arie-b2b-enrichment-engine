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
from arie.discovery.dedupe import dedupe_raw_candidates
from arie.discovery.models import (
    DiscoveryCandidate,
    DiscoveryFunnel,
    DiscoveryRun,
    DiscoveryRunStatus,
    Opportunity,
    ScreeningClass,
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
) -> tuple[DiscoveryRun, list[Opportunity]]:
    """Run one discovery loop end to end and return the completed run
    (status COMPLETE or FAILED) and its ranked opportunities.

    `discovery_provider` lets a caller (tests) inject a fake provider;
    production leaves it `None` and gets `build_discovery_provider()`'s
    real-if-configured choice.
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
    for candidate in to_promote:
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
    funnel = replace(funnel, promoted_to_leads=promoted)

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
    now: datetime,
) -> tuple[list[Opportunity], DiscoveryFunnel]:
    with pool.connection() as conn:
        candidates = repository.list_candidates(
            conn, run_id=run_id, organization_id=organization_id
        )
        promoted = [c for c in candidates if c.promoted_lead_id is not None]
        if not promoted:
            return [], funnel
        execution_mode = get_execution_mode(conn, organization_id=organization_id)

    evidence_store = PostgresEvidenceStore(pool)
    outcome_guard = ProviderOutcomeGuard(pool)

    opportunities: list[Opportunity] = []
    research_candidates = 0
    research_calls = 0
    buyer_lookups = 0

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
                now=now,
                outcome_guard=outcome_guard,
            )
            conn.commit()
        if outcome.research_attempted:
            research_candidates += 1
        if outcome.research_performed:
            research_calls += 1
            if outcome.was_buyer_lookup:
                buyer_lookups += 1
        if outcome.opportunity is not None:
            opportunities.append(outcome.opportunity)

    opportunities.sort(
        key=lambda o: (
            _PRIORITY_ORDER.get(o.priority, 9),
            -(o.score if o.score is not None else -1.0),
        )
    )
    ranked = opportunities[:requested_count]

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
        buyer_lookups=buyer_lookups,
        final_opportunities=len(ranked),
        provider_calls=provider_calls,
        provider_cost_usd=provider_cost,
        llm_cost_usd=funnel.llm_cost_usd,  # research above deliberately spends no LLM budget
    )
    return ranked, funnel


def list_opportunities(
    pool: ConnectionPool,
    *,
    ledger: PostgresCostLedger,
    organization_id: UUID,
    run_id: UUID,
    requested_count: int,
    now: datetime,
) -> list[Opportunity]:
    """Re-derive a completed run's opportunities on demand —
    `GET /discovery/runs/{run_id}/opportunities`'s read model. Safe to poll
    repeatedly: `build_opportunity`'s own selective-research step is
    idempotent (`execute_research`'s freshness check refuses to re-spend on
    an already-answered field), so a second call here costs nothing beyond
    what the first one already spent.
    """
    opportunities, _ = _build_opportunities(
        pool,
        ledger=ledger,
        organization_id=organization_id,
        run_id=run_id,
        requested_count=requested_count,
        funnel=DiscoveryFunnel(),
        now=now,
    )
    return opportunities
