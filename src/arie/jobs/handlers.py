"""Production job handlers — the wiring the handoff called M1's biggest gap.

This module composes pieces that all already existed — ``CalibratedBoundsPolicy``,
the simulated provider registry, ``PostgresEvidenceStore``, ``PostgresCostLedger``,
the state graph, and ``arie.approval.workflow.request_review`` — into the one
handler shape ``docs/06-m1-handoff.md`` describes: *"a compute_score handler
receives a JobContext, reads the lead's known facts from PostgresEvidenceStore,
runs the policy, records what it bought in PostgresCostLedger, and returns the
lead's new status."* Nothing here is a new architecture; it is the composition
the handoff's table of collaborators was written for.

**One handler, not four.** The state graph names four job types
(``compute_score`` → ``fetch_evidence`` → ``integrate_evidence`` →
``finalize_decision``), but the policy's score/buy/score loop is a single
calculation that reads evidence *content* to decide whether to keep buying
(ADR 0001; the graph's own docstring says the linear scaffold "is *not* a
reimplementation" of that loop). Splitting the loop across four handlers would
be exactly the reimplementation it warns against. So ``compute_score`` runs the
whole pipeline and walks the lead through the scaffold's statuses itself, via
``apply_transition`` inside the one work transaction — every hop lands in
``lead_events`` with a payload recording what actually happened at that stage,
versions increment normally, and the worker's atomic commit covers all of it.
The other three job types stay unclaimed by design; nothing enqueues them, and
if something ever does, the worker's existing no-handler path dead-letters it
loudly rather than silently absorbing it. If real (network) provider adapters
ever exist, ``fetch_evidence`` becomes a genuinely separate long-running phase
and this collapses back into per-stage handlers — that split is the adapters'
step to make, not this one's.

**Simulated mode replays a frozen corpus, so only corpus identities process.**
``SimulatedProvider`` raises for entities outside the generated dataset — a
deliberate guard ("a wiring error must not masquerade as poor provider
coverage"). This module honours that instead of working around it: the handler
looks the ingested lead's person up in the corpus by normalized email, and a
lead that isn't there fails with a clear error into the normal
retry/dead-letter path. That is the honest shape of "production" while the
only provider backend is the simulator; a live adapter (``PROVIDER_MODE=live``)
is refused outright below rather than half-supported.

**Durable cache and ledger are write-through subclasses, not replacements.**
``RunContext`` takes the benchmark's own ``EvidenceCache``/``CallLedger`` types;
the subclasses here add Postgres persistence underneath without changing what
the policy observes. Two deliberate asymmetries, both inherited from module
docstrings that predate this file: evidence writes and ledger writes commit in
their *own* transactions (a rollback must not un-spend money, and a surviving
cache entry is just a future cache hit), while lead state, events, scores, and
the job's completion commit together in the work transaction. Ledger idempotency
keys are derived from the job id, so a crashed-and-retried job reproduces its
keys and cannot double-charge — and because the first attempt's evidence
survived, the retry is served from cache and doesn't even re-fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from arie.approval.workflow import request_review
from arie.config import POLICY, RUNTIME
from arie.core.types import (
    Decision,
    Entity,
    EntityType,
    Evidence,
    LeadStatus,
    ProviderResult,
    ProviderStatus,
)
from arie.evidence.store import PostgresEvidenceStore
from arie.evidence.ttl_policy import ttl_for_field
from arie.identity.normalize import normalize_domain, normalize_email
from arie.ledger.store import PostgresCostLedger
from arie.observability.tracing import get_tracer, set_attributes, traced
from arie.policy.base import EvidenceCache, PolicyOutcome, RunContext
from arie.providers.base import ProviderRegistry
from arie.providers.catalog import BY_NAME
from arie.providers.simulated import CallLedger, build_from_leads
from arie.statemachine.apply import apply_transition
from arie.statemachine.transitions import next_status

if TYPE_CHECKING:  # pragma: no cover - typing only
    from arie.evalgen.schema import EvalLead
    from arie.jobs.worker import JobContext, JobHandler
    from arie.policy.production import CalibratedBoundsPolicy

_TRACER = get_tracer("arie.jobs.handlers")


class UnsupportedProviderModeError(RuntimeError):
    """Raised when handlers are built for a provider mode that has no backend.

    ``PROVIDER_MODE=live`` names a thing that does not exist yet: no real
    provider adapter has been written (ADR 0003 — real adapters go alongside
    the simulator, and none have). Refusing at build time keeps that fact
    loud; a worker silently falling back to the simulator would report costs
    and coverage for vendors it never called.
    """


class UnknownCorpusIdentityError(LookupError):
    """Raised when an ingested lead's identity is not in the frozen corpus.

    Simulated providers replay frozen observations and cannot answer for an
    entity the generator never produced — by design, not limitation (see
    ``arie.providers.simulated.UnknownEntityError``). The job fails into the
    ordinary retry/dead-letter path with this message, which is the honest
    outcome: in simulated mode, "we have no data source for this lead" is
    true.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"{detail} — PROVIDER_MODE=simulated replays the frozen eval corpus and has no "
            "data source for identities outside it. Submit a corpus identity (see README's "
            "n8n section for a known-good example) or wire a real provider adapter."
        )


