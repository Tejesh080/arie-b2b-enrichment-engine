"""Total cost of a decision, including the human it escalates to.

Every number reported so far has counted API spend only. That is the number
this project's thesis was written about, and it is also an incomplete accounting:
a policy that buys less evidence is less often confident, escalates more, and
pushes work onto a person who is not free.

Analyst time dominates API prices by orders of magnitude. A few minutes of a
loaded salary costs more than calling every provider in the catalogue several
times over. So the interesting quantity is not total cost at one assumed rate —
it is the **break-even price**: how cheap human review must be before the
cheaper-on-API policy is still cheaper overall. If that break-even is far below
any plausible rate, the API saving is an artefact of what was left out of the
sum.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from arie.policy.runner import PolicySummary

# Indicative review prices, in dollars per escalated lead. Assumptions, not
# measurements — recorded in docs/ASSUMPTIONS.md.
#
#   $0.00  the implicit assumption of an API-only accounting
#   $0.25  ~20 seconds of a $45/hr analyst
#   $1.00  ~80 seconds
#   $2.50  ~3 minutes, a realistic floor for a considered look at a lead
#   $5.00  ~6 minutes, a careful review
HUMAN_REVIEW_PRICES: tuple[float, ...] = (0.0, 0.25, 1.00, 2.50, 5.00)


@dataclass(frozen=True)
class TotalCost:
    policy: str
    api_cost_usd: float
    escalation_rate: float
    human_review_usd: float
    human_cost_usd: float
    total_usd: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def total_cost(summary: PolicySummary, human_review_usd: float) -> TotalCost:
    human = summary.escalation_rate * human_review_usd
    return TotalCost(
        policy=summary.policy,
        api_cost_usd=summary.mean_cost_usd,
        escalation_rate=summary.escalation_rate,
        human_review_usd=human_review_usd,
        human_cost_usd=round(human, 8),
        total_usd=round(summary.mean_cost_usd + human, 8),
    )


def break_even_human_cost(challenger: PolicySummary, incumbent: PolicySummary) -> float | None:
    """Review price at which `challenger` stops being cheaper than `incumbent`.

    Returns ``None`` when the ordering never flips — either because the
    challenger escalates no more than the incumbent (its advantage only widens
    as review gets pricier), or because it is already the more expensive of the
    two before any human cost is added.
    """
    api_gap = incumbent.mean_cost_usd - challenger.mean_cost_usd
    escalation_gap = challenger.escalation_rate - incumbent.escalation_rate

    if api_gap <= 0:
        return None  # challenger is not cheaper even on API alone
    if escalation_gap <= 0:
        return None  # challenger escalates less too; it wins at any price

    return api_gap / escalation_gap


def cheapest_at(summaries: list[PolicySummary], human_review_usd: float) -> PolicySummary:
    return min(summaries, key=lambda s: total_cost(s, human_review_usd).total_usd)
