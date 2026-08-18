"""`scripts.policy_lab.comparison` — the Calibrated Bounds vs. tuned
waterfall baseline comparison. Fixture numbers are chosen so
mean-of-per-seed-ratios and ratio-of-means genuinely differ, pinning that
these are computed as two distinct statistics rather than accidentally the
same expression twice (see docs/05-results.md's own 41.6% vs. 41.4% note)."""

from __future__ import annotations

import pytest
from scripts.policy_lab.comparison import compare_to_baseline
from scripts.policy_lab.stats import PolicyStats, SeedSeries


def _stats(
    policy: str,
    *,
    cost: tuple[float, ...],
    agreement: tuple[float, ...],
    calls: tuple[float, ...],
    autonomy: tuple[float, ...],
) -> PolicyStats:
    return PolicyStats(
        policy=policy,
        seeds=tuple(range(1, len(cost) + 1)),
        agreement=SeedSeries("decision_agreement", agreement),
        cost_usd=SeedSeries("mean_cost_usd", cost),
        calls=SeedSeries("mean_calls", calls),
        autonomy=SeedSeries("autonomous_rate", autonomy),
    )


def test_baseline_comparison_computes_both_cost_pct_statistics_distinctly() -> None:
    production = _stats(
        "calibrated_bounds",
        cost=(0.20, 0.30),
        agreement=(0.80, 0.84),
        calls=(5.0, 6.0),
        autonomy=(0.80, 0.90),
    )
    baseline = _stats(
        "waterfall_expensive",
        cost=(0.40, 0.50),
        agreement=(0.83, 0.83),
        calls=(7.0, 7.0),
        autonomy=(0.70, 0.70),
    )

    result = compare_to_baseline(production, baseline)

    # mean of per-seed ratios: seed1 (0.40-0.20)/0.40=0.5, seed2 (0.50-0.30)/0.50=0.4 -> mean 0.45
    assert result.cost_pct_change_mean_of_ratios == pytest.approx(0.45)
    # ratio of means: mean_prod=0.25, mean_base=0.45 -> (0.45-0.25)/0.45
    assert result.cost_pct_change_ratio_of_means == pytest.approx((0.45 - 0.25) / 0.45)
    # The two statistics must genuinely differ for this fixture -- otherwise
    # a bug computing the same expression twice would pass silently.
    assert result.cost_pct_change_mean_of_ratios != pytest.approx(
        result.cost_pct_change_ratio_of_means
    )


def test_baseline_comparison_cost_abs_diff_is_negative_when_cheaper() -> None:
    production = _stats(
        "calibrated_bounds",
        cost=(0.20, 0.30),
        agreement=(0.8, 0.8),
        calls=(5.0, 5.0),
        autonomy=(0.8, 0.8),
    )
    baseline = _stats(
        "waterfall_expensive",
        cost=(0.40, 0.40),
        agreement=(0.8, 0.8),
        calls=(7.0, 7.0),
        autonomy=(0.8, 0.8),
    )
    result = compare_to_baseline(production, baseline)
    assert result.cost_abs_diff_usd == pytest.approx(0.25 - 0.40)
    assert result.cost_abs_diff_usd < 0


def test_baseline_comparison_agreement_pp_diff_is_negative_when_worse() -> None:
    production = _stats(
        "calibrated_bounds",
        cost=(0.2, 0.2),
        agreement=(0.80, 0.84),
        calls=(5.0, 5.0),
        autonomy=(0.8, 0.8),
    )
    baseline = _stats(
        "waterfall_expensive",
        cost=(0.4, 0.4),
        agreement=(0.83, 0.83),
        calls=(7.0, 7.0),
        autonomy=(0.8, 0.8),
    )
    result = compare_to_baseline(production, baseline)
    # mean agreement: prod 0.82, base 0.83 -> -1.0pp
    assert result.agreement_pp_diff == pytest.approx(-1.0)
    # per-seed gaps in pp: seed1 (0.80-0.83)*100=-3.0, seed2 (0.84-0.83)*100=1.0
    assert result.agreement_pp_diff_stdev == pytest.approx(2.8284271247, rel=1e-6)


def test_baseline_comparison_calls_and_autonomy_diffs() -> None:
    production = _stats(
        "calibrated_bounds",
        cost=(0.2, 0.2),
        agreement=(0.8, 0.8),
        calls=(5.0, 6.0),
        autonomy=(0.80, 0.90),
    )
    baseline = _stats(
        "waterfall_expensive",
        cost=(0.4, 0.4),
        agreement=(0.8, 0.8),
        calls=(7.0, 7.0),
        autonomy=(0.70, 0.70),
    )
    result = compare_to_baseline(production, baseline)
    assert result.calls_diff == pytest.approx(5.5 - 7.0)
    assert result.autonomy_diff == pytest.approx(0.85 - 0.70)


def test_baseline_comparison_reproduces_the_published_headline_figures() -> None:
    """The real 10-seed per-seed values behind bench/out/multi_seed.json's
    headline row (seeds 42-51; see docs/05-results.md: "41.6% cheaper... sd
    11.0pp", "2.33pp lower... sd 2.07pp"). Uses the actual per-seed cost and
    agreement values, not just their means, since the headline "41.6%" is
    itself a mean of ten per-seed ratios -- an aggregate-only fixture cannot
    reproduce it (mean-of-ratios and ratio-of-means coincide when there is
    only one seed)."""
    production = _stats(
        "calibrated_bounds",
        cost=(
            0.226368,
            0.23755267,
            0.21453667,
            0.19485733,
            0.20967933,
            0.259288,
            0.37213667,
            0.222364,
            0.22696467,
            0.299612,
        ),
        agreement=(
            0.796667,
            0.846667,
            0.81,
            0.79,
            0.73,
            0.816667,
            0.89,
            0.813333,
            0.806667,
            0.813333,
        ),
        calls=(5.26,) * 10,
        autonomy=(0.8333,) * 10,
    )
    baseline = _stats(
        "waterfall_expensive",
        cost=(
            0.43678667,
            0.39114067,
            0.43070133,
            0.36942667,
            0.40756333,
            0.449664,
            0.431246,
            0.39102,
            0.44108133,
            0.45676533,
        ),
        agreement=(0.836667, 0.856667, 0.84, 0.81, 0.793333, 0.85, 0.883333, 0.84, 0.826667, 0.81),
        calls=(7.58,) * 10,
        autonomy=(0.7947,) * 10,
    )
    result = compare_to_baseline(production, baseline)
    assert result.cost_pct_change_mean_of_ratios == pytest.approx(0.4156, abs=1e-3)
    assert result.cost_pct_change_mean_of_ratios_stdev == pytest.approx(0.1097, abs=1e-3)
    assert result.cost_pct_change_ratio_of_means == pytest.approx(0.4143, abs=1e-3)
    assert result.agreement_pp_diff == pytest.approx(-2.33, abs=0.02)