def decision_route(decision: Decision, autonomous: bool) -> str:
    """Map a policy outcome onto the DECISION node's outcome vocabulary.

    Confidence gates autonomy (``confidence >= tau``), never ``is_settled`` —
    the first of the handoff's "five things most likely to be got wrong". A
    non-autonomous result escalates *whatever* the decision label says, and a
    policy that itself concluded ``escalate_human`` escalates even when
    confident about that conclusion.
    """
    if not autonomous or decision is Decision.ESCALATE_HUMAN:
        return "escalate_human"
    return str(decision)


@dataclass(frozen=True)
class SimulatedEnrichmentRuntime:
    """Everything the handler needs that is pure and process-lifetime.

    Built once at worker startup: the seeded dataset, the confidence model
    refit from the calibration split (refit, never pickled — handoff item 2),
    the simulated provider registry over the frozen observations, and an
    email-keyed index for mapping an ingested lead back onto its corpus row.
    """

    policy: CalibratedBoundsPolicy
    registry: ProviderRegistry
    corpus_by_email: dict[str, EvalLead]
    dataset_seed: int

    def corpus_lead_for(self, canonical_email: str, canonical_domain: str | None) -> EvalLead:
        lead = self.corpus_by_email.get(canonical_email)
        if lead is None:
            raise UnknownCorpusIdentityError(f"no corpus person with email {canonical_email!r}")
        corpus_domain = normalize_domain(lead.company.canonical_domain)
        if canonical_domain is not None and canonical_domain != corpus_domain:
            raise UnknownCorpusIdentityError(
                f"email {canonical_email!r} resolved to company domain {canonical_domain!r} "
                f"but the corpus places that person at {corpus_domain!r}"
            )
        return lead


def build_runtime(
    leads: list[EvalLead] | None = None, *, dataset_seed: int = 42
) -> SimulatedEnrichmentRuntime:
    """Generate (or accept) the dataset, fit the model, index the corpus.

    `leads` exists so tests that already hold the session-scoped dataset don't
    regenerate it; passing a different seed's leads with the default
    `dataset_seed` label would be a caller bug this function can't detect.
    Imports of the fitting stack are deferred so importing this module (which
    ``arie.jobs.worker`` does lazily anyway) stays cheap.
    """
    from arie.confidence.model import fit_confidence_model
    from arie.evalgen.generator import generate_dataset
    from arie.policy.production import CalibratedBoundsPolicy

    if leads is None:
        leads, _manifest = generate_dataset(seed=dataset_seed)

    calibration = [lead for lead in leads if lead.split == "calibration"]
    model = fit_confidence_model(calibration, target_error_rate=POLICY.target_autonomous_error_rate)
    _store, registry = build_from_leads(leads)

    corpus_by_email: dict[str, EvalLead] = {}
    for lead in leads:
        corpus_by_email.setdefault(normalize_email(lead.person.email), lead)

    return SimulatedEnrichmentRuntime(
        policy=CalibratedBoundsPolicy(model=model),
        registry=registry,
        corpus_by_email=corpus_by_email,
        dataset_seed=dataset_seed,
    )


