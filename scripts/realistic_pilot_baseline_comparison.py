"""Offline policy/economics comparison for the realistic pilot (2026-09-21).

**Makes no provider calls, touches no database.** For every real company in
the pilot's input dataset, reproduces byte-identical the same
``arie.providers.synthetic.synthesize_corpus_lead`` output the real
production pipeline generated when it processed that lead (deterministic:
seeded only from the company's canonical email/domain, see that module's
own "Determinism contract"). Runs `CalibratedBoundsPolicy` (the actual
production policy), `TunedWaterfall`, and `FullEnrichment`
(``arie.policy.baselines`` — no new policy code) over that identical
synthesized-evidence universe via the existing
``arie.policy.runner.evaluate_policy`` harness, unmodified.

This answers a policy/economics question only: given the same evidence
universe, how much does each policy spend and how often do they agree with
each other and with the oracle score computed on that same universe's
latent facts. It says nothing about real-world lead quality — see
``docs/evaluation/realistic-pilot-2026-09-21/manifest.md`` §3 for why that
question is out of scope for this script.

Usage:
    python scripts/realistic_pilot_baseline_comparison.py \
        data/evaluation/runs/realistic-pilot-2026-09-21/upload_ready.csv \
        data/evaluation/runs/realistic-pilot-2026-09-21/baseline_comparison.json
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

import psycopg

from arie.confidence.model import fit_confidence_model
from arie.evalgen.generator import generate_dataset
from arie.evalgen.schema import EvalLead
from arie.icp_profiles import resolve_scoring_config
from arie.identity.normalize import normalize_domain, normalize_email
from arie.policy.base import EvidenceCache, RunContext
from arie.policy.baselines import FullEnrichment, tune_waterfall
from arie.policy.production import CalibratedBoundsPolicy
from arie.policy.runner import PolicySummary, evaluate_policy
from arie.providers.simulated import CallLedger, build_from_leads
from arie.providers.synthetic import synthesize_corpus_lead
from arie.scoring.rules import use_scoring_config

POLICY_MODEL_SEED = 42
"""Same seed `arie.jobs.handlers.build_runtime()` uses by default in production
(`dataset_seed: int = 42`) — the confidence model fit here must be the exact
model production actually scored these leads with, not a different fit."""


def _load_leads(csv_path: Path) -> list[EvalLead]:
    leads: list[EvalLead] = []
    seen_emails: set[str] = set()
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            email = normalize_email(row["email"])
            if email in seen_emails:
                continue  # same dedup rule the real ingestion path applies
            seen_emails.add(email)
            domain = normalize_domain(row["company_domain"])
            leads.append(
                synthesize_corpus_lead(
                    canonical_email=email,
                    canonical_domain=domain,
                    full_name=(row.get("contact_name") or "").strip() or None,
                    company_name=(row.get("company_name") or "").strip() or None,
                )
            )
    return leads


def _confidence_model():
    all_leads, _manifest = generate_dataset(seed=POLICY_MODEL_SEED)
    calibration = [lead for lead in all_leads if lead.split == "calibration"]
    from arie.config import POLICY

    return (
        fit_confidence_model(calibration, target_error_rate=POLICY.target_autonomous_error_rate),
        calibration,
    )


def _summary_to_row(summary: PolicySummary) -> dict[str, Any]:
    payload = summary.to_json()
    return payload


def main() -> int:
    if len(sys.argv) != 5:
        print(
            f"usage: {sys.argv[0]} <upload_ready.csv> <output.json> "
            "<organization_id> <database_url>",
            file=sys.stderr,
        )
        return 1
    csv_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    organization_id = sys.argv[3]
    database_url = sys.argv[4]

    print(f"loading real-company leads from {csv_path} ...")
    leads = _load_leads(csv_path)
    print(f"{len(leads)} distinct leads (deduplicated by canonical email)")

    print(f"resolving organization {organization_id}'s confirmed ICP scoring_config "
          "(the exact config production scored these leads under)...")
    conn = psycopg.connect(database_url)
    try:
        scoring_config = resolve_scoring_config(conn, organization_id=organization_id)
    finally:
        conn.close()
    print(f"  qualify_threshold={scoring_config.qualify_threshold} "
          f"reject_threshold={scoring_config.reject_threshold} "
          f"industry_points={dict(scoring_config.industry_points)}")

    print("fitting the confidence model (seed=42, same as production build_runtime())...")
    model, calibration_leads = _confidence_model()

    _, registry = build_from_leads(leads)

    def make_context() -> RunContext:
        return RunContext(registry=registry, ledger=CallLedger(), cache=EvidenceCache())

    # Tuning needs its own registry/context built from the *calibration* leads'
    # own frozen observations -- an entirely different corpus from the 140 real
    # companies above. Never touches this pilot's leads, per the "never tune on
    # this evaluation dataset" instruction.
    _, calibration_registry = build_from_leads(calibration_leads)

    def make_calibration_context() -> RunContext:
        return RunContext(registry=calibration_registry, ledger=CallLedger(), cache=EvidenceCache())

    print("tuning the waterfall gate on the standard synthetic calibration split "
          "(never on this pilot's own leads)...")
    tuning = tune_waterfall(calibration_leads, model, make_calibration_context, max_tier="expensive")
    print(f"  chosen gate: mode={tuning.gate_mode} threshold={tuning.gate_upper_bound} "
          f"(calibration agreement={tuning.calibration_agreement:.4f})")

    policies = {
        "calibrated_bounds": CalibratedBoundsPolicy(model=model),
        "waterfall_expensive": tuning.build(model),
        "full_enrichment": FullEnrichment(model=model),
    }

    results: dict[str, Any] = {}
    per_lead: dict[str, dict[str, Any]] = {}
    for name, policy in policies.items():
        print(f"running {name} over {len(leads)} leads (under this org's confirmed scoring_config)...")
        with use_scoring_config(scoring_config):
            summary = evaluate_policy(policy, leads, make_context)
        results[name] = _summary_to_row(summary)
        for record in summary.records:
            per_lead.setdefault(record.eval_lead_id, {})[name] = {
                "decision": record.decision,
                "confidence": record.confidence,
                "autonomous": record.autonomous,
                "cost_usd": record.cost_usd,
                "calls_made": record.calls_made,
                "cache_hits": record.cache_hits,
                "stop_reason": record.stop_reason,
            }

    prod = results["calibrated_bounds"]
    waterfall = results["waterfall_expensive"]
    full = results["full_enrichment"]

    comparison = {
        "n_leads": len(leads),
        "waterfall_gate": {
            "max_tier": tuning.max_tier,
            "gate_mode": tuning.gate_mode,
            "gate_upper_bound": tuning.gate_upper_bound,
            "calibration_agreement": tuning.calibration_agreement,
            "calibration_cost": tuning.calibration_cost,
        },
        "spend_avoided_vs_waterfall_usd": round(
            waterfall["mean_cost_usd"] * len(leads) - prod["mean_cost_usd"] * len(leads), 6
        ),
        "spend_avoided_vs_waterfall_pct": round(
            1 - (prod["mean_cost_usd"] / waterfall["mean_cost_usd"]), 6
        )
        if waterfall["mean_cost_usd"]
        else None,
        "spend_avoided_vs_full_enrichment_pct": round(
            1 - (prod["mean_cost_usd"] / full["mean_cost_usd"]), 6
        )
        if full["mean_cost_usd"]
        else None,
        "calls_avoided_vs_waterfall_per_lead": round(
            waterfall["mean_calls"] - prod["mean_calls"], 4
        ),
        "calls_avoided_vs_full_enrichment_per_lead": round(
            full["mean_calls"] - prod["mean_calls"], 4
        ),
        "decision_agreement_prod_vs_waterfall": round(
            sum(
                1
                for lid, row in per_lead.items()
                if row["calibrated_bounds"]["decision"] == row["waterfall_expensive"]["decision"]
            )
            / len(per_lead),
            6,
        ),
        "decision_agreement_prod_vs_full": round(
            sum(
                1
                for lid, row in per_lead.items()
                if row["calibrated_bounds"]["decision"] == row["full_enrichment"]["decision"]
            )
            / len(per_lead),
            6,
        ),
        "leads_where_decision_differs_prod_vs_full": [
            lid
            for lid, row in per_lead.items()
            if row["calibrated_bounds"]["decision"] != row["full_enrichment"]["decision"]
        ],
        "leads_where_prod_stopped_before_full_cascade": sum(
            1
            for row in per_lead.values()
            if row["calibrated_bounds"]["calls_made"] < row["full_enrichment"]["calls_made"]
        ),
    }

    output = {
        "policy_summaries": results,
        "comparison": comparison,
        "per_lead": per_lead,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    print("\n=== policy summaries ===")
    for name, row in results.items():
        print(
            f"{name:22s} agreement={row['decision_agreement']:.4f} "
            f"mean_cost=${row['mean_cost_usd']:.5f} mean_calls={row['mean_calls']:.3f} "
            f"autonomous={row['autonomous_rate']:.4f} escalation={row['escalation_rate']:.4f} "
            f"cache_hit_rate={row['cache_hit_rate']:.4f}"
        )
    print("\n=== comparison ===")
    for key, value in comparison.items():
        if key != "leads_where_decision_differs_prod_vs_full":
            print(f"{key}: {value}")
    print(f"\nfull report written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
