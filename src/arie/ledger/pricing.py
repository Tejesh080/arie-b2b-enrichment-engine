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
}


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
