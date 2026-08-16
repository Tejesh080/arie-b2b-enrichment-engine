"""Seed sweep: is the result stable, or an artefact of one dataset?

The headline came from a single seed with a 1.00pp agreement gap — three leads
out of three hundred. A margin that thin has to be checked against sampling
variation before it is reported as a finding.

Each seed regenerates the dataset, refits the confidence model on that seed's
calibration split, and re-tunes the waterfall. Nothing is carried across seeds;
each is what a fresh deployment would see.

The sweep also prices human review, because the API-only accounting the thesis
was written against leaves out the person every escalation lands on.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arie.policy.runner import PolicySummary
from bench.cost_model import HUMAN_REVIEW_PRICES, break_even_human_cost, total_cost
from bench.harness import BenchmarkRun, run_once

DEFAULT_SEEDS: tuple[int, ...] = (42, 43, 44, 45, 46, 47, 48)


@dataclass(frozen=True)
class Spread:
    """Summary statistics for one metric across seeds."""

    name: str
    values: tuple[float, ...]

    @property
    def mean(self) -> float:
        return round(statistics.fmean(self.values), 6)

    @property
    def stdev(self) -> float:
        return round(statistics.stdev(self.values), 6) if len(self.values) > 1 else 0.0

    @property
    def minimum(self) -> float:
        return round(min(self.values), 6)

    @property
    def maximum(self) -> float:
        return round(max(self.values), 6)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mean": self.mean,
            "stdev": self.stdev,
            "min": self.minimum,
            "max": self.maximum,
            "values": [round(v, 6) for v in self.values],
        }

    def line(self) -> str:
        return (
            f"  {self.name:<38} mean={self.mean:>9.4f}  sd={self.stdev:>8.4f}  "
            f"min={self.minimum:>9.4f}  max={self.maximum:>9.4f}"
        )


def _spreads(runs: list[BenchmarkRun]) -> list[Spread]:
    return [
        Spread(
            "adaptive cost saving vs waterfall", tuple(r.cost_saving_vs_waterfall for r in runs)
        ),
        Spread(
            "agreement gap vs waterfall (pp)", tuple(r.agreement_gap_vs_waterfall_pp for r in runs)
        ),
        Spread("EVoI saving vs production", tuple(r.evoi_saving_vs_production for r in runs)),
        Spread(
            "production saving vs waterfall",
            tuple(r.production_saving_vs_waterfall for r in runs),
        ),
        Spread("production agreement gap (pp)", tuple(r.production_agreement_gap_pp for r in runs)),
        Spread("production autonomy", tuple(r.production.autonomous_rate for r in runs)),
        Spread("adaptive agreement", tuple(r.best_adaptive.decision_agreement for r in runs)),
        Spread("waterfall agreement", tuple(r.best_waterfall.decision_agreement for r in runs)),
        Spread("adaptive api cost/lead", tuple(r.best_adaptive.mean_cost_usd for r in runs)),
        Spread("waterfall api cost/lead", tuple(r.best_waterfall.mean_cost_usd for r in runs)),
        Spread("adaptive autonomy rate", tuple(r.best_adaptive.autonomous_rate for r in runs)),
        Spread("waterfall autonomy rate", tuple(r.best_waterfall.autonomous_rate for r in runs)),
        Spread("confidence ECE", tuple(r.confidence_ece for r in runs)),
        Spread("tau", tuple(r.tau for r in runs)),
        Spread(
            "esc-aware autonomy @ $2.50",
            tuple(r.escalation_aware[2.50].autonomous_rate for r in runs),
        ),
        Spread(
            "esc-aware api cost/lead @ $2.50",
            tuple(r.escalation_aware[2.50].mean_cost_usd for r in runs),
        ),
        Spread(
            "esc-aware agreement @ $2.50",
            tuple(r.escalation_aware[2.50].decision_agreement for r in runs),
        ),
    ]


def _total_cost_table(runs: list[BenchmarkRun]) -> list[dict[str, Any]]:
    """Mean total cost per policy across seeds, at each review price."""
    rows: list[dict[str, Any]] = []
    for price in HUMAN_REVIEW_PRICES:
        entry: dict[str, Any] = {"human_review_usd": price}
        for label, pick in (
            ("full", lambda r: r.full),
            ("waterfall", lambda r: r.best_waterfall),
            ("calibrated_bounds", lambda r: r.production),
            ("adaptive", lambda r: r.best_adaptive),
            # Judged at the same price it was told to assume. `price` is bound
            # as a default rather than captured, so the lambda cannot pick up a
            # later loop value if this is ever evaluated lazily.
            ("escalation_aware", lambda r, p=price: r.escalation_aware[p]),
        ):
            totals = [total_cost(pick(r), price).total_usd for r in runs]  # type: ignore[no-untyped-call]
            entry[label] = round(statistics.fmean(totals), 6)
        entry["cheapest"] = min(
            ("full", "waterfall", "calibrated_bounds", "adaptive", "escalation_aware"),
            key=lambda k: entry[k],
        )
        rows.append(entry)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ARIE benchmark across seeds")
    parser.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--out", type=Path, default=Path("bench/out"))
    args = parser.parse_args()

    runs: list[BenchmarkRun] = []
    print(f"running {len(args.seeds)} seeds: {args.seeds}\n")
    for seed in args.seeds:
        run = run_once(seed)
        runs.append(run)
        print(
            f"  seed {seed}: {run.verdict.outcome:<22} "
            f"saving={run.cost_saving_vs_waterfall:>7.1%} "
            f"gap={run.agreement_gap_vs_waterfall_pp:>6.2f}pp "
            f"adaptive_auto={run.best_adaptive.autonomous_rate:.3f} "
            f"tau={run.tau:.4f}"
        )

    print("\n" + "=" * 84)
    print("STABILITY ACROSS SEEDS (API cost only)")
    print("=" * 84)
    spreads = _spreads(runs)
    for spread in spreads:
        print(spread.line())

    outcomes: dict[str, int] = {}
    for run in runs:
        outcomes[run.verdict.outcome] = outcomes.get(run.verdict.outcome, 0) + 1
    print(f"\n  verdicts: {outcomes}")

    worst = min(runs, key=lambda r: r.cost_saving_vs_waterfall)
    print(
        f"  worst seed for saving: {worst.seed} "
        f"({worst.cost_saving_vs_waterfall:.1%}, gap {worst.agreement_gap_vs_waterfall_pp:.2f}pp, "
        f"{worst.verdict.outcome})"
    )
    widest = max(runs, key=lambda r: r.agreement_gap_vs_waterfall_pp)
    print(
        f"  worst seed for quality: {widest.seed} "
        f"(gap {widest.agreement_gap_vs_waterfall_pp:.2f}pp, "
        f"saving {widest.cost_saving_vs_waterfall:.1%}, {widest.verdict.outcome})"
    )

    print("\n" + "=" * 84)
    print("TOTAL COST INCLUDING HUMAN REVIEW (mean across seeds, $/lead)")
    print("=" * 84)
    header = (
        f"  {'review $':>9}  {'full':>9}  {'waterfall':>10}  {'calib_bnds':>11}  "
        f"{'adaptive':>9}  {'esc_aware':>10}   cheapest"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in _total_cost_table(runs):
        print(
            f"  {row['human_review_usd']:>9.2f}  {row['full']:>9.5f}  {row['waterfall']:>10.5f}  "
            f"{row['calibrated_bounds']:>11.5f}  {row['adaptive']:>9.5f}  "
            f"{row['escalation_aware']:>10.5f}   {row['cheapest']}"
        )

    break_evens = [
        be
        for be in (break_even_human_cost(r.best_adaptive, r.best_waterfall) for r in runs)
        if be is not None
    ]
    print("\n  break-even review price (adaptive stops beating waterfall above this):")
    if break_evens:
        print(
            f"    mean=${statistics.fmean(break_evens):.4f}  "
            f"min=${min(break_evens):.4f}  max=${max(break_evens):.4f}  "
            f"(n={len(break_evens)}/{len(runs)} seeds)"
        )
    else:
        print("    adaptive never loses on total cost, at any review price")

    args.out.mkdir(parents=True, exist_ok=True)
    payload = {
        "seeds": args.seeds,
        "per_seed": [
            {
                "seed": r.seed,
                "dataset_sha256": r.dataset_sha256,
                "verdict": r.verdict.to_json(),
                "cost_saving_vs_waterfall": round(r.cost_saving_vs_waterfall, 6),
                "agreement_gap_vs_waterfall_pp": r.agreement_gap_vs_waterfall_pp,
                "evoi_saving_vs_production": round(r.evoi_saving_vs_production, 6),
                "production_saving_vs_waterfall": round(r.production_saving_vs_waterfall, 6),
                "production_agreement_gap_pp": r.production_agreement_gap_pp,
                "tau": r.tau,
                "confidence_ece": r.confidence_ece,
                "policies": [s.to_json() for s in r.all_summaries],
            }
            for r in runs
        ],
        "stability": [s.to_json() for s in spreads],
        "verdict_counts": outcomes,
        "total_cost_table": _total_cost_table(runs),
        "break_even_human_review_usd": [round(b, 6) for b in break_evens],
    }
    path = args.out / "multi_seed.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["PolicySummary", "Spread", "main", "run_once"]
