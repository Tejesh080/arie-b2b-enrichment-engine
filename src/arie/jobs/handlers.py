"""Production job handlers — the wiring the handoff called M1's biggest gap.

This module composes pieces that all already existed — ``CalibratedBoundsPolicy``,
the simulated provider registry, ``PostgresEvidenceStore``, ``PostgresCostLedger``,
the state graph, and ``arie.approval.workflow.request_review`` — into the one
handler shape ``docs/architecture.md`` describes: *"a compute_score handler
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

**Simulated mode replays the frozen corpus for corpus identities, and
synthesizes deterministic simulated evidence for everything else.**
``SimulatedProvider`` still raises for entities outside its observation store —
the guard that keeps a wiring error from masquerading as poor provider
coverage. The handler looks the ingested lead's person up in the corpus by
normalized email; a hit replays its frozen observations byte-for-byte
(unchanged from before), and a miss falls back to
``arie.providers.synthetic.synthesize_corpus_lead`` — the same catalogue,
rates, and noise model, seeded from the lead's own canonical keys so the
result is deterministic and cache-coherent. Out-of-corpus leads used to fail
into the dead-letter path by design; now that the public demo accepts
arbitrary identities, a data source for them exists and the honest outcome is
a receipt, not a dead letter.

**Post-M1 P5 — ``PROVIDER_MODE=live`` has a real backend now, built and
registered by a separate function, ``_build_live_handlers``.** It has no
corpus restriction at all (any ingested lead can be enriched) and does not run
``CalibratedBoundsPolicy`` — see that function's own docstring for exactly why
not and what it runs instead. ``build_handlers`` dispatches to one builder or
the other by ``provider_mode``; both share ``_finalize_decision`` (the
DECISION-node branch, including the shadow-mode suppression below) and
``_walk_to_decision``/``_evidence_snapshot`` so the two paths can't drift
apart on those.

**Post-M1 P5 — shadow mode.** A lead ingested with ``mode="shadow"``
(``leads.is_shadow``, set once at ingestion, never updated after) still runs
the full acquisition loop and still gets a `decision_receipts` row with a real
recommendation/confidence/cost/stop-reason — but ``_finalize_decision`` routes
it to ``LeadStatus.SHADOW_EVALUATED`` instead of an authoritative branch,
skipping ``request_review`` entirely. This applies uniformly to both provider
modes: a shadow lead can run over the simulated corpus or the real live
provider, and either way it never controls a routing outcome.

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
from arie.identity.normalize import domain_from_email, normalize_domain, normalize_email
from arie.ledger.store import PostgresCostLedger
from arie.observability.tracing import get_tracer, set_attributes, traced
from arie.policy.base import EvidenceCache, PolicyOutcome, RunContext
from arie.providers.base import EnrichmentProvider, ProviderRegistry
from arie.providers.catalog import BY_NAME
from arie.providers.live_abstract import (
    LIVE_POLICY_NAME,
    AbstractCompanyEnrichmentProvider,
)
from arie.providers.simulated import CallLedger, build_from_leads
from arie.providers.synthetic import synthesize_corpus_lead
from arie.scoring.engine import ScoringResult, score_evidence
from arie.statemachine.apply import apply_transition
from arie.statemachine.transitions import next_status

if TYPE_CHECKING:  # pragma: no cover - typing only
    from arie.evalgen.schema import EvalLead
    from arie.jobs.worker import JobContext, JobHandler
    from arie.policy.production import CalibratedBoundsPolicy

_TRACER = get_tracer("arie.jobs.handlers")


class UnsupportedProviderModeError(RuntimeError):
    """Raised when handlers are built for a provider mode this module doesn't
    recognise at all (anything other than ``'simulated'`` or ``'live'``).

    Post-M1 P5: ``PROVIDER_MODE=live`` is now backed by one real adapter
    (``arie.providers.live_abstract``, ADR 0003's "real adapters go alongside
    the simulator" finally exercised) and no longer raises this — a missing
    ``ABSTRACT_COMPANY_API_KEY`` instead raises
    ``AbstractCompanyConfigurationError`` at the same build-time point, a
    distinct failure ("live mode is misconfigured") from "live mode doesn't
    exist". Refusing an unrecognised mode string at build time keeps
    misconfiguration loud rather than silently falling back to the simulator,
    which would report costs and coverage for vendors it never called.
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
    is_shadow: bool
    """Post-M1 P5. Fixed at ingestion — see ``arie.api.ingest``'s idempotency
    semantics. Read here so a single ``compute_score`` variant (simulated or
    live) can branch its own DECISION-node outcome without a second query."""
    full_name: str | None = None
    company_name: str | None = None
    """Display names as submitted, used only when synthesizing an
    out-of-corpus identity so its receipt shows what the submitter typed
    rather than generator-invented names."""


_SELECT_LEAD_IDENTITY = """
    SELECT l.company_id, l.person_id, l.is_shadow, c.canonical_domain, p.canonical_email,
           p.full_name, c.name AS company_name
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

_INSERT_DECISION_RECEIPT = """
    INSERT INTO decision_receipts (
        lead_id, decision, autonomous, confidence, tau,
        score_value, score_lower, score_upper, stop_reason,
        policy_name, scorer_version, confidence_calibration, evidence_snapshot
    ) VALUES (
        %(lead_id)s, %(decision)s, %(autonomous)s, %(confidence)s, %(tau)s,
        %(score_value)s, %(score_lower)s, %(score_upper)s, %(stop_reason)s,
        %(policy_name)s, %(scorer_version)s, %(confidence_calibration)s, %(evidence_snapshot)s
    )
"""


def _evidence_snapshot(scoring: ScoringResult) -> dict[str, Any]:
    """What `decision_receipts.evidence_snapshot` freezes — the winning source per
    field and which fields were still unknown, as they stood at decision time. See
    `arie.api.receipt`'s module docstring for why this can't be reconstructed later
    from the (company/person-keyed, mutable) `evidence` table.

    Takes the bare `ScoringResult` rather than a `PolicyOutcome` deliberately:
    both the simulated (corpus/`EvalLead`) and live (real `Entity`) handlers
    below produce one, and this is the one place their receipt-writing code is
    shared.
    """
    resolutions = scoring.resolutions
    return {
        "known": [
            {
                "field": field_name,
                "source": resolution.source,
                "confidence": resolution.confidence,
                "candidate_count": resolution.candidate_count,
                "contested": resolution.contested,
            }
            for field_name, resolution in sorted(resolutions.items())
        ],
        "unknown": list(scoring.signals.unknown_fields),
    }


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
        is_shadow=row["is_shadow"],
        full_name=row["full_name"],
        company_name=row["company_name"],
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


def _finalize_decision(
    conn: psycopg.Connection,
    *,
    lead_id: UUID,
    version: int,
    decision: Decision,
    autonomous: bool,
    is_shadow: bool,
) -> LeadStatus:
    """The DECISION node's branch — shared by every ``compute_score`` variant
    (simulated/corpus and live/real) so shadow semantics can't drift between
    them.

    **Post-M1 P5 shadow branch.** A shadow lead never reaches an authoritative
    outcome regardless of what `decision`/`autonomous` say: no
    ``request_review`` (no fake human action), no AUTO_ROUTED/REJECT/
    MANUAL_REVIEW (nothing ``workflows/n8n/outcome-sync.json``'s FINALIZED
    gate or a real CRM sync would ever see), and no overwrite of any existing
    business outcome — there is none, because a shadow lead was never routed
    in the first place. It lands on ``LeadStatus.SHADOW_EVALUATED`` instead,
    which ``arie.statemachine.transitions.TERMINAL`` includes (nothing further
    auto-advances it) but every business-semantic group
    (QUALIFIED/REJECTED/AWAITING_REVIEW/FAILURE/FINALIZED) deliberately
    excludes. `decision`/`autonomous` are still frozen into `decision_receipts`
    by the caller before this runs, so the receipt reports "ARIE would have
    escalated this" rather than losing the recommendation.
    """
    if is_shadow:
        apply_transition(
            conn,
            lead_id=lead_id,
            expected_version=version,
            new_status=LeadStatus.SHADOW_EVALUATED,
            event_type="policy:shadow_evaluated",
            payload={"decision": str(decision), "autonomous": autonomous},
        )
        return LeadStatus.SHADOW_EVALUATED

    route = decision_route(decision, autonomous)
    if route == "escalate_human":
        request_review(
            conn, lead_id=lead_id, expected_version=version, original_decision=str(decision)
        )
        return LeadStatus.AWAITING_HUMAN

    final = next_status(LeadStatus.DECISION, outcome=route)
    assert final is not None  # route is a DECISION_OUTCOMES key by construction
    apply_transition(
        conn,
        lead_id=lead_id,
        expected_version=version,
        new_status=final,
        event_type="policy:decided",
        payload={"decision": str(decision), "route": route},
    )
    return final


def _walk_to_decision(conn: psycopg.Connection, *, lead_id: UUID, version: int) -> int:
    """Advance NEW -> SCORING -> FETCHING_EVIDENCE -> INTEGRATING -> DECISION.

    Shared scaffold walk for every ``compute_score`` variant. Payloads are
    intentionally minimal (``{}``) here — the simulated handler still records
    its richer per-hop payloads itself (providers called, cost, stop reason)
    because those facts only exist once its policy has actually run; the live
    handler does the same for the hops where it has something to say.
    """
    status = LeadStatus.NEW
    while status is not LeadStatus.DECISION:
        advanced = next_status(status)
        assert advanced is not None  # NEW..INTEGRATING always advance; see transitions.py
        status = advanced
        version = apply_transition(
            conn,
            lead_id=lead_id,
            expected_version=version,
            new_status=status,
            event_type=f"policy:{status.lower()}",
            payload={},
        ).new_version
    return version


def build_handlers(
    pool: ConnectionPool,
    *,
    runtime: SimulatedEnrichmentRuntime | None = None,
    leads: list[EvalLead] | None = None,
    provider_mode: str | None = None,
    live_provider: EnrichmentProvider | None = None,
) -> dict[str, JobHandler]:
    """The worker's production handler registry.

    Covers ``compute_score`` — the job type ingestion enqueues for every new
    lead — and deliberately nothing else; see the module docstring for why the
    other scaffold job types stay unclaimed. Pass `runtime` (or `leads`) to
    skip the dataset generation + model fit, which tests holding the session
    dataset do; both provider modes need the fitted confidence model, only
    `simulated` needs the corpus/registry.

    `live_provider` lets a caller (tests, ``scripts/live_provider_smoke.py``)
    inject an already-built adapter instead of paying
    ``AbstractCompanyEnrichmentProvider.build()``'s API-key check — mirroring
    ``arie.llm.deepseek.DeepSeekSignalExtractor``'s own injectable-client
    pattern.
    """
    mode = provider_mode if provider_mode is not None else RUNTIME.provider_mode
    if mode not in ("simulated", "live"):
        raise UnsupportedProviderModeError(
            f"PROVIDER_MODE={mode!r} is not a recognised provider mode — only 'simulated' and "
            "'live' are runnable."
        )

    resolved_runtime = runtime if runtime is not None else build_runtime(leads)

    if mode == "simulated":
        return _build_simulated_handlers(pool, resolved_runtime)
    return _build_live_handlers(pool, resolved_runtime, live_provider=live_provider)


def _build_simulated_handlers(
    pool: ConnectionPool, resolved_runtime: SimulatedEnrichmentRuntime
) -> dict[str, JobHandler]:
    """``PROVIDER_MODE=simulated`` — replays the frozen corpus for identities
    in ``resolved_runtime.corpus_by_email`` (unchanged from before P5), and
    synthesizes deterministic simulated evidence for everything else (see
    ``arie.providers.synthetic``)."""
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
            _TRACER,
            "handler.compute_score",
            attributes={"arie.lead_id": job.lead_id, "arie.provider_mode": "simulated"},
        ) as span:
            identity = _load_identity(ctx.conn, job.lead_id)
            try:
                corpus_lead = resolved_runtime.corpus_lead_for(
                    identity.canonical_email, identity.canonical_domain
                )
                registry = resolved_runtime.registry
                synthetic = False
            except UnknownCorpusIdentityError:
                # Out-of-corpus identity — synthesize deterministic simulated
                # evidence instead of dead-lettering. Corpus identities never
                # reach this branch, so the frozen replay is untouched; see
                # arie.providers.synthetic for the determinism contract that
                # keeps the durable evidence cache coherent across contacts.
                domain = identity.canonical_domain or domain_from_email(identity.canonical_email)
                if domain is None:
                    # Unreachable for ingested leads (a canonical email always
                    # carries a domain), but if it ever happens, fail into the
                    # ordinary dead-letter path — which now marks the lead —
                    # rather than synthesizing from nothing.
                    raise UnknownCorpusIdentityError(
                        f"lead {job.lead_id} has no company domain to synthesize from"
                    ) from None
                corpus_lead = synthesize_corpus_lead(
                    canonical_email=identity.canonical_email,
                    canonical_domain=domain,
                    full_name=identity.full_name,
                    company_name=identity.company_name,
                )
                _, registry = build_from_leads([corpus_lead])
                synthetic = True
            entities: dict[str, tuple[EntityType, UUID]] = {
                corpus_lead.company.canonical_domain: ("company", identity.company_id),
                corpus_lead.person.email: ("person", identity.person_id),
            }

            run_ctx = RunContext(
                registry=registry,
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
                cur.execute(
                    _INSERT_DECISION_RECEIPT,
                    {
                        "lead_id": job.lead_id,
                        "decision": str(outcome.decision),
                        "autonomous": outcome.autonomous,
                        "confidence": outcome.confidence,
                        "tau": tau,
                        "score_value": outcome.scoring.bounds.current,
                        "score_lower": outcome.scoring.bounds.lower,
                        "score_upper": outcome.scoring.bounds.upper,
                        "stop_reason": outcome.stop_reason,
                        "policy_name": resolved_runtime.policy.name,
                        "scorer_version": outcome.scoring.breakdown.model_version,
                        "confidence_calibration": resolved_runtime.policy.model.method,
                        "evidence_snapshot": Jsonb(_evidence_snapshot(outcome.scoring)),
                    },
                )

            final = _finalize_decision(
                ctx.conn,
                lead_id=job.lead_id,
                version=version,
                decision=outcome.decision,
                autonomous=outcome.autonomous,
                is_shadow=identity.is_shadow,
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
                    "arie.shadow": identity.is_shadow,
                    "arie.synthetic_identity": synthetic,
                },
            )
        # Transitions were applied here, hop by hop; None tells the worker
        # there is no further transition for it to apply.
        return None

    return {"compute_score": compute_score}


def _build_live_handlers(
    pool: ConnectionPool,
    resolved_runtime: SimulatedEnrichmentRuntime,
    *,
    live_provider: EnrichmentProvider | None = None,
) -> dict[str, JobHandler]:
    """``PROVIDER_MODE=live`` (post-M1 P5) — one real adapter, any ingested
    lead (no corpus restriction).

    **Why this can't reuse `CalibratedBoundsPolicy.run`.** That method's
    signature takes an `EvalLead` and walks `arie.providers.catalog.CATALOG`
    (8 simulated providers) via `RunContext.fetch`, which resolves an entity
    from `lead.company.canonical_domain`/`lead.person.email` and looks the
    provider up in `arie.providers.catalog.BY_NAME` — a real lead has neither
    an `EvalLead` nor a catalogue entry, and adding one would perturb frozen
    dataset generation (`arie.evalgen.generator` iterates `CATALOG` to freeze
    observations) and the M0 benchmark it feeds. So this handler is a second,
    much smaller acquisition loop — trivial because there is exactly one
    provider to decide about, not eight — built from the same *lead-
    independent* primitives the simulated handler's policy sits on top of:
    `arie.scoring.engine.score_evidence` (facts -> score/bounds/signals) and
    `ConfidenceModel.predict` (both take a bare `ScoringResult`, never an
    `EvalLead`). It reuses the exact same `PostgresEvidenceStore`/
    `PostgresCostLedger`, the same `_finalize_decision` shadow/normal branch,
    and the same `decision_receipts`/`scores` inserts as the simulated path —
    only the acquisition loop above them differs.

    **The confidence model is the corpus-calibrated one, reused as-is.** No
    other calibration data exists. `ConfidenceModel.predict` only reads
    `ScoringResult.signals` (completeness, conflict, boundary distance, ...),
    which are well-defined for any evidence bundle — but applying a model
    fitted on synthetic corpus signals to real evidence is an unvalidated
    assumption, stated here and in `docs/architecture.md`'s P5 section, not
    quietly treated as equivalent to the simulated path's own guarantee.
    """
    provider = (
        live_provider if live_provider is not None else AbstractCompanyEnrichmentProvider.build()
    )
    evidence_store = PostgresEvidenceStore(pool)
    cost_ledger = PostgresCostLedger(pool)
    model = resolved_runtime.policy.model

    def compute_score(ctx: JobContext) -> None:
        job = ctx.job
        if job.lead_id is None:
            raise ValueError("compute_score requires a lead_id on the job")
        if ctx.lead_status is not LeadStatus.NEW or ctx.lead_version is None:
            raise ValueError(
                f"compute_score expects a NEW lead; lead {job.lead_id} is {ctx.lead_status}"
            )

        with traced(
            _TRACER,
            "handler.compute_score",
            attributes={"arie.lead_id": job.lead_id, "arie.provider_mode": "live"},
        ) as span:
            identity = _load_identity(ctx.conn, job.lead_id)
            version = _walk_to_decision(ctx.conn, lead_id=job.lead_id, version=ctx.lead_version)

            now = datetime.now(UTC)
            fresh = evidence_store.get_all_fresh("company", identity.company_id, now=now)
            scoring = score_evidence(fresh, now)
            already_has_fields = set(provider.provides_fields) <= {e.field_name for e in fresh}
            can_call_provider = identity.canonical_domain is not None and not already_has_fields

            if scoring.bounds.is_settled:
                stop_reason = "decision_settled"
            elif fresh and model.predict(scoring) >= model.tau:
                stop_reason = "confidence_reached"
            elif can_call_provider:
                assert identity.canonical_domain is not None  # can_call_provider guarantees this
                entity = Entity(
                    entity_type="company",
                    entity_id=identity.company_id,
                    canonical_key=identity.canonical_domain,
                )
                result = provider.fetch(entity)
                cost_ledger.record_provider_call(
                    idempotency_key=f"job:{job.job_id}:{provider.name}:{entity.canonical_key}",
                    provider=provider.name,
                    entity_type="company",
                    entity_id=identity.company_id,
                    status=result.status,
                    cost_usd=result.cost_usd,
                    latency_ms=result.latency_ms,
                    lead_id=job.lead_id,
                    cache_hit=False,
                )
                if result.status is ProviderStatus.SUCCESS and result.fields:
                    evidence_store.put_many(
                        Evidence(
                            entity_type="company",
                            entity_id=identity.company_id,
                            field_name=field_name,
                            value=value,
                            source=provider.name,
                            confidence=result.confidence,
                            ttl_seconds=ttl_for_field(field_name),
                            fetched_at=now,
                        )
                        for field_name, value in result.fields.items()
                    )
                    fresh = evidence_store.get_all_fresh("company", identity.company_id, now=now)
                    scoring = score_evidence(fresh, now)

                if scoring.bounds.is_settled:
                    stop_reason = "decision_settled"
                elif fresh and model.predict(scoring) >= model.tau:
                    stop_reason = "confidence_reached"
                else:
                    stop_reason = "all_providers_called"
            elif identity.canonical_domain is None:
                # Nothing to enrich by at all -- the one real provider
                # requires a domain this lead never resolved one for.
                stop_reason = "no_domain_available"
            else:
                # already_has_fields: the one provider's fields are already
                # fresh in the evidence store -- "ARIE decided it didn't need
                # to call a real paid API because enough evidence already
                # exists" (P5's own point), not a failure. Recorded as a
                # zero-cost cache hit, not silently skipped -- handoff item
                # #5: "cache hits must be recorded, not skipped", the same
                # rule _DurableCallLedger.record already follows for the
                # simulated path.
                cost_ledger.record_provider_call(
                    idempotency_key=f"job:{job.job_id}:{provider.name}:{identity.canonical_domain}",
                    provider=provider.name,
                    entity_type="company",
                    entity_id=identity.company_id,
                    status=ProviderStatus.SUCCESS,
                    cost_usd=0.0,
                    latency_ms=0.0,
                    lead_id=job.lead_id,
                    cache_hit=True,
                )
                stop_reason = "all_providers_called"

            confidence = model.predict(scoring)
            autonomous = confidence >= model.tau

            with ctx.conn.cursor() as cur:
                cur.execute(
                    _INSERT_SCORE,
                    {
                        "lead_id": job.lead_id,
                        "total_score": scoring.breakdown.total_score,
                        "decision_confidence": confidence,
                        "component_breakdown": Jsonb(scoring.breakdown.components),
                        "model_version": scoring.breakdown.model_version,
                    },
                )
                cur.execute(
                    _INSERT_DECISION_RECEIPT,
                    {
                        "lead_id": job.lead_id,
                        "decision": str(scoring.decision),
                        "autonomous": autonomous,
                        "confidence": confidence,
                        "tau": model.tau,
                        "score_value": scoring.bounds.current,
                        "score_lower": scoring.bounds.lower,
                        "score_upper": scoring.bounds.upper,
                        "stop_reason": stop_reason,
                        "policy_name": LIVE_POLICY_NAME,
                        "scorer_version": scoring.breakdown.model_version,
                        "confidence_calibration": model.method,
                        "evidence_snapshot": Jsonb(_evidence_snapshot(scoring)),
                    },
                )

            final = _finalize_decision(
                ctx.conn,
                lead_id=job.lead_id,
                version=version,
                decision=scoring.decision,
                autonomous=autonomous,
                is_shadow=identity.is_shadow,
            )

            set_attributes(
                span,
                {
                    "arie.decision": str(scoring.decision),
                    "arie.confidence": confidence,
                    "arie.autonomous": autonomous,
                    "arie.lead.final_status": str(final),
                    "arie.stop_reason": stop_reason,
                    "arie.shadow": identity.is_shadow,
                },
            )
        return None

    return {"compute_score": compute_score}
