"""Counterfactual regret and the pre-registered verdict.

The gap identified in the competitive research: every project in this space
reports "we made fewer API calls", which is an *input*, not a result. The
question that matters is what those skipped calls cost in decision quality.

``counterfactual_regret`` answers it directly — against full enrichment as the
reference, how many decisions changed, and of those, how many got worse rather
than better. "We cut spend 60% and changed 1.4% of decisions, 0.4% of them for
the worse" is a claim; "we made fewer calls" is not.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from arie.policy.runner import PolicySummary


@dataclass(frozen=True)
class CounterfactualRegret:
    """What changed when a policy stopped early, relative to buying everything."""

    reference: str
    policy: str
    n_leads: int

    changed: int
    """Decisions that differ from the reference policy's."""

    improved: int
    """Changed *and* now agree with the oracle — stopping early helped."""

    worsened: int
    """Changed and now disagree — the real cost of stopping early."""

    changed_rate: float
    worsened_rate: float
    net_agreement_delta: float
    cost_saved_per_lead: float
    cost_saved_pct: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def counterfactual_regret(reference: PolicySummary, policy: PolicySummary) -> CounterfactualRegret:
    by_id = {r.eval_lead_id: r for r in reference.records}
    changed = improved = worsened = 0

    for record in policy.records:
        baseline = by_id.get(record.eval_lead_id)
        if baseline is None or baseline.decision == record.decision:
            continue
        changed += 1
        if record.correct and not baseline.correct:
            improved += 1
        elif baseline.correct and not record.correct:
            worsened += 1

    n = policy.n_leads
    saved = reference.mean_cost_usd - policy.mean_cost_usd

    return CounterfactualRegret(
        reference=reference.policy,
        policy=policy.policy,
        n_leads=n,
        changed=changed,
        improved=improved,
        worsened=worsened,
        changed_rate=round(changed / n, 6) if n else 0.0,
        worsened_rate=round(worsened / n, 6) if n else 0.0,
        net_agreement_delta=round(policy.decision_agreement - reference.decision_agreement, 6),
        cost_saved_per_lead=round(saved, 8),
        cost_saved_pct=round(saved / reference.mean_cost_usd, 6)
        if reference.mean_cost_usd
        else 0.0,
    )


# --- pre-registered M0 criteria ---------------------------------------------
#
# Fixed in docs/03-mvp.md before any result existed. Evaluated mechanically so
# the verdict cannot drift toward whatever the numbers happened to show.

AGREEMENT_TOLERANCE_PP = 1.0
STRONG_COST_REDUCTION = 0.40
WATERFALL_COST_REDUCTION = 0.20
WEAK_AGREEMENT_LOSS_PP = 2.0


@dataclass(frozen=True)
class Verdict:
    outcome: str
    holds: bool
    detail: str
    evidence: dict[str, float]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _pp(value: float) -> float:
    return round(value * 100, 3)


def evaluate_verdict(
    full: PolicySummary, waterfall: PolicySummary, adaptive: PolicySummary
) -> Verdict:
    """Apply the pre-registered criteria to the measured frontier.

    Deliberately mechanical. The row that fires is determined by the numbers,
    including the row that falsifies the thesis.
    """
    agreement_gap_vs_full = _pp(full.decision_agreement - adaptive.decision_agreement)
    agreement_gap_vs_waterfall = _pp(waterfall.decision_agreement - adaptive.decision_agreement)
    cost_cut_vs_full = (
        (full.mean_cost_usd - adaptive.mean_cost_usd) / full.mean_cost_usd
        if full.mean_cost_usd
        else 0.0
    )
    cost_cut_vs_waterfall = (
        (waterfall.mean_cost_usd - adaptive.mean_cost_usd) / waterfall.mean_cost_usd
        if waterfall.mean_cost_usd
        else 0.0
    )

    evidence = {
        "adaptive_agreement": adaptive.decision_agreement,
        "waterfall_agreement": waterfall.decision_agreement,
        "full_agreement": full.decision_agreement,
        "agreement_gap_vs_full_pp": agreement_gap_vs_full,
        "agreement_gap_vs_waterfall_pp": agreement_gap_vs_waterfall,
        "cost_cut_vs_full": round(cost_cut_vs_full, 6),
        "cost_cut_vs_waterfall": round(cost_cut_vs_waterfall, 6),
    }

    # Weak result: cheaper, but decision quality visibly degraded.
    if agreement_gap_vs_full > WEAK_AGREEMENT_LOSS_PP:
        return Verdict(
            outcome="WEAK",
            holds=False,
            detail=(
                f"Adaptive loses {agreement_gap_vs_full:.2f}pp of decision agreement "
                f"against full enrichment (limit {WEAK_AGREEMENT_LOSS_PP}pp). "
                "Report the frontier; claim no win."
            ),
            evidence=evidence,
        )

    beats_waterfall = (
        agreement_gap_vs_waterfall <= AGREEMENT_TOLERANCE_PP
        and cost_cut_vs_waterfall >= WATERFALL_COST_REDUCTION
    )

    if beats_waterfall and (
        agreement_gap_vs_full <= AGREEMENT_TOLERANCE_PP
        and cost_cut_vs_full >= STRONG_COST_REDUCTION
    ):
        return Verdict(
            outcome="THESIS_HOLDS_STRONGLY",
            holds=True,
            detail=(
                f"Matches full enrichment within {agreement_gap_vs_full:.2f}pp at "
                f"{cost_cut_vs_full:.1%} lower cost, and beats the tuned waterfall "
                f"by {cost_cut_vs_waterfall:.1%} at matched agreement."
            ),
            evidence=evidence,
        )

    if beats_waterfall:
        return Verdict(
            outcome="THESIS_HOLDS",
            holds=True,
            detail=(
                f"Beats the tuned waterfall by {cost_cut_vs_waterfall:.1%} cost at "
                f"matched agreement (gap {agreement_gap_vs_waterfall:.2f}pp). "
                "This is the criterion that matters."
            ),
            evidence=evidence,
        )

    if cost_cut_vs_full >= STRONG_COST_REDUCTION:
        return Verdict(
            outcome="THESIS_FALSIFIED",
            holds=False,
            detail=(
                "Adaptive beats full enrichment but not the tuned waterfall "
                f"(cost cut {cost_cut_vs_waterfall:.1%}, agreement gap "
                f"{agreement_gap_vs_waterfall:.2f}pp). Waterfall heuristics already "
                "capture most of the available gain. The honest finding: pivot the "
                "narrative to calibration and escalation control, which stand alone."
            ),
            evidence=evidence,
        )

    return Verdict(
        outcome="INCONCLUSIVE",
        holds=False,
        detail=(
            "Adaptive shows no material advantage over either baseline "
            f"(cost cut vs full {cost_cut_vs_full:.1%}, vs waterfall "
            f"{cost_cut_vs_waterfall:.1%})."
        ),
        evidence=evidence,
    )
