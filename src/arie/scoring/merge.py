"""Reconcile conflicting provider observations into a single fact bundle.

Providers disagree — that is the point of having several. This module decides
whose value wins, and it is deliberately deterministic: the same observations
always produce the same facts, so a benchmark result never depends on dict
ordering or iteration luck.

Conflict policy: **highest provider precision for that field wins**, ties broken
by provider name for stability. Precision here is a static property of the
catalogue (a premium firmographics source is more trustworthy on employee_count
than a website scrape), not something inferred at runtime.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from arie.core.types import ProviderStatus
from arie.evalgen.schema import ProviderObservation
from arie.providers.catalog import BY_NAME


def _field_precision(provider_name: str) -> float:
    """How much to trust a provider, derived from its declared error rates.

    Deriving this from the catalogue rather than hardcoding a separate table
    means a provider's trustworthiness cannot drift out of sync with the noise
    actually applied to its observations.
    """
    spec = BY_NAME[provider_name]
    return 1.0 - max(spec.categorical_error, spec.numeric_noise)


def merge_observations(
    observations: Mapping[str, ProviderObservation],
    allowed_providers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Collapse observations into the fact dict the scorer consumes.

    ``allowed_providers`` restricts which sources are visible — this is how the
    validity gate evaluates a cheap-tier-only policy against the full-information
    ceiling using exactly the same code path.
    """
    allowed = set(allowed_providers) if allowed_providers is not None else None

    # field -> (precision, provider_name, value)
    best: dict[str, tuple[float, str, Any]] = {}

    for provider_name in sorted(observations):
        if allowed is not None and provider_name not in allowed:
            continue

        obs = observations[provider_name]
        if obs.status is not ProviderStatus.SUCCESS:
            continue

        precision = _field_precision(provider_name)
        for field_name, value in sorted(obs.fields.items()):
            if value is None:
                # A provider returning null for a field is not evidence of
                # absence; it just did not resolve that attribute.
                continue
            current = best.get(field_name)
            if current is None or (precision, provider_name) > (current[0], current[1]):
                best[field_name] = (precision, provider_name, value)

    return {name: value for name, (_, _, value) in best.items()}


def merged_cost(
    observations: Mapping[str, ProviderObservation],
    allowed_providers: Iterable[str] | None = None,
) -> float:
    """Total spend for the given provider subset, including billed misses."""
    allowed = set(allowed_providers) if allowed_providers is not None else None
    return sum(
        obs.cost_usd for name, obs in observations.items() if allowed is None or name in allowed
    )