@dataclass(frozen=True)
class _LeadIdentity:
    company_id: UUID
    person_id: UUID
    canonical_email: str
    canonical_domain: str | None


_SELECT_LEAD_IDENTITY = """
    SELECT l.company_id, l.person_id, c.canonical_domain, p.canonical_email
    FROM leads l
    LEFT JOIN companies c ON c.company_id = l.company_id
    LEFT JOIN persons  p ON p.person_id  = l.person_id
    WHERE l.lead_id = %(lead_id)s
"""

_INSERT_SCORE = """
    INSERT INTO scores (lead_id, total_score, decision_confidence, component_breakdown, model_version)
    VALUES (%(lead_id)s, %(total_score)s, %(decision_confidence)s, %(component_breakdown)s,
            %(model_version)s)
"""


def _load_identity(conn: psycopg.Connection, lead_id: UUID) -> _LeadIdentity:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_LEAD_IDENTITY, {"lead_id": lead_id})
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no lead {lead_id}")
    if row["company_id"] is None or row["person_id"] is None or row["canonical_email"] is None:
        raise ValueError(
            f"lead {lead_id} has no resolved company/person identity — it did not come "
            "through the ingestion path and cannot be enriched"
        )
    return _LeadIdentity(
        company_id=row["company_id"],
        person_id=row["person_id"],
        canonical_email=row["canonical_email"],
        canonical_domain=row["canonical_domain"],
    )


class _DurableEvidenceCache(EvidenceCache):
    """The in-memory cache, backed by the ``evidence`` table underneath.

    Provider-keyed, exactly like the benchmark's cache: a durable hit for
    ``(provider, key)`` requires every field that provider declares, fresh,
    *from that provider*. The store itself would support answering a field
    from any source (its docstring calls that widening out as where the real
    saving lives), but serving a cross-provider hit here would change what
    the policy observes relative to the benchmark — that widening is policy
    logic for a future step, taken deliberately, not smuggled in through a
    cache wrapper. One knowable divergence the other way: a provider MISS has
    no fields to persist, so misses are re-fetched where the benchmark's
    in-memory cache would have remembered them — visible only as repeat calls
    on missing providers, never as different evidence.
    """

    def __init__(self, store: PostgresEvidenceStore, entities: dict[str, tuple[EntityType, UUID]]):
        super().__init__()
        self._store = store
        self._entities = entities

    def get(self, provider: str, canonical_key: str) -> ProviderResult | None:
        hit = super().get(provider, canonical_key)
        if hit is not None:
            return hit
        mapped = self._entities.get(canonical_key)
        if mapped is None:
            return None
        entity_type, entity_id = mapped
        spec = BY_NAME[provider]

        freshest: dict[str, Evidence] = {}
        for row in self._store.get_all_fresh(entity_type, entity_id):
            if row.source == provider and row.field_name not in freshest:
                freshest[row.field_name] = row
        if not set(spec.provides_fields) <= freshest.keys():
            return None

        result = ProviderResult(
            fields={name: freshest[name].value for name in spec.provides_fields},
            confidence=min(freshest[name].confidence for name in spec.provides_fields),
            cost_usd=0.0,
            latency_ms=0.0,
            status=ProviderStatus.SUCCESS,
            raw={"provider": provider, "entity": canonical_key, "durable_cache": True},
        )
        super().put(provider, canonical_key, result)
        return result

    def put(self, provider: str, canonical_key: str, result: ProviderResult) -> None:
        super().put(provider, canonical_key, result)
        mapped = self._entities.get(canonical_key)
        if mapped is None or result.status is not ProviderStatus.SUCCESS or not result.fields:
            return
        entity_type, entity_id = mapped
        now = datetime.now(UTC)
        self._store.put_many(
            Evidence(
                entity_type=entity_type,
                entity_id=entity_id,
                field_name=name,
                value=value,
                source=provider,
                confidence=result.confidence,
                ttl_seconds=ttl_for_field(name),
                fetched_at=now,
            )
            for name, value in result.fields.items()
        )


