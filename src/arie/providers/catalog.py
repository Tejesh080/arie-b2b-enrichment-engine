"""The simulated provider catalogue.

Declarative data, not behaviour — deliberately so, because two very different
consumers must agree on it exactly:

  * ``arie.evalgen`` generates each lead's provider observations offline and
    freezes them into the dataset, and
  * ``SimulatedProvider`` (implemented in a later step) serves those frozen
    observations at runtime.

Pre-generating observations rather than sampling them per run is what guarantees
every strategy sees *identical* provider behaviour. Sampling at call time would
mean the adaptive policy and the waterfall baseline faced different luck, and
any measured difference between them would be partly noise.

Every parameter here is an assumption. See ``docs/ASSUMPTIONS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arie.core.types import EntityType


@dataclass(frozen=True)
class ProviderSpec:
    """Cost, coverage, and error profile of one simulated source."""

    name: str
    entity_type: EntityType
    capability: str
    provides_fields: tuple[str, ...]

    base_cost_usd: float
    p50_latency_ms: int
    p95_latency_ms: int

    # Probability the provider has *any* data for a typical (non-obscure) entity.
    base_coverage: float

    # How strongly this provider's coverage degrades on obscure companies.
    # Non-zero for every provider, which is what makes misses correlate: a
    # single per-company obscurity draw drags them all down together. With
    # independent misses, "just try the next provider" would always eventually
    # work and the benchmark would never test the *stopping* decision.
    obscurity_sensitivity: float

    # Multiplicative log-normal sigma applied to numeric fields.
    numeric_noise: float
    # Probability a categorical field is reported as a confusable neighbour.
    categorical_error: float
    # Probability of a hard transport failure (distinct from a data miss).
    failure_rate: float

    # Whether the provider charges even when it returns nothing. Sources conflict
    # on real-world billing here, so the benchmark reports both regimes.
    bill_on_miss: bool = False

    tier: str = field(default="mid")


# Ordered cheapest-first. The tuple order is the tuned-waterfall baseline's
# execution order, so it is part of the experiment, not cosmetic.
CATALOG: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="inbound_payload",
        entity_type="person",
        capability="submitted_data",
        # What arrived with the lead itself — a form fill, list import, or
        # webhook. Free and always present, but self-reported and therefore
        # noisy: people describe their own titles inconsistently.
        #
        # Modelling this is not optional. Without a free stage-0 source, the
        # cheap tier could never reach a qualifying score, every cheap-only
        # decision would be "reject", and the validity gate would pass
        # trivially while proving nothing.
        provides_fields=("title_seniority", "title_function"),
        base_cost_usd=0.0,
        p50_latency_ms=0,
        p95_latency_ms=0,
        base_coverage=1.0,
        obscurity_sensitivity=0.0,  # the lead submitted it; obscurity is irrelevant
        numeric_noise=0.0,
        categorical_error=0.18,
        failure_rate=0.0,
        tier="free",
    ),
    ProviderSpec(
        name="internal_crm",
        entity_type="company",
        capability="existing_record",
        provides_fields=("industry", "employee_count"),
        base_cost_usd=0.0,
        p50_latency_ms=15,
        p95_latency_ms=60,
        base_coverage=0.35,  # only leads we have seen before
        obscurity_sensitivity=0.30,
        numeric_noise=0.25,  # stale internal data drifts
        categorical_error=0.08,
        failure_rate=0.005,
        tier="free",
    ),
    ProviderSpec(
        name="dns_web",
        entity_type="company",
        capability="web_presence",
        provides_fields=("industry",),
        base_cost_usd=0.0008,
        p50_latency_ms=250,
        p95_latency_ms=1400,
        base_coverage=0.88,
        obscurity_sensitivity=0.45,
        numeric_noise=0.0,
        categorical_error=0.16,  # inferring industry from a website is lossy
        failure_rate=0.03,
        tier="cheap",
    ),
    ProviderSpec(
        name="firmographics_basic",
        entity_type="company",
        capability="firmographics",
        provides_fields=("employee_count", "industry"),
        base_cost_usd=0.012,
        p50_latency_ms=320,
        p95_latency_ms=1100,
        base_coverage=0.82,
        obscurity_sensitivity=0.55,
        numeric_noise=0.30,
        categorical_error=0.07,
        failure_rate=0.02,
        tier="cheap",
    ),
    ProviderSpec(
        name="contact_enrich",
        entity_type="person",
        capability="person_profile",
        provides_fields=("title_seniority", "title_function"),
        base_cost_usd=0.055,
        p50_latency_ms=480,
        p95_latency_ms=2200,
        base_coverage=0.74,
        obscurity_sensitivity=0.60,
        numeric_noise=0.0,
        categorical_error=0.10,
        failure_rate=0.025,
        tier="mid",
    ),
    ProviderSpec(
        name="firmographics_premium",
        entity_type="company",
        capability="firmographics",
        provides_fields=("employee_count", "industry"),
        base_cost_usd=0.090,
        p50_latency_ms=600,
        p95_latency_ms=2600,
        base_coverage=0.91,
        obscurity_sensitivity=0.35,
        numeric_noise=0.12,
        categorical_error=0.03,
        failure_rate=0.015,
        tier="mid",
    ),
    ProviderSpec(
        name="intent_signals",
        entity_type="company",
        capability="intent",
        # The only affordable route to buying intent and trigger events — the
        # two fields most able to flip a decision.
        provides_fields=("buying_intent", "recent_trigger_event"),
        base_cost_usd=0.250,
        p50_latency_ms=1100,
        p95_latency_ms=4800,
        base_coverage=0.62,
        obscurity_sensitivity=0.70,
        numeric_noise=0.22,
        categorical_error=0.12,
        failure_rate=0.04,
        bill_on_miss=True,  # intent vendors commonly charge for the lookup
        tier="expensive",
    ),
    ProviderSpec(
        name="deep_research",
        entity_type="company",
        capability="deep_research",
        # The only source of the disqualifying flag. A policy that never reaches
        # here cannot detect a blocker no matter how much else it buys.
        provides_fields=(
            "buying_intent",
            "recent_trigger_event",
            "disqualifying_flag",
            "employee_count",
            "industry",
        ),
        base_cost_usd=0.600,
        p50_latency_ms=4200,
        p95_latency_ms=15000,
        base_coverage=0.88,
        obscurity_sensitivity=0.40,
        numeric_noise=0.08,
        categorical_error=0.04,
        failure_rate=0.05,
        bill_on_miss=True,
        tier="expensive",
    ),
)

BY_NAME: dict[str, ProviderSpec] = {p.name: p for p in CATALOG}

# The "cheapest tier" used by the dataset-validity gate. If a policy restricted
# to these can already match the full-information ceiling, the dataset is too
# easy to prove anything and generation is rejected.
CHEAP_TIER: tuple[str, ...] = tuple(p.name for p in CATALOG if p.tier in ("free", "cheap"))

ALL_PROVIDERS: tuple[str, ...] = tuple(p.name for p in CATALOG)


def total_catalog_cost() -> float:
    """Cost of calling every provider once — the full-enrichment baseline."""
    return sum(p.base_cost_usd for p in CATALOG)
