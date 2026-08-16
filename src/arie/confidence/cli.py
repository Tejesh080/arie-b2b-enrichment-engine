"""Fit the confidence model and emit its calibration report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from arie.confidence.model import fit_confidence_model
from arie.config import POLICY
from arie.evalgen.generator import generate_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Fit the ARIE confidence model")
    parser.add_argument("--seed", type=int, default=42, help="dataset seed")
    parser.add_argument(
        "--target-error-rate", type=float, default=POLICY.target_autonomous_error_rate
    )
    parser.add_argument("--method", default="auto", choices=["auto", "platt", "isotonic"])
    parser.add_argument("--out", type=Path, default=Path("bench/out"))
    args = parser.parse_args()

    leads, _ = generate_dataset(seed=args.seed)
    calibration = [lead for lead in leads if lead.split == "calibration"]

    model = fit_confidence_model(
        calibration, target_error_rate=args.target_error_rate, method=args.method
    )

    print(f"calibration method : {model.method}")
    print(f"fitted on          : {len(calibration)} calibration leads (test split untouched)")
    print()
    print(model.report.reliability_table())
    print()
    threshold = model.threshold
    print(f"tau                : {threshold.tau:.4f}")
    print(f"coverage           : {threshold.coverage:.1%} of held-out states")
    print(f"observed error     : {threshold.observed_error_rate:.4f}")
    print(
        f"error upper bound  : {threshold.error_rate_upper_bound:.4f} "
        f"(target {threshold.target_error_rate}, delta {threshold.delta})"
    )
    print(f"guarantee met      : {threshold.guarantee_met}")
    print()
    print("coefficients:")
    for name, value in sorted(model.coefficients().items(), key=lambda kv: -abs(kv[1])):
        print(f"  {name:26s} {value:+.4f}")

    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / "confidence_report.json"
    report_path.write_text(
        json.dumps(
            {
                "method": model.method,
                "dataset_seed": args.seed,
                "n_calibration_leads": len(calibration),
                "calibration": model.report.to_json(),
                "threshold": model.threshold.to_json(),
                "coefficients": model.coefficients(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
