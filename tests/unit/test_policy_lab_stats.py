"""`scripts.policy_lab.stats` — per-policy statistics recomputed directly
from raw per-seed rows, deliberately independent of the artifact's own
precomputed `stability` array (see the module docstring for why)."""

from __future__ import annotations

import statistics
from typing import Any

import pytest
from scripts.policy_lab.stats import (
    StatsError,
    count_evaluated_policy_variants,
    extract_all,
    extract_policy_stats,
)


def _policy_row(
    name: str, *, cost: float, agreement: float, calls: float, autonomy: float
) -> dict[str, Any]:
    return {
        "policy": name,
        "mean_cost_usd": cost,
        "decision_agreement": agreement,
        "mean_calls": calls,
        "autonomous_rate": autonomy,
    }


def _artifact() -> dict[str, Any]:
    return {
        "seeds": [1, 2],
        "per_seed": [
            {
                "seed": 1,
                "policies": [
                    _policy_row(
                        "calibrated_bounds", cost=0.20, agreement=0.80, calls=5.0, autonomy=0.80
                    ),
                    _policy_row(
                        "waterfall_expensive", cost=0.40, agreement=0.83, calls=7.0, autonomy=0.70
                    ),
                ],
            },
            {
                "seed": 2,
                "policies": [
                    _policy_row(
                        "calibrated_bounds", cost=0.30, agreement=0.90, calls=6.0, autonomy=0.90
                    ),
                    _policy_row(
                        "waterfall_expensive", cost=0.50, agreement=0.83, calls=7.0, autonomy=0.70
                    ),
                ],
            },
        ],
        "stability": [],
    }


def test_extract_policy_stats_preserves_seed_order() -> None:
    stats = extract_policy_stats(_artifact(), "calibrated_bounds")
    assert stats.seeds == (1, 2)
    assert stats.cost_usd.values == (0.20, 0.30)
    assert stats.agreement.values == (0.80, 0.90)
    assert stats.calls.values == (5.0, 6.0)
    assert stats.autonomy.values == (0.80, 0.90)


def test_seed_series_mean_stdev_min_max_match_statistics_module() -> None:
    stats = extract_policy_stats(_artifact(), "calibrated_bounds")
    assert stats.cost_usd.mean == pytest.approx(statistics.fmean([0.20, 0.30]))
    assert stats.cost_usd.stdev == pytest.approx(statistics.stdev([0.20, 0.30]))
    assert stats.cost_usd.minimum == 0.20
    assert stats.cost_usd.maximum == 0.30


def test_seed_series_stdev_is_zero_for_a_single_value() -> None:
    stats = extract_policy_stats(
        {
            "seeds": [1],
            "per_seed": [
                {
                    "seed": 1,
                    "policies": [
                        _policy_row(
                            "calibrated_bounds", cost=0.2, agreement=0.8, calls=5, autonomy=0.8
                        )
                    ],
                }
            ],
            "stability": [],
        },
        "calibrated_bounds",
    )
    assert stats.cost_usd.stdev == 0.0


def test_extract_policy_stats_raises_when_a_seed_is_missing_the_policy() -> None:
    artifact = _artifact()
    del artifact["per_seed"][1]["policies"][0]  # drop calibrated_bounds from seed 2
    with pytest.raises(StatsError, match="calibrated_bounds"):
        extract_policy_stats(artifact, "calibrated_bounds")


def test_extract_policy_stats_raises_on_seed_count_mismatch() -> None:
    artifact = _artifact()
    artifact["seeds"] = [1, 2, 3]
    with pytest.raises(StatsError, match="inconsistent"):
        extract_policy_stats(artifact, "calibrated_bounds")


def test_extract_policy_stats_raises_on_seed_order_mismatch() -> None:
    artifact = _artifact()
    artifact["per_seed"][0]["seed"] = 99
    with pytest.raises(StatsError, match="order"):
        extract_policy_stats(artifact, "calibrated_bounds")


def test_extract_all_returns_one_entry_per_requested_policy() -> None:
    result = extract_all(_artifact(), ("calibrated_bounds", "waterfall_expensive"))
    assert set(result) == {"calibrated_bounds", "waterfall_expensive"}
    assert result["waterfall_expensive"].cost_usd.mean == pytest.approx(0.45)


def test_count_evaluated_policy_variants_counts_distinct_names_in_first_seed() -> None:
    assert count_evaluated_policy_variants(_artifact()) == 2