class _DurableCallLedger(CallLedger):
    """The in-memory ledger, mirrored into ``provider_calls`` row by row.

    The idempotency key is derived from the job id: a retried job reproduces
    the same keys and ``PostgresCostLedger``'s UNIQUE constraint refuses the
    duplicate charge — ADR 0002's "never double-charge on retry", inherited
    rather than reimplemented. Entity ids are the database's own
    company/person ids (so ``v_lead_cost`` and evidence rows agree on
    identity), falling back to the simulator's derived id only for an entity
    this lead's run doesn't own.
    """

    def __init__(
        self,
        pg_ledger: PostgresCostLedger,
        *,
        lead_id: UUID,
        job_id: UUID,
        entities: dict[str, tuple[EntityType, UUID]],
    ) -> None:
        super().__init__()
        self._pg = pg_ledger
        self._lead_id = lead_id
        self._job_id = job_id
        self._entities = entities

    def record(
        self, provider: str, entity: Entity, result: ProviderResult, *, cache_hit: bool = False
    ) -> None:
        super().record(provider, entity, result, cache_hit=cache_hit)
        mapped = self._entities.get(entity.canonical_key)
        entity_type, entity_id = (
            mapped
            if mapped is not None
            else (
                entity.entity_type,
                entity.entity_id,
            )
        )
        self._pg.record_provider_call(
            idempotency_key=f"job:{self._job_id}:{provider}:{entity.canonical_key}",
            provider=provider,
            entity_type=entity_type,
            entity_id=entity_id,
            status=result.status,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            lead_id=self._lead_id,
            cache_hit=cache_hit,
        )


def _scaffold_payloads(outcome: PolicyOutcome, tau: float) -> dict[str, dict[str, Any]]:
    """What each scaffold hop's lead_event records about the stage it names."""
    return {
        "SCORING": {"model_version": outcome.scoring.breakdown.model_version},
        "FETCHING_EVIDENCE": {
            "providers_called": list(outcome.providers_called),
            "cost_usd": outcome.cost_usd,
            "cache_hits": outcome.cache_hits,
        },
        "INTEGRATING": {"stop_reason": outcome.stop_reason},
        "DECISION": {
            "decision": str(outcome.decision),
            "confidence": outcome.confidence,
            "tau": tau,
            "autonomous": outcome.autonomous,
        },
    }


