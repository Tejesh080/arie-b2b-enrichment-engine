"""Correlated provider misses.

This is the least obvious design property in the dataset and the easiest to get
silently wrong, so it gets its own tests.

If provider misses were independent, a policy could always recover from a miss
by trying the next source, and enrichment would degrade into "keep going until
something answers". The *stopping* decision — the entire thesis — would never be
exercised. Real vendors fail together on the same thin, poorly-documented
accounts, and the generator models that with a single per-company obscurity
draw shared by every provider.
"""

from __future__ import annotations

import statistics

from arie.core.types import ProviderStatus
from arie.evalgen.schema import EvalLead


def _miss_count(lead: EvalLead) -> int:
    return sum(1 for obs in lead.observations.values() if obs.status is not ProviderStatus.SUCCESS)


def test_obscurity_drives_miss_rate(leads: list[EvalLead]) -> None:
    """The correlation mechanism must actually be wired up."""
    low = [_miss_count(x) for x in leads if x.company.obscurity < 0.25]
    high = [_miss_count(x) for x in leads if x.company.obscurity > 0.60]

    assert low and high, "dataset lacks obscurity spread"
    mean_low = statistics.mean(low)
    mean_high = statistics.mean(high)

    assert mean_high > mean_low + 0.5, (
        f"Obscure companies average {mean_high:.2f} misses vs {mean_low:.2f} for "
        "clear ones — provider failures are not correlated, so 'try the next "
        "provider' would always work and stopping would never be tested."
    )


def test_miss_counts_are_overdispersed(leads: list[EvalLead]) -> None:
    """Correlation signature: variance exceeds the independent-Bernoulli case.

    Under independence the per-lead miss count would be Poisson-binomial, whose
    variance is bounded above by mean * (1 - mean/n). Shared obscurity pushes
    the observed variance past that bound.
    """
    counts = [_miss_count(x) for x in leads]
    n_providers = len(leads[0].observations)

    mean = statistics.mean(counts)
    variance = statistics.variance(counts)
    independent_bound = mean * (1 - mean / n_providers)

    assert variance > independent_bound, (
        f"Miss-count variance {variance:.3f} does not exceed the independence "
        f"bound {independent_bound:.3f}; misses appear uncorrelated."
    )


def test_some_leads_are_barely_observable(leads: list[EvalLead]) -> None:
    """The irreducibly-hard tail must exist.

    These are the leads where even unlimited spend cannot resolve the answer.
    They set the honest floor for the human-escalation rate; a dataset without
    them would let the escalation rate be driven to zero as an artefact.
    """
    n_providers = len(leads[0].observations)
    starved = [x for x in leads if _miss_count(x) >= n_providers - 2]
    assert starved, "no leads are substantially unobservable; escalation floor is artificial"


def test_free_payload_always_available(leads: list[EvalLead]) -> None:
    """Stage-0 data arrives with the lead and cannot miss.

    Without a free source that always resolves, no lead could ever be decided
    cheaply and the cheap tier would be uniformly useless.
    """
    for lead in leads:
        payload = lead.observations["inbound_payload"]
        assert payload.status is ProviderStatus.SUCCESS
        assert payload.cost_usd == 0.0


def test_failed_calls_are_not_billed(leads: list[EvalLead]) -> None:
    """Transport failures cost nothing; data misses may, per provider policy."""
    for lead in leads:
        for obs in lead.observations.values():
            if obs.status is ProviderStatus.ERROR:
                assert obs.cost_usd == 0.0
