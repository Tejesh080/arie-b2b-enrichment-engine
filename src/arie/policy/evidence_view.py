"""Turning provider results into a scored view, incrementally.

Policies accumulate results one call at a time and need to re-score after each.
This keeps that bookkeeping in one place so all three strategies build their
fact bundle identically — a difference here would show up as a decision-quality
difference and be misattributed to the strategy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from arie.core.types import ProviderResult, ProviderStatus
from arie.providers.catalog import BY_NAME, CATALOG
from arie.scoring.engine import ScoringResult, score_resolved
from arie.scoring.merge import Candidate, facts_from, resolve_candidates


def _precision(provider_name: str) -> float:
    spec = BY_NAME[provider_name]
    return 1.0 - max(spec.categorical_error, spec.numeric_noise)


def candidates_from_results(results: Mapping[str, ProviderResult]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for provider_name in sorted(results):
        result = results[provider_name]
        if result.status is not ProviderStatus.SUCCESS:
            continue
        precision = _precision(provider_name)
        candidates.extend(
            Candidate(field_name=name, value=value, source=provider_name, confidence=precision)
            for name, value in sorted(result.fields.items())
        )
    return candidates


def score_results(results: Mapping[str, ProviderResult]) -> ScoringResult:
    resolutions = resolve_candidates(candidates_from_results(results))
    return score_resolved(facts_from(resolutions), resolutions)


def remaining_providers(called: Iterable[str]) -> list[str]:
    """Uncalled providers, in catalogue (cheapest-first) order.

    Order matters for reproducibility: it breaks ties deterministically when two
    candidates have equal expected value.
    """
    seen = set(called)
    return [spec.name for spec in CATALOG if spec.name not in seen]
