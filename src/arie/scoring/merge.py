"""Reconcile conflicting evidence into a single fact bundle.

Sources disagree — that is the point of having several. This module decides
whose value wins and, just as importantly, records *that* they disagreed, since
unresolved disagreement is one of the strongest signals that a decision is not
yet safe to make autonomously.

Two things are deliberate here:

**Resolution is deterministic.** The same inputs always produce the same facts,
so a benchmark result never depends on dict ordering or iteration luck.

**Conflict is measured in score impact, not raw value difference.** Two sources
reporting 180 and 210 employees both land in the same band and contribute
identical points — that is not a decision-relevant conflict, and counting it as
one would flood the confidence model with noise. Two sources reporting 40 and 60
straddle a band boundary and genuinely disagree about the score.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from arie.core.types import Evidence, ProviderStatus
from arie.evalgen.schema import ProviderObservation
from arie.providers.catalog import BY_NAME
from arie.scoring.rules import SCORED_FIELDS, field_points, is_unknown

# Disagreements smaller than this many points are treated as agreement. Guards
# against float noise in continuous fields (e.g. two intent readings of 0.7001
# and 0.7002) being reported as conflict.
CONFLICT_EPSILON = 0.5


@dataclass(frozen=True)
class Candidate:
    """One source's claim about one field."""

    field_name: str
    value: Any
    source: str
    confidence: float


@dataclass(frozen=True)
class FieldResolution:
    """The winning value for a field, plus what was overruled to get there."""

    field_name: str
    value: Any
    source: str
    confidence: float
    candidate_count: int
    conflict_points: float
    """Largest score-relevant disagreement among candidates, in points."""

    @property
    def contested(self) -> bool:
        return self.conflict_points > CONFLICT_EPSILON


def _conflict_points(candidates: Sequence[Candidate]) -> float:
    """Widest score gap between any two candidates for a field."""
    if len(candidates) < 2:
        return 0.0
    points = [field_points(c.field_name, c.value) for c in candidates]
    return max(points) - min(points)


def resolve_candidates(candidates: Iterable[Candidate]) -> dict[str, FieldResolution]:
    """Pick a winner per field: highest confidence, ties broken by source name.

    Tie-breaking on the source name is arbitrary but *stable*, which is what
    matters — an unstable tiebreak would make scores depend on evidence
    insertion order.
    """
    grouped: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if candidate.field_name not in SCORED_FIELDS or is_unknown(candidate.value):
            # A null from a source is not evidence of absence, just an
            # unresolved attribute. The UNKNOWN sentinel is dropped for the
            # same reason and one more: a resolution is what the Decision
            # Receipt renders as "this field is known, and this source won
            # it". A value ARIE could not map is not a won field, and listing
            # it as one would make the receipt claim knowledge it does not
            # have. It leaves no fact behind — `facts_from` therefore omits
            # the field, and `arie.scoring.engine` reads it as unknown, which
            # is exactly right.
            continue
        grouped.setdefault(candidate.field_name, []).append(candidate)

    resolutions: dict[str, FieldResolution] = {}
    for field_name, group in sorted(grouped.items()):
        winner = max(group, key=lambda c: (c.confidence, c.source))
        resolutions[field_name] = FieldResolution(
            field_name=field_name,
            value=winner.value,
            source=winner.source,
            confidence=winner.confidence,
            candidate_count=len(group),
            conflict_points=_conflict_points(group),
        )
    return resolutions


def facts_from(resolutions: Mapping[str, FieldResolution]) -> dict[str, Any]:
    return {name: resolution.value for name, resolution in resolutions.items()}


# --- adapters ----------------------------------------------------------------


def _observation_precision(provider_name: str) -> float:
    """Trust in a provider, derived from its declared error rates.

    Deriving this from the catalogue rather than maintaining a parallel table
    means a provider's trustworthiness cannot drift out of sync with the noise
    actually applied to its observations.
    """
    spec = BY_NAME[provider_name]
    return 1.0 - max(spec.categorical_error, spec.numeric_noise)


def candidates_from_observations(
    observations: Mapping[str, ProviderObservation],
    allowed_providers: Iterable[str] | None = None,
) -> list[Candidate]:
    allowed = set(allowed_providers) if allowed_providers is not None else None
    candidates: list[Candidate] = []

    for provider_name in sorted(observations):
        if allowed is not None and provider_name not in allowed:
            continue
        observation = observations[provider_name]
        if observation.status is not ProviderStatus.SUCCESS:
            continue
        precision = _observation_precision(provider_name)
        candidates.extend(
            Candidate(
                field_name=field_name, value=value, source=provider_name, confidence=precision
            )
            for field_name, value in sorted(observation.fields.items())
        )
    return candidates


def candidates_from_evidence(evidence: Iterable[Evidence], now: datetime) -> list[Candidate]:
    """Evidence competes on *staleness-discounted* confidence.

    A cached fact is neither free-and-perfect nor worthless. Decaying its
    confidence with age is what lets a zero-cost stale value lose honestly to a
    fresh paid one, instead of being either blindly trusted or discarded.
    """
    return [
        Candidate(
            field_name=item.field_name,
            value=item.value,
            source=item.source,
            confidence=item.effective_confidence(now),
        )
        for item in evidence
    ]


def merge_observations(
    observations: Mapping[str, ProviderObservation],
    allowed_providers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Collapse observations into the fact dict the scorer consumes.

    ``allowed_providers`` restricts which sources are visible — this is how the
    validity gate evaluates a cheap-tier-only policy against the
    full-information ceiling through exactly the same code path.
    """
    return facts_from(
        resolve_candidates(candidates_from_observations(observations, allowed_providers))
    )


def merged_cost(
    observations: Mapping[str, ProviderObservation],
    allowed_providers: Iterable[str] | None = None,
) -> float:
    """Total spend for the given provider subset, including billed misses."""
    allowed = set(allowed_providers) if allowed_providers is not None else None
    return sum(
        obs.cost_usd for name, obs in observations.items() if allowed is None or name in allowed
    )
