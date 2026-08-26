"""Live spend caps (Live V1 Foundation, Phase 6).

Two layers under test, both without a database:

* :class:`arie.config.LiveBudgetConfig` — the defaults, the env parsing, and
  the one configuration that is refused outright.
* :class:`arie.live.budget.LiveSpendGuard`'s decision arithmetic, driven
  through a fake pool so the ledger's *values* are controlled exactly. The
  SQL itself is exercised against a real database in
  ``tests/integration/test_live_provider_integration.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from arie.config import LiveBudgetConfig
from arie.live.budget import (
    BUDGET_STOP_REASONS,
    DAILY_BUDGET_EXHAUSTED,
    PER_LEAD_BUDGET_EXHAUSTED,
    LiveSpendGuard,
)
from arie.live.providers import LIVE_PROVIDER_NAMES, REGISTERED_LIVE_PROVIDER_NAMES
from arie.providers.apollo_contract import APOLLO_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME

# ------------------------------------------------------------------ config --


def test_defaults_are_small_enough_to_be_safe_unread() -> None:
    """A deployment that never reads the docstring must still be bounded. Both
    defaults are chosen to be far above real demo volume and far below an
    amount worth losing overnight."""
    config = LiveBudgetConfig()
    assert config.daily_usd == pytest.approx(2.00)
    assert config.per_lead_usd == pytest.approx(0.05)


def test_the_daily_default_still_affords_a_realistic_day_of_enrichment() -> None:
    """A cap so tight it blocks ordinary use would be turned off, which is
    worse than a cap sized honestly. At Abstract's estimated unit cost the
    default buys roughly a thousand lookups."""
    from arie.config import LiveProviderConfig

    per_call = LiveProviderConfig().cost_usd_per_call
    assert LiveBudgetConfig().daily_usd / per_call > 1000


def test_env_overrides_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_PROVIDER_DAILY_BUDGET_USD", "10.5")
    monkeypatch.setenv("LIVE_PROVIDER_PER_LEAD_BUDGET_USD", "0.25")
    config = LiveBudgetConfig()
    assert config.daily_usd == pytest.approx(10.5)
    assert config.per_lead_usd == pytest.approx(0.25)


def test_a_per_lead_cap_above_the_daily_cap_is_refused() -> None:
    """One lead able to consume the whole day makes the daily cap decorative.
    Refusing at construction is what turns that from a subtle operational
    surprise into a startup failure."""
    with pytest.raises(ValueError, match="exceeds"):
        LiveBudgetConfig(daily_usd=1.0, per_lead_usd=2.0)


def test_negative_caps_are_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        LiveBudgetConfig(daily_usd=-1.0, per_lead_usd=-1.0)


def test_the_caps_are_not_exposed_to_the_browser() -> None:
    """Server-side only. A public-prefixed variable would ship the exact cap to
    every visitor; ARIE's frontend lives in a separate repository and has no
    access to this process's environment either way."""
    for name in ("LIVE_PROVIDER_DAILY_BUDGET_USD", "LIVE_PROVIDER_PER_LEAD_BUDGET_USD"):
        assert not name.startswith(("NEXT_PUBLIC_", "VITE_", "PUBLIC_"))
        assert f"NEXT_PUBLIC_{name}" not in os.environ


# ---------------------------------------------------------------- the guard --


class _FakeCursor:
    def __init__(self, answers: dict[str, Decimal]) -> None:
        self._answers = answers
        self._row: dict[str, Decimal] | None = None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        key = "lead" if "lead_id" in params else "daily"
        self._row = {"spent": self._answers[key]}

    def fetchone(self) -> dict[str, Decimal] | None:
        return self._row

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, answers: dict[str, Decimal]) -> None:
        self._answers = answers

    def cursor(self, **_kwargs: Any) -> _FakeCursor:
        return _FakeCursor(self._answers)

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakePool:
    """Stands in for a ConnectionPool. Only `connection()` is ever used."""

    def __init__(self, *, lead: float, daily: float) -> None:
        self._answers = {"lead": Decimal(str(lead)), "daily": Decimal(str(daily))}

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        yield _FakeConnection(self._answers)


def _guard(*, lead: float, daily: float, per_lead: float, daily_cap: float) -> LiveSpendGuard:
    return LiveSpendGuard(
        _FakePool(lead=lead, daily=daily),  # type: ignore[arg-type]
        LiveBudgetConfig(daily_usd=daily_cap, per_lead_usd=per_lead),
    )


def test_a_call_within_both_caps_is_permitted() -> None:
    guard = _guard(lead=0.0, daily=0.0, per_lead=0.05, daily_cap=2.0)
    allowance = guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.00165)
    assert allowance.permitted
    assert allowance.reason is None


def test_the_check_is_predictive_not_retrospective() -> None:
    """The cap is "would this call take me over", not "has this call taken me
    over". For a metered API the second is discovered by exceeding it."""
    guard = _guard(lead=0.049, daily=0.0, per_lead=0.05, daily_cap=2.0)
    # 0.049 is under the cap; 0.049 + 0.002 is not.
    assert not guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.002).permitted
    assert guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.001).permitted


