"""Entry point: `python -m scripts.demo.cli` (invoked by `scripts/demo.ps1`).

Ensure the stack is up -> select deterministic demo leads -> run scenarios
A/B/D (C rides on A) against ARIE's public API -> render a terminal summary
and a static HTML/JSON report. Every step is bounded; nothing hangs.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from scripts.demo import stack
from scripts.demo.client import ArieClient, DemoApiError, DemoTimeoutError
from scripts.demo.corpus import DemoCorpusError, select_demo_corpus
from scripts.demo.report import DemoRun, compute_summary, render_html, render_json
from scripts.demo.scenarios import run_scenario_a, run_scenario_b, run_scenario_d

DEFAULT_OUTPUT_DIR = stack.REPO_ROOT / "demo-output"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARIE one-command Decision Receipt demo")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Wipe local Docker volumes first (docker compose down -v). Destructive.",
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--health-timeout", type=float, default=90.0, help="Seconds to wait for /healthz."
    )
    parser.add_argument(
        "--decision-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for each lead to reach a decision.",
    )
    parser.add_argument(
        "--skip-stack",
        action="store_true",
        help="Don't start Docker services — assume the stack is already running.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    client = ArieClient(base_url=args.base_url)

    try:
        if args.fresh:
            print(
                "-Fresh: removing local demo containers and volumes "
                "(docker compose down -v) — this deletes local ARIE demo data."
            )
            stack.wipe()
        if not args.skip_stack and not client.is_healthy():
            print(f"Starting required services: {', '.join(stack.REQUIRED_SERVICES)} ...")
            stack.ensure_running()
        print(f"Waiting for ARIE to become healthy at {args.base_url} ...")
        client.wait_for_health(timeout_s=args.health_timeout)
    except (stack.StackError, DemoTimeoutError) as exc:
        print(f"\nARIE is not reachable: {exc}", file=sys.stderr)
        return 1

    print("Selecting deterministic demo leads from the frozen corpus ...")
    try:
        corpus = select_demo_corpus()
    except DemoCorpusError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    run_id = uuid.uuid4().hex[:8]
    try:
        print(f"Scenario A — autonomous decision: {corpus.autonomous_lead.person.email}")
        scenario_a, idempotency = run_scenario_a(
            client, corpus, run_id=run_id, decision_timeout_s=args.decision_timeout
        )
        print(f"  -> {scenario_a.recommended_action} / autonomous={scenario_a.autonomous}")

        print(f"Scenario B — human escalation: {corpus.escalating_lead.person.email}")
        scenario_b_before, scenario_b_after = run_scenario_b(
            client, corpus, run_id=run_id, decision_timeout_s=args.decision_timeout
        )
        print(
            f"  -> recommendation={scenario_b_before.recommended_action} "
            f"(autonomous={scenario_b_before.autonomous}) -> final={scenario_b_after.final_status}"
        )

        print("Scenario D — company-level evidence reuse ...")
        scenario_d = run_scenario_d(
            client, corpus, run_id=run_id, decision_timeout_s=args.decision_timeout
        )
        if scenario_d is None:
            print("  -> no measured cache reuse this run; omitted from the report.")
    except (DemoApiError, DemoTimeoutError) as exc:
        print(f"\nDemo run failed: {exc}", file=sys.stderr)
        return 1

    run = DemoRun(
        generated_at=datetime.now(UTC).isoformat(),
        run_id=run_id,
        scenario_a=scenario_a,
        scenario_a_idempotency=idempotency,
        scenario_b_before=scenario_b_before,
        scenario_b_after=scenario_b_after,
        scenario_d=scenario_d,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "arie-demo.html"
    json_path = output_dir / "arie-demo.json"
    html_path.write_text(render_html(run), encoding="utf-8")
    json_path.write_text(render_json(run), encoding="utf-8")

    summary = compute_summary(run)
    print()
    print("=" * 60)
    print("ARIE Decision Receipt demo — summary")
    print("=" * 60)
    print(f"Leads demonstrated:    {summary.leads_demonstrated}")
    print(f"Autonomous decisions:  {summary.autonomous_decisions}")
    print(f"Human escalations:     {summary.human_escalations}")
    print(f"Human overrides:       {summary.human_overrides}")
    print(f"Fresh provider calls:  {summary.fresh_provider_calls}")
    print(f"Cache reuses:          {summary.cache_reuses}")
    print(f"Total actual spend:    ${summary.total_spend_usd}")
    print(f"Idempotent redelivery: {'OK' if idempotency.is_idempotent else 'UNEXPECTED DUPLICATE'}")
    print()
    print("Demo complete.")
    print(f"Report: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
