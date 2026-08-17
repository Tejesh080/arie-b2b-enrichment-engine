"""Deterministic demo identities, selected from the frozen seed-42 corpus.

Never hardcodes an email/domain as ground truth without verifying it against
the policy actually running today — `build_runtime` fits the same confidence
model `arie.jobs.worker` fits at boot, over the same corpus, so "this lead
auto-routes" here means what it will mean once the demo actually posts it to
a running stack.
"""

from __future__ import annotations

from dataclasses import dataclass

from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_runtime, decision_route
from arie.policy.base import EvidenceCache, RunContext
from arie.providers.simulated import CallLedger

CANONICAL_SEED = 42
"""The same seed `arie.jobs.handlers.build_runtime`'s default and
`tests/conftest.py`'s `CANONICAL_SEED` use — the frozen corpus everything else
in this repo demonstrates against."""

# Known-good identities from prior manual verification (docs/06-m1-handoff.md,
# README's n8n walkthrough). Tried first because they're the identities this
# repo's own docs already name; if the corpus or policy ever changes under
# them, `select_demo_corpus` falls back to scanning rather than asserting a
# stale fact.
_KNOWN_AUTONOMOUS_EMAIL = "nadia.delacroix@lumen500.com"
_KNOWN_ESCALATING_EMAIL = "nadia.haddad@cobalt500.com"


class DemoCorpusError(RuntimeError):
    """Raised when no lead in the corpus satisfies a demo scenario's requirement.

    Means the frozen corpus or the production policy changed enough that this
    demo can no longer find a deterministic example — a real problem worth
    surfacing, not something to paper over with a fabricated identity.
    """


@dataclass(frozen=True)
class DemoCorpus:
    runtime: SimulatedEnrichmentRuntime
    autonomous_lead: EvalLead
    escalating_lead: EvalLead
    same_company_pair: tuple[EvalLead, EvalLead] | None
    """Two distinct people at the same company, for the evidence-reuse
    scenario — `None` if the corpus has no such pair (scenario D is then
    omitted, not faked)."""


def _find_lead(leads: list[EvalLead], email: str) -> EvalLead | None:
    for lead in leads:
        if lead.person.email == email:
            return lead
    return None


def _route_of(runtime: SimulatedEnrichmentRuntime, lead: EvalLead) -> str:
    ctx = RunContext(registry=runtime.registry, ledger=CallLedger(), cache=EvidenceCache())
    outcome = runtime.policy.run(lead, ctx)
    return decision_route(outcome.decision, outcome.autonomous)


def _first_matching(
    runtime: SimulatedEnrichmentRuntime, leads: list[EvalLead], wanted_route: str
) -> EvalLead:
    """First corpus lead (by email, for determinism) whose in-memory outcome
    takes `wanted_route` — the same fallback shape `tests/integration/
    test_pipeline_integration.py`'s `_corpus_lead_with_route` uses, so a demo
    run and the test suite agree on what "deterministic" means."""
    for lead in sorted(leads, key=lambda x: x.person.email):
        if _route_of(runtime, lead) == wanted_route:
            return lead
    raise DemoCorpusError(
        f"no corpus lead routes to {wanted_route!r} — the frozen corpus or the "
        "production policy changed; this demo needs at least one deterministic "
        "example of each scenario"
    )


def _verified_lead(
    runtime: SimulatedEnrichmentRuntime, leads: list[EvalLead], email: str, wanted_route: str
) -> EvalLead:
    """The known-good identity, if it still actually routes as documented;
    otherwise the first corpus lead that does."""
    candidate = _find_lead(leads, email)
    if candidate is not None and _route_of(runtime, candidate) == wanted_route:
        return candidate
    return _first_matching(runtime, leads, wanted_route)


def _same_company_pair(leads: list[EvalLead]) -> tuple[EvalLead, EvalLead] | None:
    """Two distinct people at the same company, chosen deterministically (the
    lowest-sorting domain with >=2 people, then its two lowest-sorting
    emails) so the pair is stable across runs on the same corpus."""
    by_domain: dict[str, list[EvalLead]] = {}
    for lead in leads:
        by_domain.setdefault(lead.company.canonical_domain, []).append(lead)

    for domain in sorted(by_domain):
        people = sorted(by_domain[domain], key=lambda x: x.person.email)
        if len(people) >= 2:
            return people[0], people[1]
    return None


def select_demo_corpus(
    *, seed: int = CANONICAL_SEED, leads: list[EvalLead] | None = None
) -> DemoCorpus:
    """Generate the corpus, fit the confidence model, and pick this run's
    demo identities — all pure/local, no network, no running stack required.

    Costs the same couple of seconds `arie.jobs.worker.main()` already pays at
    boot (dataset generation + model refit); paid once per demo invocation.
    `leads` exists so tests holding the session-scoped dataset don't
    regenerate it — the same reason `build_runtime` takes the same parameter.
    """
    if leads is None:
        from arie.evalgen.generator import generate_dataset

        leads, _manifest = generate_dataset(seed=seed)
    runtime = build_runtime(leads=leads, dataset_seed=seed)

    autonomous_lead = _verified_lead(runtime, leads, _KNOWN_AUTONOMOUS_EMAIL, "auto_route")
    escalating_lead = _verified_lead(runtime, leads, _KNOWN_ESCALATING_EMAIL, "escalate_human")

    return DemoCorpus(
        runtime=runtime,
        autonomous_lead=autonomous_lead,
        escalating_lead=escalating_lead,
        same_company_pair=_same_company_pair(leads),
    )
