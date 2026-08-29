"""Which live providers run, and in which order — the pure half of the loop.

The acquisition loop itself needs a database (evidence store, ledger, spend
guard) and is exercised in ``tests/integration/test_live_multi_provider_integration.py``.
Everything here is decidable without one: the ordering rule, how an injected
set is resolved, and the stopping predicate that decides whether a second
provider is worth calling at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from arie.core.types import Entity, EntityType, Evidence, ProviderResult
from arie.evidence.ttl_policy import ttl_for_field
from arie.jobs.handlers import (
    _STOP_PERSON_EVIDENCE_NOT_MATERIAL,
    _STOP_SETTLED,
    _default_stop_check,
    _enough_evidence,
    _live_entity_refs,
    _option_c_stop_check,
    _resolve_live_providers,
)
from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES, acquisition_order
from arie.providers.apollo_contract import APOLLO_PROVIDER_NAME
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.scoring.engine import score_evidence


@dataclass(frozen=True)
class _StubProvider:
    """Structurally an ``EnrichmentProvider``; makes no requests."""

    name: str
    entity_type: EntityType = "company"
    provides_fields: tuple[str, ...] = ()
    base_cost_usd: float = 0.0
    p50_latency_ms: int = 1
    p95_latency_ms: int = 1

    def fetch(self, entity: Entity) -> ProviderResult:  # pragma: no cover - never called
        raise AssertionError("ordering tests must not fetch")


_ABSTRACT = _StubProvider(name=ABSTRACT_PROVIDER_NAME)
_APOLLO = _StubProvider(name=APOLLO_PROVIDER_NAME, entity_type="person")


# ------------------------------------------------------------------ ordering --


def test_the_company_provider_runs_before_the_person_provider() -> None:
    """Cheapest-first, and company-first. Abstract costs an order of magnitude
    less and its evidence is shared by every future lead at the same employer;
    Apollo's is per-person and can never be amortised that way."""
    assert acquisition_order((_ABSTRACT, _APOLLO)) == (_ABSTRACT, _APOLLO)


def test_an_injected_set_in_the_wrong_order_is_corrected_not_honoured() -> None:
    """The reason ordering is applied here rather than trusted from the caller.
    A test or a smoke script that happens to list the providers the other way
    round would otherwise exercise a policy production never runs — buying
    person evidence before finding out whether firmographics already settled
    the question."""
    assert acquisition_order((_APOLLO, _ABSTRACT)) == (_ABSTRACT, _APOLLO)


def test_an_unrecognised_provider_sorts_last_and_cannot_displace_a_known_one() -> None:
    """Keeps an experimental adapter runnable without editing the registry,
    while guaranteeing it can never push a reviewed provider out of position."""
    experimental = _StubProvider(name="some_experimental_provider")
    assert acquisition_order((experimental, _APOLLO, _ABSTRACT)) == (
        _ABSTRACT,
        _APOLLO,
        experimental,
    )


def test_the_registered_order_is_the_order_the_handler_uses() -> None:
    """Ties the tuple that documents the policy to the function that applies
    it, so editing one without the other cannot go unnoticed."""
    stubs = [_StubProvider(name=name) for name in reversed(REGISTERED_LIVE_PROVIDER_NAMES)]
    assert [p.name for p in acquisition_order(stubs)] == list(REGISTERED_LIVE_PROVIDER_NAMES)


# ----------------------------------------------------------------- selection --


def test_injecting_a_single_provider_means_exactly_that_one() -> None:
    """``live_provider`` is how a caller deliberately exercises one adapter in
    isolation. Interpreting it as "this one plus whatever the environment can
    build" would make a focused test quietly call a second paid API."""
    assert _resolve_live_providers(live_provider=_ABSTRACT, live_providers=None) == (_ABSTRACT,)


def test_injecting_a_set_uses_that_set_in_acquisition_order() -> None:
    resolved = _resolve_live_providers(live_provider=None, live_providers=[_APOLLO, _ABSTRACT])
    assert resolved == (_ABSTRACT, _APOLLO)


def test_passing_both_injection_forms_is_a_caller_bug() -> None:
    """Silently merging or silently preferring one would make acquisition order
    depend on an argument the caller thought was redundant."""
    with pytest.raises(ValueError, match="not both"):
        _resolve_live_providers(live_provider=_ABSTRACT, live_providers=[_APOLLO])


# ------------------------------------------------------------------ entities --


def _identity(*, domain: str | None) -> object:
    from arie.jobs.handlers import _LeadIdentity

    return _LeadIdentity(
        company_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        canonical_email="dana@northwind-analytics.test",
        canonical_domain=domain,
        is_shadow=False,
    )


def test_a_lead_with_a_domain_can_reach_both_entity_types() -> None:
    refs = _live_entity_refs(_identity(domain="northwind-analytics.test"))  # type: ignore[arg-type]
    assert set(refs) == {"company", "person"}
    assert refs["company"].canonical_key == "northwind-analytics.test"
    assert refs["person"].canonical_key == "dana@northwind-analytics.test"


def test_a_free_mail_lead_has_no_company_entity_but_still_has_a_person() -> None:
    """The case that used to end acquisition outright. A gmail.com lead
    resolves no company domain, so a domain-keyed provider must be skipped —
    but the person provider keys on the email, which every ingested lead has,
    so it can still run."""
    refs = _live_entity_refs(_identity(domain=None))  # type: ignore[arg-type]
    assert set(refs) == {"person"}


# ------------------------------------------------------------------ stopping --


def _company_evidence(**fields: object) -> list[Evidence]:
    now = datetime.now(UTC)
    entity_id = uuid.uuid4()
    return [
        Evidence(
            entity_type="company",
            entity_id=entity_id,
            field_name=name,
            value=value,
            source=ABSTRACT_PROVIDER_NAME,
            confidence=0.8,
            ttl_seconds=ttl_for_field(name),
            fetched_at=now,
        )
        for name, value in fields.items()
    ]


