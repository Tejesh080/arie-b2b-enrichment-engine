"""The durable cost ledger — every provider and model call, and what it cost.

Relationship to M0: ``arie.providers.simulated.CallLedger`` is an in-memory list
that lives for one benchmark pass. This is the persistent equivalent, writing to
``provider_calls`` and ``model_calls``, and it mirrors that class's semantics
deliberately — most visibly that **a cache hit is recorded as a zero-cost call
rather than not recorded at all**. Dropping cache hits would make cache hit rate
unmeasurable, and that metric is the entire justification for the company-level
evidence store (see ``docs/architecture.md``, "the five things most likely to be
got wrong").

**Ledger writes commit in their own transaction, on purpose.**

This is the one place in M1 where *not* joining the caller's transaction is the
correct choice, and it is worth being explicit about because the rest of the
system argues the opposite way. ``arie.jobs.worker`` deliberately commits job
completion and the lead's state transition together, so the queue can never
believe work finished while the lead sat still. The ledger is the mirror image:
if a provider call succeeds and the handler then crashes, the work transaction
rolls back — and if the ledger row rolled back with it, the retry would call the
provider again, be charged again, and the ledger would have no record that the
first charge ever happened. Money already spent is not undone by a rollback, so
the row recording it must not be either.

What makes that safe rather than merely one-sided is ``idempotency_key``: the
retry reproduces the same key, the UNIQUE constraint rejects the second insert,
and ``recorded=False`` comes back telling the caller it has already paid for
this exact call. That is what ADR 0002's "never double-charge a provider on
retry" means in practice — enforced by a database constraint, not by a
convention someone has to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.core.types import EntityType, ProviderStatus
from arie.ledger.pricing import ModelTier, model_call_cost_usd, price_for, usd
from arie.observability.tracing import get_tracer, traced

_TRACER = get_tracer("arie.ledger")

# The no-op DO UPDATE is the same idiom arie.jobs.queue and
# arie.identity.resolver use: ON CONFLICT DO NOTHING would not return the
# existing row, and the caller needs its call_id to correlate a duplicate
# against the charge it already made.
_RECORD_PROVIDER_CALL = """
    INSERT INTO provider_calls (
        lead_id, provider, entity_type, entity_id, idempotency_key,
        completed_at, latency_ms, cost_usd, status, cache_hit, raw_response_ref,
        error_kind, credits_used, cost_basis
    ) VALUES (
        %(lead_id)s, %(provider)s, %(entity_type)s, %(entity_id)s, %(idempotency_key)s,
        %(completed_at)s, %(latency_ms)s, %(cost_usd)s, %(status)s, %(cache_hit)s,
        %(raw_response_ref)s, %(error_kind)s, %(credits_used)s, %(cost_basis)s
    )
    ON CONFLICT (idempotency_key) DO UPDATE
        SET idempotency_key = provider_calls.idempotency_key
    RETURNING call_id, cost_usd, (xmax = 0) AS created
"""

_RECORD_MODEL_CALL = """
    INSERT INTO model_calls (
        lead_id, model, tier, purpose, escalated_from,
        prompt_tokens, completion_tokens, cost_usd, latency_ms, idempotency_key
    ) VALUES (
        %(lead_id)s, %(model)s, %(tier)s, %(purpose)s, %(escalated_from)s,
        %(prompt_tokens)s, %(completion_tokens)s, %(cost_usd)s, %(latency_ms)s,
        %(idempotency_key)s
    )
    ON CONFLICT (idempotency_key) DO UPDATE
        SET idempotency_key = model_calls.idempotency_key
    RETURNING call_id, cost_usd, (xmax = 0) AS created
