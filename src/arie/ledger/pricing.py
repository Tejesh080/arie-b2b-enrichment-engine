"""Model prices, and the token-count-to-dollars conversion.

Why this module is only about *models* and not about providers: the two report
cost in fundamentally different units. An enrichment provider tells you what it
charged — ``ProviderResult.cost_usd`` is an observation, and the ledger records
it verbatim. An LLM API tells you how many tokens it consumed and leaves the
arithmetic to you, so a model's cost has to be *derived*, and the price table it
is derived from is a configuration input that can be wrong. Keeping the derived
side separate is what makes it obvious which numbers in the cost views are
reported and which are computed.

⚠️ **Every price below is a published list price, recorded by hand. None of it is
measured.** In line with this project's rule about not presenting derived numbers
as measured ones, that is stated here rather than implied: these are the prices
the ledger will multiply token counts by, and if a price is stale or a discount
applies, every cost view is wrong by exactly that factor and nothing in the
system will notice. Step 10, which wires the first real model call, is where
these stop being assumptions — reconcile the computed cost against whatever the
API's own usage reporting returns and correct the table (see
``docs/benchmark.md``).

Decimal throughout, not float. This is money that gets summed across thousands
of rows into ``v_lead_cost``; binary floating point accumulates error in exactly
that pattern, and the column is NUMERIC on the other side anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

ModelTier = Literal["cheap", "strong"]

_PER_MILLION = Decimal(1_000_000)


class UnknownModelError(KeyError):
    """Raised when asked to price a model that isn't in the table.

    Deliberately not a $0.00 fallback. A model missing from the price table
    would otherwise be recorded as free, and "free" is indistinguishable in
    every downstream view from a genuinely cheap call — the cascade would look
    like it was saving money precisely when it was spending untracked money.
    Failing here means an unpriced model is caught the first time it is called,
    not discovered in a quarterly cost review.
    """


@dataclass(frozen=True)
class ModelPrice:
    """List price for one model, per million tokens, input and output separately."""

    model: str
    tier: ModelTier
    usd_per_1m_input_tokens: Decimal
    usd_per_1m_output_tokens: Decimal

    def cost_usd(self, *, prompt_tokens: int, completion_tokens: int) -> Decimal:
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError(
                f"token counts must be non-negative, got prompt={prompt_tokens} "
                f"completion={completion_tokens}"
            )
        return (
            Decimal(prompt_tokens) * self.usd_per_1m_input_tokens
            + Decimal(completion_tokens) * self.usd_per_1m_output_tokens
        ) / _PER_MILLION


# DeepSeek is the planned Step 10 model (it is the key this project actually
# has). Both tiers are listed so the cheap -> strong cascade that
# `v_model_escalation` measures has somewhere to record an escalation to.
MODEL_PRICES: dict[str, ModelPrice] = {
    "deepseek-chat": ModelPrice(
        model="deepseek-chat",
        tier="cheap",
        usd_per_1m_input_tokens=Decimal("0.27"),
        usd_per_1m_output_tokens=Decimal("1.10"),
    ),
    "deepseek-reasoner": ModelPrice(
        model="deepseek-reasoner",
        tier="strong",
        usd_per_1m_input_tokens=Decimal("0.55"),
        usd_per_1m_output_tokens=Decimal("2.19"),
    ),
    # The M7 fake provider (`arie.llm.fake_provider`). Zero is not a fallback
    # here, it is the correct price: `FakeLLMProvider` calls nothing and is
    # billed by nobody. `UnknownModelError` still fires for every real model
    # missing from this table, which is the property that matters — this entry
    # is a specific named exception, not a hole in the rule. Its `tier` is
    # "cheap" so it lands on the cheap side of `v_model_escalation` rather
    # than inflating the strong-model count in a test database.
    "fake-llm": ModelPrice(
        model="fake-llm",
        tier="cheap",
        usd_per_1m_input_tokens=Decimal("0"),
        usd_per_1m_output_tokens=Decimal("0"),
    ),
    # Amazon Bedrock, us-east-1, standard on-demand. Unlike every other entry
    # in this table these figures were read from the AWS Price List API
    # (`aws pricing get-products --service-code AmazonBedrock`, usagetype
    # `USE1-NovaMicro-*` / `USE1-Gemma-3-4B-IT-*`, effective 2026-09-01) rather
    # than transcribed from a pricing page — but they are still *list* prices,
    # so the warning at the top of this module applies unchanged. Batch, flex
    # and priority tiers are cheaper/dearer and are deliberately not modelled:
    # ARIE issues ordinary synchronous Converse calls only.
    #
    # Nova Micro is the Ask ARIE default (`DEFAULT_BEDROCK_MODEL`). Note the
    # shape: cheaper input than Gemma, dearer output. That favours ARIE, whose
    # copilot calls are ~1,400 input tokens against ~20 output.
    "amazon.nova-micro-v1:0": ModelPrice(
        model="amazon.nova-micro-v1:0",
        tier="cheap",
        usd_per_1m_input_tokens=Decimal("0.035"),
        usd_per_1m_output_tokens=Decimal("0.14"),
    ),
    # Retained, though not the default, so the benchmark that chose Nova over
    # it stays runnable — see `arie.llm.bedrock_provider.BEDROCK_MODELS`.
    "google.gemma-3-4b-it": ModelPrice(
        model="google.gemma-3-4b-it",
        tier="cheap",
        usd_per_1m_input_tokens=Decimal("0.04"),
        usd_per_1m_output_tokens=Decimal("0.08"),
    ),
}

GUARDRAIL_TEXT_UNIT_CHARS = 1_000
"""Characters per billable Bedrock guardrail "text unit", rounded up.