@dataclass(frozen=True)
class _StubModel:
    """Stands in for ``ConfidenceModel`` — only ``predict``/``tau`` are read."""

    value: float
    tau: float = 0.8

    def predict(self, scoring: object) -> float:
        return self.value


def test_nothing_observed_never_stops_acquisition() -> None:
    """Without this guard the confidence branch is asked about an empty bundle,
    and a model that happened to answer above tau would mean no provider is
    ever called at all."""
    scoring = score_evidence([], datetime.now(UTC))
    assert _enough_evidence(scoring, has_evidence=False, model=_StubModel(0.99)) is None  # type: ignore[arg-type]


def test_confidence_at_tau_stops_acquisition() -> None:
    """The rule that makes the second provider conditional. Comparison is
    ``>=``, matching the simulated path's own gate exactly — the two must not
    drift on where the boundary sits."""
    scoring = score_evidence(
        _company_evidence(industry="construction", employee_count=5), datetime.now(UTC)
    )
    assert (
        _enough_evidence(scoring, has_evidence=True, model=_StubModel(0.8))  # type: ignore[arg-type]
        == "confidence_reached"
    )


def test_confidence_below_tau_keeps_buying() -> None:
    scoring = score_evidence(
        _company_evidence(industry="software", employee_count=240), datetime.now(UTC)
    )
    assert _enough_evidence(scoring, has_evidence=True, model=_StubModel(0.79)) is None  # type: ignore[arg-type]


def test_bounds_cannot_settle_on_company_evidence_alone_and_that_is_honest() -> None:
    """Not a bug, and worth pinning so nobody 'fixes' it. No live provider
    supplies ``disqualifying_flag``, and while it is unknown ``compute_bounds``
    pins the score floor at zero — so the reachable interval always straddles a
    boundary. Live acquisition therefore stops on *confidence*, never on
    settled bounds. Making bounds settle here would mean pretending an
    unchecked blocker had been checked."""
    scoring = score_evidence(
        _company_evidence(industry="software", employee_count=240), datetime.now(UTC)
    )

    assert not scoring.bounds.is_settled
    assert scoring.bounds.lower == 0.0
    assert "disqualifying_flag" in scoring.signals.unknown_fields


# --------------------------------------------------------- option c stopping --

_HUNTER = _StubProvider(
    name=HUNTER_PROVIDER_NAME,
    entity_type="person",
    provides_fields=("title_seniority", "title_function"),
)


def test_default_stop_check_is_exactly_enough_evidence_for_any_provider() -> None:
    """The parameterisation must not change existing behaviour: passing no
    stop_check to _acquire_live_evidence has to be indistinguishable from
    calling _enough_evidence directly, for a company provider or a person
    one — _default_stop_check has no opinion on which provider is next."""
    scoring = score_evidence(
        _company_evidence(industry="construction", employee_count=5), datetime.now(UTC)
    )
    direct = _enough_evidence(scoring, has_evidence=True, model=_StubModel(0.8))  # type: ignore[arg-type]

    for provider in (_ABSTRACT, _HUNTER):
        assert (
            _default_stop_check(scoring, True, _StubModel(0.8), provider)  # type: ignore[arg-type]
            == direct
        )


def test_option_c_always_calls_the_company_provider_first() -> None:
    """With no evidence at all, bounds are trivially unsettled and Abstract
    is not a person provider — option_c must never refuse the first call."""
    scoring = score_evidence([], datetime.now(UTC))
    assert (
        _option_c_stop_check(scoring, False, _StubModel(0.99), _ABSTRACT)  # type: ignore[arg-type]
        is None
    )


def test_option_c_skips_hunter_when_its_fields_cannot_change_the_recommendation() -> None:
    """A tiny nonprofit: even Hunter's best possible title fields could not
    lift the score into contention. option_c must skip it, and never touch
    the confidence model to decide that (a model scored to always say
    'keep buying' proves the skip came from the bounds/best-case check)."""
    scoring = score_evidence(
        _company_evidence(industry="nonprofit", employee_count=5), datetime.now(UTC)
    )
    result = _option_c_stop_check(scoring, True, _StubModel(0.0), _HUNTER)  # type: ignore[arg-type]
    assert result == _STOP_PERSON_EVIDENCE_NOT_MATERIAL


def test_option_c_calls_hunter_when_its_best_case_could_flip_the_recommendation() -> None:
    """A mid-size software company: best-case title_seniority/title_function
    would cross QUALIFY_THRESHOLD. option_c must call Hunter even though a
    confidence model set to always say 'stop' would otherwise have ended
    acquisition here under the optimized strategy."""
    scoring = score_evidence(
        _company_evidence(industry="software", employee_count=150), datetime.now(UTC)
    )
    optimized_would_stop = _enough_evidence(
        scoring,
        has_evidence=True,
        model=_StubModel(0.99),  # type: ignore[arg-type]
    )
    assert optimized_would_stop == "confidence_reached"

    result = _option_c_stop_check(scoring, True, _StubModel(0.99), _HUNTER)  # type: ignore[arg-type]
    assert result is None


def test_option_c_stops_on_settled_bounds_the_same_way_optimized_does() -> None:
    """A disqualified lead's bounds pin to zero regardless of strategy — this
    must still be the strongest, cheapest possible skip under option_c too."""
    scoring = score_evidence(_company_evidence(disqualifying_flag=True), datetime.now(UTC))
    assert scoring.bounds.is_settled

    result = _option_c_stop_check(scoring, True, _StubModel(0.0), _HUNTER)  # type: ignore[arg-type]
    assert result == _STOP_SETTLED
