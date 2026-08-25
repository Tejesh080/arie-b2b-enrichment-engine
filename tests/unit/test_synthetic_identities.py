"""Synthetic out-of-corpus identities — determinism, cache coherence, and a
demo-worthy outcome mix.

The public demo accepts arbitrary leads; ``arie.providers.synthetic`` answers
for them with the corpus generator's own observation model, seeded from the
lead's canonical keys. These tests pin the properties the durable evidence
cache and the demo depend on. The end-to-end path (an out-of-corpus lead
ingested over HTTP reaching a terminal status) is covered in
tests/integration/test_pipeline_integration.py.
"""

from __future__ import annotations

import collections

import pytest

from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_runtime
from arie.policy.base import EvidenceCache, RunContext
from arie.providers.catalog import CATALOG
from arie.providers.simulated import CallLedger, build_from_leads
from arie.providers.synthetic import synthesize_corpus_lead


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


class _MemoryCache(EvidenceCache):
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], object] = {}

    def get(self, provider: str, key: str):  # type: ignore[override]
        return self._items.get((provider, key))

    def put(self, provider: str, key: str, result) -> None:  # type: ignore[override]
        self._items[(provider, key)] = result


def _synth(email: str, domain: str, **kw) -> EvalLead:
    return synthesize_corpus_lead(canonical_email=email, canonical_domain=domain, **kw)


# ------------------------------------------------------------- determinism --


def test_the_same_identity_always_synthesizes_the_same_lead() -> None:
    """The durable evidence cache stores yesterday's answer; a fresh call
    today must produce the identical one or receipts stop reconciling."""
    a = _synth("dana.reeve@thorncroft.io", "thorncroft.io")
    b = _synth("dana.reeve@thorncroft.io", "thorncroft.io")
    assert a == b


def test_two_contacts_at_one_company_share_its_company_observations() -> None:
    """Company evidence is cached at the company key; the second contact must
    see byte-identical company observations or the cache would be lossy."""
    first = _synth("ava@quillhaven.com", "quillhaven.com")
    second = _synth("noor@quillhaven.com", "quillhaven.com")

    assert first.company == second.company
    company_providers = [s.name for s in CATALOG if s.entity_type == "company"]
    for name in company_providers:
        assert first.observations[name] == second.observations[name]

    assert first.person != second.person


def test_different_identities_synthesize_different_leads() -> None:
    a = _synth("lee@emberfall.com", "emberfall.com")
    b = _synth("lee@galecrest.com", "galecrest.com")
    assert a.company != b.company
    assert a.observations != b.observations


def test_submitted_display_names_survive_into_the_lead() -> None:
    """The receipt shows what the submitter typed, not generator inventions."""
    lead = _synth(
        "p.raman@northwind.com",
        "northwind.com",
        full_name="Priya Raman",
        company_name="Northwind Logistics",
    )
    assert lead.person.full_name == "Priya Raman"
    assert lead.company.legal_name == "Northwind Logistics"


def test_missing_display_names_fall_back_to_readable_derivations() -> None:
    lead = _synth("jordan.p.lee@velvetpine.co", "velvetpine.co")
    assert lead.person.full_name == "Jordan P Lee"
    assert "Velvetpine" in lead.company.legal_name


# ------------------------------------------------ economics and outcome mix --


def test_observations_use_the_catalogue_and_only_the_catalogue() -> None:
    lead = _synth("kit@umberline.com", "umberline.com")
    assert set(lead.observations) == {spec.name for spec in CATALOG}
    for spec in CATALOG:
        obs = lead.observations[spec.name]
        # A successful observation bills the catalogue rate; misses follow
        # bill_on_miss; failures are never billed — same semantics the frozen
        # corpus was generated with.
        assert obs.cost_usd in (0.0, spec.base_cost_usd)


def test_the_policy_reaches_every_outcome_over_a_sample_of_identities(
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """The demo must be able to show an autonomous route, a reject, and a
    human escalation from leads a visitor typed in — otherwise arbitrary
    submissions are a single-outcome dead end and the demo teaches nothing."""
    decisions = collections.Counter()
    autonomy = collections.Counter()

    words = [
        "alder",
        "birch",
        "cedar",
        "dell",
        "ember",
        "frost",
        "gale",
        "harbor",
        "iris",
        "juniper",
        "keel",
        "larch",
        "mesa",
        "nadir",
        "onyx",
        "pine",
        "quill",
        "ridge",
        "sable",
        "thorn",
        "umber",
        "vale",
        "wren",
        "xenon",
        "yarrow",
        "zephyr",
        "apex",
        "bolt",
        "crux",
        "dune",
    ]
    for word in words:
        for i in range(3):
            domain = f"{word}works{i}.com"
            lead = _synth(f"contact{i}.{word}@{domain}", domain)
            _, registry = build_from_leads([lead])
            ctx = RunContext(registry=registry, ledger=CallLedger(), cache=_MemoryCache())
            outcome = runtime.policy.run(lead, ctx)
            decisions[str(outcome.decision)] += 1
            autonomy[outcome.autonomous] += 1
            assert outcome.cost_usd <= 1.5, "a synthesized lead blew the demo budget cap"

    assert decisions["auto_route"] > 0
    assert decisions["reject"] > 0
    assert autonomy[False] > 0, "no synthesized lead ever escalated to a human"
    assert autonomy[True] > 0, "no synthesized lead ever resolved autonomously"


# ----------------------------------------------------------- corpus safety --


def test_synthetic_ids_cannot_collide_with_corpus_ids(leads: list[EvalLead]) -> None:
    """Corpus ids are `cal#####`/`tst#####`-shaped; synthetic ids carry a
    `syn:` prefix. A collision would let a synthesized lead poison the
    frozen observation store's keying assumptions."""
    lead = _synth("someone@anywhere.com", "anywhere.com")
    assert lead.company.company_id.startswith("syn:")
    assert lead.person.person_id.startswith("syn:")
    corpus_company_ids = {c.company.company_id for c in leads}
    assert lead.company.company_id not in corpus_company_ids
