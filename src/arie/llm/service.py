"""The one path an M7 feature takes to reach a model.

Budget check, provider call, schema validation, usage ledgering — in that
order, once, here. Every later M7 feature (profile generation, CSV mapping,
lead explanation, research planning, batch summary, copilot, feedback
analysis) calls :meth:`LLMService.generate` and gets back a validated object or
a stated reason there isn't one. The alternative is seven features each
deciding for themselves whether to check a budget and whether to record a
cost, which is how a feature ships that spends money nobody can see.

**It is a seam, not an agent.** There is no loop, no tool registry, no
planning step, no decision about what to do next. One request in, one
validated value or one failure out. The model never chooses a provider, never
reads or writes application state, and never triggers a consequential action;
everything it could influence has to pass back through a caller that holds a
typed value.

**Refusal is a normal outcome, not an error.** :class:`IntelligenceResult`
always comes back. A budget-exhausted organization, an unconfigured provider,
a model outage and a response that failed validation all arrive as
``value=None`` with a reason a customer can be shown, and the deterministic
pipeline continues unchanged. Nothing in M7 may make a scored lead depend on a
model answering.

**Ordering: authorize, then call, then record.** A refused call must not reach
the provider at all — ``tests/unit/test_llm_service.py`` asserts the provider
saw zero calls — because a budget that is checked after the money is spent is
not a budget. And every billed attempt is recorded even when the overall
generation failed, for the reason ``arie.llm.deepseek.record_extraction_cost``
gives: a vendor bills for a completion whose JSON didn't match our schema just
the same, and dropping it would make the feature's true cost invisible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Generic, TypeVar
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from arie.config import INTELLIGENCE, IntelligenceConfig
from arie.ledger.pricing import model_call_cost_usd, usd
from arie.ledger.store import PostgresCostLedger
from arie.llm.budget import BudgetDecision, LLMBudgetReason, authorize_llm_call, get_llm_limits
from arie.llm.factory import build_llm_provider
from arie.llm.provider import LLMProvider, LLMPurpose, LLMUnavailableError, LLMUsage
from arie.llm.structured import (
    UNTRUSTED_DATA_RULE,
    StructuredOutcome,
    UntrustedBlock,
    generate_structured,
    render_untrusted,
    schema_json,
)
from arie.observability.tracing import get_tracer, set_attributes, traced

__all__ = ["IntelligenceResult", "LLMService"]

_TRACER = get_tracer("arie.llm.service")

T = TypeVar("T", bound=BaseModel)

_CHARS_PER_TOKEN = 4
"""Only used for the *pre-call* input-token estimate, never for billing —
billed figures come from the vendor's own ``usage`` block. Crude on purpose:
the estimate exists to be conservative, and a tokenizer dependency to sharpen
a number that is deliberately rounded up would be cost without benefit."""


@dataclass(frozen=True)
class IntelligenceResult(Generic[T]):
    """What one :meth:`LLMService.generate` produced.

    ``value`` is the only thing a caller should act on, and it is either a
    validated instance of the requested Pydantic model or ``None``. There is no
    partial state and no raw dict: a caller cannot accidentally use unvalidated
    model output because it is never handed any.
    """

    value: T | None
    reason: LLMBudgetReason
    detail: str
    """One sentence, safe to show a customer. Explains why there is no value —
    or, on success, that the call was within budget."""
    cost_usd: Decimal
    """Modelled cost of what was actually spent, summed over billed attempts.
    Zero for a refused call, because nothing was spent. Never a billed
    figure — see ``arie.ledger.pricing``."""
    usage: LLMUsage
    provider: str | None = None
    model: str | None = None
    call_ids: tuple[UUID, ...] = ()
    """``model_calls.call_id`` for every row this generation wrote, so a caller
    can cite the exact ledger rows a customer-facing cost figure came from."""
    outcome: StructuredOutcome[T] | None = None
    """The raw generation record — attempts, per-attempt errors, whether a
    repair retry happened. For Advanced Details and for tests; ``None`` when no
    call was made."""

    @property
    def succeeded(self) -> bool:
        return self.value is not None


class LLMService:
    """Budget-aware, ledgered, schema-validated access to a model.

    Construct with an explicit `provider` to pin one (tests, and any caller
    that already built one for a batch and does not want a fresh HTTP client
    per lead). Otherwise a provider is built per call from configuration and
    the organization's ``preferred_llm_model``, and closed again — correct but
    not free, so batch-shaped callers should pass one in.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        ledger: PostgresCostLedger | None = None,
        provider: LLMProvider | None = None,
        config: IntelligenceConfig | None = None,
    ) -> None:
        self._pool = pool
        self._ledger = ledger or PostgresCostLedger(pool)
        self._provider = provider
        self._config = config or INTELLIGENCE

    def generate(
        self,
        *,
        organization_id: UUID,
        purpose: LLMPurpose,
        model_type: type[T],
        instructions: str,
        now: datetime,
        untrusted: tuple[UntrustedBlock, ...] = (),
        batch_id: UUID | None = None,
        lead_id: UUID | None = None,
        idempotency_key: str | None = None,
        max_output_tokens: int | None = None,
    ) -> IntelligenceResult[T]:
        """Produce a validated `model_type`, or say why there isn't one.

        `organization_id` comes from the caller's authenticated context and is
        never read from a request body — the standing rule for every
        tenant-scoped write in this codebase. It scopes the budget, the ledger
        row, and (through RLS) what either can see.

        `instructions` is trusted text; `untrusted` is customer and
        third-party data. They are kept in separate messages by
        ``arie.llm.structured`` — do not concatenate them before calling.
        """
        with traced(
            _TRACER,
            "llm.service.generate",
            attributes={
                "arie.organization_id": str(organization_id),
                "arie.llm.purpose": str(purpose),
                "arie.llm.schema": model_type.__name__,
                "arie.batch_id": str(batch_id) if batch_id else "",
            },
        ) as span:
            try:
                provider = self._resolve_provider(organization_id)
            except LLMUnavailableError as exc:
                decision = BudgetDecision.provider_unavailable(str(exc))
                set_attributes(
                    span, {"arie.llm.allowed": False, "arie.llm.reason": str(decision.reason)}
                )
                return _refused(decision)

            owned = provider is not self._provider
            try:
                estimate = self._estimate_cost(
                    provider,
                    instructions=instructions,
                    untrusted=untrusted,
                    model_type=model_type,
                    max_output_tokens=max_output_tokens,
                )
                with self._pool.connection() as conn:
                    decision = authorize_llm_call(
                        conn,
                        organization_id=organization_id,
                        estimated_cost_usd=estimate,
                        now=now,
                        batch_id=batch_id,
                    )
                set_attributes(
                    span,
                    {
                        "arie.llm.allowed": decision.allowed,
                        "arie.llm.reason": str(decision.reason),
                        "arie.llm.estimated_cost_usd": float(estimate),
                    },
                )
                if not decision.allowed:
                    # Returns before the provider is touched. This ordering is
                    # the budget guarantee.
                    return _refused(decision)

                outcome: StructuredOutcome[T] = generate_structured(
                    provider,
                    model_type=model_type,
                    instructions=instructions,
                    untrusted=untrusted,
                    config=self._config,
                    max_output_tokens=max_output_tokens,
                )
            finally:
                if owned:
                    provider.close()

            call_ids, spent = self._record(
                outcome,
                organization_id=organization_id,
                purpose=purpose,
                batch_id=batch_id,
                lead_id=lead_id,
                idempotency_key=idempotency_key,
            )
            set_attributes(
                span,
                {
                    "arie.llm.succeeded": outcome.succeeded,
                    "arie.cost_usd": float(spent),
                    "arie.llm.ledger_rows": len(call_ids),
                },
            )
            return IntelligenceResult(
                value=outcome.value,
                reason=LLMBudgetReason.ALLOWED,
                detail=(
                    decision.detail
                    if outcome.succeeded
                    else f"the AI response could not be used ({outcome.failure})."
                ),
                cost_usd=spent,
                usage=outcome.usage,
                provider=outcome.provider,
                model=outcome.model,
                call_ids=call_ids,
                outcome=outcome,
            )

    def _resolve_provider(self, organization_id: UUID) -> LLMProvider:
        if self._provider is not None:
            return self._provider
        with self._pool.connection() as conn:
            limits = get_llm_limits(conn, organization_id=organization_id)
        return build_llm_provider(config=self._config, preferred_model=limits.preferred_llm_model)

    def _estimate_cost(
        self,
        provider: LLMProvider,
        *,
        instructions: str,
        untrusted: tuple[UntrustedBlock, ...],
        model_type: type[T],
        max_output_tokens: int | None,
    ) -> Decimal:
        """A deliberately pessimistic price for the call about to be made.

        Worst case on both sides. The input side counts every character that
        will be sent — the standing untrusted-data rule, the instructions, the
        fenced data as it will actually be truncated, and the JSON Schema the
        provider prepends. The output side assumes a full
        ``max_output_tokens``, and the whole thing is multiplied by
        ``max_attempts`` because a repair retry bills again.

        Pessimism is the point: an organization stops just before its ceiling
        rather than just after it, which is the only way "never silently
        exceed a configured budget" can hold when the true cost is not knowable
        until afterwards.
        """
        rendered, _ = render_untrusted(untrusted, max_chars=self._config.max_untrusted_chars)
        prompt_chars = (
            len(UNTRUSTED_DATA_RULE)
            + len(instructions)
            + len(rendered)
            + len(schema_json(model_type))
        )
        prompt_tokens = prompt_chars // _CHARS_PER_TOKEN + 1
        completion_tokens = max_output_tokens or self._config.max_output_tokens
        per_attempt = model_call_cost_usd(
            provider.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return per_attempt * max(1, self._config.max_attempts)

    def _record(
        self,
        outcome: StructuredOutcome[T],
        *,
        organization_id: UUID,
        purpose: LLMPurpose,
        batch_id: UUID | None,
        lead_id: UUID | None,
        idempotency_key: str | None,
    ) -> tuple[tuple[UUID, ...], Decimal]:
        """Ledger every billed attempt. Returns its call ids and total cost.

        One row per billable attempt, keyed by attempt index when the caller
        supplied an idempotency base — the same shape (and the same reason) as
        ``arie.llm.deepseek.record_extraction_cost``: a crashed-and-resumed
        caller replaying this must not double-record, and each attempt needs a
        stable distinct key for that to work.

        ``actual_cost_usd`` is never passed. DeepSeek reports token counts and
        no charge, so there is no billed figure to record, and supplying the
        modelled one would make the column a lie.
        """
        call_ids: list[UUID] = []
        spent = Decimal(0)
        for index, attempt in enumerate(outcome.attempts):
            if not attempt.billable:
                continue
            key = f"{idempotency_key}:attempt{index}" if idempotency_key else None
            write = self._ledger.record_model_call(
                model=outcome.model,
                purpose=str(purpose),
                prompt_tokens=attempt.usage.prompt_tokens,
                completion_tokens=attempt.usage.completion_tokens,
                organization_id=organization_id,
                lead_id=lead_id,
                latency_ms=attempt.latency_ms,
                idempotency_key=key,
                provider=outcome.provider,
                batch_id=batch_id,
            )
            call_ids.append(write.call_id)
            if write.recorded:
                spent += usd(write.cost_usd)
        return tuple(call_ids), spent


def _refused(decision: BudgetDecision) -> IntelligenceResult[T]:
    return IntelligenceResult(
        value=None,
        reason=decision.reason,
        detail=decision.detail,
        cost_usd=Decimal(0),
        usage=LLMUsage(),
    )
