"""The handler registry and its pure parts — no database, no network.

The registration test is the one that pins the Step 12 runtime fix: a worker
that boots with zero handlers processes nothing, and nothing else in the
suite would notice, because every other test hands `run_worker_cycle` its own
handlers. The full pipeline behaviour lives in
tests/integration/test_pipeline_integration.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from psycopg_pool import ConnectionPool

from arie.config import LiveProviderConfig
from arie.core.types import Decision, Entity, EntityType, LeadStatus, ProviderResult, ProviderStatus
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import (
    SimulatedEnrichmentRuntime,
    UnknownCorpusIdentityError,
    UnsupportedProviderModeError,
    build_handlers,
    build_runtime,
    decision_route,
)
from arie.providers.live_abstract import (
    AbstractCompanyConfigurationError,
    AbstractCompanyEnrichmentProvider,
)
from arie.statemachine.transitions import job_type_for


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    """Built from the session dataset so the model is fitted once per module."""
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def unopened_pool() -> ConnectionPool:
    """A pool that never connects — handler construction must not need a database."""
    return ConnectionPool("dbname=never_connected", open=False)


# ------------------------------------------------------------ registration --


def test_the_ingestion_entry_job_type_has_a_handler(
    runtime: SimulatedEnrichmentRuntime, unopened_pool: ConnectionPool
) -> None:
    """`POST /leads` enqueues exactly one job type; a worker booted from
    build_handlers must be able to claim and process it. This is the
    regression test for the boot path that shipped with zero handlers."""
    handlers = build_handlers(unopened_pool, runtime=runtime, provider_mode="simulated")

    entry_job_type = job_type_for(LeadStatus.NEW)
    assert entry_job_type is not None
    assert entry_job_type in handlers
    assert len(handlers) >= 1


def test_every_registered_handler_names_a_job_type_the_graph_defines(
    runtime: SimulatedEnrichmentRuntime, unopened_pool: ConnectionPool
) -> None:
    """A handler for a job type nothing can enqueue would be dead code wearing
    a registration."""
    handlers = build_handlers(unopened_pool, runtime=runtime, provider_mode="simulated")

    graph_job_types = {
        job_type for status in LeadStatus if (job_type := job_type_for(status)) is not None
    }
    assert set(handlers) <= graph_job_types


def test_an_unrecognised_provider_mode_is_refused_loudly(
    runtime: SimulatedEnrichmentRuntime, unopened_pool: ConnectionPool
) -> None:
    """Post-M1 P5: only 'simulated' and 'live' are recognised now. A worker
    silently falling back to the simulator for a typo'd mode would report
    coverage for vendors it never called."""
    with pytest.raises(UnsupportedProviderModeError):
        build_handlers(unopened_pool, runtime=runtime, provider_mode="lyve")


@dataclass(frozen=True)
class _FakeLiveProvider:
    """Satisfies `EnrichmentProvider` structurally, with no network at all —
    for tests that only need live mode to *build*, not to call anything real."""

    name: str = "fake_live_provider"
    entity_type: EntityType = "company"
    provides_fields: tuple[str, ...] = ("employee_count", "industry")
    base_cost_usd: float = 0.001
    p50_latency_ms: int = 100
    p95_latency_ms: int = 500
    result: ProviderResult = field(
        default_factory=lambda: ProviderResult(
            fields={"employee_count": 250, "industry": "software"},
            confidence=0.8,
            cost_usd=0.001,
            latency_ms=42.0,
            status=ProviderStatus.SUCCESS,
        )
    )

    def fetch(self, entity: Entity) -> ProviderResult:
        return self.result


def test_live_provider_mode_builds_a_handler_with_an_injected_provider(
    runtime: SimulatedEnrichmentRuntime, unopened_pool: ConnectionPool
) -> None:
    """Post-M1 P5: PROVIDER_MODE=live now has a real backend. Injecting a fake
    adapter (mirroring DeepSeekSignalExtractor's own test-injection pattern)
    proves the dispatch and registration without any network or database."""
    handlers = build_handlers(
        unopened_pool,
        runtime=runtime,
        provider_mode="live",
        live_provider=_FakeLiveProvider(),
    )
    assert "compute_score" in handlers


def test_the_simulated_path_is_structurally_immune_to_the_strategy_knob(
    runtime: SimulatedEnrichmentRuntime, unopened_pool: ConnectionPool
) -> None:
    """The public demo runs PROVIDER_MODE=simulated, and the provider-strategy
    machinery must be unable to touch it. Proven behaviourally: a strategy
    value that the live builder rejects loudly (below) is inert on the
    simulated builder, because only the live path ever resolves it."""
    handlers = build_handlers(
        unopened_pool,
        runtime=runtime,
        provider_mode="simulated",
        live_strategy="garbage",  # type: ignore[arg-type]
    )
    assert "compute_score" in handlers


def test_a_garbage_strategy_fails_the_live_build_loudly(
    runtime: SimulatedEnrichmentRuntime, unopened_pool: ConnectionPool
) -> None:
    """The other half of the pair above: the same injected value that the
    simulated builder ignored refuses to build a live worker — the missing-key
    treatment, applied to strategy misconfiguration."""
    from arie.live.strategy import UnsupportedLiveStrategyError

    with pytest.raises(UnsupportedLiveStrategyError, match="garbage"):
        build_handlers(
            unopened_pool,
            runtime=runtime,
            provider_mode="live",
            live_provider=_FakeLiveProvider(),
            live_strategy="garbage",  # type: ignore[arg-type]
        )


def test_the_evaluation_strategy_builds_a_live_handler(
    runtime: SimulatedEnrichmentRuntime, unopened_pool: ConnectionPool
) -> None:
    handlers = build_handlers(
        unopened_pool,
        runtime=runtime,
        provider_mode="live",
        live_provider=_FakeLiveProvider(),
        live_strategy="evaluation_parallel",
    )
    assert "compute_score" in handlers


def test_an_injected_stop_check_builds_a_live_handler_under_the_optimized_name(
    runtime: SimulatedEnrichmentRuntime, unopened_pool: ConnectionPool
) -> None:
    """Option C's verification path: live_stop_check is accepted alongside
    the two named strategies without needing a third one — it overrides
    _acquire_live_evidence's stopping rule, not which named strategy
    resolve_strategy sees. Proven structurally, same as the other
    build-time-only assertions in this file: this cannot exercise the
    stop_check itself without a database, only that the plumbing accepts it
    and still produces a working handler."""
    from arie.jobs.handlers import _option_c_stop_check

    handlers = build_handlers(
        unopened_pool,
        runtime=runtime,
        provider_mode="live",
        live_provider=_FakeLiveProvider(),
        live_stop_check=_option_c_stop_check,
    )
    assert "compute_score" in handlers


def test_live_provider_mode_with_no_injected_provider_builds_without_a_system_key(
    runtime: SimulatedEnrichmentRuntime,
    unopened_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Productization M5 Part 2: ordinary (non-test, non-smoke-script) live
    processing must never depend on a process-wide system credential being
    configured — each organization's own BYOK credential is resolved per
    job, not at worker startup. Before M5, building with no injected
    provider and no system key raised `AbstractCompanyConfigurationError` at
    build time (`_default_live_providers` built every adapter eagerly); this
    is the replacement invariant, proven the same way — patch the key away
    and show the *build* no longer even looks at it. `credential_unavailable`
    per-organization handling is covered where organization/provider state
    actually lives: `tests/integration/test_provider_availability_
    integration.py`.
    """
    monkeypatch.setattr(
        "arie.providers.live_abstract.LIVE_PROVIDER", LiveProviderConfig(api_key="")
    )
    handlers = build_handlers(unopened_pool, runtime=runtime, provider_mode="live")
    assert "compute_score" in handlers


def test_live_provider_mode_with_an_injected_provider_still_fails_clearly_on_a_missing_key(
    runtime: SimulatedEnrichmentRuntime,
    unopened_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one path that still may use the system credential — explicit
    injection with no `client` override — keeps the original "missing key
    fails at build time" guarantee: `_resolve_live_providers` still calls
    `_default_live_providers()` whenever a caller asks for every adapter by
    passing neither `live_provider` nor `live_providers`... except
    `_build_live_handlers` itself never does that anymore (see the test
    above). This test instead proves the adapter-level guarantee directly:
    building a real, unconfigured adapter still raises, regardless of which
    milestone's code calls `.build()`.
    """
    monkeypatch.setattr(
        "arie.providers.live_abstract.LIVE_PROVIDER", LiveProviderConfig(api_key="")
    )
    with pytest.raises(AbstractCompanyConfigurationError):
        AbstractCompanyEnrichmentProvider.build()


# ----------------------------------------------------------- corpus lookup --


def test_a_corpus_identity_resolves_to_its_own_lead(
    runtime: SimulatedEnrichmentRuntime, leads: list[EvalLead]
) -> None:
    sample = leads[0]
    found = runtime.corpus_lead_for(sample.person.email, sample.company.canonical_domain)
    assert found.person.email == sample.person.email
    assert found.company.canonical_domain == sample.company.canonical_domain


def test_an_unknown_identity_raises_with_an_actionable_message(
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    with pytest.raises(UnknownCorpusIdentityError) as exc_info:
        runtime.corpus_lead_for("nobody@not-in-the-corpus.test", "not-in-the-corpus.test")
    assert "frozen eval corpus" in str(exc_info.value)


def test_an_email_at_the_wrong_company_is_rejected(
    runtime: SimulatedEnrichmentRuntime, leads: list[EvalLead]
) -> None:
    """A corpus person whose ingested lead resolved to a different company
    would replay another company's observations — refuse, don't guess."""
    sample = leads[0]
    with pytest.raises(UnknownCorpusIdentityError):
        runtime.corpus_lead_for(sample.person.email, "some-other-company.test")


def test_a_missing_db_domain_does_not_block_the_corpus_match(
    runtime: SimulatedEnrichmentRuntime, leads: list[EvalLead]
) -> None:
    """`companies.canonical_domain` is nullable; an email match alone is
    sufficient when there is no domain to cross-check."""
    sample = leads[0]
    found = runtime.corpus_lead_for(sample.person.email, None)
    assert found.person.email == sample.person.email


# ---------------------------------------------------------- decision route --


@pytest.mark.parametrize(
    ("decision", "autonomous", "expected"),
    [
        (Decision.AUTO_ROUTE, True, "auto_route"),
        (Decision.REJECT, True, "reject"),
        # The policy itself concluding "a human should look" escalates even
        # when it is confident about that conclusion.
        (Decision.ESCALATE_HUMAN, True, "escalate_human"),
        # Below tau, *every* decision escalates — autonomy is gated on
        # calibrated confidence, never on the decision label or is_settled.
        (Decision.AUTO_ROUTE, False, "escalate_human"),
        (Decision.REJECT, False, "escalate_human"),
        (Decision.ESCALATE_HUMAN, False, "escalate_human"),
    ],
)
def test_decision_route(decision: Decision, autonomous: bool, expected: str) -> None:
    assert decision_route(decision, autonomous) == expected
