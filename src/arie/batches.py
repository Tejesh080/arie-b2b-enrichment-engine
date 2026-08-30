"""CSV bulk lead upload — Productization M3.

Parses an uploaded CSV into individually validated rows, creates one
`lead_batches` row plus one `lead_batch_rows` row per uploaded line, and
ingests every well-formed row through the **existing, unmodified**
`arie.api.ingest.ingest_lead` — the same identity resolution, idempotency,
and job-enqueue path a single `POST /leads` call already uses. This module
adds only the CSV-to-`LeadIngestCommand` translation and the row-level audit
trail; there is no second ingestion or scoring pipeline anywhere below.

**Never evaluates a cell as a formula.** Python's `csv` module (used here)
treats every field as plain text; nothing in this module opens the file in a
spreadsheet application or interprets a leading `=`/`+`/`-`/`@` as a
formula. That protection is one-directional — it says nothing about a
*future* feature that re-exports these values into a new spreadsheet file,
which would need to neutralize such a leading character on the way out.

**Duplicate handling, explicitly:**

* The *same email* appearing twice in one file, or across two different
  uploads, resolves to the *same* `external_ref` (`f"csv:{normalized_email}"`)
  and therefore the same lead via `leads`' own existing idempotent-insert
  behaviour (`arie.api.ingest._INSERT_LEAD`'s `ON CONFLICT`) — no batch-level
  deduplication logic is needed or written here. Both rows are independently
  recorded as `accepted` in `lead_batch_rows` (each was individually
  well-formed), but only the *first* row to actually reach `ingest_lead`
  creates a new lead; the rest match it (`created=False`) and their
  `lead_batch_rows.lead_id` still names the one real lead.
* Re-uploading the identical file creates a second, independent
  `lead_batches` row (this is a new upload, honestly represented as one),
  but every one of its rows resolves to the *same* leads as the first
  upload — no duplicate leads, no duplicate jobs. `leads.batch_id` keeps
  pointing at whichever batch created the lead first (first write wins,
  same rule `is_shadow` already follows), so the second batch's own
  progress view will not show that lead as its own — an accurate, if
  slightly surprising, reflection of "these rows didn't create new work."
* `organization_id` is never read from the CSV — every ingested command
  carries the caller's own `organization_id` from `AuthContext`, identically
  to `POST /leads`.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from arie.api.ingest import LeadIngestCommand, ingest_lead
from arie.identity.normalize import normalize_domain, normalize_email
from arie.identity.resolver import IdentityResolver
from arie.jobs.queue import PostgresJobQueue
from arie.statemachine.transitions import AWAITING_REVIEW, FAILURE, QUALIFIED, REJECTED

__all__ = [
    "MAX_FILE_SIZE_BYTES",
    "MAX_ROWS",
    "SOURCE",
    "BatchProgress",
    "BatchRecord",
    "BatchRowRecord",
    "MalformedCsvError",
    "batch_progress",
    "create_batch",
    "get_batch",
    "list_batch_rows",
    "list_batches",
    "parse_csv",
]

MAX_FILE_SIZE_BYTES = 1_000_000
"""1 MB. Generous for a few hundred rows of plain text, small enough that
decoding and parsing it costs nothing worth measuring."""

MAX_ROWS = 200
"""Deliberately modest for V1: this endpoint validates and ingests rows
*synchronously*, inside one HTTP request proxied through the existing
Next.js server route (25s internal timeout, see `docs` on the frontend
proxy) — no new queue or background-upload mechanism was introduced for
this milestone. 200 rows, each costing a handful of DB round trips through
the unmodified `ingest_lead` path, comfortably fits that budget on typical
pooled-Postgres latency; raising this limit is a job for a genuinely
asynchronous upload pipeline, not a bigger number here."""

SOURCE = "csv_upload"
"""`leads.source` for every batch-ingested row, for every organization —
constant, not per-batch. This is what lets the *same* email uploaded in two
different batches (or in a batch and then again in a fresh one) share one
idempotency key with the other and resolve to the same lead; per-batch
sourcing would make every batch's rows un-deduplicatable against each
other."""

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "email address"),
    "first_name": ("first name", "firstname"),
    "last_name": ("last name", "lastname"),
    "full_name": ("full name", "fullname", "name"),
    "company_name": ("company", "company name", "organization"),
    "company_domain": ("domain", "company domain", "website"),
    "title": ("title", "job title"),
}
"""Canonical field -> accepted header spellings (already in `_normalize_header`
form: lowercased, underscores/extra whitespace collapsed to single spaces).
Deliberately small and literal, not fuzzy-matched — the brief's own "do not
over-engineer fuzzy mapping initially" — but centralised here so a new alias
is a one-line addition, not a scattered special case."""

_MAX_LENGTHS: dict[str, int] = {
    "email": 320,
    "company_domain": 253,
    "company_name": 200,
    "full_name": 200,
    "title": 200,
}
"""Mirrors `arie.api.schemas.IngestLeadRequest`'s own field length limits
exactly — a row this module accepts must also be acceptable to the ordinary
`POST /leads` path it feeds into."""


class MalformedCsvError(ValueError):
    """The file itself could not be treated as a valid upload at all — not
    valid UTF-8, no parseable header row, no recognisable email column, or
    zero data rows. Raised before any `lead_batches` row is created; a
    validation problem with *individual rows* is not this — those are
    recorded as rejected rows in an otherwise-created batch instead."""


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    raw: dict[str, str]
    validation_status: Literal["accepted", "rejected"]
    validation_error: str | None
    command: LeadIngestCommand | None
    """`None` iff `validation_status == "rejected"`."""


def _normalize_header(name: str) -> str:
    return re.sub(r"[\s_]+", " ", name.strip().lower())


def _map_columns(fieldnames: Sequence[str]) -> dict[str, str]:
    """Canonical field name -> the actual header text present in this file."""
    normalized_lookup = {_normalize_header(name): name for name in fieldnames if name}
    result: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized_lookup:
                result[canonical] = normalized_lookup[alias]
                break
    return result


def _rejected(row_number: int, raw: dict[str, str], reason: str) -> ParsedRow:
    return ParsedRow(
        row_number=row_number,
        raw=raw,
        validation_status="rejected",
        validation_error=reason,
        command=None,
    )


def _validate_row(
    row_number: int, raw: dict[str, str], field_map: dict[str, str], *, organization_id: UUID
) -> ParsedRow:
    def cell(field: str) -> str | None:
        header = field_map.get(field)
        if header is None:
            return None
        value = (raw.get(header) or "").strip()
        return value or None

    email = cell("email")
    if email is None:
        return _rejected(row_number, raw, "email is required")
    if len(email) > _MAX_LENGTHS["email"]:
        return _rejected(row_number, raw, "email exceeds 320 characters")
    try:
        normalized_email = normalize_email(email)
    except ValueError as exc:
        return _rejected(row_number, raw, f"invalid email: {exc}")

    company_domain = cell("company_domain")
    if company_domain is not None:
        if len(company_domain) > _MAX_LENGTHS["company_domain"]:
            return _rejected(row_number, raw, "company_domain exceeds 253 characters")
        try:
            normalize_domain(company_domain)
        except ValueError as exc:
            return _rejected(row_number, raw, f"invalid company_domain: {exc}")

    company_name = cell("company_name")
    if company_name is not None and len(company_name) > _MAX_LENGTHS["company_name"]:
        return _rejected(row_number, raw, "company_name exceeds 200 characters")

    joined_name = " ".join(part for part in (cell("first_name"), cell("last_name")) if part)
    full_name: str | None = cell("full_name") or joined_name or None
    if full_name is not None and len(full_name) > _MAX_LENGTHS["full_name"]:
        return _rejected(row_number, raw, "full_name exceeds 200 characters")

    title = cell("title")
    if title is not None and len(title) > _MAX_LENGTHS["title"]:
        return _rejected(row_number, raw, "title exceeds 200 characters")

    command = LeadIngestCommand(
        source=SOURCE,
        email=email,
        organization_id=organization_id,
        external_ref=f"csv:{normalized_email}",
        company_domain=company_domain,
        company_name=company_name,
        full_name=full_name,
        title=title,
    )
    return ParsedRow(
        row_number=row_number,
        raw=raw,
        validation_status="accepted",
        validation_error=None,
        command=command,
    )


def parse_csv(content: bytes, *, organization_id: UUID) -> list[ParsedRow]:
    """Parse and validate every row. Raises :class:`MalformedCsvError` for a
    file-level problem; a row-level problem is recorded on that row's
    `ParsedRow` instead of raising.
    """
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise MalformedCsvError(f"file exceeds the {MAX_FILE_SIZE_BYTES}-byte limit")

    try:
        text = content.decode("utf-8-sig")  # transparently strips a BOM if present
    except UnicodeDecodeError as exc:
        raise MalformedCsvError(f"file is not valid UTF-8: {exc}") from exc

    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames
        raw_rows = list(reader)
    except csv.Error as exc:
        raise MalformedCsvError(f"could not parse CSV: {exc}") from exc

    if not fieldnames:
        raise MalformedCsvError("file has no header row")

    field_map = _map_columns(fieldnames)
    if "email" not in field_map:
        raise MalformedCsvError("missing required column: email (or a recognised alias)")

    if not raw_rows:
        raise MalformedCsvError("file has no data rows")
    if len(raw_rows) > MAX_ROWS:
        raise MalformedCsvError(
            f"file has {len(raw_rows)} rows, exceeding the {MAX_ROWS}-row limit"
        )

    return [
        _validate_row(index, dict(raw), field_map, organization_id=organization_id)
        for index, raw in enumerate(raw_rows, start=1)
    ]


@dataclass(frozen=True)
class BatchRecord:
    batch_id: UUID
    organization_id: UUID
    filename: str
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    created_by_user_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class BatchRowRecord:
    batch_id: UUID
    row_number: int
    raw_row: dict[str, Any]
    validation_status: Literal["accepted", "rejected"]
    validation_error: str | None
    lead_id: UUID | None
    lead_status: str | None
    """Joined live from `leads.status` — `None` when `lead_id` is `None`
    (a rejected row) or, vanishingly rarely, if the lead was deleted."""


@dataclass(frozen=True)
class BatchProgress:
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    processing_count: int
    qualified_count: int
    rejected_lead_count: int
    review_count: int
    failed_count: int
    provider_cost_usd: float
    model_cost_usd: float

    @property
    def is_complete(self) -> bool:
        return self.processing_count == 0

    @property
    def total_cost_usd(self) -> float:
        return self.provider_cost_usd + self.model_cost_usd


def _row_to_batch(row: dict[str, Any]) -> BatchRecord:
    return BatchRecord(
        batch_id=row["batch_id"],
        organization_id=row["organization_id"],
        filename=row["filename"],
        total_rows=row["total_rows"],
        accepted_rows=row["accepted_rows"],
        rejected_rows=row["rejected_rows"],
        created_by_user_id=row["created_by_user_id"],
        created_at=row["created_at"],
    )


_INSERT_BATCH = """
    INSERT INTO lead_batches (
        organization_id, filename, total_rows, accepted_rows, rejected_rows, created_by_user_id
    ) VALUES (
        %(organization_id)s, %(filename)s, %(total_rows)s, %(accepted_rows)s, %(rejected_rows)s,
        %(created_by_user_id)s
    )
    RETURNING batch_id, organization_id, filename, total_rows, accepted_rows, rejected_rows,
              created_by_user_id, created_at
