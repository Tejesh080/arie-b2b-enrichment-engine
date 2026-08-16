"""The benchmark: three strategies, one frozen test set, one verdict.

Discipline enforced here:

* The confidence model and the waterfall's gate are fitted on the **calibration**
  split only.
* Every policy sees the same frozen observations, the same cache, and the same
  lead ordering.
* The pre-registered verdict is computed mechanically from the numbers, so a
  result that falsifies the thesis reports itself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from arie.confidence.model import fit_confidence_model
from arie.config import POLICY
from arie.evalgen.generator import generate_dataset
from arie.policy.adaptive import AdaptiveVoI, estimate_disqualifier_rate
from arie.policy.base import EvidenceCache, RunContext
from arie.policy.baselines import (
    TIER_ORDER,
    BoundsOnlyStopping,
    FullEnrichment,
    tune_waterfall,
)
from arie.policy.runner import PolicySummary, evaluate_policy
from arie.providers.simulated import CallLedger, build_from_leads
from bench.metrics import counterfactual_regret, evaluate_verdict

VALUE_SCALES: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 8.0)


def _context_factory(leads: list) -> object:  # type: ignore[type-arg]
    _, registry = build_from_leads(leads)

    def make() -> RunContext:
        # Fresh ledger and cache per policy run: a warm cache carried between
        # policies would hand later ones free evidence.
        return RunContext(registry=registry, ledger=CallLedger(), cache=EvidenceCache())

    return make


def _row(summary: PolicySummary) -> str:
    return (
        f"{summary.policy:<24} {summary.decision_agreement:>9.4f} "
        f"{summary.mean_cost_usd:>10.5f} {summary.mean_calls:>7.2f} "
        f"{summary.cache_hit_rate:>8.3f} {summary.autonomous_rate:>8.3f} "
        f"{summary.autonomous_error_rate:>9.4f} {summary.false_reject_rate:>8.4f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ARIE benchmark")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("bench/out"))
    parser.add_argument("--dataset", type=Path, default=None, help="unused; dataset is seeded")
    args = parser.parse_args()

    leads, manifest = generate_dataset(seed=args.seed)
    calibration = [x for x in leads if x.split == "calibration"]
    test = [x for x in leads if x.split == "test"]

    print(
        f"dataset {manifest.content_sha256[:16]}  calibration={len(calibration)}  test={len(test)}"
    )

    model = fit_confidence_model(calibration, target_error_rate=POLICY.target_autonomous_error_rate)
    print(f"confidence: method={model.method} ECE={model.report.ece:.4f} tau={model.tau:.4f}")

    disqualifier_rate = estimate_disqualifier_rate(calibration)
    print(f"disqualifier base rate (calibration): {disqualifier_rate:.4f}")

    make_calibration_ctx = _context_factory(leads)
    make_test_ctx = _context_factory(leads)

    # --- tune the waterfall, on calibration only ----------------------------
    tunings = {
        tier: tune_waterfall(calibration, model, make_calibration_ctx, tier) for tier in TIER_ORDER
    }
    print("\nwaterfall tuning (calibration split):")
    for tier, tuning in tunings.items():
        print(
            f"  {tier:<10} gate={tuning.gate_mode}@{tuning.gate_upper_bound:<5.1f} "
            f"agreement={tuning.calibration_agreement:.4f} "
            f"cost={tuning.calibration_cost:.5f}"
        )

    # --- evaluate everything on the frozen test split -----------------------
    full = evaluate_policy(FullEnrichment(model=model), test, make_test_ctx)  # type: ignore[arg-type]

    # Ablation: every stopping signal the adaptive policy has, except EVoI
    # ordering. Isolates how much of the win is attributable to the
    # value-of-information machinery rather than to score bounds alone.
    ablation = evaluate_policy(BoundsOnlyStopping(model=model), test, make_test_ctx)  # type: ignore[arg-type]

    waterfalls = [
        evaluate_policy(tunings[tier].build(model), test, make_test_ctx)  # type: ignore[arg-type]
        for tier in TIER_ORDER
    ]

    adaptives = [
        evaluate_policy(
            AdaptiveVoI(
                model=model,
                disqualifier_rate=disqualifier_rate,
                budget_usd_cap=POLICY.lead_budget_usd_cap,
                latency_penalty_usd_per_sec=POLICY.latency_penalty_usd_per_sec,
                value_scale=scale,
            ),
            test,
            make_test_ctx,  # type: ignore[arg-type]
        )
        for scale in VALUE_SCALES
    ]

    header = (
        f"{'policy':<24} {'agreement':>9} {'cost/lead':>10} {'calls':>7} "
        f"{'cache':>8} {'auto':>8} {'auto_err':>9} {'false_rej':>8}"
    )
    print("\n" + header)
    print("-" * len(header))
    print(_row(full))
    print(_row(ablation))
    for summary in waterfalls:
        print(_row(summary))
    for summary in adaptives:
        print(_row(summary))

    # Best waterfall and best adaptive: highest agreement, cheapest on ties.
    best_key = lambda s: (s.decision_agreement, -s.mean_cost_usd)  # noqa: E731
    best_waterfall = max(waterfalls, key=best_key)
    best_adaptive = max(adaptives, key=best_key)

    regrets = [
        counterfactual_regret(full, best_waterfall),
        counterfactual_regret(full, ablation),
        counterfactual_regret(full, best_adaptive),
    ]
    print("\ncounterfactual regret (reference: full enrichment):")
    for regret in regrets:
        print(
            f"  {regret.policy:<24} changed={regret.changed_rate:.4f} "
            f"worsened={regret.worsened_rate:.4f} improved={regret.improved} "
            f"agreement_delta={regret.net_agreement_delta:+.4f} "
            f"cost_saved={regret.cost_saved_pct:.1%}"
        )

    print("\nagreement by difficulty band:")
    for summary in (full, best_waterfall, best_adaptive):
        bands = "  ".join(f"{k}={v:.4f}" for k, v in summary.agreement_by_band.items())
        print(f"  {summary.policy:<24} {bands}")

    print("\ncost by difficulty band:")
    for summary in (full, best_waterfall, best_adaptive):
        bands = "  ".join(f"{k}=${v:.5f}" for k, v in summary.cost_by_band.items())
        print(f"  {summary.policy:<24} {bands}")

    print("\nadaptive stop reasons:")
    for reason, count in best_adaptive.stop_reasons.items():
        print(f"  {reason:<24} {count}")

    verdict = evaluate_verdict(full, best_waterfall, best_adaptive)
    print("\n" + "=" * 72)
    print(f"PRE-REGISTERED VERDICT: {verdict.outcome}")
    print("=" * 72)
    print(verdict.detail)

    evoi_cost_delta = (
        (ablation.mean_cost_usd - best_adaptive.mean_cost_usd) / ablation.mean_cost_usd
        if ablation.mean_cost_usd
        else 0.0
    )
    print("")
    print(
        f"attribution: EVoI ordering saves {evoi_cost_delta:.1%} over bounds-only "
        f"stopping ({ablation.mean_calls:.2f} -> {best_adaptive.mean_calls:.2f} calls), "
        f"for {best_adaptive.decision_agreement - ablation.decision_agreement:+.4f} agreement"
    )

    args.out.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset_sha256": manifest.content_sha256,
        "seed": args.seed,
        "confidence": {
            "method": model.method,
            "ece": model.report.ece,
            "tau": model.tau,
            "threshold": model.threshold.to_json(),
        },
        "disqualifier_rate": disqualifier_rate,
        "waterfall_tuning": {
            tier: {
                "gate": t.gate_upper_bound,
                "gate_mode": t.gate_mode,
                "calibration_agreement": t.calibration_agreement,
            }
            for tier, t in tunings.items()
        },
        "policies": [s.to_json() for s in [full, ablation, *waterfalls, *adaptives]],
        "regret": [r.to_json() for r in regrets],
        "verdict": verdict.to_json(),
    }
    results_path = args.out / "benchmark.json"
    results_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
