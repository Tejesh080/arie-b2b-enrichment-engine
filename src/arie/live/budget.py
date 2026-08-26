"""Live spend caps (Live V1 Foundation, Phase 6).

Two ceilings, checked **before** any real provider call:

* **per-lead** — one pathological lead cannot consume the day's budget.
* **global daily** — a stuck queue, a retry storm, or a bad deploy cannot
  consume the account.

Both are enforced *predictively*: the guard asks "would this call take me over
the cap?" and refuses if so. Checking afterwards would mean the cap is only
ever discovered by exceeding it, which for a metered API is the one moment the
check needed to have already happened.

**Where the numbers come from.** ``provider_calls`` — the same durable ledger
the Decision Receipt and every cost metric read, filtered to
``arie.live.providers.LIVE_PROVIDER_NAMES``. Reading the ledger rather than
keeping an in-process counter is what makes the cap hold across workers,
restarts, and concurrent jobs; an in-memory counter would reset on every deploy
and would be per-process, which for a horizontally-scaled worker is not a cap
at all.

The live provider names filter is not cosmetic. Simulated-mode runs write
``provider_calls`` rows with the *catalogue's* fictional prices — a benchmark
pass would otherwise blow through a real daily budget without spending a cent.

**Known limit, stated rather than papered over: this is a soft cap under
concurrency.** Two workers can both read "spent $1.99 of $2.00", both decide
one $0.002 call fits, and both make it. The overshoot is bounded by
(concurrent workers x per-call cost) — cents, at any plausible worker count —
and the alternative, a serializing lock or an advisory-locked reservation
table, buys exactness at the price of a new failure mode (a crashed worker
holding a reservation) for a guarantee nobody needs at this precision. A cap
that is occasionally $0.004 loose is doing its job; a cap that deadlocks the
queue is not.

**Failure is never silent.** A guard that cannot read the ledger raises rather
than returning "permitted" — see :meth:`LiveSpendGuard.allowance`. Failing open
on a spend cap is the one direction that costs money.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.config import LIVE_BUDGET, LiveBudgetConfig
from arie.ledger.pricing import usd
from arie.live.providers import LIVE_PROVIDER_NAMES
from arie.observability.tracing import get_tracer, set_attributes, traced

__all__ = [
    "BUDGET_STOP_REASONS",
    "DAILY_BUDGET_EXHAUSTED",
    "PER_LEAD_BUDGET_EXHAUSTED",
    "LiveSpendGuard",
    "SpendAllowance",
]

_TRACER = get_tracer("arie.live.budget")

PER_LEAD_BUDGET_EXHAUSTED = "per_lead_budget_exhausted"
DAILY_BUDGET_EXHAUSTED = "daily_budget_exhausted"

BUDGET_STOP_REASONS: frozenset[str] = frozenset({PER_LEAD_BUDGET_EXHAUSTED, DAILY_BUDGET_EXHAUSTED})
"""The stop-reason codes a budget refusal produces. Exported so the receipt's
explanation table and the tests can agree on the vocabulary without restating
the strings."""

# Spend on live providers only, today (UTC), excluding cache hits — which are
# ledgered at zero cost by `PostgresCostLedger.record_provider_call` but are
# still rows, and counting them would make a well-cached day look expensive.
#
# `date_trunc('day', now() AT TIME ZONE 'utc')` rather than a Python-computed
# boundary: the ledger's `completed_at` is written by the database's own clock,
# so the window boundary should come from the same clock. A worker with a
# skewed system time would otherwise get a different day than the rows it is
# summing.
_SELECT_DAILY_LIVE_SPEND = """
    SELECT COALESCE(SUM(cost_usd), 0) AS spent
    FROM provider_calls
    WHERE provider = ANY(%(providers)s)
      AND NOT cache_hit
      AND completed_at >= date_trunc('day', (now() AT TIME ZONE 'utc'))