"""

_INSERT_BATCH_ROW = """
    INSERT INTO lead_batch_rows (
        batch_id, row_number, organization_id, raw_row, validation_status, validation_error, lead_id
    ) VALUES (
        %(batch_id)s, %(row_number)s, %(organization_id)s, %(raw_row)s, %(validation_status)s,
        %(validation_error)s, %(lead_id)s
    )
"""

_SELECT_BATCH = """
    SELECT batch_id, organization_id, filename, total_rows, accepted_rows, rejected_rows,
           created_by_user_id, created_at
    FROM lead_batches
    WHERE batch_id = %(batch_id)s AND organization_id = %(organization_id)s
"""

_SELECT_BATCHES_FOR_ORG = """
    SELECT batch_id, organization_id, filename, total_rows, accepted_rows, rejected_rows,
           created_by_user_id, created_at
    FROM lead_batches
    WHERE organization_id = %(organization_id)s
    ORDER BY created_at DESC
    LIMIT %(limit)s OFFSET %(offset)s
"""

_SELECT_BATCH_ROWS = """
    SELECT r.batch_id, r.row_number, r.raw_row, r.validation_status, r.validation_error,
           r.lead_id, l.status AS lead_status
    FROM lead_batch_rows r
    LEFT JOIN leads l ON l.lead_id = r.lead_id
    WHERE r.batch_id = %(batch_id)s AND r.organization_id = %(organization_id)s
    ORDER BY r.row_number ASC
    LIMIT %(limit)s OFFSET %(offset)s
