"""Postgres persistence for `discovery_runs` / `discovery_candidates` —
migration 0037. Plain CRUD, no business logic; `arie.discovery.orchestrator`
owns the stage sequencing, this module only owns getting rows in and out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from arie.discovery.models import (
    DiscoveryCandidate,
    DiscoveryFunnel,
    DiscoveryRun,
    DiscoveryRunStatus,
    ScreeningClass,
)

__all__ = [
    "create_run",
    "get_run",
    "insert_candidates",
    "list_candidates",
    "list_runs",
    "update_candidate_promoted_lead",
    "update_candidate_screening",
    "update_run_status",
]

_INSERT_RUN = """
    INSERT INTO discovery_runs (
        organization_id, profile_version, status, requested_opportunity_count,
        market, max_candidates, created_by_user_id
    )
    VALUES (%(organization_id)s, %(profile_version)s, %(status)s, %(requested_opportunity_count)s,
            %(market)s, %(max_candidates)s, %(created_by_user_id)s)
    RETURNING run_id, organization_id, profile_version, status, requested_opportunity_count,
              market, max_candidates, created_by_user_id, error_detail, funnel,
              created_at, started_at, completed_at
"""

_SELECT_RUN = """
    SELECT run_id, organization_id, profile_version, status, requested_opportunity_count,
           market, max_candidates, created_by_user_id, error_detail, funnel,
           created_at, started_at, completed_at
    FROM discovery_runs
    WHERE run_id = %(run_id)s AND organization_id = %(organization_id)s
"""

_SELECT_RUNS = """
    SELECT run_id, organization_id, profile_version, status, requested_opportunity_count,
           market, max_candidates, created_by_user_id, error_detail, funnel,
           created_at, started_at, completed_at
    FROM discovery_runs
    WHERE organization_id = %(organization_id)s
    ORDER BY created_at DESC
    LIMIT %(limit)s
"""

_UPDATE_RUN_STATUS = """
    UPDATE discovery_runs
    SET status = %(status)s,
        funnel = %(funnel)s,
        error_detail = %(error_detail)s,
        started_at = COALESCE(started_at, CASE WHEN %(status)s != 'draft' THEN %(now)s END),
        completed_at = CASE WHEN %(status)s IN ('complete', 'failed', 'cancelled') THEN %(now)s ELSE completed_at END
    WHERE run_id = %(run_id)s AND organization_id = %(organization_id)s
"""


def _row_to_run(row: dict[str, Any]) -> DiscoveryRun:
    return DiscoveryRun(
        run_id=row["run_id"],
        organization_id=row["organization_id"],
        profile_version=row["profile_version"],
        status=DiscoveryRunStatus(row["status"]),
        requested_opportunity_count=row["requested_opportunity_count"],
        market=row["market"],
        max_candidates=row["max_candidates"],
        created_by_user_id=row["created_by_user_id"],
        error_detail=row["error_detail"],
        funnel=DiscoveryFunnel.from_dict(row["funnel"] or {}),
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def create_run(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    profile_version: int | None,
    requested_opportunity_count: int,
    market: str | None,
    max_candidates: int,
    created_by_user_id: UUID | None,
) -> DiscoveryRun:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _INSERT_RUN,
            {
                "organization_id": organization_id,
                "profile_version": profile_version,
                "status": str(DiscoveryRunStatus.DRAFT),
                "requested_opportunity_count": requested_opportunity_count,
                "market": market,
                "max_candidates": max_candidates,
                "created_by_user_id": created_by_user_id,
            },
        )
        row = cur.fetchone()
    assert row is not None
    return _row_to_run(row)


def get_run(
    conn: psycopg.Connection, *, run_id: UUID, organization_id: UUID
) -> DiscoveryRun | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_RUN, {"run_id": run_id, "organization_id": organization_id})
        row = cur.fetchone()
    return _row_to_run(row) if row is not None else None


def list_runs(
    conn: psycopg.Connection, *, organization_id: UUID, limit: int = 25
) -> list[DiscoveryRun]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_RUNS, {"organization_id": organization_id, "limit": limit})
        rows = cur.fetchall()
    return [_row_to_run(row) for row in rows]


def update_run_status(
    conn: psycopg.Connection,
    *,
    run_id: UUID,
    organization_id: UUID,
    status: DiscoveryRunStatus,
    funnel: DiscoveryFunnel,
    error_detail: str | None,
    now: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_RUN_STATUS,
            {
                "run_id": run_id,
                "organization_id": organization_id,
                "status": str(status),
                "funnel": Jsonb(funnel.as_dict()),
                "error_detail": error_detail,
                "now": now,
            },
        )


_INSERT_CANDIDATE = """
    INSERT INTO discovery_candidates (
        run_id, organization_id, company_name, domain, source_url, snippet,
        source_provider, search_query
    )
    VALUES (%(run_id)s, %(organization_id)s, %(company_name)s, %(domain)s, %(source_url)s,
            %(snippet)s, %(source_provider)s, %(search_query)s)
    ON CONFLICT (run_id, domain) DO NOTHING
    RETURNING candidate_id, run_id, organization_id, company_name, domain, source_url, snippet,
              source_provider, search_query, screening_class, screening_reason,
              promoted_lead_id, created_at
