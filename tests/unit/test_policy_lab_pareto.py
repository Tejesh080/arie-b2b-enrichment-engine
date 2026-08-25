"""`scripts.policy_lab.pareto` — dominance and frontier computation. The
frontier must fall out of the data, never a hardcoded policy list, so these
tests exercise synthetic point sets and assert on the computed membership."""

from __future__ import annotations

from scripts.policy_lab.pareto import ParetoPoint, compute_frontier, dominates


def test_cheaper_and_better_agreement_dominates() -> None:
    a = ParetoPoint("a", cost=0.20, agreement=0.85)
    b = ParetoPoint("b", cost=0.30, agreement=0.80)
    assert dominates(a, b) is True
    assert dominates(b, a) is False


def test_cheaper_but_worse_agreement_does_not_dominate() -> None:
    cheap_worse = ParetoPoint("a", cost=0.20, agreement=0.75)
    expensive_better = ParetoPoint("b", cost=0.30, agreement=0.85)
    assert dominates(cheap_worse, expensive_better) is False
    assert dominates(expensive_better, cheap_worse) is False


def test_identical_points_do_not_dominate_each_other() -> None:
    a = ParetoPoint("a", cost=0.20, agreement=0.80)
    b = ParetoPoint("b", cost=0.20, agreement=0.80)
    assert dominates(a, b) is False
    assert dominates(b, a) is False


def test_same_cost_strictly_better_agreement_dominates() -> None:
    better = ParetoPoint("a", cost=0.20, agreement=0.85)
    worse = ParetoPoint("b", cost=0.20, agreement=0.80)
    assert dominates(better, worse) is True


def test_same_agreement_strictly_cheaper_dominates() -> None:
    cheaper = ParetoPoint("a", cost=0.15, agreement=0.80)
    pricier = ParetoPoint("b", cost=0.20, agreement=0.80)
    assert dominates(cheaper, pricier) is True


def test_frontier_keeps_increasing_cost_increasing_agreement_points() -> None:
    """A, B, C trace a genuine frontier (each pricier point buys strictly
    better agreement); D is strictly worse than C on both axes."""
    a = ParetoPoint("A", cost=0.20, agreement=0.75)
    b = ParetoPoint("B", cost=0.30, agreement=0.80)
    c = ParetoPoint("C", cost=0.40, agreement=0.85)
    d = ParetoPoint("D", cost=0.25, agreement=0.70)

    result = compute_frontier([a, b, c, d])

    assert result.frontier == {"A", "B", "C"}
    assert result.dominated_by["D"] == "A"
    assert result.dominated_by["A"] is None
    assert result.dominated_by["B"] is None
    assert result.dominated_by["C"] is None


def test_frontier_reproduces_the_production_vs_evoi_finding() -> None:
    """The exact shape docs/benchmark.md reports: calibrated_bounds is
    cheaper AND better-agreement than adaptive_voi_x1, so it dominates
    outright; full_enrichment and waterfall_expensive are each other's
    cheaper-but-worse / pricier-but-better trade-off and stay on the frontier."""
    full = ParetoPoint("full_enrichment", cost=0.4447, agreement=0.8390)
    waterfall = ParetoPoint("waterfall_expensive", cost=0.4205, agreement=0.8347)
    calibrated = ParetoPoint("calibrated_bounds", cost=0.2463, agreement=0.8113)
    adaptive = ParetoPoint("adaptive_voi_x1", cost=0.2906, agreement=0.8093)

    result = compute_frontier([full, waterfall, calibrated, adaptive])

    assert result.frontier == {"full_enrichment", "waterfall_expensive", "calibrated_bounds"}
    assert result.dominated_by["adaptive_voi_x1"] == "calibrated_bounds"


def test_frontier_is_empty_input_safe() -> None:
    result = compute_frontier([])
    assert result.frontier == frozenset()
    assert result.dominated_by == {}


def test_a_single_point_is_always_on_the_frontier() -> None:
    result = compute_frontier([ParetoPoint("only", cost=0.5, agreement=0.5)])
    assert result.frontier == {"only"}
    assert result.dominated_by["only"] is None
