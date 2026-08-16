"""Simulated provider layer.

The properties under test are the ones the benchmark's validity rests on:
identical provider behaviour for every strategy, exact cost reconciliation, and
a hard distinction between "this vendor has no data" and "the harness is
broken".
"""

from __future__ import annotations

import pytest

from arie.core.types import Entity, ProviderStatus
from arie.evalgen.schema import EvalLead
from arie.providers.base import EnrichmentProvider, ProviderRegistry
from arie.providers.catalog import ALL_PROVIDERS, BY_NAME, CATALOG
from arie.providers.simulated import (
    CallLedger,
    ObservationStore,
    SimulatedProvider,
    UnknownEntityError,
    build_from_leads,
    company_entity,
    entity_for,
    entity_id_for,
    fetch_all,
    person_entity,
)


@pytest.fixture(scope="module")
def store_and_registry(leads: list[EvalLead]) -> tuple[ObservationStore, ProviderRegistry]:
    return build_from_leads(leads)


# --- protocol conformance ----------------------------------------------------


def test_every_catalogue_provider_satisfies_the_protocol(
    store_and_registry: tuple[ObservationStore, ProviderRegistry],
) -> None:
    """Structural conformance is what lets real adapters drop in unchanged."""
    store, _ = store_and_registry
    for spec in CATALOG:
        provider = SimulatedProvider(spec=spec, store=store)
        assert isinstance(provider, EnrichmentProvider)
        assert provider.name == spec.name
        assert provider.base_cost_usd == spec.base_cost_usd


def test_registry_exposes_all_providers(
    store_and_registry: tuple[ObservationStore, ProviderRegistry],
) -> None:
    _, registry = store_and_registry
    assert {p.name for p in registry.all()} == set(ALL_PROVIDERS)


# --- determinism -------------------------------------------------------------


def test_repeated_fetch_is_identical(
    leads: list[EvalLead], store_and_registry: tuple[ObservationStore, ProviderRegistry]
) -> None:
    _, registry = store_and_registry
    lead = leads[0]
    for spec in CATALOG:
        provider = registry.get(spec.name)
        entity = entity_for(lead, spec.entity_type)
        assert provider.fetch(entity) == provider.fetch(entity)


def test_independent_stores_agree(leads: list[EvalLead]) -> None:
    """Two stores built from the same dataset must be interchangeable.

    This is the property that guarantees every strategy in the benchmark faces
    identical provider behaviour, even if each constructs its own store.
    """
    _, registry_a = build_from_leads(leads)
    _, registry_b = build_from_leads(leads)

    for lead in leads[:40]:
        for spec in CATALOG:
            entity = entity_for(lead, spec.entity_type)
            a = registry_a.get(spec.name).fetch(entity)
            b = registry_b.get(spec.name).fetch(entity)
            assert a == b


def test_entity_ids_are_stable_and_distinct() -> None:
    assert entity_id_for("acme.com") == entity_id_for("acme.com")
    assert entity_id_for("acme.com") != entity_id_for("acme.io")


# --- correlated misses survive the layer ------------------------------------


def test_company_providers_return_one_answer_per_company(leads: list[EvalLead]) -> None:
    """The property that makes company-level caching lossless.

    Every contact at a company must receive byte-identical company-provider
    responses. Otherwise the cache would hand contact B whatever contact A
    happened to draw, and full enrichment would get two different answers from
    calling one company API twice.
    """
    _, registry = build_from_leads(leads)

    by_company: dict[str, list[EvalLead]] = {}
    for lead in leads:
        by_company.setdefault(lead.company.company_id, []).append(lead)
    multi = [group for group in by_company.values() if len(group) > 1]
    assert multi, "dataset has no multi-contact companies to check"

    for group in multi:
        for spec in CATALOG:
            if spec.entity_type != "company":
                continue
            provider = registry.get(spec.name)
            results = [provider.fetch(company_entity(lead)) for lead in group]
            assert all(r == results[0] for r in results), (
                f"{spec.name} returned differing results across contacts at "
                f"{group[0].company.canonical_domain}"
            )


