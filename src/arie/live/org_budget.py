"""Organization-level modeled-spend enforcement (Productization M5 Part 6).

`organizations.max_modeled_spend_usd_per_month` has existed since
Productization M4 Part 9 (`migrations/0026_organization_limits.sql`), but
`arie.limits.get_usage_against_limits` only ever *reports* usage against it
(`GET /organization/limits`) — nothing enforces it, and the whole job-
processing package (`arie.jobs`) never imports `arie.limits` at all. This
module is the enforcement: a pre-call gate, checked alongside (not instead
of) the existing global `arie.live.budget.LiveSpendGuard`, scoped to one
organization's own month-to-date spend against its own configured cap.

**Relationship to `LiveSpendGuard`.** That guard is an unscoped, global
operational safety net — originally sized for ARIE's own system-credentialed
usage, and left exactly as it was for this milestone (see
`arie.live.provider_availability`'s and this module's own docstrings, and
the M5 change plan's "known limitations": a shared global daily cap and a
shared global quota cooldown are not per-tenant guarantees, and this
milestone does not change that). This module is additional, not a
replacement — every organization-credentialed live call must pass *both*
guards before it may proceed.

**Concurrency — read honestly, not oversold.** :meth:`OrganizationSpendGuard.
allowance` takes a Postgres advisory transaction lock scoped to
`(organization_id, provider)` — `pg_advisory_xact_lock`, the same primitive
`arie.provider_configs.set_provider_credential` already uses for
`(organization_id, provider)` serialization — before reading month-to-date
spend. This guarantees two concurrent *checks* for the same organization and
provider never run their SELECT concurrently against each other (no
simultaneous stale reads). It does **not** guarantee two concurrent *checks*
always disagree about headroom: `allowance` releases its lock and returns
before its caller has made the provider call or recorded its cost (see
below), so a second check can still land in the gap between a first check
succeeding and that first call's ledger row actually being committed — a
version of this was verified directly by `tests/integration/
test_org_budget_integration.py`, which found exactly this gap when hammering
`allowance()` with zero delay between check and write. Do not read the lock
as making concurrent checks mutually exclusive in effect, only in the order
their reads execute.

**Why this gap is left open rather than closed.** Closing it fully means
either holding this lock across the caller's subsequent, unbounded-latency
vendor HTTP call (trading a bounded overshoot for a new failure mode: a slow
or crashed call blocking every other job for that organization+provider —
precisely the tradeoff `arie.live.budget`'s own module docstring already
declined for the existing global cap, for the same reason), or a reserve-
then-settle ledger protocol (write a provisional charge atomically with the
check, reconcile it once the real result is known) — a genuine, larger
design change to `arie.ledger.store`'s insert/idempotency semantics that is
out of scope for this milestone. In realistic operation the gap is bounded
by *how many of the same organization's jobs are mid-flight against the same
provider at once* — each bounded below by the provider's own HTTP round-trip
time, not by zero, the way this module's own adversarial test hammers it —
which is small at any plausible worker count and exactly zero at this
milestone's one-lead-at-a-time canary scale. A future milestone that
processes real concurrent live traffic at meaningful volume should revisit
this as a reserve/settle protocol rather than trust the bound getting
tighter on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.ledger.pricing import usd

__all__ = ["ORGANIZATION_LIMIT_REACHED", "OrganizationSpendAllowance", "OrganizationSpendGuard"]

ORGANIZATION_LIMIT_REACHED = "organization_limit_reached"
"""The stop-reason code this guard's refusal produces — Part 4/16's
vocabulary, alongside `arie.live.budget.BUDGET_STOP_REASONS`."""

_LOCK_ORG_PROVIDER_SPEND = "SELECT pg_advisory_xact_lock(hashtext(%(organization_id)s::text || ':live_spend:' || %(provider)s))"

_SELECT_MONTHLY_CAP = """
    SELECT max_modeled_spend_usd_per_month FROM organizations WHERE organization_id = %(organization_id)s
"""

# Month-to-date, this organization's own rows only, excluding cache hits —
# the same "not cache_hit" exclusion `arie.live.budget`'s daily query uses,
# for the same reason: a cache hit is ledgered at zero cost and counting it
# would make a well-cached month look expensive without spending anything.
_SELECT_MONTH_TO_DATE_SPEND = """
    SELECT COALESCE(SUM(cost_usd), 0) AS spent
    FROM provider_calls
    WHERE organization_id = %(organization_id)s
      AND NOT cache_hit
      AND completed_at >= date_trunc('month', (now() AT TIME ZONE 'utc'))
"""


@dataclass(frozen=True)
class OrganizationSpendAllowance:
    """Whether one prospective provider call may proceed against
    `organization_id`'s own monthly modeled-spend cap."""

    permitted: bool
    reason: str | None
    """`None` when permitted; otherwise :data:`ORGANIZATION_LIMIT_REACHED`."""

    estimated_cost_usd: Decimal
    month_to_date_spent_usd: Decimal
    monthly_cap_usd: Decimal | None
    """`None` only if `organization_id` does not exist (defensive; a job's
    own organization always does) — treated as "no configured cap to check
    against," i.e. permitted."""

    def audit(self) -> dict[str, float | str | bool]:
        payload: dict[str, float | str | bool] = {
            "permitted": self.permitted,
            "estimated_cost_usd": float(self.estimated_cost_usd),
            "month_to_date_spent_usd": float(self.month_to_date_spent_usd),
        }
        if self.monthly_cap_usd is not None:
            payload["monthly_cap_usd"] = float(self.monthly_cap_usd)
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class OrganizationSpendGuard:
    """Reads `organizations.max_modeled_spend_usd_per_month` and this
    organization's own ledger, and answers "may I make this call?".

    Holds no mutable state, same as its three siblings in `arie.live`: every
    :meth:`allowance` call re-reads both the cap and the spend.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def allowance(
        self, *, organization_id: UUID, provider: str, estimated_cost_usd: float | Decimal
    ) -> OrganizationSpendAllowance:
        """Decide whether a call costing `estimated_cost_usd` may proceed
        against `organization_id`'s own monthly modeled-spend cap.

        Never swallows a database error: if the ledger cannot be read, the
        exception propagates and the job fails into the ordinary retry
        path — a cap that fails open is not a cap, the same rule
        `LiveSpendGuard.allowance` follows.
        """
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _LOCK_ORG_PROVIDER_SPEND,
                {"organization_id": organization_id, "provider": provider},
            )
            cur.execute(_SELECT_MONTHLY_CAP, {"organization_id": organization_id})
            cap_row = cur.fetchone()
            cur.execute(_SELECT_MONTH_TO_DATE_SPEND, {"organization_id": organization_id})
            spend_row = cur.fetchone()

        assert spend_row is not None  # COALESCE(SUM(...), 0) always returns one row
        spent = usd(spend_row["spent"])
        estimate = usd(estimated_cost_usd)

        if cap_row is None or cap_row["max_modeled_spend_usd_per_month"] is None:
            return OrganizationSpendAllowance(
                permitted=True,
                reason=None,
                estimated_cost_usd=estimate,
                month_to_date_spent_usd=spent,
                monthly_cap_usd=None,
            )

        cap = usd(cap_row["max_modeled_spend_usd_per_month"])
        permitted = spent + estimate <= cap
        return OrganizationSpendAllowance(
            permitted=permitted,
            reason=None if permitted else ORGANIZATION_LIMIT_REACHED,
            estimated_cost_usd=estimate,
            month_to_date_spent_usd=spent,
            monthly_cap_usd=cap,
        )
