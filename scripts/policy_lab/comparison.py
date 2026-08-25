"""Calibrated Bounds vs. the tuned waterfall — the one comparison the report
treats as a focused, named claim rather than a table cell.

Reports both the mean-of-per-seed-ratios cost saving (what `docs/benchmark.md`
calls "41.6% cheaper... sd 11.0pp") and the ratio-of-means reading (what that
same doc calls "41.4%... in aggregate dollar terms"), because they are
genuinely different statistics computed from the same data — this repo's own
convention is to report both rather than pick one silently.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from scripts.policy_lab.stats import PolicyStats


@dataclass(frozen=True)
class BaselineComparison:
    production_policy: str
    baseline_policy: str

    cost_abs_diff_usd: float
    """production - baseline, mean of means. Negative = production cheaper."""

    cost_pct_change_ratio_of_means: float
    """(mean(baseline) - mean(production)) / mean(baseline). Positive = cheaper."""

    cost_pct_change_mean_of_ratios: float
    """Mean across seeds of that seed's own (baseline - production) / baseline.
    Positive = cheaper. This is the headline "41.6%"-style figure."""

    cost_pct_change_mean_of_ratios_stdev: float

    agreement_pp_diff: float
    """(production.mean - baseline.mean) * 100. Negative = production lower."""

    agreement_pp_diff_stdev: float
    """Stdev across seeds of that seed's own (production - baseline) agreement
    gap in percentage points."""

    calls_diff: float
    autonomy_diff: float


def compare_to_baseline(production: PolicyStats, baseline: PolicyStats) -> BaselineComparison:
    per_seed_cost_ratios = [
        (b_cost - p_cost) / b_cost
        for p_cost, b_cost in zip(production.cost_usd.values, baseline.cost_usd.values, strict=True)
        if b_cost
    ]
    per_seed_agreement_gaps_pp = [
        (p_agr - b_agr) * 100
        for p_agr, b_agr in zip(production.agreement.values, baseline.agreement.values, strict=True)
    ]

    baseline_mean_cost = baseline.cost_usd.mean
    production_mean_cost = production.cost_usd.mean

    return BaselineComparison(
        production_policy=production.policy,
        baseline_policy=baseline.policy,
        cost_abs_diff_usd=production_mean_cost - baseline_mean_cost,
        cost_pct_change_ratio_of_means=(
            (baseline_mean_cost - production_mean_cost) / baseline_mean_cost
            if baseline_mean_cost
            else 0.0
        ),
        cost_pct_change_mean_of_ratios=statistics.fmean(per_seed_cost_ratios),
        cost_pct_change_mean_of_ratios_stdev=(
            statistics.stdev(per_seed_cost_ratios) if len(per_seed_cost_ratios) > 1 else 0.0
        ),
        agreement_pp_diff=(production.agreement.mean - baseline.agreement.mean) * 100,
        agreement_pp_diff_stdev=(
            statistics.stdev(per_seed_agreement_gaps_pp)
            if len(per_seed_agreement_gaps_pp) > 1
            else 0.0
        ),
        calls_diff=production.calls.mean - baseline.calls.mean,
        autonomy_diff=production.autonomy.mean - baseline.autonomy.mean,
    )
