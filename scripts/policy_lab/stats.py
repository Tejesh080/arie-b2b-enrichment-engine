"""Per-policy statistics across seeds, computed directly from the artifact's
raw `per_seed[].policies[]` rows.

Deliberately does not read the artifact's precomputed `stability` array for
`adaptive_voi_x1`: those rows summarize `best_adaptive` (the best-of-seven
`value_scale` variant per seed), not the single un-scaled `adaptive_voi_x1`
policy this report is about (see docs/05-results.md's own note on the
distinction). Recomputing from the raw per-seed rows, with the same method
(`statistics.fmean`/`stdev`) `bench/multi_seed.py` uses, keeps this module
self-contained and independently verifiable against the artifact.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

PRIMARY_POLICIES: tuple[str, ...] = (
    "full_enrichment",
    "waterfall_expensive",
    "calibrated_bounds",
    "adaptive_voi_x1",
)

PRODUCTION_POLICY = "calibrated_bounds"
BASELINE_POLICY = "waterfall_expensive"

DISPLAY_NAMES: dict[str, str] = {
    "full_enrichment": "Full enrichment",
    "waterfall_expensive": "Tuned waterfall",
    "calibrated_bounds": "Calibrated Bounds",
    "adaptive_voi_x1": "Adaptive EVoI",
}


class StatsError(RuntimeError):
    """The artifact doesn't contain a named policy for every seed — a
    corrupt or unexpectedly-shaped artifact, not something to guess past."""


@dataclass(frozen=True)
class SeedSeries:
    """One metric's values across seeds, in seed order."""

    name: str
    values: tuple[float, ...]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values)

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.values) if len(self.values) > 1 else 0.0

    @property
    def minimum(self) -> float:
        return min(self.values)

    @property
    def maximum(self) -> float:
        return max(self.values)


@dataclass(frozen=True)
class PolicyStats:
    policy: str
    seeds: tuple[int, ...]
    agreement: SeedSeries
    cost_usd: SeedSeries
    calls: SeedSeries
    autonomy: SeedSeries


def _policy_row(seed_entry: dict[str, Any], policy_name: str, seed: int) -> dict[str, Any]:
    for row in seed_entry.get("policies", []):
        if row.get("policy") == policy_name:
            return dict(row)
    raise StatsError(
        f"seed {seed}'s benchmark artifact has no '{policy_name}' policy row — "
        "the artifact may be from an older benchmark version that evaluated a "
        "different set of policies."
    )


def extract_policy_stats(artifact: dict[str, Any], policy_name: str) -> PolicyStats:
    """Pull one policy's metrics out of every seed in the artifact, in the
    artifact's own seed order."""
    seeds = tuple(int(s) for s in artifact["seeds"])
    per_seed = artifact["per_seed"]
    if len(per_seed) != len(seeds):
        raise StatsError(
            f"artifact declares {len(seeds)} seeds but has {len(per_seed)} "
            "per-seed entries — inconsistent artifact."
        )

    agreement: list[float] = []
    cost: list[float] = []
    calls: list[float] = []
    autonomy: list[float] = []
    for seed, entry in zip(seeds, per_seed, strict=True):
        if int(entry.get("seed", seed)) != seed:
            raise StatsError(
                f"per_seed entry order doesn't match 'seeds' list "
                f"(expected seed {seed}, found {entry.get('seed')})."
            )
        row = _policy_row(entry, policy_name, seed)
        agreement.append(float(row["decision_agreement"]))
        cost.append(float(row["mean_cost_usd"]))
        calls.append(float(row["mean_calls"]))
        autonomy.append(float(row["autonomous_rate"]))

    return PolicyStats(
        policy=policy_name,
        seeds=seeds,
        agreement=SeedSeries("decision_agreement", tuple(agreement)),
        cost_usd=SeedSeries("mean_cost_usd", tuple(cost)),
        calls=SeedSeries("mean_calls", tuple(calls)),
        autonomy=SeedSeries("autonomous_rate", tuple(autonomy)),
    )


def extract_all(
    artifact: dict[str, Any], policy_names: tuple[str, ...] = PRIMARY_POLICIES
) -> dict[str, PolicyStats]:
    return {name: extract_policy_stats(artifact, name) for name in policy_names}


def count_evaluated_policy_variants(artifact: dict[str, Any]) -> int:
    """Total distinct policy names present in seed 0's row — for provenance
    context (the artifact evaluates more tuning variants, e.g. per-tier
    waterfalls and per-`value_scale` EVoI runs, than the four this report
    compares)."""
    names = {row.get("policy") for row in artifact["per_seed"][0].get("policies", [])}
    return len(names)