def test_person_providers_vary_within_a_company(leads: list[EvalLead]) -> None:
    """Person-scoped data must remain per-person.

    The mirror of the test above: if person providers were also company-scoped,
    every contact at a company would look identical and title-based scoring
    would carry no information.
    """
    _, registry = build_from_leads(leads)
    provider = registry.get("contact_enrich")

    by_company: dict[str, list[EvalLead]] = {}
    for lead in leads:
        by_company.setdefault(lead.company.company_id, []).append(lead)

    varied = 0
    for group in (g for g in by_company.values() if len(g) > 1):
        results = [provider.fetch(person_entity(lead)) for lead in group]
        if any(r != results[0] for r in results):
            varied += 1
    assert varied > 0, "person-scoped providers returned identical data for all contacts"


def test_obscure_companies_still_miss_more(leads: list[EvalLead]) -> None:
    """The correlated-miss structure must survive replay unchanged."""
    _, registry = build_from_leads(leads)

    def misses(lead: EvalLead) -> int:
        return sum(
            1
            for spec in CATALOG
            if registry.get(spec.name).fetch(entity_for(lead, spec.entity_type)).status
            is not ProviderStatus.SUCCESS
        )

    low = [misses(x) for x in leads if x.company.obscurity < 0.25]
    high = [misses(x) for x in leads if x.company.obscurity > 0.60]
    assert sum(high) / len(high) > sum(low) / len(low) + 0.5


# --- miss and error handling -------------------------------------------------


def test_miss_returns_no_fields_and_zero_confidence(leads: list[EvalLead]) -> None:
    _, registry = build_from_leads(leads)
    seen = 0
    for lead in leads:
        for spec in CATALOG:
            result = registry.get(spec.name).fetch(entity_for(lead, spec.entity_type))
            if result.status is ProviderStatus.MISS:
                seen += 1
                assert result.fields == {}
                assert result.confidence == 0.0
    assert seen > 0, "no misses in the dataset — coverage model is not exercised"


def test_miss_billing_follows_provider_policy(leads: list[EvalLead]) -> None:
    """Vendors differ on whether a fruitless lookup is billed.

    Sources conflict on real-world behaviour, so the catalogue models both and
    the simulator must honour whichever a provider declares — a policy that
    optimises spend cannot be evaluated against wrong prices.
    """
    _, registry = build_from_leads(leads)
    for lead in leads:
        for spec in CATALOG:
            result = registry.get(spec.name).fetch(entity_for(lead, spec.entity_type))
            if result.status is ProviderStatus.MISS:
                expected = spec.base_cost_usd if spec.bill_on_miss else 0.0
                assert result.cost_usd == expected, f"{spec.name} mis-billed a miss"


def test_transport_errors_are_free(leads: list[EvalLead]) -> None:
    _, registry = build_from_leads(leads)
    for lead in leads:
        for spec in CATALOG:
            result = registry.get(spec.name).fetch(entity_for(lead, spec.entity_type))
            if result.status is ProviderStatus.ERROR:
                assert result.cost_usd == 0.0
                assert result.fields == {}


def test_unknown_entity_raises_rather_than_reporting_a_miss(
    store_and_registry: tuple[ObservationStore, ProviderRegistry],
) -> None:
    """A wiring bug must not be able to disguise itself as poor coverage."""
    _, registry = store_and_registry
    ghost = Entity(
        entity_type="company",
        entity_id=entity_id_for("does-not-exist.example"),
        canonical_key="does-not-exist.example",
    )
    with pytest.raises(UnknownEntityError):
        registry.get("firmographics_basic").fetch(ghost)


def test_entity_type_mismatch_is_rejected(
    leads: list[EvalLead], store_and_registry: tuple[ObservationStore, ProviderRegistry]
) -> None:
    _, registry = store_and_registry
    with pytest.raises(ValueError, match="serves company entities"):
        registry.get("firmographics_basic").fetch(person_entity(leads[0]))


def test_providers_never_return_undeclared_fields(leads: list[EvalLead]) -> None:
    """The EVoI controller estimates value from `provides_fields`.

    A provider returning more than it declares would silently invalidate those
    estimates in a way that is very hard to trace back to its cause.
    """
    _, registry = build_from_leads(leads)
    for lead in leads[:60]:
        for spec in CATALOG:
            result = registry.get(spec.name).fetch(entity_for(lead, spec.entity_type))
            assert set(result.fields) <= set(spec.provides_fields)


# --- cost accounting ---------------------------------------------------------


