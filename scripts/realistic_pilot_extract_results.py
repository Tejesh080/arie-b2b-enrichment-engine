"""Pull the realistic pilot's Track A results for the eval organization,
via the read-only `arie_mcp_readonly` role and `mcp_diag` views only (the
same boundary the MCP server itself uses) -- no base-table access, full
dataset (not capped at 50 rows the way the MCP tool is).

Usage:
    MCP_READONLY_DATABASE_URL=... python scripts/realistic_pilot_extract_results.py \
        8f9763af-796d-4798-b65a-a0867158ac92 \
        data/evaluation/runs/realistic-pilot-2026-09-21/track_a_results.json
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

import psycopg
from psycopg.rows import dict_row


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <organization_id> <output.json>", file=sys.stderr)
        return 1
    org_id = sys.argv[1]
    out_path = sys.argv[2]

    conn = psycopg.connect(os.environ["MCP_READONLY_DATABASE_URL"], row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lead_id, status, is_shadow, created_at, updated_at, "
                "receipt_decision, receipt_autonomous, receipt_confidence, "
                "receipt_score_value, receipt_score_lower, receipt_score_upper, "
                "receipt_stop_reason, latest_job_id, latest_job_status "
                "FROM mcp_diag.v_lead_recent WHERE organization_id = %(org)s "
                "ORDER BY created_at",
                {"org": org_id},
            )
            leads = cur.fetchall()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, sum(cost_usd) AS cost_usd, sum(credits_used) AS credits_used, "
                "sum(call_count) AS call_count, sum(cache_hit_count) AS cache_hit_count "
                "FROM mcp_diag.v_cost_rollup WHERE organization_id = %(org)s AND day >= '2020-01-01' "
                "GROUP BY provider ORDER BY cost_usd DESC NULLS LAST",
                {"org": org_id},
            )
            cost_rows = cur.fetchall()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, job_type, count(*) AS n FROM mcp_diag.v_failed_jobs "
                "WHERE lead_id IN (SELECT lead_id FROM mcp_diag.v_lead_recent WHERE organization_id = %(org)s) "
                "GROUP BY status, job_type",
                {"org": org_id},
            )
            failed_jobs = cur.fetchall()
    finally:
        conn.close()

    n = len(leads)
    decisions = Counter(row["receipt_decision"] for row in leads if row["receipt_decision"])
    stop_reasons = Counter(row["receipt_stop_reason"] for row in leads if row["receipt_stop_reason"])
    statuses = Counter(row["status"] for row in leads)
    autonomous_n = sum(1 for row in leads if row["receipt_autonomous"])
    with_receipt = [row for row in leads if row["receipt_decision"] is not None]
    without_receipt = [row for row in leads if row["receipt_decision"] is None]

    def _avg(field: str) -> float | None:
        vals = [float(row[field]) for row in with_receipt if row[field] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _bounds_width(row: dict) -> float | None:
        if row["receipt_score_lower"] is None or row["receipt_score_upper"] is None:
            return None
        return float(row["receipt_score_upper"]) - float(row["receipt_score_lower"])

    widths = [_bounds_width(row) for row in with_receipt]
    widths = [w for w in widths if w is not None]

    total_cost = sum(float(r["cost_usd"] or 0) for r in cost_rows)
    total_calls = sum(int(r["call_count"] or 0) for r in cost_rows)
    total_cache_hits = sum(int(r["cache_hit_count"] or 0) for r in cost_rows)

    report = {
        "organization_id": org_id,
        "n_leads": n,
        "raw_leads": [dict(row) for row in leads],
        "n_with_decision_receipt": len(with_receipt),
        "n_without_decision_receipt": len(without_receipt),
        "lead_status_distribution": dict(statuses),
        "decision_distribution": dict(decisions),
        "stop_reason_distribution": dict(stop_reasons),
        "autonomous_rate": round(autonomous_n / len(with_receipt), 4) if with_receipt else None,
        "escalation_rate": round(1 - autonomous_n / len(with_receipt), 4) if with_receipt else None,
        "mean_confidence": _avg("receipt_confidence"),
        "mean_score_value": _avg("receipt_score_value"),
        "mean_bounds_width": round(sum(widths) / len(widths), 4) if widths else None,
        "median_bounds_width": sorted(widths)[len(widths) // 2] if widths else None,
        "cost_by_evidence_source": [dict(row) for row in cost_rows],
        "total_modeled_cost_usd": round(total_cost, 6),
        "total_evidence_calls": total_calls,
        "total_cache_hits": total_cache_hits,
        "mean_cost_per_lead_usd": round(total_cost / n, 6) if n else None,
        "mean_calls_per_lead": round(total_calls / n, 4) if n else None,
        "failed_or_dead_letter_jobs": [dict(row) for row in failed_jobs],
        "leads_without_receipt_detail": [
            {"lead_id": str(row["lead_id"]), "status": row["status"], "latest_job_status": row["latest_job_status"]}
            for row in without_receipt
        ],
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(json.dumps({k: v for k, v in report.items() if k not in (
        "cost_by_evidence_source", "leads_without_receipt_detail", "failed_or_dead_letter_jobs",
        "raw_leads",
    )}, indent=2, default=str))
    print(f"\nfull report written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