"""

# No dedup possible without a key: NULL never conflicts under a UNIQUE index,
# so this path is a plain insert and is kept separate rather than passing NULL
# through the ON CONFLICT statement, where the DO UPDATE clause would be dead
# code that reads as if it did something.
_RECORD_MODEL_CALL_UNKEYED = """
    INSERT INTO model_calls (
        lead_id, model, tier, purpose, escalated_from,
        prompt_tokens, completion_tokens, cost_usd, latency_ms
    ) VALUES (
        %(lead_id)s, %(model)s, %(tier)s, %(purpose)s, %(escalated_from)s,
        %(prompt_tokens)s, %(completion_tokens)s, %(cost_usd)s, %(latency_ms)s
    )
    RETURNING call_id, cost_usd
"""

_SELECT_LEAD_COST = """
    SELECT lead_id, status, provider_cost_usd, model_cost_usd, total_cost_usd,
           provider_calls, cache_hits, provider_latency_ms
    FROM v_lead_cost
    WHERE lead_id = %(lead_id)s
"""


@dataclass(frozen=True)
class LedgerWrite:
    """Outcome of one ledger write."""

    call_id: UUID
    cost_usd: Decimal
    recorded: bool
    """False if this exact call was already in the ledger.

    The caller has already been charged for it; ``cost_usd`` is what the
    *original* row recorded, not what this duplicate attempt claimed. A worker
    retrying a job uses this to tell "I need to make this call" apart from "I
    already made and paid for this call and only lost the result".
    """


@dataclass(frozen=True)
class LeadCost:
    """One lead's rolled-up spend, read back from ``v_lead_cost``."""

    lead_id: UUID
    status: str
    provider_cost_usd: Decimal
    model_cost_usd: Decimal
    total_cost_usd: Decimal
    provider_calls: int
    """Billable calls: cache hits are excluded (``COUNT(*) FILTER (WHERE NOT cache_hit)``)."""
    cache_hits: int
    provider_latency_ms: int


