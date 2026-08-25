"""Total-cost accounting and seed-stability machinery.

The arithmetic here decides whether the project's headline claim survives, so it
gets the same scrutiny as the policies themselves. A break-even formula with a
sign error would quietly convert a negative result into a positive one.
"""

from __future__ import annotations

import pytest
from bench.cost_model import (
    break_even_human_cost,
    cheapest_at,
    total_cost,
)
from bench.harness import run_once
from bench.multi_seed import DEFAULT_SEEDS, Spread

from arie.core.types import Decision
from arie.evalgen.schema import EvalLead
from arie.policy.base import PolicyOutcome
from arie.policy.evidence_view import score_results
from arie.policy.runner import PolicySummary, summarise


def _summary(
    name: str,
    leads: list[EvalLead],
    autonomous: list[bool],
    cost: float,
) -> PolicySummary:
    """Build a summary with a chosen autonomy pattern and flat per-lead cost."""
    scoring = score_results({})
    outcomes = [
        PolicyOutcome(
            decision=lead.oracle_decision,
            confidence=0.99 if auto else 0.1,
            autonomous=auto,
            providers_called=(),
            cost_usd=cost,
            latency_ms=0.0,
            cache_hits=0,
            stop_reason="test",
            scoring=scoring,
        )
        for lead, auto in zip(leads, autonomous, strict=True)
    ]
    return summarise(name, leads, outcomes)


# --- total cost --------------------------------------------------------------


def test_free_review_leaves_api_cost_unchanged(test_split: list[EvalLead]) -> None:
    leads = test_split[:10]
    summary = _summary("p", leads, [False] * 10, cost=0.30)
    assert total_cost(summary, 0.0).total_usd == pytest.approx(0.30)


def test_review_cost_scales_with_escalation_rate(test_split: list[EvalLead]) -> None:
    leads = test_split[:10]
    half = _summary("half", leads, [True] * 5 + [False] * 5, cost=0.10)
    none = _summary("none", leads, [True] * 10, cost=0.10)

    assert half.escalation_rate == pytest.approx(0.5)
    assert total_cost(half, 2.0).total_usd == pytest.approx(0.10 + 1.0)
    assert total_cost(none, 2.0).total_usd == pytest.approx(0.10)


def test_a_fully_autonomous_policy_pays_no_review_cost(test_split: list[EvalLead]) -> None:
    leads = test_split[:10]
    summary = _summary("auto", leads, [True] * 10, cost=0.40)
    assert total_cost(summary, 100.0).total_usd == pytest.approx(0.40)


# --- break-even --------------------------------------------------------------


def test_break_even_is_where_the_orderings_cross(test_split: list[EvalLead]) -> None:
    """The number that decides whether the API saving is real."""
    leads = test_split[:10]
    # Cheap on API but escalates everything.
    challenger = _summary("cheap", leads, [False] * 10, cost=0.20)
    # Pricier on API, escalates half.
    incumbent = _summary("pricey", leads, [True] * 5 + [False] * 5, cost=0.40)

    break_even = break_even_human_cost(challenger, incumbent)
    assert break_even is not None
    # api_gap 0.20 / escalation_gap 0.50 = 0.40
    assert break_even == pytest.approx(0.40)

    just_below = 0.40 - 1e-6
    just_above = 0.40 + 1e-6
    assert (
        total_cost(challenger, just_below).total_usd < total_cost(incumbent, just_below).total_usd
    )
    assert (
        total_cost(challenger, just_above).total_usd > total_cost(incumbent, just_above).total_usd
    )


def test_no_break_even_when_the_challenger_also_escalates_less(
    test_split: list[EvalLead],
) -> None:
    """Cheaper on both axes wins at every review price."""
    leads = test_split[:10]
    challenger = _summary("better", leads, [True] * 8 + [False] * 2, cost=0.20)
    incumbent = _summary("worse", leads, [True] * 2 + [False] * 8, cost=0.40)
    assert break_even_human_cost(challenger, incumbent) is None


def test_no_break_even_when_the_challenger_is_not_cheaper_on_api(
    test_split: list[EvalLead],
) -> None:
    leads = test_split[:10]
    challenger = _summary("pricey", leads, [False] * 10, cost=0.50)
    incumbent = _summary("cheap", leads, [True] * 10, cost=0.10)
    assert break_even_human_cost(challenger, incumbent) is None


def test_cheapest_policy_changes_as_review_gets_expensive(
    test_split: list[EvalLead],
) -> None:
    """The finding in miniature: ranking by API cost alone is not ranking by cost."""
    leads = test_split[:10]
    frugal = _summary("frugal", leads, [False] * 10, cost=0.10)
    thorough = _summary("thorough", leads, [True] * 10, cost=0.40)

    assert cheapest_at([frugal, thorough], 0.0).policy == "frugal"
    assert cheapest_at([frugal, thorough], 5.0).policy == "thorough"


# --- spread statistics -------------------------------------------------------


def test_spread_reports_the_full_range() -> None:
    spread = Spread("metric", (0.1, 0.2, 0.3))
    assert spread.mean == pytest.approx(0.2)
    assert spread.minimum == pytest.approx(0.1)
    assert spread.maximum == pytest.approx(0.3)
    assert spread.stdev > 0


def test_spread_of_one_value_has_no_deviation() -> None:
    assert Spread("metric", (0.5,)).stdev == 0.0


def test_default_seeds_matches_the_documented_reproduction_command() -> None:
    """Pins the gap found reconciling docs/benchmark.md against a fresh run.

    `DEFAULT_SEEDS` was seven seeds `(42, ..., 48)` while every "mean across 10
    seeds" claim in README/benchmark.md was actually produced by
    `--seeds 42 43 44 45 46 47 48 49 50 51` — the reproduction command
    documented at the top of benchmark.md, which this test also pins so the
    two cannot drift apart silently again. Running `python -m bench.multi_seed`
    with no arguments (as README's Quick Start tells a reader to) must measure
    the same seeds the published numbers claim.
    """
    assert DEFAULT_SEEDS == (42, 43, 44, 45, 46, 47, 48, 49, 50, 51)


# --- harness -----------------------------------------------------------------


@pytest.mark.slow
def test_harness_is_reproducible() -> None:
    """A stability sweep is worthless if individual runs are not stable."""
    first = run_once(42)
    second = run_once(42)
    assert first.dataset_sha256 == second.dataset_sha256
    assert first.verdict.outcome == second.verdict.outcome
    assert first.best_adaptive.mean_cost_usd == second.best_adaptive.mean_cost_usd
    assert first.cost_saving_vs_waterfall == second.cost_saving_vs_waterfall


@pytest.mark.slow
def test_different_seeds_produce_different_datasets() -> None:
    assert run_once(42).dataset_sha256 != run_once(43).dataset_sha256


@pytest.mark.slow
def test_every_policy_decision_is_a_valid_decision() -> None:
    run = run_once(42)
    valid = {str(d) for d in Decision}
    for summary in run.all_summaries:
        assert {r.decision for r in summary.records} <= valid