A guardrail is **not** billed by tokens, which is why it cannot be folded into
:class:`ModelPrice` and why this module grew a second pricing concept rather
than a third model entry."""

GUARDRAIL_UNIT_PRICES: dict[str, Decimal] = {
    "topic": Decimal("0.00015"),
    "content": Decimal("0.00015"),
    "sensitive_information": Decimal("0.00010"),
    "contextual_grounding": Decimal("0.00010"),
    "word": Decimal("0"),
}
"""USD per text unit, per policy, us-east-1 — from the AWS Price List API
(``USE1-Guardrail-*UnitsConsumed``, effective 2026-09-01).

``word`` is genuinely $0.00 (AWS does not charge for word policies), which is
the one place in this module a zero is a real price rather than a missing one —
the same named-exception reasoning ``fake-llm`` gets above.

These rates matter more than their size suggests. On an Ask ARIE intent
classification the guardrail costs roughly **sixteen times** the model call it
protects, so a cost model that counted only tokens would be wrong by an order
of magnitude in the direction that hides spend."""


def guardrail_cost_usd(
    *,
    topic_units: int = 0,
    content_units: int = 0,
    sensitive_information_units: int = 0,
    contextual_grounding_units: int = 0,
    word_units: int = 0,
) -> Decimal:
    """What one guardrail application cost, from its per-policy unit counts.

    Counts come from Bedrock's own ``invocationMetrics.usage`` block, so this
    is arithmetic over *reported* usage against a list price — the same
    derived-not-measured status every model cost in this module has.

    Raises ``ValueError`` on a negative count, matching
    :meth:`ModelPrice.cost_usd`: a negative unit count is a parsing bug, and
    silently pricing it as a credit would understate spend.
    """
    counts = {
        "topic": topic_units,
        "content": content_units,
        "sensitive_information": sensitive_information_units,
        "contextual_grounding": contextual_grounding_units,
        "word": word_units,
    }
    negative = {name: n for name, n in counts.items() if n < 0}
    if negative:
        raise ValueError(f"guardrail unit counts must be non-negative, got {negative}")
    return sum(
        (GUARDRAIL_UNIT_PRICES[name] * Decimal(n) for name, n in counts.items()),
        Decimal(0),
    )


def estimated_guardrail_units(chars: int) -> int:
    """Billable text units for `chars` characters of guarded content.

    Rounded **up**, and at least one unit for any non-empty content: a 12-
    character question is billed as a whole unit, and an estimate that rounded
    down would promise a budget it cannot keep.
    """
    if chars <= 0:
        return 0
    return -(-chars // GUARDRAIL_TEXT_UNIT_CHARS)


def price_for(model: str) -> ModelPrice:
    try:
        return MODEL_PRICES[model]
    except KeyError as exc:
        raise UnknownModelError(
            f"no price recorded for model {model!r} — add it to MODEL_PRICES rather than "
            "letting an unpriced call be ledgered as free"
        ) from exc


def model_call_cost_usd(model: str, *, prompt_tokens: int, completion_tokens: int) -> Decimal:
    """Cost of one model call. Raises ``UnknownModelError`` for an unpriced model."""
    return price_for(model).cost_usd(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )


def usd(amount: float | int | str | Decimal) -> Decimal:
    """Convert a reported cost to Decimal without inheriting float noise.

    Via ``str`` on purpose: ``Decimal(0.055)`` is
    ``0.05500000000000000027755575615628913510590791702270507812500``, whereas
    ``Decimal(str(0.055))`` is ``0.055`` — the number the provider actually
    meant. The simulator and every real adapter report ``cost_usd`` as a float
    (``arie.core.types.ProviderResult``), so this is the boundary where reported
    money stops being binary floating point.
    """
    return amount if isinstance(amount, Decimal) else Decimal(str(amount))
