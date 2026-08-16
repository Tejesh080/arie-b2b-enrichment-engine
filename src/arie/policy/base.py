"""Policy interface and shared execution context.

Every strategy runs against the same context: the same frozen observations, the
same cache, the same ledger. That uniformity is what makes the comparison
meaningful — a policy must win by *deciding better*, not by being handed cheaper
infrastructure.

The cache in particular is shared deliberately. Company-level caching is a large
real-world saving, and if only the adaptive policy benefited from it the
headline number would be measuring the cache rather than the acquisition
strategy.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from arie.core.types import Decision, ProviderResult, VoIEvaluation
from arie.evalgen.schema import EvalLead, ValueTier
from arie.providers.base import ProviderRegistry
from arie.providers.catalog import BY_NAME
from arie.providers.simulated import CallLedger, entity_for
from arie.scoring.engine import ScoringResult

# Value of getting one decision right, by deal tier. Only the *ratios* matter:
# these scale the benefit side of the EVoI comparison against provider prices.
#
# The absolute levels are assumptions (see docs/ASSUMPTIONS.md). Note they sit
# far above provider costs, which is realistic and is precisely why teams
# over-enrich by default — almost any call looks justified if you assume it
# might change the answer. The policy's job is to identify when it cannot.
BUSINESS_VALUE_USD: dict[ValueTier, float] = {
    "small": 5.0,
    "medium": 20.0,
    "large": 80.0,
    "whale": 400.0,
}


@dataclass
class EvidenceCache:
    """Company- and person-level result cache, shared across policies.

    Within a single benchmark pass nothing expires, so a hit simply means this
    entity was already fetched for an earlier lead. That is the realistic
    batch-processing case: contacts arrive clustered by company.
    """

    _entries: dict[tuple[str, str], ProviderResult] = field(default_factory=dict)

    def get(self, provider: str, canonical_key: str) -> ProviderResult | None:
        return self._entries.get((provider, canonical_key))

    def put(self, provider: str, canonical_key: str, result: ProviderResult) -> None:
        self._entries[(provider, canonical_key)] = result

    def clear(self) -> None:
        self._entries.clear()


@dataclass
class RunContext:
    """Everything a policy is allowed to touch while processing a lead."""

    registry: ProviderRegistry
    ledger: CallLedger
    cache: EvidenceCache

    def fetch(self, provider_name: str, lead: EvalLead) -> tuple[ProviderResult, bool]:
        """Call a provider, going through the cache. Returns (result, was_cached)."""
        spec = BY_NAME[provider_name]
        entity = entity_for(lead, spec.entity_type)

        cached = self.cache.get(provider_name, entity.canonical_key)
        if cached is not None:
            self.ledger.record(provider_name, entity, cached, cache_hit=True)
            return cached, True

        result = self.registry.get(provider_name).fetch(entity)
        self.cache.put(provider_name, entity.canonical_key, result)
        self.ledger.record(provider_name, entity, result, cache_hit=False)
        return result, False


@dataclass(frozen=True)
class PolicyOutcome:
    """What a policy decided, and what it spent getting there."""

    decision: Decision
    confidence: float
    autonomous: bool
    """Whether confidence cleared tau — i.e. whether a human would be involved."""

    providers_called: tuple[str, ...]
    cost_usd: float
    latency_ms: float
    cache_hits: int
    stop_reason: str
    scoring: ScoringResult
    voi_trace: tuple[VoIEvaluation, ...] = ()

    @property
    def calls_made(self) -> int:
        return len(self.providers_called)


class Policy(Protocol):
    """A strategy for deciding which providers to buy before committing."""

    @property
    def name(self) -> str: ...

    def run(self, lead: EvalLead, ctx: RunContext) -> PolicyOutcome: ...


def business_value(lead: EvalLead, scale: float = 1.0) -> float:
    return BUSINESS_VALUE_USD[lead.value_tier] * scale


def collect_costs(ctx: RunContext, marker: int) -> tuple[float, float, int]:
    """Spend, latency, and cache hits recorded since `marker`."""
    records = ctx.ledger.records[marker:]
    return (
        round(sum(r.cost_usd for r in records), 10),
        round(sum(r.latency_ms for r in records), 6),
        sum(1 for r in records if r.cache_hit),
    )


def observations_from(results: Iterable[tuple[str, ProviderResult]]) -> dict[str, ProviderResult]:
    return dict(results)
