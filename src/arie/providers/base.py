"""Provider abstraction.

The single most important property of this module: the policy controller, the
scoring engine, and the benchmark harness never learn whether they are talking
to a simulator or a live API. That is what lets the benchmark run offline and
deterministically while the production path calls real vendors, without a
separate code path for either.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from arie.core.types import Entity, EntityType, ProviderResult


@runtime_checkable
class EnrichmentProvider(Protocol):
    """A source of evidence about an entity, with a declared cost/latency profile.

    The declared profile is not cosmetic — the EVoI controller reads `base_cost_usd`
    and `p50_latency_ms` to decide whether calling this provider is worth it,
    *before* calling it. A provider that misdeclares its cost corrupts the policy.
    """

    name: str
    entity_type: EntityType

    # Fields this provider can supply. The controller uses this to estimate
    # P(flips decision) — a provider covering fields we already know with high
    # confidence has low expected information value.
    provides_fields: tuple[str, ...]

    base_cost_usd: float
    p50_latency_ms: int
    p95_latency_ms: int

    def fetch(self, entity: Entity) -> ProviderResult:
        """Retrieve evidence for `entity`.

        Implementations MUST NOT raise for a data miss — return
        ProviderStatus.MISS instead. Raising is reserved for genuine transport or
        provider failures, which the caller treats as a failover trigger rather
        than as "no data exists".
        """
        ...


class ProviderRegistry:
    """Ordered lookup of providers, grouped by the capability they serve.

    Capability grouping (e.g. "email_finder" -> [hunter, apollo, ...]) is what
    makes failover a *VoI re-evaluation* rather than a blind retry: when one
    provider hard-fails, the controller reconsiders which remaining candidate has
    the best net EVoI, which may well be "none — stop here".
    """

    def __init__(self) -> None:
        self._by_name: dict[str, EnrichmentProvider] = {}
        self._by_capability: dict[str, list[str]] = {}

    def register(self, provider: EnrichmentProvider, capability: str) -> None:
        if provider.name in self._by_name:
            raise ValueError(f"provider already registered: {provider.name}")
        self._by_name[provider.name] = provider
        self._by_capability.setdefault(capability, []).append(provider.name)

    def get(self, name: str) -> EnrichmentProvider:
        return self._by_name[name]

    def all(self) -> list[EnrichmentProvider]:
        return list(self._by_name.values())

    def for_capability(self, capability: str) -> list[EnrichmentProvider]:
        return [self._by_name[n] for n in self._by_capability.get(capability, [])]

    def for_entity_type(self, entity_type: EntityType) -> list[EnrichmentProvider]:
        return [p for p in self._by_name.values() if p.entity_type == entity_type]
