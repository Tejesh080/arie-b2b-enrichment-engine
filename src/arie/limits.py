"""Organization usage limits/quotas (Productization M4 Part 9). Sensible,
server-enforced ceilings — **not billing**: no plan tiers, no payment
integration, no distinction beyond "the configured number" on
`organizations` (`migrations/0026_organization_limits.sql`). Enforcement
reuses `arie.usage.get_usage_summary` for the calendar-month lead/spend
totals — never a second, independently-computed count that could drift
from what `GET /usage` itself reports.

**The ceiling is operational, and it gates estimated live cost.** It used to
gate `UsageSummary.total_cost_usd`, which folded the simulated catalogue's
fictional prices in with everything else — one audited organization had
"spent" $50.33 against a $50.00/month ceiling entirely on test fixtures priced
from a benchmark assumption.

It deliberately does **not** gate `actual_spend_usd`. A guard on confirmed
billed money fails open precisely when spending is least visible: a vendor that
reports no per-call charge, a free tier, and a pre-paid credit all leave actual
spend at zero while real requests keep going out. Estimated live cost prices
the *work* rather than the invoice, so it stays meaningful in all three cases.
Actual spend is financial reporting; simulated cost is evaluation reporting;
neither is a budget. See `arie.ledger.cost_basis` for the three concepts.

**Technical debt, stated.** The column behind the ceiling is still
`organizations.max_modeled_spend_usd_per_month` (migration 0026). It is
surfaced as `estimated_live_spend_limit_usd`, which is what it now means.
Renaming a column with live consumers is a separate, riskier change than
correcting what is compared against it; the API name is the one that has to be
right today, and it is.
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
    "set_limits",
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
    max_csv_rows_per_upload: int
    period_start: datetime
    period_end: datetime

    # --- the operational allowance ----------------------------------------
    #
    # Gated on estimated live cost, not on money. A guard on confirmed billed
    # money fails open exactly when spending is least visible: a vendor that
    # reports no per-call charge, a free tier, and a pre-paid credit all leave
    # `actual_spend_usd` at zero while real requests keep going out. Estimated
    # live cost prices the work rather than the invoice.
    estimated_live_spend_used_usd: float = 0.0
    """Live-service usage this period, at list/credit-equivalent prices."""
    estimated_live_spend_limit_usd: float = 0.0
    """The operational ceiling. Backed by
    `organizations.max_modeled_spend_usd_per_month`, whose column name is now
    wrong — see this module's docstring for the technical-debt note."""
    estimated_live_spend_remaining_usd: float = 0.0

    # --- the three cost concepts, never mixed ------------------------------
    actual_spend_usd: float = 0.0
    """Confirmed marginal money billed. **Financial reporting only**, never a
    guard. Zero is a real answer for a free-credit call; an unknown charge
    contributes nothing rather than being guessed at."""
    estimated_live_cost_usd: float = 0.0
    """The economic cost of using live services: real provider *and* model
    calls at list/credit-equivalent prices, including free-tier and
    credit-funded ones. Same figure as `estimated_live_spend_used_usd`, in the
    cost breakdown rather than the used/limit/remaining triple."""
    modelled_spend_usd: float = 0.0
    """Simulated catalogue and evaluation cost. Reporting only; it consumes no
    allowance of any kind."""


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


_SET_LIMITS = """
    UPDATE organizations
    SET max_leads_per_month = %(max_leads_per_month)s,
        max_csv_rows_per_upload = %(max_csv_rows_per_upload)s,
        max_modeled_spend_usd_per_month = %(max_modeled_spend_usd_per_month)s,
        updated_at = now()
    WHERE organization_id = %(organization_id)s
"""


def set_limits(
    conn: psycopg.Connection, *, organization_id: UUID, limits: OrganizationLimits
) -> None:
    """Overwrite this organization's ceilings and commit. The one write path
    onto these three columns other than the M4 defaults every organization
    row is created with — Productization M6's
    `arie.billing.plans.sync_organization_limits` calls this every time
    billing state changes, so a plan's numbers (`arie.billing.plans
    .PLAN_DEFINITIONS`) become the actual enforced ceiling without this
    module's own enforcement functions changing at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            _SET_LIMITS,
            {
                "organization_id": organization_id,
                "max_leads_per_month": limits.max_leads_per_month,
                "max_csv_rows_per_upload": limits.max_csv_rows_per_upload,
                "max_modeled_spend_usd_per_month": limits.max_modeled_spend_usd_per_month,
            },
        )
    conn.commit()


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
    spend_remaining = max(
        0.0, limits.max_modeled_spend_usd_per_month - usage.estimated_live_cost_usd
    )
    return UsageAgainstLimits(
        leads_used=usage.leads_processed,
        leads_limit=limits.max_leads_per_month,
        leads_remaining=max(0, limits.max_leads_per_month - usage.leads_processed),
        # The monetary allowance gates money, and nothing else. It used to
        # read `total_cost_usd`, which folded the simulated catalogue's
        # fictional prices in with everything else: one audited organization
        # had "spent" $50.33 against a $50.00/month ceiling entirely on test
        # fixtures priced from a benchmark assumption. Simulated work is still
        # reported — under `modelled_spend_usd` — it simply cannot exhaust a
        # real budget.
        estimated_live_spend_used_usd=usage.estimated_live_cost_usd,
        estimated_live_spend_limit_usd=limits.max_modeled_spend_usd_per_month,
        estimated_live_spend_remaining_usd=spend_remaining,
        actual_spend_usd=usage.actual_spend_usd,
        estimated_live_cost_usd=usage.estimated_live_cost_usd,
        modelled_spend_usd=usage.modelled_spend_usd,
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
