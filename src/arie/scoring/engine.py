"""Deterministic scorer over partial evidence.

The important idea in this module is **score bounds**. Given what is currently
known, the reachable score has a floor and a ceiling determined by what the
still-unknown fields could contribute. When that whole interval falls on one
side of a decision boundary, the decision is *already settled* — no purchasable
evidence can change it, so enrichment should stop.

That is decision stability computed deterministically, with no probability
involved. It is the floor the value-of-information layer sits on: the EVoI
controller only has to reason about cases this cannot already resolve, and
"deterministic before probabilistic" is honoured rather than asserted.

One asymmetry is worth calling out, because it drives real behaviour: while the
disqualifying flag is unknown, the score floor is **zero** — a blocker can
nullify any lead however strong it looks. So a lead can never be *proven* safe
to auto-route until that flag has been checked, no matter how much else is
bought. The catalogue exposes it through a single expensive provider, which is
exactly the tension the acquisition policy exists to resolve.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from arie.core.types import Decision, Evidence, ScoreBreakdown
from arie.scoring.merge import (
    FieldResolution,
    candidates_from_evidence,
    facts_from,
    resolve_candidates,
)
from arie.scoring.rules import (
    COMPLETENESS_WEIGHTS,
    DISQUALIFIER_FIELD,
    MAX_FIELD_POINTS,
    QUALIFY_THRESHOLD,
    REJECT_THRESHOLD,
    SCORED_FIELDS,
    decide,
    is_disqualified,
    score_facts,
)


@dataclass(frozen=True)
class ScoreBounds:
    """Reachable score interval given the fields still unknown."""

    current: float
    lower: float
    upper: float

    @property
    def settled_decision(self) -> Decision | None:
        """The decision, if no unknown evidence could change it.

        ``None`` means the interval straddles a boundary and the outcome is
        still genuinely open.
        """
        if self.lower >= QUALIFY_THRESHOLD:
            return Decision.AUTO_ROUTE
        if self.upper < REJECT_THRESHOLD:
            return Decision.REJECT
        if self.lower >= REJECT_THRESHOLD and self.upper < QUALIFY_THRESHOLD:
            # The entire reachable range sits inside the borderline band, so
            # the lead is provably one a human should look at.
            return Decision.ESCALATE_HUMAN
        return None

    @property
    def is_settled(self) -> bool:
        return self.settled_decision is not None

    @property
    def width(self) -> float:
        return self.upper - self.lower


@dataclass(frozen=True)
class EvidenceSignals:
    """Uncertainty features for the confidence model.

    Deliberately *not* a confidence number. These are the raw, interpretable
    inputs; turning them into a calibrated probability is a separate step fitted
    on held-out data. Emitting a confidence directly from here would be the
    uncalibrated-magic-number pattern this project exists to avoid.
    """

    completeness: float
    """Share of decision-relevant information known, weighted by field impact."""

    conflict_rate: float
    """Of the fields with more than one source, the share that disagree."""

    max_conflict_points: float
    """Largest score-relevant disagreement between sources, in points."""

    mean_source_confidence: float
    """Average confidence of the winning values, after staleness decay."""

    boundary_distance: float
    """How far the current score sits from the nearest decision boundary."""

    total_candidates: int
    """How many individual claims were gathered, across all fields.

    Evidence *volume*, distinct from completeness (which counts fields covered).
    Needed to disentangle conflict from enrichment depth: disagreement can only
    be observed once several sources have reported, so without this a conflict
    signal silently proxies for "many providers were called" and its effect
    inverts."""

    known_fields: tuple[str, ...]
    unknown_fields: tuple[str, ...]
    contested_fields: tuple[str, ...]
    stale_fields: tuple[str, ...]


@dataclass(frozen=True)
class ScoringResult:
    breakdown: ScoreBreakdown
    decision: Decision
    bounds: ScoreBounds
    signals: EvidenceSignals
    facts: dict[str, Any]
    resolutions: dict[str, FieldResolution]

    @property
    def total_score(self) -> float:
        return self.breakdown.total_score


def compute_bounds(facts: Mapping[str, Any]) -> ScoreBounds:
    """Floor and ceiling of the score over all possible unknown values."""
    current = score_facts(dict(facts)).total_score

    if is_disqualified(facts):
        # Nothing can rescue a confirmed blocker; the score is pinned at zero.
        return ScoreBounds(current=0.0, lower=0.0, upper=0.0)

    unknown = [name for name in SCORED_FIELDS if facts.get(name) is None]

    # Unknown additive fields contribute zero today and at most their ceiling
    # later, so they only raise the upper bound.
    upper = current + sum(MAX_FIELD_POINTS.get(name, 0.0) for name in unknown)

    # An unchecked blocker can zero everything, so the floor collapses to zero.
    lower = 0.0 if DISQUALIFIER_FIELD in unknown else current

    return ScoreBounds(current=current, lower=lower, upper=upper)


def _boundary_distance(score: float) -> float:
    return min(abs(score - QUALIFY_THRESHOLD), abs(score - REJECT_THRESHOLD))


def compute_signals(
    facts: Mapping[str, Any],
    resolutions: Mapping[str, FieldResolution],
    stale_fields: Iterable[str] = (),
) -> EvidenceSignals:
    known = tuple(name for name in SCORED_FIELDS if facts.get(name) is not None)
    unknown = tuple(name for name in SCORED_FIELDS if facts.get(name) is None)

    total_weight = sum(COMPLETENESS_WEIGHTS.values())
    known_weight = sum(COMPLETENESS_WEIGHTS.get(name, 0.0) for name in known)
    completeness = known_weight / total_weight if total_weight else 0.0

    # Only fields with more than one source can disagree. Using every field as
    # the denominator would dilute the rate toward zero and make genuine
    # conflict invisible whenever coverage was sparse.
    multi_source = [r for r in resolutions.values() if r.candidate_count > 1]
    contested = [r for r in multi_source if r.contested]
    conflict_rate = len(contested) / len(multi_source) if multi_source else 0.0
    max_conflict = max((r.conflict_points for r in resolutions.values()), default=0.0)

    confidences = [r.confidence for r in resolutions.values()]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    return EvidenceSignals(
        completeness=round(completeness, 6),
        conflict_rate=round(conflict_rate, 6),
        max_conflict_points=round(max_conflict, 6),
        mean_source_confidence=round(mean_confidence, 6),
        boundary_distance=round(_boundary_distance(score_facts(dict(facts)).total_score), 6),
        total_candidates=sum(r.candidate_count for r in resolutions.values()),
        known_fields=known,
        unknown_fields=unknown,
        contested_fields=tuple(sorted(r.field_name for r in contested)),
        stale_fields=tuple(sorted(stale_fields)),
    )


def score_resolved(
    facts: Mapping[str, Any],
    resolutions: Mapping[str, FieldResolution] | None = None,
    stale_fields: Iterable[str] = (),
) -> ScoringResult:
    """Score an already-resolved fact bundle.

    The oracle path enters here with complete facts and no resolutions; the
    runtime path enters through :func:`score_evidence`. Both traverse the same
    scoring code, which is what keeps oracle agreement meaningful.
    """
    resolutions = dict(resolutions or {})
    breakdown = score_facts(dict(facts))
    return ScoringResult(
        breakdown=breakdown,
        decision=decide(breakdown.total_score),
        bounds=compute_bounds(facts),
        signals=compute_signals(facts, resolutions, stale_fields),
        facts=dict(facts),
        resolutions=resolutions,
    )


def score_evidence(evidence: Iterable[Evidence], now: datetime) -> ScoringResult:
    """Score a lead from its accumulated evidence.

    Expired evidence is dropped rather than merely down-weighted: past its TTL
    its effective confidence is zero, so keeping it would only add a
    zero-confidence candidate that can never win but would still inflate the
    conflict denominator.
    """
    items = [item for item in evidence if not item.is_expired(now)]
    stale = tuple(
        sorted(
            {item.field_name for item in items if item.effective_confidence(now) < item.confidence}
        )
    )
    resolutions = resolve_candidates(candidates_from_evidence(items, now))
    return score_resolved(facts_from(resolutions), resolutions, stale)
