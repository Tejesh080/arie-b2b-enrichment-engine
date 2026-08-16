"""Single-seed benchmark report.

A thin CLI over :mod:`bench.harness`, so this and the seed sweep run identical
logic. For stability and total cost including human review, use
``python -m bench.multi_seed`` — a single seed is not enough to judge a result
this close to the noise floor, as the project found the hard way.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from arie.policy.runner import PolicySummary
from bench.cost_model import HUMAN_REVIEW_PRICES, total_cost
from bench.harness import run_once


def _row(summary: PolicySummary) -> str:
    return (
        f"{summary.policy:<24} {summary.decision_agreement:>9.4f} "
        f"{summary.mean_cost_usd:>10.5f} {summary.mean_calls:>7.2f} "
        f"{summary.cache_hit_rate:>8.3f} {summary.autonomous_rate:>8.3f} "
        f"{summary.autonomous_error_rate:>9.4f} {summary.false_reject_rate:>8.4f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ARIE benchmark for one seed")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("bench/out"))
    parser.add_argument("--dataset", type=Path, default=None, help="unused; dataset is seeded")
    args = parser.parse_args()

    run = run_once(args.seed)

    print(f"dataset {run.dataset_sha256[:16]}  cal={run.n_calibration}  test={run.n_test}")
    print(
        f"confidence: method={run.confidence_method} ECE={run.confidence_ece:.4f} tau={run.tau:.4f}"
    )
    print(f"disqualifier base rate (calibration): {run.disqualifier_rate:.4f}")

    print("")
    print("waterfall tuning (calibration split):")
    for tier, tuning in run.tunings.items():
        print(
            f"  {tier:<10} gate={tuning.gate_mode}@{tuning.gate_upper_bound:<5.1f} "
            f"agreement={tuning.calibration_agreement:.4f} "
            f"cost={tuning.calibration_cost:.5f}"
        )

    header = (
        f"{'policy':<24} {'agreement':>9} {'cost/lead':>10} {'calls':>7} "
        f"{'cache':>8} {'auto':>8} {'auto_err':>9} {'false_rej':>8}"
    )
    print("")
    print(header)
    print("-" * len(header))
    print(_row(run.full))
    print(_row(run.production))
    for summary in run.waterfalls:
        print(_row(summary))
    for summary in run.adaptives:
        print(_row(summary))

    print("")
    print("counterfactual regret (reference: full enrichment):")
    for regret in run.regrets:
        print(
            f"  {regret.policy:<24} changed={regret.changed_rate:.4f} "
            f"worsened={regret.worsened_rate:.4f} improved={regret.improved} "
            f"agreement_delta={regret.net_agreement_delta:+.4f} "
            f"cost_saved={regret.cost_saved_pct:.1%}"
        )

    print("")
    print("total cost including human review ($/lead):")
    print(f"  {'review $':>9}  {'full':>9}  {'waterfall':>10}  {'production':>11}  {'evoi':>9}")
    for price in HUMAN_REVIEW_PRICES:
        print(
            f"  {price:>9.2f}  {total_cost(run.full, price).total_usd:>9.5f}  "
            f"{total_cost(run.best_waterfall, price).total_usd:>10.5f}  "
            f"{total_cost(run.production, price).total_usd:>11.5f}  "
            f"{total_cost(run.best_adaptive, price).total_usd:>9.5f}"
        )

    print("")
    print("agreement by difficulty band:")
    for summary in (run.full, run.best_waterfall, run.production, run.best_adaptive):
        bands = "  ".join(f"{k}={v:.4f}" for k, v in summary.agreement_by_band.items())
        print(f"  {summary.policy:<24} {bands}")

    print("")
    print("production stop reasons:")
    for reason, count in run.production.stop_reasons.items():
        print(f"  {reason:<24} {count}")

    print("")
    print("=" * 72)
    print(f"PRE-REGISTERED VERDICT (EVoI vs tuned waterfall): {run.verdict.outcome}")
    print("=" * 72)
    print(run.verdict.detail)
    print("")
    print(
        f"EVoI vs production policy:     {run.evoi_saving_vs_production:+.1%} cost, "
        f"{run.best_adaptive.decision_agreement - run.production.decision_agreement:+.4f} agreement"
    )
    print(
        f"production vs tuned waterfall: {run.production_saving_vs_waterfall:+.1%} cost, "
        f"{run.production_agreement_gap_pp:+.2f}pp agreement"
    )

    args.out.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset_sha256": run.dataset_sha256,
        "seed": args.seed,
        "confidence": {
            "method": run.confidence_method,
            "ece": run.confidence_ece,
            "tau": run.tau,
        },
        "policies": [s.to_json() for s in run.all_summaries],
        "regret": [r.to_json() for r in run.regrets],
        "verdict": run.verdict.to_json(),
    }
    path = args.out / "benchmark.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
