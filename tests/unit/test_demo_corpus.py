"""`scripts.demo.corpus` — deterministic demo identity selection.

Uses the session-scoped `leads` fixture (seed 42, the same corpus everything
else in this repo demonstrates against — `tests/conftest.py`) so this doesn't
regenerate the dataset or refit the confidence model a second time.
"""

from __future__ import annotations

from scripts.demo.corpus import select_demo_corpus

from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import decision_route
from arie.policy.base import EvidenceCache, RunContext
from arie.providers.simulated import CallLedger


def test_autonomous_lead_actually_routes_autonomously(leads: list[EvalLead]) -> None:
    corpus = select_demo_corpus(leads=leads)
    ctx = RunContext(registry=corpus.runtime.registry, ledger=CallLedger(), cache=EvidenceCache())
    outcome = corpus.runtime.policy.run(corpus.autonomous_lead, ctx)

    assert decision_route(outcome.decision, outcome.autonomous) == "auto_route"
    assert outcome.autonomous is True


def test_escalating_lead_actually_escalates(leads: list[EvalLead]) -> None:
    corpus = select_demo_corpus(leads=leads)
    ctx = RunContext(registry=corpus.runtime.registry, ledger=CallLedger(), cache=EvidenceCache())
    outcome = corpus.runtime.policy.run(corpus.escalating_lead, ctx)

    assert decision_route(outcome.decision, outcome.autonomous) == "escalate_human"


def test_the_two_demo_leads_are_different_people(leads: list[EvalLead]) -> None:
    corpus = select_demo_corpus(leads=leads)
    assert corpus.autonomous_lead.person.email != corpus.escalating_lead.person.email


def test_documented_demo_identities_are_still_current(leads: list[EvalLead]) -> None:
    """Pins the exact identities docs/06-m1-handoff.md and README.md name --
    if this ever fails, the corpus or the policy changed and the docs (and
    this test) need updating, not silently falling back."""
    corpus = select_demo_corpus(leads=leads)
    assert corpus.autonomous_lead.person.email == "nadia.delacroix@lumen500.com"
    assert corpus.escalating_lead.person.email == "nadia.haddad@cobalt500.com"


def test_same_company_pair_is_two_distinct_people_at_one_company(leads: list[EvalLead]) -> None:
    corpus = select_demo_corpus(leads=leads)
    assert corpus.same_company_pair is not None
    lead1, lead2 = corpus.same_company_pair
    assert lead1.person.email != lead2.person.email
    assert lead1.company.canonical_domain == lead2.company.canonical_domain


def test_selection_is_deterministic_across_calls(leads: list[EvalLead]) -> None:
    first = select_demo_corpus(leads=leads)
    second = select_demo_corpus(leads=leads)
    assert first.autonomous_lead.person.email == second.autonomous_lead.person.email
    assert first.escalating_lead.person.email == second.escalating_lead.person.email
    assert first.same_company_pair == second.same_company_pair
