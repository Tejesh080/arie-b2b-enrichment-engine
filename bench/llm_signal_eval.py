"""Delta measurement: DeepSeek extraction vs. the deterministic baseline.

Analogous to `bench/multi_seed.py`, but for M1 Step 10's one narrow LLM task
rather than the M0 policies — same shape of question ("does the sophisticated
approach actually beat the simple one"), asked about a completely different,
much smaller corpus that never touches `arie.evalgen` or `data/eval/`.

**Always safe to run with no configuration.** The deterministic baseline scores
first and costs nothing. The DeepSeek side runs only if `DEEPSEEK_API_KEY` is
set (`arie.config.LLM.configured`) — otherwise this prints the baseline
report, says plainly that the live comparison was skipped, and exits 0. That
is what makes this the "optional live-API evaluation path... never required
in CI" the M1 Step 10 gate asked for: nothing about running the unit test
suite or CI needs this script to succeed, or even to attempt a network call.

A full run costs a fraction of a cent (26 short prompts against
`deepseek-chat`) and takes a few seconds — nowhere near `bench/multi_seed.py`'s
~15 minutes, so there is no reason to background it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from arie.config import LLM
from arie.llm.baseline import extract_signal_deterministic
from arie.llm.deepseek import DeepSeekSignalExtractor
from arie.llm.eval import (
    LABELED_SAMPLES,
    DeltaReport,
    EvalReport,
    compute_delta,
    evaluate_extractor,
)
from arie.llm.schema import ExtractedSignal


def _pct(accuracy: float) -> str:
    return f"{accuracy * 100:5.1f}%"


def _print_report(report: EvalReport) -> None:
    print(f"  {report.extractor_name}")
    for field in (
        report.buying_intent,
        report.trigger_category,
        report.disqualifying_signal,
        report.exact_match,
    ):
        print(f"    {field.field:<24} {field.correct:>2}/{field.total:<2}  {_pct(field.accuracy)}")
    if report.failed_extractions:
        print(f"    failed extractions: {', '.join(report.failed_extractions)}")


def _print_delta(delta: DeltaReport) -> None:
    print()
    print("  delta (challenger - baseline)")
    rows = (
        ("has_buying_intent", delta.buying_intent_delta),
        ("trigger_event_category", delta.trigger_category_delta),
        ("disqualifying_signal", delta.disqualifying_signal_delta),
        ("exact_match (strict)", delta.exact_match_delta),
    )
    for name, value in rows:
        sign = "+" if value >= 0 else ""
        print(f"    {name:<24} {sign}{value * 100:.1f}pp")


def run_llm_extractor(text: str) -> ExtractedSignal | None:
    with DeepSeekSignalExtractor() as extractor:
        outcome = extractor.extract_signal(text)
    return outcome.signal


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delta measurement: DeepSeek signal extraction vs. the deterministic baseline"
    )
    parser.add_argument("--out", type=Path, default=Path("bench/out/llm_signal_eval.json"))
    args = parser.parse_args()

    print("=" * 72)
    print("arie.llm signal extraction — delta vs. deterministic baseline")
    print("=" * 72)
    print(
        f"corpus: {len(LABELED_SAMPLES)} samples (smoke-test scale, not "
        "statistically powered — see arie.llm.eval's module docstring)"
    )
    print()

    baseline_report = evaluate_extractor("deterministic baseline", extract_signal_deterministic)
    _print_report(baseline_report)

    report_out: dict[str, object] = {
        "n_samples": len(LABELED_SAMPLES),
        "baseline": _report_to_dict(baseline_report),
    }

    if not LLM.configured:
        print()
        print("DEEPSEEK_API_KEY is not set — skipping the live comparison.")
        print("This is expected outside a manual run; nothing in CI requires this script.")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report_out, indent=2))
        print(f"\nwrote {args.out}")
        return 0

    print()
    llm_report = evaluate_extractor(LLM.model, run_llm_extractor)
    _print_report(llm_report)

    delta = compute_delta(baseline_report, llm_report)
    _print_delta(delta)

    report_out["challenger"] = _report_to_dict(llm_report)
    report_out["delta"] = {
        "has_buying_intent": delta.buying_intent_delta,
        "trigger_event_category": delta.trigger_category_delta,
        "disqualifying_signal": delta.disqualifying_signal_delta,
        "exact_match": delta.exact_match_delta,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report_out, indent=2))
    print(f"\nwrote {args.out}")
    return 0


def _report_to_dict(report: EvalReport) -> dict[str, object]:
    return {
        "extractor_name": report.extractor_name,
        "n_samples": report.n_samples,
        "has_buying_intent_accuracy": report.buying_intent.accuracy,
        "trigger_event_category_accuracy": report.trigger_category.accuracy,
        "disqualifying_signal_accuracy": report.disqualifying_signal.accuracy,
        "exact_match_accuracy": report.exact_match.accuracy,
        "failed_extractions": list(report.failed_extractions),
    }


if __name__ == "__main__":
    raise SystemExit(main())
