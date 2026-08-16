"""Calibration quality measurement.

A confidence number is only useful if it means what it says: among predictions
made at 0.8, roughly 80% should be correct. Accuracy does not measure this and
neither does AUC — a model can rank perfectly while being systematically
overconfident. These are the metrics that catch that.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_BINS = 15


@dataclass(frozen=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_frequency: float

    @property
    def gap(self) -> float:
        """Signed miscalibration. Positive means overconfident."""
        return self.mean_predicted - self.observed_frequency


@dataclass(frozen=True)
class CalibrationReport:
    n_samples: int
    ece: float
    """Expected calibration error — mean |predicted - observed|, count-weighted."""

    mce: float
    """Maximum calibration error — the worst single bin."""

    brier: float
    """Mean squared error of the probabilities; rewards sharpness *and* calibration."""

    base_rate: float
    bins: tuple[ReliabilityBin, ...]

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["bins"] = [asdict(b) | {"gap": round(b.gap, 6)} for b in self.bins]
        return payload

    def reliability_table(self) -> str:
        lines = [
            f"{'bin':>12} {'n':>6} {'predicted':>10} {'observed':>10} {'gap':>8}",
            "-" * 50,
        ]
        for b in self.bins:
            if b.count == 0:
                continue
            lines.append(
                f"{b.lower:>5.2f}-{b.upper:<5.2f} {b.count:>6d} "
                f"{b.mean_predicted:>10.4f} {b.observed_frequency:>10.4f} {b.gap:>+8.4f}"
            )
        lines.append("-" * 50)
        lines.append(f"ECE={self.ece:.4f}  MCE={self.mce:.4f}  Brier={self.brier:.4f}")
        return "\n".join(lines)


def calibration_report(
    predicted: Sequence[float], actual: Sequence[int], n_bins: int = DEFAULT_BINS
) -> CalibrationReport:
    """Bin predictions and compare stated confidence against observed frequency."""
    if len(predicted) != len(actual):
        raise ValueError("predicted and actual must be the same length")
    if not predicted:
        raise ValueError("cannot compute calibration on an empty sample")

    n = len(predicted)
    edges = [i / n_bins for i in range(n_bins + 1)]
    bins: list[ReliabilityBin] = []
    weighted_gap = 0.0
    max_gap = 0.0

    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        # Upper edge is inclusive only in the final bin, so a prediction of
        # exactly 1.0 is counted rather than silently dropped.
        members = [
            (p, a)
            for p, a in zip(predicted, actual, strict=True)
            if (lo <= p < hi) or (i == n_bins - 1 and p == hi)
        ]
        if not members:
            bins.append(ReliabilityBin(lo, hi, 0, 0.0, 0.0))
            continue

        mean_predicted = sum(p for p, _ in members) / len(members)
        observed = sum(a for _, a in members) / len(members)
        gap = abs(mean_predicted - observed)

        weighted_gap += (len(members) / n) * gap
        max_gap = max(max_gap, gap)
        bins.append(
            ReliabilityBin(
                lower=lo,
                upper=hi,
                count=len(members),
                mean_predicted=round(mean_predicted, 6),
                observed_frequency=round(observed, 6),
            )
        )

    brier = sum((p - a) ** 2 for p, a in zip(predicted, actual, strict=True)) / n

    return CalibrationReport(
        n_samples=n,
        ece=round(weighted_gap, 6),
        mce=round(max_gap, 6),
        brier=round(brier, 6),
        base_rate=round(sum(actual) / n, 6),
        bins=tuple(bins),
    )