"""

# Per-lead spend is NOT filtered by provider name. A lead's budget is a budget
# for that lead's whole enrichment, and in live mode every row against it is a
# live row anyway. Filtering here would also silently exempt any future
# provider someone forgot to add to LIVE_PROVIDER_NAMES — the per-lead cap is
# the backstop for exactly that mistake.
_SELECT_LEAD_SPEND = """
    SELECT COALESCE(SUM(cost_usd), 0) AS spent
    FROM provider_calls
    WHERE lead_id = %(lead_id)s
      AND NOT cache_hit
"""


@dataclass(frozen=True)
class SpendAllowance:
    """Whether one prospective provider call may proceed, and the arithmetic."""

    permitted: bool
    reason: str | None
    """``None`` when permitted; otherwise one of :data:`BUDGET_STOP_REASONS` —
    the same string that becomes the lead's ``stop_reason``."""

    estimated_cost_usd: Decimal
    lead_spent_usd: Decimal
    lead_cap_usd: Decimal
    daily_spent_usd: Decimal
    daily_cap_usd: Decimal

    def audit(self) -> dict[str, float | str | bool]:
        """Span/event-safe summary. Money as floats, deliberately: these are
        observability attributes, and the authoritative figures stay ``Decimal``
        in the ledger."""
        payload: dict[str, float | str | bool] = {
            "permitted": self.permitted,
            "estimated_cost_usd": float(self.estimated_cost_usd),
            "lead_spent_usd": float(self.lead_spent_usd),
            "lead_cap_usd": float(self.lead_cap_usd),
            "daily_spent_usd": float(self.daily_spent_usd),
            "daily_cap_usd": float(self.daily_cap_usd),
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class LiveSpendGuard:
    """Reads the durable ledger and answers "may I make this call?".

    Holds no mutable state: every :meth:`allowance` call re-reads the ledger.
    That is deliberate — a cached total is a cap that silently stops being one
    the moment a second worker exists.
    """

    def __init__(self, pool: ConnectionPool, config: LiveBudgetConfig | None = None) -> None:
        self._pool = pool
        self._config = config if config is not None else LIVE_BUDGET

    @property
    def config(self) -> LiveBudgetConfig:
        return self._config

    def daily_spent_usd(self) -> Decimal:
        return self._scalar(_SELECT_DAILY_LIVE_SPEND, {"providers": list(LIVE_PROVIDER_NAMES)})

    def lead_spent_usd(self, lead_id: UUID) -> Decimal:
        return self._scalar(_SELECT_LEAD_SPEND, {"lead_id": lead_id})

    def allowance(self, *, lead_id: UUID, estimated_cost_usd: float | Decimal) -> SpendAllowance:
        """Decide whether a call costing ``estimated_cost_usd`` may proceed.

        Per-lead is checked before daily so the refusal names the *tighter*
        constraint — a lead that has exhausted its own budget should say so,
        not blame the account's.

        Never swallows a database error: if the ledger cannot be read, the
        exception propagates and the job fails into the ordinary retry path.
        A spend cap that fails open is not a spend cap.
        """
        estimate = usd(estimated_cost_usd)

        with traced(_TRACER, "live.budget.allowance", attributes={"arie.lead_id": lead_id}) as span:
            lead_spent = self.lead_spent_usd(lead_id)
            daily_spent = self.daily_spent_usd()
            lead_cap = usd(self._config.per_lead_usd)
            daily_cap = usd(self._config.daily_usd)

            reason: str | None = None
            if lead_spent + estimate > lead_cap:
                reason = PER_LEAD_BUDGET_EXHAUSTED
            elif daily_spent + estimate > daily_cap:
                reason = DAILY_BUDGET_EXHAUSTED

            allowance = SpendAllowance(
                permitted=reason is None,
                reason=reason,
                estimated_cost_usd=estimate,
                lead_spent_usd=lead_spent,
                lead_cap_usd=lead_cap,
                daily_spent_usd=daily_spent,
                daily_cap_usd=daily_cap,
            )
            set_attributes(span, {f"arie.budget.{k}": v for k, v in allowance.audit().items()})
            return allowance

    def _scalar(self, sql: str, params: dict[str, object]) -> Decimal:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        assert row is not None  # COALESCE(SUM(...), 0) always returns one row
        return usd(row["spent"])
