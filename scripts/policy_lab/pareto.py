"""Pareto dominance and frontier membership, computed from data — never a
hardcoded list of which policies "win".

Two objectives: minimize cost, maximize synthetic-oracle agreement.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParetoPoint:
    policy: str
    cost: float
    agreement: float


@dataclass(frozen=True)
class FrontierResult:
    frontier: frozenset[str]
    """Policy names with no dominator among the compared set."""

    dominated_by: dict[str, str | None]
    """Policy name -> the name of one policy that dominates it, or `None` if
    it is on the frontier. When more than one policy could dominate it, this
    holds the first found in input order — membership in `frontier` is the
    fact that matters, this is just an illustrative example for reporting."""


def dominates(a: ParetoPoint, b: ParetoPoint) -> bool:
    """`a` dominates `b`: no more expensive, no worse agreement, and
    strictly better in at least one of the two."""
    no_more_expensive = a.cost <= b.cost
    no_worse_agreement = a.agreement >= b.agreement
    strictly_better = a.cost < b.cost or a.agreement > b.agreement
    return no_more_expensive and no_worse_agreement and strictly_better


def compute_frontier(points: list[ParetoPoint]) -> FrontierResult:
    dominated_by: dict[str, str | None] = {}
    for candidate in points:
        dominator = next(
            (
                other.policy
                for other in points
                if other.policy != candidate.policy and dominates(other, candidate)
            ),
            None,
        )
        dominated_by[candidate.policy] = dominator
    frontier = frozenset(p.policy for p in points if dominated_by[p.policy] is None)
    return FrontierResult(frontier=frontier, dominated_by=dominated_by)
