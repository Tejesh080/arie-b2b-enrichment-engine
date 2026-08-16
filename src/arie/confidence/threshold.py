"""Autonomy threshold selection.

Picks the confidence level τ above which the system may act without a human.

The naive approach — sweep τ and keep the point where the *observed* error rate
first drops below target — is optimistic. With few accepted samples, observing
zero errors is weak evidence that the true error rate is low, and the resulting
τ silently under-delivers on its guarantee.

Instead this uses a **Clopper-Pearson upper confidence bound** on the selective
error rate: choose the smallest τ (i.e. the largest accepted set) for which we
can say, with confidence 1-δ, that the true error rate among accepted
predictions is at most the target rate. Two errors out of ten is not the same evidence as
twenty out of a hundred, and the bound reflects that.

This is a risk-controlling threshold in the spirit of conformal selective
prediction. The guarantee is marginal — it holds on average over the population
the calibration data was drawn from, not conditionally for any particular lead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from scipy.stats import beta

# Probability that the stated error bound actually holds. 0.05 is conventional.
DEFAULT_DELTA = 0.05

# τ above every achievable confidence: accept nothing, escalate everything.
# Used when the target error rate cannot be met at any operating point.
REJECT_ALL_THRESHOLD = 1.01


def clopper_pearson_upper(errors: int, n: int, delta: float = DEFAULT_DELTA) -> float:
    """Exact binomial upper confidence bound on an error rate.

    Returns 1.0 for an empty sample: observing nothing justifies no claim.
    """
    if n <= 0:
        return 1.0
    if errors >= n:
        return 1.0
    # Beta(k+1, n-k) is the standard exact upper bound for k successes in n.
    return float(beta.ppf(1.0 - delta, errors + 1, n - errors))


@dataclass(frozen=True)
class ThresholdSelection:
    tau: float
    target_error_rate: float
    delta: float

    n_evaluated: int
    n_accepted: int
    observed_errors: int

    observed_error_rate: float
    error_rate_upper_bound: float

    @property
    def coverage(self) -> float:
        """Share of decisions the system may make autonomously."""
        return self.n_accepted / self.n_evaluated if self.n_evaluated else 0.0

    @property
    def guarantee_met(self) -> bool:
        return self.n_accepted > 0 and self.error_rate_upper_bound <= self.target_error_rate

    def to_json(self) -> dict[str, Any]:
        return asdict(self) | {
            "coverage": round(self.coverage, 6),
            "guarantee_met": self.guarantee_met,
        }


def select_threshold(
    confidences: Sequence[float],
    correct: Sequence[int],
    target_error_rate: float,
    delta: float = DEFAULT_DELTA,
) -> ThresholdSelection:
    """Find the lowest τ whose bounded selective error still meets the target.

    Lowest, not highest: among thresholds that satisfy the guarantee we want the
    one that automates the most. A τ of 0.99 is trivially safe and useless.
    """
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must be the same length")
    if not confidences:
        raise ValueError("cannot select a threshold from an empty sample")

    n_total = len(confidences)
    # Descending confidence: each step down admits the next-most-confident
    # prediction, so the candidate accepted sets are exactly the prefixes.
    ordered = sorted(zip(confidences, correct, strict=True), key=lambda pair: pair[0], reverse=True)

    best: ThresholdSelection | None = None
    errors = 0

    for i, (confidence, is_correct) in enumerate(ordered, start=1):
        errors += 1 - is_correct
        # Ties matter: accepting at this confidence means accepting every
        # prediction of equal confidence, so only evaluate at genuine breaks.
        if i < n_total and ordered[i][0] == confidence:
            continue

        bound = clopper_pearson_upper(errors, i, delta)
        if bound <= target_error_rate:
            best = ThresholdSelection(
                tau=confidence,
                target_error_rate=target_error_rate,
                delta=delta,
                n_evaluated=n_total,
                n_accepted=i,
                observed_errors=errors,
                observed_error_rate=round(errors / i, 6),
                error_rate_upper_bound=round(bound, 6),
            )

    if best is not None:
        return best

    # No operating point is defensible — escalate everything rather than
    # quietly loosening the guarantee. A system that cannot meet its stated
    # error budget should say so.
    total_errors = sum(1 - c for c in correct)
    return ThresholdSelection(
        tau=REJECT_ALL_THRESHOLD,
        target_error_rate=target_error_rate,
        delta=delta,
        n_evaluated=n_total,
        n_accepted=0,
        observed_errors=0,
        observed_error_rate=0.0,
        error_rate_upper_bound=round(clopper_pearson_upper(total_errors, n_total, delta), 6),
    )