def test_ledger_reconciles_against_the_dataset(leads: list[EvalLead]) -> None:
    """Ledger totals must equal the frozen observations exactly.

    Every cost claim the project makes is computed from this ledger, so a
    rounding drift here would silently corrupt the headline result.
    """
    _, registry = build_from_leads(leads)
    ledger = CallLedger()
    sample = leads[:80]
    for lead in sample:
        fetch_all(registry, lead, ledger)

    expected = sum(lead.observations[name].cost_usd for lead in sample for name in ALL_PROVIDERS)
    assert ledger.total_cost_usd == pytest.approx(expected, abs=1e-9)
    assert ledger.billable_calls == len(sample) * len(ALL_PROVIDERS)


def test_full_enrichment_never_exceeds_catalogue_price(leads: list[EvalLead]) -> None:
    """Per-lead spend is bounded by the sum of list prices.

    Misses can be billed, but nothing may cost *more* than calling everything
    once — an upper bound the adaptive policy is measured against.
    """
    _, registry = build_from_leads(leads)
    ceiling = sum(spec.base_cost_usd for spec in CATALOG)
    for lead in leads[:80]:
        ledger = CallLedger()
        fetch_all(registry, lead, ledger)
        assert ledger.total_cost_usd <= ceiling + 1e-9


def test_partial_enrichment_costs_strictly_less(leads: list[EvalLead]) -> None:
    _, registry = build_from_leads(leads)
    cheap_total = 0.0
    full_total = 0.0
    for lead in leads[:80]:
        cheap, full = CallLedger(), CallLedger()
        fetch_all(registry, lead, cheap, providers=["inbound_payload", "dns_web"])
        fetch_all(registry, lead, full)
        cheap_total += cheap.total_cost_usd
        full_total += full.total_cost_usd
    assert cheap_total < full_total


def test_cache_hits_are_recorded_as_free_calls(leads: list[EvalLead]) -> None:
    """Cache hits must appear in the ledger, priced at zero.

    Omitting them would make cache-hit rate unmeasurable — and that metric is
    the justification for the company-level evidence store.
    """
    _, registry = build_from_leads(leads)
    lead = leads[0]
    provider = registry.get("firmographics_basic")
    entity = company_entity(lead)
    result = provider.fetch(entity)

    ledger = CallLedger()
    ledger.record("firmographics_basic", entity, result, cache_hit=False)
    ledger.record("firmographics_basic", entity, result, cache_hit=True)

    assert ledger.billable_calls == 1
    assert ledger.cache_hits == 1
    assert ledger.cache_hit_rate == 0.5
    assert ledger.total_cost_usd == pytest.approx(result.cost_usd)
    assert ledger.total_latency_ms == pytest.approx(result.latency_ms)


def test_declared_latency_profile_is_respected(leads: list[EvalLead]) -> None:
    """Observed latencies must track the catalogue's declared distribution.

    The EVoI controller penalises slow providers using the declared p50/p95, so
    a provider whose real latency diverged from its declaration would corrupt
    the stopping decision.
    """
    _, registry = build_from_leads(leads)
    for spec in CATALOG:
        if spec.p50_latency_ms == 0:
            continue
        samples = sorted(
            registry.get(spec.name).fetch(entity_for(lead, spec.entity_type)).latency_ms
            for lead in leads
        )
        median = samples[len(samples) // 2]
        # Log-normal draws are noisy at this sample size; the assertion is that
        # the declared p50 is the right order of magnitude, not exact.
        assert 0.4 * spec.p50_latency_ms <= median <= 2.5 * spec.p50_latency_ms, (
            f"{spec.name} median latency {median:.0f}ms vs declared p50 {spec.p50_latency_ms}ms"
        )


def test_conflicting_frozen_observations_are_rejected(leads: list[EvalLead]) -> None:
    """Guards the invariant that gives the store its meaning."""
    lead = leads[0]
    twin_company = [x for x in leads if x.company.company_id == lead.company.company_id]
    assert twin_company

    tampered = dict(lead.observations)
    spec = BY_NAME["firmographics_basic"]
    original = tampered[spec.name]
    tampered[spec.name] = type(original)(
        provider=original.provider,
        status=original.status,
        fields={**original.fields, "employee_count": 999_999},
        latency_ms=original.latency_ms,
        cost_usd=original.cost_usd,
    )
    mutated = type(lead)(
        **{**lead.__dict__, "observations": tampered, "eval_lead_id": lead.eval_lead_id + "x"}
    )

    with pytest.raises(ValueError, match="conflicting frozen observations"):
        ObservationStore([lead, mutated])
