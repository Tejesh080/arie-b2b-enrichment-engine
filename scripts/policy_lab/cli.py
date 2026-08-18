"""Entry point: `python -m scripts.policy_lab.cli` (invoked by
`scripts/policy-lab.ps1`).

Normal mode consumes the frozen `bench/out/multi_seed.json` artifact and
never touches the benchmark. `--regenerate` is the explicit, separate opt-in
to run it first — the default path must not silently re-run a ~15-minute
benchmark.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.policy_lab.artifacts import (
    DEFAULT_ARTIFACT_PATH,
    DEFAULT_MANIFEST_PATH,
    REPO_ROOT,
    ArtifactError,
    load_artifact,
    load_dataset_manifest,
    relative_to_repo,
)
from scripts.policy_lab.report import build_report, render_html, render_json

DEFAULT_OUTPUT_DIR = REPO_ROOT / "demo-output"
_BENCHMARK_TIMEOUT_S = 1800.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARIE Policy Lab — Pareto visualization of P3")
    parser.add_argument(
        "--artifact",
        type=Path,
        default=DEFAULT_ARTIFACT_PATH,
        help="Path to the multi-seed benchmark artifact (default: bench/out/multi_seed.json).",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help=(
            "Run `python -m bench.multi_seed` first to produce a fresh artifact "
            "(~15 minutes, offline). The default does NOT do this."
        ),
    )
    return parser.parse_args(argv)


def _regenerate_benchmark() -> int:
    print("Running python -m bench.multi_seed (this takes roughly 15 minutes) ...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "bench.multi_seed"],
            cwd=REPO_ROOT,
            timeout=_BENCHMARK_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(
            f"\nbench.multi_seed did not finish within {_BENCHMARK_TIMEOUT_S:.0f}s. "
            "Run it yourself:\n  python -m bench.multi_seed",
            file=sys.stderr,
        )
        return 1
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.regenerate:
        rc = _regenerate_benchmark()
        if rc != 0:
            print(f"\nbench.multi_seed exited {rc}; not generating a report.", file=sys.stderr)
            return rc

    print(f"Locating frozen benchmark artifact: {args.artifact}")
    try:
        artifact = load_artifact(args.artifact)
    except ArtifactError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    manifest = load_dataset_manifest(DEFAULT_MANIFEST_PATH)
    if manifest is None:
        print(
            f"Note: dataset manifest not found at {DEFAULT_MANIFEST_PATH} — "
            "provenance section will omit generator/rules version."
        )

    print("Computing per-policy statistics, Pareto frontier, and baseline comparison ...")
    report = build_report(
        artifact,
        manifest,
        artifact_display_path=relative_to_repo(args.artifact),
        generated_at=datetime.now(UTC).isoformat(),
    )

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "policy-lab.html"
    json_path = output_dir / "policy-lab.json"
    html_path.write_text(render_html(report), encoding="utf-8")
    json_path.write_text(render_json(report), encoding="utf-8")

    comparison = report.comparison
    print()
    print("=" * 60)
    print("ARIE Policy Lab — summary")
    print("=" * 60)
    print(
        f"Seeds:                 {min(report.seeds)}-{max(report.seeds)} ({len(report.seeds)} seeds)"
    )
    print(f"Pareto frontier:       {sorted(report.frontier.frontier)}")
    dominated = {k: v for k, v in report.frontier.dominated_by.items() if v}
    print(f"Dominated:             {dominated or 'none'}")
    print(
        f"Calibrated Bounds vs. tuned waterfall: "
        f"{comparison.cost_pct_change_mean_of_ratios:.1%} cheaper, "
        f"{comparison.agreement_pp_diff:+.2f}pp agreement"
    )
    print()
    print("Policy Lab generated.")
    print(f"Report: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