class PostgresCostLedger:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, conninfo: str, *, min_size: int = 1, max_size: int = 10) -> PostgresCostLedger:
        pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size, open=True)
        return cls(pool)

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> PostgresCostLedger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def record_provider_call(
        self,
        *,
        idempotency_key: str,
        provider: str,
        entity_type: EntityType,
        entity_id: UUID,
        status: ProviderStatus,
        cost_usd: float | Decimal,
        latency_ms: float,
        lead_id: UUID | None = None,
        cache_hit: bool = False,
        raw_response_ref: str | None = None,
        completed_at: datetime | None = None,
        error_kind: str | None = None,
        credits_used: float | Decimal | None = None,
        cost_basis: str | None = None,
    ) -> LedgerWrite:
        """Record one provider call. Idempotent on `idempotency_key`.

        A cache hit is stored at zero cost and zero latency, matching
        ``CallLedger.record``'s treatment exactly so benchmark totals and
        production totals mean the same thing. Any `cost_usd` passed alongside
        ``cache_hit=True`` is the price the call *would* have cost and is
        deliberately not billed.

        The three provenance fields (0010) are optional and unbilled — pure
        record-keeping. `error_kind` is the adapter's stable failure vocabulary
        (the quota-cooldown guard reads `quota_exhausted`/`insufficient_credits`
        rows back out of this ledger); `credits_used` is the vendor's own
        metering unit where the vendor counts credits rather than currency; and
        `cost_basis` names what `cost_usd` *is* — a modelled credit equivalent,
        a modelled list price, or (reserved) a vendor-billed figure. A cache
        hit zeroes `credits_used` the same way it zeroes cost: nothing was
        consumed, and recording the would-have-been figure would double-count
        the original call's.
        """
        billed_cost = Decimal(0) if cache_hit else usd(cost_usd)
        billed_latency = 0 if cache_hit else int(latency_ms)

        with traced(
            _TRACER,
            "ledger.record_provider_call",
            attributes={
                "arie.provider": provider,
                "arie.lead_id": lead_id,
                "arie.cache_hit": cache_hit,
                "arie.provider_status": str(status),
            },
        ) as span:
            with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    _RECORD_PROVIDER_CALL,
                    {
                        "lead_id": lead_id,
                        "provider": provider,
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "idempotency_key": idempotency_key,
                        "completed_at": completed_at or datetime.now(UTC),
                        "latency_ms": billed_latency,
                        "cost_usd": billed_cost,
                        "status": str(status),
                        "cache_hit": cache_hit,
                        "raw_response_ref": raw_response_ref,
                        "error_kind": error_kind,
                        "credits_used": None
                        if cache_hit or credits_used is None
                        else Decimal(str(credits_used)),
                        "cost_basis": None if cache_hit else cost_basis,
                    },
                )
                row = cur.fetchone()
                assert row is not None
                conn.commit()

            write = LedgerWrite(
                call_id=row["call_id"], cost_usd=row["cost_usd"], recorded=row["created"]
            )
            span.set_attribute("arie.ledger.recorded", write.recorded)
            span.set_attribute("arie.cost_usd", float(write.cost_usd))
            return write

    def record_model_call(
        self,
        *,
        model: str,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
        lead_id: UUID | None = None,
        latency_ms: float | None = None,
        escalated_from: str | None = None,
        idempotency_key: str | None = None,
        cost_usd: float | Decimal | None = None,
    ) -> LedgerWrite:
        """Record one model call, pricing it from token counts.

        `tier` is looked up from ``MODEL_PRICES`` rather than accepted from the
        caller: it is what ``v_model_escalation`` divides by, so letting a call
        site label ``deepseek-reasoner`` as "cheap" would corrupt the one metric
        that says whether the cascade is earning its complexity.

        `cost_usd` overrides the computed price and exists for the case Step 10
        will hit — an API that reports its own charge, including discounts and
        cache-token pricing this table doesn't model. Passing it is how a
        *reported* cost gets in; omitting it derives one. Unpriced models raise
        rather than ledger as free (see ``arie.ledger.pricing``).
        """
        price = price_for(model)
        tier: ModelTier = price.tier
        resolved_cost = (
            usd(cost_usd)
            if cost_usd is not None
            else model_call_cost_usd(
                model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            )
        )

        params = {
            "lead_id": lead_id,
            "model": model,
            "tier": tier,
            "purpose": purpose,
            "escalated_from": escalated_from,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": resolved_cost,
            "latency_ms": int(latency_ms) if latency_ms is not None else None,
        }

        with traced(
            _TRACER,
            "ledger.record_model_call",
            attributes={
                "arie.model": model,
                "arie.model_tier": tier,
                "arie.lead_id": lead_id,
                "arie.purpose": purpose,
            },
        ) as span:
            with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                if idempotency_key is None:
                    cur.execute(_RECORD_MODEL_CALL_UNKEYED, params)
                else:
                    cur.execute(_RECORD_MODEL_CALL, {**params, "idempotency_key": idempotency_key})
                row = cur.fetchone()
                assert row is not None
                conn.commit()

            write = LedgerWrite(
                call_id=row["call_id"],
                cost_usd=row["cost_usd"],
                recorded=row.get("created", True),
            )
            span.set_attribute("arie.ledger.recorded", write.recorded)
            span.set_attribute("arie.cost_usd", float(write.cost_usd))
            return write

    def lead_cost(self, lead_id: UUID) -> LeadCost | None:
        """One lead's spend, read through ``v_lead_cost``.

        Reads the view rather than re-summing the tables in Python: the view is
        what the cost reporting is actually based on, so anything that reads
        around it can agree with the ledger while disagreeing with the numbers
        anyone else looks at.
        """
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT_LEAD_COST, {"lead_id": lead_id})
            row = cur.fetchone()
            if row is None:
                return None
            return LeadCost(
                lead_id=row["lead_id"],
                status=row["status"],
                provider_cost_usd=row["provider_cost_usd"],
                model_cost_usd=row["model_cost_usd"],
                total_cost_usd=row["total_cost_usd"],
                provider_calls=row["provider_calls"],
                cache_hits=row["cache_hits"],
                provider_latency_ms=row["provider_latency_ms"],
            )
