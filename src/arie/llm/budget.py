"""May this organization spend an LLM call right now?

The question is asked *before* a provider is constructed or reached, and the
answer is a value, not an exception. That shape is the whole design: M7's
failure rule is that a budget-exhausted organization keeps getting scored
leads, decision receipts and provider evidence exactly as before — it just
stops getting model-written explanations. A guard that raised would make
"budget exhausted" indistinguishable from "something broke" at every call
site, and the natural handling of a raise (let it propagate) is the one
behaviour this must never have.

**The estimate is deliberately pessimistic.** A pre-call check cannot know the
token counts of a call that has not happened, so callers price the worst case
— the prompt they are about to send, plus a full ``max_output_tokens`` of
response — and the guard authorises against that. An organization therefore
stops slightly *before* its ceiling rather than slightly after it, which is
what "never silently exceed a configured budget" has to mean when the cost is
only knowable afterwards.

**Two scopes, counted differently, and the difference is intentional.** The
monthly ceiling counts every ``model_calls`` row the organization has, whatever
wrote it: it is the organization's total spend on models, and an exclusion
would make it a number that agrees with no invoice. The per-batch ceilings
count only rows tagged with that ``batch_id``, which is a column only M7's own
service populates — a batch budget is about the work of processing one upload,
and nothing else can be attributed to it.

Not billing. M6's entitlement system decides what an organization is *entitled*
to; this decides what it has *left*. `arie.billing.plans` may later call
:func:`set_llm_limits` to make a plan's numbers the enforced ceiling, the same
way it already calls ``arie.limits.set_limits`` — nothing here needs to change
for that, and nothing here knows what anyone pays.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

import psycopg

from arie.ledger.pricing import usd

__all__ = [
    "BudgetDecision",
    "LLMBudgetReason",
    "LLMLimits",
    "LLMSpend",
    "authorize_llm_call",
    "evaluate_budget",
    "get_llm_limits",
    "get_llm_spend",
    "set_llm_limits",
]


class LLMBudgetReason(StrEnum):
    """Why a call was allowed or refused.

    A closed vocabulary rather than a message, because these surface to a
    customer in Advanced Details ("ARIE did not write an explanation for this
    lead, and here is why") and get counted in batch insights. Free-form
    strings could not be counted, and a customer reading "budget exhausted"
    needs to know *which* budget to raise.
    """

    ALLOWED = "allowed"
    BATCH_CALL_LIMIT_REACHED = "batch_call_limit_reached"
    BATCH_COST_LIMIT_REACHED = "batch_cost_limit_reached"
    MONTHLY_COST_LIMIT_REACHED = "monthly_cost_limit_reached"
    LLM_DISABLED = "llm_disabled"
    """A ceiling configured to zero. Distinct from reaching a ceiling: nothing
    was spent, and nothing will be, until the number changes. This is how an
    organization turns the intelligence layer off without a feature flag."""
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """No provider is configured, or constructing one failed. Not a budget
    outcome at all, but callers branch on one enum and this is the shape that
    lets them."""


@dataclass(frozen=True)
class LLMLimits:
    """One organization's configured LLM ceilings."""

    max_llm_calls_per_batch: int
    max_llm_cost_usd_per_batch: Decimal
    max_llm_cost_usd_per_month: Decimal
    preferred_llm_model: str | None
    """``None`` means "use the deployment default" (``LLM_MODEL``), which is
    what every organization means today. Validated against
    ``arie.ledger.pricing.MODEL_PRICES`` by
    ``arie.llm.factory.build_llm_provider``, not by a database CHECK — see
    ``migrations/0034_llm_usage_and_budgets.sql``."""


@dataclass(frozen=True)
class LLMSpend:
    """What has already been spent, in the two scopes the ceilings cover."""

    batch_calls: int
    batch_cost_usd: Decimal
    month_cost_usd: Decimal
    """Modelled cost (``model_calls.cost_usd``), not billed cost. The ledger's
    ``actual_cost_usd`` is NULL unless a vendor stated a charge, so budgeting
    against it would budget against nothing — see ``arie.ledger.pricing``'s
    warning about which numbers here are measured and which are computed."""


@dataclass(frozen=True)
class BudgetDecision:
    """The answer, with everything a caller needs to explain or log it."""

    allowed: bool
    reason: LLMBudgetReason
    detail: str
    """One sentence, safe to show a customer. Never contains a credential, a
    prompt, or customer data — only figures already visible on their own
    settings and usage screens."""
    estimated_cost_usd: Decimal
    limits: LLMLimits | None = None
    spend: LLMSpend | None = None
    """``None`` on a :attr:`LLMBudgetReason.PROVIDER_UNAVAILABLE` decision,
    which is reached without touching the database."""

    @classmethod
    def provider_unavailable(cls, detail: str) -> BudgetDecision:
        return cls(
            allowed=False,
            reason=LLMBudgetReason.PROVIDER_UNAVAILABLE,
            detail=detail,
            estimated_cost_usd=Decimal(0),
        )


_SELECT_LLM_LIMITS = """
    SELECT max_llm_calls_per_batch, max_llm_cost_usd_per_batch,
           max_llm_cost_usd_per_month, preferred_llm_model
    FROM organizations
    WHERE organization_id = %(organization_id)s
"""

_SET_LLM_LIMITS = """
    UPDATE organizations
    SET max_llm_calls_per_batch = %(max_llm_calls_per_batch)s,
        max_llm_cost_usd_per_batch = %(max_llm_cost_usd_per_batch)s,
        max_llm_cost_usd_per_month = %(max_llm_cost_usd_per_month)s,
        preferred_llm_model = %(preferred_llm_model)s,
        updated_at = now()
    WHERE organization_id = %(organization_id)s
"""

_SELECT_BATCH_SPEND = """
    SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost
    FROM model_calls
    WHERE organization_id = %(organization_id)s
      AND batch_id = %(batch_id)s
"""

_SELECT_MONTH_SPEND = """
    SELECT COALESCE(SUM(cost_usd), 0) AS cost
    FROM model_calls
    WHERE organization_id = %(organization_id)s
      AND created_at >= %(period_start)s
      AND created_at < %(period_end)s
"""


def _calendar_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    # Duplicated from `arie.limits` rather than imported: that module's copy is
    # private to its own quota semantics, and importing a leading-underscore
    # helper across modules to save six lines couples two independent ceilings
    # that are free to disagree about what a period is later.
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def get_llm_limits(conn: psycopg.Connection, *, organization_id: UUID) -> LLMLimits:
    with conn.cursor() as cur:
        cur.execute(_SELECT_LLM_LIMITS, {"organization_id": organization_id})
        row = cur.fetchone()
    assert row is not None  # an authenticated caller's own organization always exists
    return LLMLimits(
        max_llm_calls_per_batch=row[0],
        max_llm_cost_usd_per_batch=usd(row[1]),
        max_llm_cost_usd_per_month=usd(row[2]),
        preferred_llm_model=row[3],
    )


def set_llm_limits(conn: psycopg.Connection, *, organization_id: UUID, limits: LLMLimits) -> None:
    """Overwrite this organization's LLM ceilings and commit.

    Mirrors ``arie.limits.set_limits`` exactly, including committing: these are
    settings writes, not part of a lead's transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            _SET_LLM_LIMITS,
            {
                "organization_id": organization_id,
                "max_llm_calls_per_batch": limits.max_llm_calls_per_batch,
                "max_llm_cost_usd_per_batch": limits.max_llm_cost_usd_per_batch,
                "max_llm_cost_usd_per_month": limits.max_llm_cost_usd_per_month,
                "preferred_llm_model": limits.preferred_llm_model,
            },
        )
    conn.commit()


def get_llm_spend(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    now: datetime,
    batch_id: UUID | None = None,
) -> LLMSpend:
    """Spend so far, in this calendar month and (optionally) in this batch.

    `now` is supplied by the caller rather than read here, matching
    ``arie.limits.get_usage_against_limits`` and every other place time flows
    top-down in this codebase.

    Both queries are organization-scoped in their WHERE clause even though RLS
    already isolates the table. Application-layer filtering plus RLS is this
    codebase's standing posture — the database policy is defence in depth for
    the application filter, not a replacement for it.
    """
    batch_calls = 0
    batch_cost = Decimal(0)
    if batch_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                _SELECT_BATCH_SPEND,
                {"organization_id": organization_id, "batch_id": batch_id},
            )
            row = cur.fetchone()
        assert row is not None  # COUNT/COALESCE always return exactly one row
        batch_calls = int(row[0])
        batch_cost = usd(row[1])

    period_start, period_end = _calendar_month_bounds(now)
    with conn.cursor() as cur:
        cur.execute(
            _SELECT_MONTH_SPEND,
            {
                "organization_id": organization_id,
                "period_start": period_start,
                "period_end": period_end,
            },
        )
        row = cur.fetchone()
    assert row is not None
    return LLMSpend(batch_calls=batch_calls, batch_cost_usd=batch_cost, month_cost_usd=usd(row[0]))


def authorize_llm_call(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    estimated_cost_usd: Decimal,
    now: datetime,
    batch_id: UUID | None = None,
) -> BudgetDecision:
    """Read this organization's limits and spend, then :func:`evaluate_budget`.

    The only part of this module that touches the database. Everything that
    *decides* anything lives in :func:`evaluate_budget`, which is a pure
    function — so the refusal rules are exhaustively unit-testable without a
    Postgres, and the integration test only has to prove that these two queries
    return what the pure function expects.
    """
    return evaluate_budget(
        limits=get_llm_limits(conn, organization_id=organization_id),
        spend=get_llm_spend(conn, organization_id=organization_id, now=now, batch_id=batch_id),
        estimated_cost_usd=estimated_cost_usd,
        batch_scoped=batch_id is not None,
    )


def evaluate_budget(
    *,
    limits: LLMLimits,
    spend: LLMSpend,
    estimated_cost_usd: Decimal,
    batch_scoped: bool,
) -> BudgetDecision:
    """Decide whether one LLM call of `estimated_cost_usd` may proceed.

    Never raises for a budget outcome and never mutates anything — a refused
    call must leave no trace, because it is not a failure, it is the system
    working. Checks run cheapest-consequence-first (the call ceiling, then the
    batch cost ceiling, then the monthly one) purely so the reported reason is
    the most actionable of the ones that apply; when several would refuse, the
    call is refused either way.

    A zero ceiling refuses before any spend comparison. That is the difference
    between :attr:`LLMBudgetReason.LLM_DISABLED` and a limit *reached*: one
    says the organization turned this off, the other says it ran out.

    `batch_scoped` is False for work that belongs to no CSV upload — generating
    a targeting profile, answering a copilot question. Those are bounded by the
    monthly ceiling only; charging them against "this batch" would be charging
    them against a batch that does not exist.
    """
    estimate = usd(estimated_cost_usd)

    def refuse(reason: LLMBudgetReason, detail: str) -> BudgetDecision:
        return BudgetDecision(
            allowed=False,
            reason=reason,
            detail=detail,
            estimated_cost_usd=estimate,
            limits=limits,
            spend=spend,
        )

    if (
        limits.max_llm_calls_per_batch == 0
        or limits.max_llm_cost_usd_per_batch == 0
        or limits.max_llm_cost_usd_per_month == 0
    ):
        return refuse(
            LLMBudgetReason.LLM_DISABLED,
            "AI assistance is switched off for this organization (an LLM limit is set to zero).",
        )

    if batch_scoped and spend.batch_calls + 1 > limits.max_llm_calls_per_batch:
        return refuse(
            LLMBudgetReason.BATCH_CALL_LIMIT_REACHED,
            f"this batch has already used its {limits.max_llm_calls_per_batch} AI calls.",
        )

    if batch_scoped and spend.batch_cost_usd + estimate > limits.max_llm_cost_usd_per_batch:
        return refuse(
            LLMBudgetReason.BATCH_COST_LIMIT_REACHED,
            f"this batch has reached its ${limits.max_llm_cost_usd_per_batch} AI spend limit.",
        )

    if spend.month_cost_usd + estimate > limits.max_llm_cost_usd_per_month:
        return refuse(
            LLMBudgetReason.MONTHLY_COST_LIMIT_REACHED,
            f"this organization has reached its ${limits.max_llm_cost_usd_per_month} "
            "monthly AI spend limit.",
        )

    return BudgetDecision(
        allowed=True,
        reason=LLMBudgetReason.ALLOWED,
        detail="within configured AI budgets.",
        estimated_cost_usd=estimate,
        limits=limits,
        spend=spend,
    )
