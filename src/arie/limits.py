"""Organization usage limits/quotas (Productization M4 Part 9). Sensible,
server-enforced ceilings — **not billing**: no plan tiers, no payment
integration, no distinction beyond "the configured number" on
`organizations` (`migrations/0026_organization_limits.sql`). Enforcement
reuses `arie.usage.get_usage_summary` for the calendar-month lead/spend
totals — never a second, independently-computed count that could drift
from what `GET /usage` itself reports.

Modeled spend, never billed spend — the same distinction
`arie.usage.UsageSummary`/`arie.ledger.pricing` already draw. A
`max_modeled_spend_usd_per_month` ceiling bounds ARIE's own modelled-cost
arithmetic, not a real vendor invoice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

from arie.usage import get_usage_summary

__all__ = [
    "LimitExceededError",
    "OrganizationLimits",
    "UsageAgainstLimits",
    "enforce_csv_row_quota",
    "enforce_lead_quota",
    "get_limits",
    "get_usage_against_limits",
]


class LimitExceededError(Exception):
    """A configured ceiling has been reached. Carries a human-readable
    message only — callers map this to a 429/422 at the API layer, never a
    5xx (this is an expected, sensible-default outcome, not a bug)."""


@dataclass(frozen=True)
class OrganizationLimits:
    max_leads_per_month: int
    max_csv_rows_per_upload: int
    max_modeled_spend_usd_per_month: float


@dataclass(frozen=True)
class UsageAgainstLimits:
    """`GET /organization/limits`'s response shape — `used`/`limit`/
    `remaining` for each metric that has a meaningful current-usage figure.
    `max_csv_rows_per_upload` has no "used" (it bounds one upload, not a
    running total), so it appears bare."""

    leads_used: int
    leads_limit: int
    leads_remaining: int
    modeled_spend_used_usd: float
    modeled_spend_limit_usd: float
    modeled_spend_remaining_usd: float
    max_csv_rows_per_upload: int
    period_start: datetime
    period_end: datetime


_SELECT_LIMITS = """
    SELECT max_leads_per_month, max_csv_rows_per_upload, max_modeled_spend_usd_per_month
    FROM organizations
    WHERE organization_id = %(organization_id)s
"""


def get_limits(conn: psycopg.Connection, *, organization_id: UUID) -> OrganizationLimits:
    with conn.cursor() as cur:
        cur.execute(_SELECT_LIMITS, {"organization_id": organization_id})
        row = cur.fetchone()
    assert row is not None  # an authenticated caller's own organization always exists
    return OrganizationLimits(
        max_leads_per_month=row[0],
        max_csv_rows_per_upload=row[1],
        max_modeled_spend_usd_per_month=float(row[2]),
    )


def _calendar_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def get_usage_against_limits(
    conn: psycopg.Connection, *, organization_id: UUID, now: datetime
) -> UsageAgainstLimits:
    """`now` is a required parameter, not read from `datetime.now()`
    internally — this codebase treats "the current time" as something a
    caller supplies, not something a lower-level module reaches for itself
    (matches every other place time flows top-down here, e.g. `arie.icp
    _profiles.create_profile`'s `now()` living only in SQL, never Python)."""
    limits = get_limits(conn, organization_id=organization_id)
    period_start, period_end = _calendar_month_bounds(now)
    usage = get_usage_summary(
        conn, organization_id=organization_id, from_at=period_start, to_at=period_end
    )
    return UsageAgainstLimits(
        leads_used=usage.leads_processed,
        leads_limit=limits.max_leads_per_month,
        leads_remaining=max(0, limits.max_leads_per_month - usage.leads_processed),
        modeled_spend_used_usd=usage.total_cost_usd,
        modeled_spend_limit_usd=limits.max_modeled_spend_usd_per_month,
        modeled_spend_remaining_usd=max(
            0.0, limits.max_modeled_spend_usd_per_month - usage.total_cost_usd
        ),
        max_csv_rows_per_upload=limits.max_csv_rows_per_upload,
        period_start=period_start,
        period_end=period_end,
    )


def enforce_lead_quota(conn: psycopg.Connection, *, organization_id: UUID, now: datetime) -> None:
    """Raise :class:`LimitExceededError` if this organization has already
    reached its monthly lead quota. Call before accepting a request that
    would create at least one new lead (`POST /leads`, `POST /batches`) —
    a coarse "already over quota, nothing new until next month" gate rather
    than predicting whether one specific request would tip the balance.
    """
    usage = get_usage_against_limits(conn, organization_id=organization_id, now=now)
    if usage.leads_remaining <= 0:
        raise LimitExceededError(
            f"monthly lead quota reached ({usage.leads_used}/{usage.leads_limit} this period)"
        )


def enforce_csv_row_quota(
    conn: psycopg.Connection, *, organization_id: UUID, row_count: int
) -> None:
    """Raise :class:`LimitExceededError` if `row_count` exceeds this
    organization's configured per-upload ceiling. Purely a limits check —
    `arie.batches.MAX_ROWS`'s own technical hard cap is unaffected and
    unchanged; both apply, whichever is stricter.
    """
    limits = get_limits(conn, organization_id=organization_id)
    if row_count > limits.max_csv_rows_per_upload:
        raise LimitExceededError(
            f"CSV has {row_count} rows, exceeding this organization's "
            f"{limits.max_csv_rows_per_upload}-row upload limit"
        )