def test_a_call_landing_exactly_on_the_cap_is_permitted() -> None:
    """Boundary stated explicitly: the cap is a ceiling to reach, not to stay
    under. Spending exactly the budget is spending the budget."""
    guard = _guard(lead=0.048, daily=0.0, per_lead=0.05, daily_cap=2.0)
    assert guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.002).permitted


def test_per_lead_exhaustion_is_reported_as_such() -> None:
    guard = _guard(lead=0.05, daily=0.0, per_lead=0.05, daily_cap=2.0)
    allowance = guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.002)
    assert not allowance.permitted
    assert allowance.reason == PER_LEAD_BUDGET_EXHAUSTED


def test_daily_exhaustion_is_reported_as_such() -> None:
    guard = _guard(lead=0.0, daily=2.0, per_lead=0.05, daily_cap=2.0)
    allowance = guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.002)
    assert not allowance.permitted
    assert allowance.reason == DAILY_BUDGET_EXHAUSTED


def test_the_tighter_constraint_is_the_one_named() -> None:
    """With both exhausted, the refusal blames the lead's own budget rather
    than the account's — the operator's next question is "why did this lead
    cost so much", and the reason code should point at it."""
    guard = _guard(lead=0.05, daily=2.0, per_lead=0.05, daily_cap=2.0)
    assert guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.002).reason == (
        PER_LEAD_BUDGET_EXHAUSTED
    )


def test_a_zero_budget_blocks_every_call() -> None:
    guard = _guard(lead=0.0, daily=0.0, per_lead=0.0, daily_cap=0.0)
    assert not guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.00001).permitted


def test_the_allowance_carries_the_full_arithmetic_for_the_audit_trail() -> None:
    guard = _guard(lead=0.01, daily=0.5, per_lead=0.05, daily_cap=2.0)
    allowance = guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.002)

    assert allowance.lead_spent_usd == Decimal("0.01")
    assert allowance.lead_cap_usd == Decimal("0.05")
    assert allowance.daily_spent_usd == Decimal("0.5")
    assert allowance.daily_cap_usd == Decimal("2.0")
    assert allowance.estimated_cost_usd == Decimal("0.002")

    audit = allowance.audit()
    assert audit["permitted"] is True
    assert "reason" not in audit


def test_money_never_becomes_a_float_in_the_comparison() -> None:
    """`Decimal` throughout, the same rule `arie.ledger.pricing.usd` enforces
    for reported cost. A float comparison here would let 0.1 + 0.2 > 0.3 refuse
    a call that fits."""
    guard = _guard(lead=0.1, daily=0.0, per_lead=0.3, daily_cap=2.0)
    allowance = guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.2)
    assert isinstance(allowance.lead_spent_usd, Decimal)
    assert allowance.permitted


def test_a_ledger_read_failure_propagates_rather_than_failing_open() -> None:
    """A spend cap that returns "permitted" when it cannot read the ledger is
    not a spend cap. The job fails into the ordinary retry path instead."""

    class _BrokenPool:
        @contextmanager
        def connection(self) -> Iterator[_FakeConnection]:
            raise RuntimeError("connection refused")
            yield  # pragma: no cover - unreachable, satisfies the generator protocol

    guard = LiveSpendGuard(_BrokenPool(), LiveBudgetConfig())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="connection refused"):
        guard.allowance(lead_id=uuid4(), estimated_cost_usd=0.002)


def test_the_budget_stop_reasons_are_exactly_the_two_refusals() -> None:
    assert {PER_LEAD_BUDGET_EXHAUSTED, DAILY_BUDGET_EXHAUSTED} == BUDGET_STOP_REASONS


# ------------------------------------------------------- which providers bill --


def test_the_daily_query_covers_every_provider_that_could_bill() -> None:
    """Including Apollo, which is defined but not yet wired. A paid provider
    added to the codebase and forgotten here would be invisible to the cap —
    so it is added *before* it can spend, not after."""
    assert ABSTRACT_PROVIDER_NAME in LIVE_PROVIDER_NAMES
    assert APOLLO_PROVIDER_NAME in LIVE_PROVIDER_NAMES


def test_only_abstract_is_actually_registered_today() -> None:
    assert REGISTERED_LIVE_PROVIDER_NAMES == (ABSTRACT_PROVIDER_NAME,)
    assert set(REGISTERED_LIVE_PROVIDER_NAMES) <= set(LIVE_PROVIDER_NAMES)


def test_no_simulated_catalogue_provider_counts_against_the_live_budget() -> None:
    """The filter that stops a benchmark run from exhausting a real account:
    simulated providers ledger their *fictional* catalogue prices."""
    from arie.providers.catalog import ALL_PROVIDERS

    assert set(ALL_PROVIDERS).isdisjoint(LIVE_PROVIDER_NAMES)