"""

_SELECT_CANDIDATES = """
    SELECT candidate_id, run_id, organization_id, company_name, domain, source_url, snippet,
           source_provider, search_query, screening_class, screening_reason,
           promoted_lead_id, created_at
    FROM discovery_candidates
    WHERE run_id = %(run_id)s AND organization_id = %(organization_id)s
    ORDER BY created_at ASC
"""

_UPDATE_CANDIDATE_SCREENING = """
    UPDATE discovery_candidates
    SET screening_class = %(screening_class)s, screening_reason = %(screening_reason)s
    WHERE candidate_id = %(candidate_id)s AND organization_id = %(organization_id)s
"""

_UPDATE_CANDIDATE_LEAD = """
    UPDATE discovery_candidates
    SET promoted_lead_id = %(lead_id)s
    WHERE candidate_id = %(candidate_id)s AND organization_id = %(organization_id)s
"""


def _row_to_candidate(row: dict[str, Any]) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        candidate_id=row["candidate_id"],
        run_id=row["run_id"],
        organization_id=row["organization_id"],
        company_name=row["company_name"],
        domain=row["domain"],
        source_url=row["source_url"],
        snippet=row["snippet"] or "",
        source_provider=row["source_provider"],
        search_query=row["search_query"],
        screening_class=ScreeningClass(row["screening_class"]) if row["screening_class"] else None,
        screening_reason=row["screening_reason"],
        promoted_lead_id=row["promoted_lead_id"],
        created_at=row["created_at"],
    )


@dataclass(frozen=True)
class NewCandidate:
    company_name: str
    domain: str
    source_url: str
    snippet: str
    source_provider: str
    search_query: str


def insert_candidates(
    conn: psycopg.Connection, *, run_id: UUID, organization_id: UUID, candidates: list[NewCandidate]
) -> list[DiscoveryCandidate]:
    """Insert deduplicated candidates. `ON CONFLICT (run_id, domain) DO
    NOTHING` is belt-and-braces — callers already dedupe before this — and
    means a retried insert never duplicates a row nor errors."""
    inserted: list[DiscoveryCandidate] = []
    with conn.cursor(row_factory=dict_row) as cur:
        for c in candidates:
            cur.execute(
                _INSERT_CANDIDATE,
                {
                    "run_id": run_id,
                    "organization_id": organization_id,
                    "company_name": c.company_name[:300],
                    "domain": c.domain,
                    "source_url": c.source_url[:2000],
                    "snippet": c.snippet[:2000],
                    "source_provider": c.source_provider,
                    "search_query": c.search_query[:200],
                },
            )
            row = cur.fetchone()
            if row is not None:
                inserted.append(_row_to_candidate(row))
    return inserted


def list_candidates(
    conn: psycopg.Connection, *, run_id: UUID, organization_id: UUID
) -> list[DiscoveryCandidate]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_CANDIDATES, {"run_id": run_id, "organization_id": organization_id})
        rows = cur.fetchall()
    return [_row_to_candidate(row) for row in rows]


def update_candidate_screening(
    conn: psycopg.Connection,
    *,
    candidate_id: UUID,
    organization_id: UUID,
    screening_class: ScreeningClass,
    screening_reason: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_CANDIDATE_SCREENING,
            {
                "candidate_id": candidate_id,
                "organization_id": organization_id,
                "screening_class": str(screening_class),
                "screening_reason": screening_reason[:500],
            },
        )


def update_candidate_promoted_lead(
    conn: psycopg.Connection, *, candidate_id: UUID, organization_id: UUID, lead_id: UUID
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_CANDIDATE_LEAD,
            {"candidate_id": candidate_id, "organization_id": organization_id, "lead_id": lead_id},
        )
