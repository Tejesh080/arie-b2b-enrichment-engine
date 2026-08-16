"""Feature extraction for the confidence model.

The model answers one question: **given the evidence gathered so far, what is
the probability that the decision I would make right now matches the oracle?**

Note what is *not* here. There is no "is the score high" feature — the model
predicts correctness, not desirability. A confidently-rejected lead should score
high confidence, and folding score level into the features would conflate "good
lead" with "well-understood lead".

The feature order is fixed and asserted, because a silent reordering between
fitting and prediction would produce a model that is confidently wrong rather
than one that crashes.
"""

from __future__ import annotations

from arie.scoring.engine import ScoringResult
from arie.scoring.rules import MAX_TOTAL_SCORE, SCORED_FIELDS

FEATURE_NAMES: tuple[str, ...] = (
    "completeness",
    "evidence_density",
    "mean_source_confidence",
    "boundary_distance",
    "bounds_width",
    "is_settled",
    "unknown_field_ratio",
    "conflict_rate",
    "max_conflict_points",
    "contested_field_ratio",
)

# Roughly the candidate count at which a lead is thoroughly sourced; used only
# to scale the density feature into [0, 1].
_DENSITY_SATURATION = 2.0


def extract_features(result: ScoringResult) -> dict[str, float]:
    signals = result.signals
    bounds = result.bounds
    n_fields = len(SCORED_FIELDS)

    return {
        "completeness": signals.completeness,
        # Evidence volume, separate from field coverage. Without it the
        # conflict features act as a proxy for "many providers were called"
        # and their learned effect inverts — disagreement would appear to
        # *increase* confidence.
        "evidence_density": min(signals.total_candidates / (n_fields * _DENSITY_SATURATION), 1.0),
        "mean_source_confidence": signals.mean_source_confidence,
        # Distance from the nearest decision boundary, normalised. A lead
        # sitting on a threshold flips on the smallest observation error, so
        # this is the dominant driver of decision fragility.
        "boundary_distance": min(signals.boundary_distance / MAX_TOTAL_SCORE, 1.0),
        "bounds_width": min(bounds.width / MAX_TOTAL_SCORE, 1.0),
        # Included as a feature, NOT as an override. A settled decision means no
        # unknown evidence can change it — but the known evidence may itself be
        # wrong, so settled is informative rather than conclusive.
        "is_settled": 1.0 if bounds.is_settled else 0.0,
        "unknown_field_ratio": len(signals.unknown_fields) / n_fields,
        "conflict_rate": signals.conflict_rate,
        "max_conflict_points": min(signals.max_conflict_points / MAX_TOTAL_SCORE, 1.0),
        "contested_field_ratio": len(signals.contested_fields) / n_fields,
    }


def feature_vector(result: ScoringResult) -> list[float]:
    features = extract_features(result)
    return [features[name] for name in FEATURE_NAMES]
