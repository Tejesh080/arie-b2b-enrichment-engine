"""Model cost arithmetic — the derived half of the cost ledger.

Provider costs are *reported* by the provider and recorded verbatim. Model costs
are *computed* from token counts, which means they can be silently wrong in ways
a reported number can't. These tests pin the arithmetic and, more importantly,
the two failure modes that would make a cost view lie without erroring:
an unpriced model recorded as free, and float noise accumulating across rows.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arie.ledger.pricing import (
    MODEL_PRICES,
    ModelPrice,
    UnknownModelError,
    model_call_cost_usd,
    price_for,
    usd,
)


def test_cost_is_input_and_output_priced_separately() -> None:
    price = ModelPrice(
        model="probe",
        tier="cheap",
        usd_per_1m_input_tokens=Decimal("1.00"),
        usd_per_1m_output_tokens=Decimal("10.00"),
    )
    # 1M input at $1 + 0.5M output at $10 = $1.00 + $5.00
    assert price.cost_usd(prompt_tokens=1_000_000, completion_tokens=500_000) == Decimal("6.00")


def test_zero_tokens_costs_nothing() -> None:
    assert model_call_cost_usd("deepseek-chat", prompt_tokens=0, completion_tokens=0) == Decimal(0)


def test_cost_is_exact_not_floating_point() -> None:
    """The reason the whole module is Decimal.

    Summed over thousands of ledger rows, binary floating point drifts in
    exactly the pattern `v_lead_cost` produces. Repeating one small charge a
    thousand times has to land on the exact figure, not near it.
    """
    one_call = model_call_cost_usd("deepseek-chat", prompt_tokens=1_000, completion_tokens=100)
    assert one_call == Decimal("0.00038")  # 1000*0.27/1e6 + 100*1.10/1e6

    total = sum((one_call for _ in range(1_000)), start=Decimal(0))
    assert total == Decimal("0.38000")


def test_unknown_model_raises_rather_than_pricing_at_zero() -> None:
    """An unpriced model must not be ledgered as free.

    Free is indistinguishable downstream from genuinely cheap, so the cascade
    would look like it was saving money precisely when it was spending money
    nobody was tracking.
    """
    with pytest.raises(UnknownModelError, match="no price recorded"):
        model_call_cost_usd("gpt-hypothetical", prompt_tokens=10, completion_tokens=10)


def test_negative_token_counts_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        model_call_cost_usd("deepseek-chat", prompt_tokens=-1, completion_tokens=0)


def test_usd_converts_reported_floats_without_binary_noise() -> None:
    """`ProviderResult.cost_usd` is a float; the ledger column is NUMERIC.

    Going through `str` is what makes $0.055 stay $0.055 instead of becoming
    0.05500000000000000027755575615628913510590791702270507812500.
    """
    assert usd(0.055) == Decimal("0.055")
    assert usd(0.1) + usd(0.2) == Decimal("0.3")
    assert usd(Decimal("1.23")) == Decimal("1.23")
    assert usd("0.6") == Decimal("0.6")


def test_every_priced_model_has_a_tier_the_schema_accepts() -> None:
    """`model_calls.tier` has a CHECK constraint; an invalid tier fails at INSERT.

    The ledger reads tier from this table rather than from the caller, so a
    typo here would be a runtime insert failure on a real call, not a lint error.
    """
    for name, price in MODEL_PRICES.items():
        assert price.model == name, "table key and ModelPrice.model must agree"
        assert price.tier in ("cheap", "strong")


def test_both_cascade_tiers_are_represented() -> None:
    """`v_model_escalation` divides strong calls by cheap ones.

    With only one tier priced, that metric could never be computed and the
    cascade could never be shown to be earning its complexity.
    """
    tiers = {price.tier for price in MODEL_PRICES.values()}
    assert tiers == {"cheap", "strong"}


def test_strong_tier_costs_more_than_cheap() -> None:
    """Not a law of nature, but if it ever inverts the cascade is pointless.

    A failure here means the price table was edited into a state where
    escalating to the strong model saves money, which would make every
    escalation decision in Step 10 backwards.
    """
    cheap = [p for p in MODEL_PRICES.values() if p.tier == "cheap"]
    strong = [p for p in MODEL_PRICES.values() if p.tier == "strong"]
    assert max(p.usd_per_1m_output_tokens for p in cheap) < min(
        p.usd_per_1m_output_tokens for p in strong
    )


def test_price_for_returns_the_registered_entry() -> None:
    assert price_for("deepseek-chat").tier == "cheap"
    assert price_for("deepseek-reasoner").tier == "strong"
