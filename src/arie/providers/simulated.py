"""Simulated provider layer.

Serves the frozen observations produced by ``arie.evalgen`` through the same
``EnrichmentProvider`` Protocol that real adapters will implement. The policy
controller, scorer, and benchmark cannot tell the difference — which is what
lets the benchmark run offline and deterministically while production calls live
vendors, with no separate code path for either.

**Replay, not resampling.** This layer never draws a random number. All
stochasticity — coverage, noise, latency, failures, and the shared per-company
obscurity that correlates misses — was resolved at generation time and frozen
into the dataset. Sampling here would mean the adaptive policy and the waterfall
baseline faced different luck, and part of any measured difference between them
would be noise rather than strategy.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from arie.core.types import Entity, EntityType, ProviderResult, ProviderStatus
from arie.evalgen.schema import EvalLead, ProviderObservation
from arie.providers.base import ProviderRegistry
from arie.providers.catalog import CATALOG, ProviderSpec

# Stable namespace so a canonical key always maps to the same UUID, in this
# process and any other.
ENTITY_NAMESPACE = uuid.UUID("6f1c9f8e-0a3d-4f2b-9c5e-1d7a2b3c4d5e")


class UnknownEntityError(KeyError):
    """Raised when a provider is asked about an entity not in the dataset.

    Deliberately *not* returned as a MISS. A data miss means "this vendor has
    nothing on that company", which is a legitimate outcome the policy must
    handle. An unknown key means the harness asked about something that does
    not exist — a bug. Collapsing the two would let wiring errors masquerade as
    poor provider coverage and silently corrupt the benchmark.
    """


def entity_id_for(canonical_key: str) -> uuid.UUID:
    return uuid.uuid5(ENTITY_NAMESPACE, canonical_key)


def company_entity(lead: EvalLead) -> Entity:
    return Entity(
        entity_type="company",
        entity_id=entity_id_for(lead.company.canonical_domain),
        canonical_key=lead.company.canonical_domain,
        attributes={"legal_name": lead.company.legal_name},
    )


def person_entity(lead: EvalLead) -> Entity:
    return Entity(
        entity_type="person",
        entity_id=entity_id_for(lead.person.email),
        canonical_key=lead.person.email,
        attributes={"full_name": lead.person.full_name},
    )


def entity_for(lead: EvalLead, entity_type: EntityType) -> Entity:
    return company_entity(lead) if entity_type == "company" else person_entity(lead)


class ObservationStore:
    """Frozen provider responses, indexed by (provider, canonical key).

    Keyed on the *business* key — domain for companies, email for people —
    rather than the lead id, because that is the granularity the responses were
    generated at. Two contacts at one company resolve to the same company key
    and therefore the same company-provider response, which is what makes
    company-level caching lossless.
    """

    def __init__(self, leads: Iterable[EvalLead]) -> None:
        self._by_key: dict[tuple[str, str], ProviderObservation] = {}
        self._specs = {spec.name: spec for spec in CATALOG}

        for lead in leads:
            for provider_name, observation in lead.observations.items():
                spec = self._specs[provider_name]
                key = (
                    lead.company.canonical_domain
                    if spec.entity_type == "company"
                    else lead.person.email
                )
                existing = self._by_key.get((provider_name, key))
                if existing is not None and existing != observation:
                    # Would mean the generator produced two different answers
                    # for one entity — the exact incoherence this keying is
                    # designed to prevent. Fail loudly rather than pick one.
                    raise ValueError(f"conflicting frozen observations for {provider_name} @ {key}")
                self._by_key[(provider_name, key)] = observation

    def get(self, provider_name: str, canonical_key: str) -> ProviderObservation:
        try:
            return self._by_key[(provider_name, canonical_key)]
        except KeyError as exc:
            raise UnknownEntityError(
                f"no frozen observation for provider={provider_name!r} entity={canonical_key!r}"
            ) from exc

    def __len__(self) -> int:
        return len(self._by_key)


@dataclass(frozen=True)
class SimulatedProvider:
    """Replays one provider's frozen responses.

    Satisfies ``EnrichmentProvider`` structurally. The cost, latency, and status
    reported here are the ones recorded at generation time, so the ledger totals
    reconcile exactly against the dataset.
    """

    spec: ProviderSpec
    store: ObservationStore

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def entity_type(self) -> EntityType:
        return self.spec.entity_type

    @property
    def provides_fields(self) -> tuple[str, ...]:
        return self.spec.provides_fields

    @property
    def base_cost_usd(self) -> float:
        return self.spec.base_cost_usd

    @property
    def p50_latency_ms(self) -> int:
        return self.spec.p50_latency_ms

    @property
    def p95_latency_ms(self) -> int:
        return self.spec.p95_latency_ms

    def fetch(self, entity: Entity) -> ProviderResult:
        if entity.entity_type != self.spec.entity_type:
            raise ValueError(
                f"{self.name} serves {self.spec.entity_type} entities, got {entity.entity_type}"
            )

        observation = self.store.get(self.spec.name, entity.canonical_key)

        # Only ever surface fields this provider claims to supply. The EVoI
        # controller estimates information value from `provides_fields`; a
        # provider returning more than it declares would make those estimates
        # wrong in a way that is very hard to trace back.
        fields = {
            name: value
            for name, value in observation.fields.items()
            if name in self.spec.provides_fields
        }

        return ProviderResult(
            fields=fields,
            confidence=self._confidence(observation.status),
            cost_usd=observation.cost_usd,
            latency_ms=observation.latency_ms,
            status=observation.status,
            raw={"provider": self.spec.name, "entity": entity.canonical_key},
        )

    def _confidence(self, status: ProviderStatus) -> float:
        """Source reliability, derived from the declared error profile.

        Deriving it rather than hardcoding a parallel table means a provider's
        stated trustworthiness cannot drift away from the noise actually
        applied to its observations.
        """
        if status is not ProviderStatus.SUCCESS:
            return 0.0
        return round(1.0 - max(self.spec.categorical_error, self.spec.numeric_noise), 4)


@dataclass
class CallRecord:
    provider: str
    canonical_key: str
    status: ProviderStatus
    cost_usd: float
    latency_ms: float
    cache_hit: bool


@dataclass
class CallLedger:
    """Running account of what a policy spent.

    Cache hits are recorded as calls with zero cost, not omitted. Dropping them
    would make cache-hit rate unmeasurable, and it is the metric that justifies
    the company-level evidence store.
    """

    records: list[CallRecord] = field(default_factory=list)

    def record(
        self, provider: str, entity: Entity, result: ProviderResult, *, cache_hit: bool = False
    ) -> None:
        self.records.append(
            CallRecord(
                provider=provider,
                canonical_key=entity.canonical_key,
                status=result.status,
                cost_usd=0.0 if cache_hit else result.cost_usd,
                latency_ms=0.0 if cache_hit else result.latency_ms,
                cache_hit=cache_hit,
            )
        )

    @property
    def total_cost_usd(self) -> float:
        return round(sum(r.cost_usd for r in self.records), 10)

    @property
    def total_latency_ms(self) -> float:
        """Wall-clock proxy: sequential calls sum.

        Parallel execution would take the max within a batch instead. The
        benchmark reports sequential latency because it is the conservative
        reading and does not depend on an execution-model assumption.
        """
        return round(sum(r.latency_ms for r in self.records), 6)

    @property
    def billable_calls(self) -> int:
        return sum(1 for r in self.records if not r.cache_hit)

    @property
    def cache_hits(self) -> int:
        return sum(1 for r in self.records if r.cache_hit)

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / len(self.records) if self.records else 0.0

    def by_status(self, status: ProviderStatus) -> int:
        return sum(1 for r in self.records if r.status is status)


def build_registry(store: ObservationStore) -> ProviderRegistry:
    """Register every catalogue provider against a frozen observation store."""
    registry = ProviderRegistry()
    for spec in CATALOG:
        registry.register(SimulatedProvider(spec=spec, store=store), capability=spec.capability)
    return registry


def build_from_leads(leads: Iterable[EvalLead]) -> tuple[ObservationStore, ProviderRegistry]:
    store = ObservationStore(leads)
    return store, build_registry(store)


def fetch_all(
    registry: ProviderRegistry,
    lead: EvalLead,
    ledger: CallLedger | None = None,
    providers: Iterable[str] | None = None,
) -> Mapping[str, ProviderResult]:
    """Call a set of providers for one lead — the full-enrichment baseline.

    Provider order follows the catalogue, so results and ledger entries are
    deterministic regardless of the caller's iteration order.
    """
    wanted = set(providers) if providers is not None else None
    results: dict[str, ProviderResult] = {}

    for spec in CATALOG:
        if wanted is not None and spec.name not in wanted:
            continue
        provider = registry.get(spec.name)
        entity = entity_for(lead, spec.entity_type)
        result = provider.fetch(entity)
        results[spec.name] = result
        if ledger is not None:
            ledger.record(spec.name, entity, result)

    return results