"""

_SELECT_LEAD_STATUS_COUNTS = """
    SELECT status, count(*) AS n
    FROM leads
    WHERE batch_id = %(batch_id)s AND organization_id = %(organization_id)s
    GROUP BY status
"""

# Two independent scalar subqueries rather than one query joining both
# provider_calls and model_calls to the same lead set — a lead with N
# provider_calls and M model_calls would otherwise produce N*M joined rows
# and inflate both sums.
_SELECT_BATCH_COST = """
    SELECT
        (SELECT COALESCE(SUM(cost_usd), 0) FROM provider_calls
          WHERE cache_hit = false
            AND lead_id IN (SELECT lead_id FROM leads
                             WHERE batch_id = %(batch_id)s AND organization_id = %(organization_id)s)
        ) AS provider_cost_usd,
        (SELECT COALESCE(SUM(cost_usd), 0) FROM model_calls
          WHERE lead_id IN (SELECT lead_id FROM leads
                             WHERE batch_id = %(batch_id)s AND organization_id = %(organization_id)s)
        ) AS model_cost_usd
"""


def create_batch(
    conn: psycopg.Connection,
    *,
    resolver: IdentityResolver,
    queue: PostgresJobQueue,
    organization_id: UUID,
    created_by_user_id: UUID,
    filename: str,
    content: bytes,
) -> BatchRecord:
    """Parse, persist, and ingest one uploaded CSV. Commits per row.

    Raises :class:`MalformedCsvError` before touching the database at all
    for a file-level problem — no `lead_batches` row is created in that
    case. Once parsing succeeds, the batch row commits first (with its final
    `accepted_rows`/`rejected_rows` counts already known from validation),
    then each row is ingested and recorded with its own commit — not one
    transaction for the whole file — so a request that dies partway leaves
    every row processed so far durably persisted rather than rolling all of
    it back. See the module docstring for what a partial or repeated upload
    means for deduplication.
    """
    rows = parse_csv(content, organization_id=organization_id)
    accepted_rows = sum(1 for row in rows if row.validation_status == "accepted")
    rejected_rows = len(rows) - accepted_rows

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _INSERT_BATCH,
            {
                "organization_id": organization_id,
                "filename": filename,
                "total_rows": len(rows),
                "accepted_rows": accepted_rows,
                "rejected_rows": rejected_rows,
                "created_by_user_id": created_by_user_id,
            },
        )
        batch_row = cur.fetchone()
    assert batch_row is not None
    conn.commit()
    batch_id: UUID = batch_row["batch_id"]

    for row in rows:
        lead_id: UUID | None = None
        if row.command is not None:
            result = ingest_lead(
                conn,
                resolver=resolver,
                queue=queue,
                command=replace(row.command, batch_id=batch_id),
            )
            lead_id = result.lead_id
        with conn.cursor() as cur:
            cur.execute(
                _INSERT_BATCH_ROW,
                {
                    "batch_id": batch_id,
                    "row_number": row.row_number,
                    "organization_id": organization_id,
                    "raw_row": Jsonb(row.raw),
                    "validation_status": row.validation_status,
                    "validation_error": row.validation_error,
                    "lead_id": lead_id,
                },
            )
        conn.commit()

    return _row_to_batch(batch_row)


def get_batch(
    conn: psycopg.Connection, *, organization_id: UUID, batch_id: UUID
) -> BatchRecord | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_BATCH, {"batch_id": batch_id, "organization_id": organization_id})
        row = cur.fetchone()
    return _row_to_batch(row) if row is not None else None


def list_batches(
    conn: psycopg.Connection, *, organization_id: UUID, limit: int = 20, offset: int = 0
) -> list[BatchRecord]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _SELECT_BATCHES_FOR_ORG,
            {"organization_id": organization_id, "limit": limit, "offset": offset},
        )
        rows = cur.fetchall()
    return [_row_to_batch(row) for row in rows]


def list_batch_rows(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    batch_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[BatchRowRecord]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _SELECT_BATCH_ROWS,
            {
                "batch_id": batch_id,
                "organization_id": organization_id,
                "limit": limit,
                "offset": offset,
            },
        )
        rows = cur.fetchall()
    return [
        BatchRowRecord(
            batch_id=row["batch_id"],
            row_number=row["row_number"],
            raw_row=dict(row["raw_row"]),
            validation_status=row["validation_status"],
            validation_error=row["validation_error"],
            lead_id=row["lead_id"],
            lead_status=row["lead_status"],
        )
        for row in rows
    ]


def batch_progress(
    conn: psycopg.Connection, *, organization_id: UUID, batch: BatchRecord
) -> BatchProgress:
    """Live processing state, computed fresh from `leads`/`provider_calls`/
    `model_calls` — never from a stored counter. See the module docstring
    for why: `leads`/`jobs` are already the source of truth for processing
    state, and a cached copy on `lead_batches` would just be a second place
    for it to go stale.

    Deliberately does **not** use `batch.accepted_rows` as the denominator
    for "how many leads are outstanding": a *row* count and a *distinct
    lead* count diverge whenever a batch contains two rows for the same
    email (see the module docstring's duplicate-handling section) — both
    rows are `accepted`, but only one `leads` row is ever attributed to this
    batch. The denominator here is `sum(status_counts.values())`, the total
    number of *leads* actually attributed to this batch — always consistent
    with the same `GROUP BY` query the per-outcome counts below come from.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _SELECT_LEAD_STATUS_COUNTS,
            {"batch_id": batch.batch_id, "organization_id": organization_id},
        )
        status_counts = {row["status"]: row["n"] for row in cur.fetchall()}
        cur.execute(
            _SELECT_BATCH_COST, {"batch_id": batch.batch_id, "organization_id": organization_id}
        )
        cost_row = cur.fetchone()
    assert cost_row is not None

    total_leads = sum(status_counts.values())
    qualified = sum(n for status, n in status_counts.items() if status in QUALIFIED)
    rejected_leads = sum(n for status, n in status_counts.items() if status in REJECTED)
    review = sum(n for status, n in status_counts.items() if status in AWAITING_REVIEW)
    failed = sum(n for status, n in status_counts.items() if status in FAILURE)
    accounted = qualified + rejected_leads + review + failed
    processing = max(0, total_leads - accounted)

    return BatchProgress(
        total_rows=batch.total_rows,
        accepted_rows=batch.accepted_rows,
        rejected_rows=batch.rejected_rows,
        processing_count=processing,
        qualified_count=qualified,
        rejected_lead_count=rejected_leads,
        review_count=review,
        failed_count=failed,
        provider_cost_usd=float(cost_row["provider_cost_usd"]),
        model_cost_usd=float(cost_row["model_cost_usd"]),
    )
