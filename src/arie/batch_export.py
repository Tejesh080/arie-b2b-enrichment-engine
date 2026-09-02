"""A batch's results, as a CSV a customer can take elsewhere. M7 Slice 7, Part I.

**No LLM, no per-row loop.** Every cell is either a raw identity field
(company, contact, email) or something `arie.recommendations
.build_recommendation` already computed deterministically for the row —
`short_reason`, never a generated explanation. Exporting 200 rows costs
nothing beyond the query that reads them.

**CSV formula injection is neutralized on every cell (Part I3).** A cell
whose text begins with ``=``, ``+``, ``-``, or ``@`` — including a company
name, since that text ultimately came from someone else's spreadsheet
upload — is prefixed with a leading apostrophe before being written, the
standard mitigation for spreadsheet software that would otherwise evaluate
it as a formula on open. Nothing here trusts a cell's origin; every cell
gets the same treatment, deterministic fields included.

**No technical receipt.** This module never reads `decision_receipts` or
`evidence` directly, and the columns below are named exactly what Part I1
asked for — no raw JWT, API key, prompt, or provider configuration is ever
in scope to leak.
"""

from __future__ import annotations

import csv
import io
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.core.types import LeadStatus
from arie.recommendations import DecisionSignal, build_recommendation

__all__ = [
    "CSV_COLUMNS",
    "batch_export_filename",
    "export_batch_rows_csv",
    "neutralize_formula_prefix",
]

CSV_COLUMNS: tuple[str, ...] = (
    "company",
    "contact",
    "email",
    "priority",
    "score",
    "confidence",
    "reason",
    "next_action",
    "research_status",
    "status",
    "profile_version",
)

_FORMULA_PREFIXES = ("=", "+", "-", "@")


def neutralize_formula_prefix(value: str) -> str:
    """OWASP's CSV-injection mitigation: a leading apostrophe stops a
    spreadsheet from ever reading the cell as a formula, and is invisible
    once opened — the text still reads correctly to a human."""
    if value and value[0] in _FORMULA_PREFIXES:
        return f"'{value}"
    return value


_SELECT_EXPORT_ROWS = """
    SELECT r.lead_id,
           c.name AS company_name,
           p.full_name AS contact_name,
           p.canonical_email AS email,
           l.status AS lead_status, l.is_shadow,
           dr.decision, dr.confidence, dr.score_value, dr.evidence_snapshot, dr.icp_profile_version
    FROM lead_batch_rows r
    JOIN leads l ON l.lead_id = r.lead_id
    LEFT JOIN companies c ON c.company_id = l.company_id
    LEFT JOIN persons p ON p.person_id = l.person_id
    LEFT JOIN decision_receipts dr ON dr.lead_id = l.lead_id
    WHERE r.batch_id = %(batch_id)s AND r.organization_id = %(organization_id)s
      AND r.lead_id IS NOT NULL
    ORDER BY r.row_number ASC
"""
"""Every accepted row of one batch, in upload order — bounded by the same
`arie.batches.MAX_ROWS` an upload was already capped to, so this is never an
unbounded scan. A rejected row (`r.lead_id IS NULL`) never resolved to a
lead and has nothing to export."""


def batch_export_filename(batch_filename: str) -> str:
    """A safe `Content-Disposition` filename derived from the batch's own
    upload name — alphanumerics, dash, underscore, dot only, so nothing in a
    customer-chosen filename can inject a header value or a path segment."""
    stem = batch_filename.rsplit(".", 1)[0] if "." in batch_filename else batch_filename
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in stem).strip("-") or "batch"
    return f"{safe}-results.csv"


def export_batch_rows_csv(
    conn: psycopg.Connection, *, organization_id: UUID, batch_id: UUID
) -> str:
    """The full CSV body, as text. Tenant-scoped by `organization_id` in the
    query itself — a batch belonging to a different organization returns no
    rows, indistinguishable at this layer from an empty batch (the caller
    already 404s on an unknown/foreign `batch_id` before reaching here)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_EXPORT_ROWS, {"batch_id": batch_id, "organization_id": organization_id})
        rows = cur.fetchall()

    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)

    for row in rows:
        snapshot = row["evidence_snapshot"] or {}
        cells: list[str]
        if row["decision"] is not None and row["score_value"] is not None:
            signal = DecisionSignal.from_decision_row(
                lead_status=LeadStatus(row["lead_status"]),
                shadow=bool(row["is_shadow"]),
                decision=row["decision"],
                confidence=float(row["confidence"]) if row["confidence"] is not None else None,
                score_value=float(row["score_value"]),
                evidence_snapshot=snapshot,
                profile_version=row["icp_profile_version"],
            )
            recommendation = build_recommendation(row["lead_id"], signal)
            cells = [
                row["company_name"] or "",
                row["contact_name"] or "",
                row["email"] or "",
                str(recommendation.priority),
                f"{recommendation.score:.1f}" if recommendation.score is not None else "",
                f"{recommendation.confidence:.2f}" if recommendation.confidence is not None else "",
                recommendation.short_reason,
                str(recommendation.next_action),
                str(recommendation.research_status),
                str(row["lead_status"]),
                str(recommendation.profile_version)
                if recommendation.profile_version is not None
                else "",
            ]
        else:
            cells = [
                row["company_name"] or "",
                row["contact_name"] or "",
                row["email"] or "",
                "",
                "",
                "",
                "ARIE is still evaluating this lead."
                if row["lead_status"] not in ("FAILED", "DEAD_LETTER")
                else "Processing failed before a decision was reached.",
                "",
                "",
                str(row["lead_status"]),
                "",
            ]
        writer.writerow([neutralize_formula_prefix(cell) for cell in cells])

    return buffer.getvalue()