def build_handlers(
    pool: ConnectionPool,
    *,
    runtime: SimulatedEnrichmentRuntime | None = None,
    leads: list[EvalLead] | None = None,
    provider_mode: str | None = None,
) -> dict[str, JobHandler]:
    """The worker's production handler registry.

    Covers ``compute_score`` — the job type ingestion enqueues for every new
    lead — and deliberately nothing else; see the module docstring for why the
    other scaffold job types stay unclaimed. Pass `runtime` (or `leads`) to
    skip the dataset generation + model fit, which tests holding the session
    dataset do.
    """
    mode = provider_mode if provider_mode is not None else RUNTIME.provider_mode
    if mode != "simulated":
        raise UnsupportedProviderModeError(
            f"PROVIDER_MODE={mode!r} has no provider adapters — none have been written "
            "(ADR 0003). Only 'simulated' is currently runnable."
        )

    resolved_runtime = runtime if runtime is not None else build_runtime(leads)
    evidence_store = PostgresEvidenceStore(pool)
    cost_ledger = PostgresCostLedger(pool)

    def compute_score(ctx: JobContext) -> None:
        job = ctx.job
        if job.lead_id is None:
            raise ValueError("compute_score requires a lead_id on the job")
        if ctx.lead_status is not LeadStatus.NEW or ctx.lead_version is None:
            raise ValueError(
                f"compute_score expects a NEW lead; lead {job.lead_id} is {ctx.lead_status}"
            )

        with traced(
            _TRACER, "handler.compute_score", attributes={"arie.lead_id": job.lead_id}
        ) as span:
            identity = _load_identity(ctx.conn, job.lead_id)
            corpus_lead = resolved_runtime.corpus_lead_for(
                identity.canonical_email, identity.canonical_domain
            )
            entities: dict[str, tuple[EntityType, UUID]] = {
                corpus_lead.company.canonical_domain: ("company", identity.company_id),
                corpus_lead.person.email: ("person", identity.person_id),
            }

            run_ctx = RunContext(
                registry=resolved_runtime.registry,
                ledger=_DurableCallLedger(
                    cost_ledger, lead_id=job.lead_id, job_id=job.job_id, entities=entities
                ),
                cache=_DurableEvidenceCache(evidence_store, entities),
            )
            outcome = resolved_runtime.policy.run(corpus_lead, run_ctx)
            tau = resolved_runtime.policy.model.tau

            # Walk the scaffold NEW -> ... -> DECISION as the graph defines it,
            # one audited transition per hop, all inside the work transaction.
            payloads = _scaffold_payloads(outcome, tau)
            version = ctx.lead_version
            status = LeadStatus.NEW
            while status is not LeadStatus.DECISION:
                advanced = next_status(status)
                assert advanced is not None  # NEW..INTEGRATING always advance; see transitions.py
                status = advanced
                version = apply_transition(
                    ctx.conn,
                    lead_id=job.lead_id,
                    expected_version=version,
                    new_status=status,
                    event_type=f"policy:{status.lower()}",
                    payload=payloads.get(str(status), {}),
                ).new_version

            with ctx.conn.cursor() as cur:
                cur.execute(
                    _INSERT_SCORE,
                    {
                        "lead_id": job.lead_id,
                        "total_score": outcome.scoring.breakdown.total_score,
                        "decision_confidence": outcome.confidence,
                        "component_breakdown": Jsonb(outcome.scoring.breakdown.components),
                        "model_version": outcome.scoring.breakdown.model_version,
                    },
                )

            route = decision_route(outcome.decision, outcome.autonomous)
            final: LeadStatus
            if route == "escalate_human":
                request_review(
                    ctx.conn,
                    lead_id=job.lead_id,
                    expected_version=version,
                    original_decision=str(outcome.decision),
                )
                final = LeadStatus.AWAITING_HUMAN
            else:
                branched = next_status(LeadStatus.DECISION, outcome=route)
                assert branched is not None  # route is a DECISION_OUTCOMES key by construction
                final = branched
                apply_transition(
                    ctx.conn,
                    lead_id=job.lead_id,
                    expected_version=version,
                    new_status=final,
                    event_type="policy:decided",
                    payload={"decision": str(outcome.decision), "route": route},
                )

            set_attributes(
                span,
                {
                    "arie.decision": str(outcome.decision),
                    "arie.confidence": outcome.confidence,
                    "arie.autonomous": outcome.autonomous,
                    "arie.lead.final_status": str(final),
                    "arie.cost_usd": outcome.cost_usd,
                    "arie.stop_reason": outcome.stop_reason,
                },
            )
        # Transitions were applied here, hop by hop; None tells the worker
        # there is no further transition for it to apply.
        return None

    return {"compute_score": compute_score}
