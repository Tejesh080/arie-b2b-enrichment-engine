"""The adaptive expected-value-of-information policy.

For each candidate provider:

    EVoI(p) = P(p changes the decision) * business_value
              - expected_cost(p)
              - latency_penalty(p)

Buy the best positive-EVoI candidate; stop when none is positive, when the
decision is provably settled, when confidence clears tau, or when the hard
budget cap trips.

Estimating **P(p changes the decision)** is where the substance is::

    P(flip) = P(the current decision is wrong)
              * P(this provider supplies the evidence that reveals it)

The first term is the calibrated confidence model, which is what makes "stop
when the decision stabilises" a mechanism rather than a slogan: as evidence
accumulates and confidence rises, the stake shrinks and buying stops paying for
itself. The second is the provider's share of the outstanding
decision-relevant information, weighted by field influence and discounted by
coverage.

A purely *marginal* estimator — "can this one provider move the score across a
threshold?" — was tried first and fails badly. From an empty state no single
provider supplies enough points to reach even the reject boundary, so every
candidate scores zero and the policy buys nothing at all. It is kept as a
**floor** on the estimate, because when it is positive one call really can
settle the question outright.

The disqualifying flag is handled separately with a base rate estimated on the
calibration split. It can drop the score to zero from anywhere, so folding it
into a uniform interval would make the single provider that reveals it look
enormously valuable for every lead.

The uniform prior over reachable scores is the weakest assumption here and is
recorded in docs/ASSUMPTIONS.md. It is deliberately not fitted: a learned flip
model would be one more thing to calibrate, and the point of this layer is that
the acquisition decision stays inspectable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from arie.confidence.model import ConfidenceModel
from arie.core.types import Decision, ProviderResult, VoIEvaluation
from arie.evalgen.schema import EvalLead
from arie.policy.base import PolicyOutcome, RunContext, business_value, collect_costs
from arie.policy.evidence_view import remaining_providers, score_results
from arie.providers.catalog import BY_NAME, ProviderSpec
from arie.scoring.engine import compute_bounds
from arie.scoring.rules import (
    COMPLETENESS_WEIGHTS,
    DISQUALIFIER_FIELD,
    MAX_FIELD_POINTS,
    QUALIFY_THRESHOLD,
    REJECT_THRESHOLD,
    SCORED_FIELDS,
    decide,
)


def _decision_measure(lo: float, hi: float, decision: Decision) -> float:
    """Length of the sub-interval of [lo, hi] that maps to `decision`."""
    if hi <= lo:
        return 0.0
    if decision is Decision.AUTO_ROUTE:
        return max(0.0, hi - max(lo, QUALIFY_THRESHOLD))
    if decision is Decision.REJECT:
        return max(0.0, min(hi, REJECT_THRESHOLD) - lo)
    return max(0.0, min(hi, QUALIFY_THRESHOLD) - max(lo, REJECT_THRESHOLD))


def marginal_flip_probability(
    facts: Mapping[str, object], spec: ProviderSpec, disqualifier_rate: float
) -> float:
    """Chance that `spec` **alone** flips the decision, under a uniform prior.

    Retained because it is a genuine hard signal: when this is positive, one
    call can settle the question outright.

    It is *not* sufficient on its own, and the failure is instructive. From an
    empty state the score is zero, so no single provider supplies enough points
    to cross the reject threshold — every candidate scores zero, the policy buys
    nothing, and every lead is rejected. That is textbook myopia: the estimator
    cannot see that a *combination* of calls would change the answer. Used
    alone it does not produce a conservative policy, it produces a paralysed
    one.
    """
    unknown = [name for name in spec.provides_fields if facts.get(name) is None]
    if not unknown:
        return 0.0

    bounds = compute_bounds(facts)
    current = bounds.current
    current_decision = decide(current)

    # The disqualifier is rare and catastrophic; treat it with its base rate
    # rather than folding it into a uniform interval.
    disqualifier_flip = 0.0
    if DISQUALIFIER_FIELD in unknown and current_decision is not Decision.REJECT:
        disqualifier_flip = disqualifier_rate

    gain = sum(MAX_FIELD_POINTS.get(name, 0.0) for name in unknown)
    additive_flip = 0.0
    if gain > 0:
        lo, hi = current, current + gain
        same = _decision_measure(lo, hi, current_decision)
        additive_flip = max(0.0, ((hi - lo) - same) / (hi - lo))

    # Either route can flip it; combine as independent chances.
    return min(1.0, disqualifier_flip + (1.0 - disqualifier_flip) * additive_flip)


def information_share(facts: Mapping[str, object], spec: ProviderSpec) -> float:
    """Fraction of the outstanding decision-relevant information `spec` supplies.

    Weighted by each field's influence on the score, and discounted by the
    provider's coverage — a source that usually returns nothing supplies little
    in expectation.
    """
    unknown = [name for name in SCORED_FIELDS if facts.get(name) is None]
    if not unknown:
        return 0.0

    outstanding = sum(COMPLETENESS_WEIGHTS.get(name, 0.0) for name in unknown)
    if outstanding <= 0:
        return 0.0

    supplied = sum(
        COMPLETENESS_WEIGHTS.get(name, 0.0) for name in spec.provides_fields if name in unknown
    )
    return spec.base_coverage * supplied / outstanding


def estimate_flip_probability(
    facts: Mapping[str, object],
    spec: ProviderSpec,
    disqualifier_rate: float,
    decision_wrong_probability: float,
) -> float:
    """Probability that calling `spec` changes the decision we would make now.

    Decomposed as::

        P(flip) = P(the current decision is wrong)
                  * P(this provider supplies the evidence that reveals it)

    The first term comes from the calibrated confidence model — this is where
    that model earns its place in the acquisition loop rather than only gating
    autonomy at the end. As evidence accumulates and confidence rises, the stake
    falls and the policy naturally stops buying: *stop when the decision
    stabilises*, expressed directly.

    The marginal single-provider flip probability is taken as a floor, so a
    provider that can settle the question by itself is never valued below its
    standalone worth.
    """
    share = information_share(facts, spec)
    combined = decision_wrong_probability * share
    marginal = marginal_flip_probability(facts, spec, disqualifier_rate)
    return min(1.0, max(combined, marginal))


def expected_cost(spec: ProviderSpec) -> float:
    """Cost weighted by the chance the call returns anything.

    A provider that bills on a miss costs its full price regardless; one that
    does not is only charged when it succeeds. Ignoring this would misprice
    exactly the expensive intent sources the policy most needs to reason about.
    """
    if spec.bill_on_miss:
        return spec.base_cost_usd
    return spec.base_cost_usd * spec.base_coverage


def estimate_disqualifier_rate(calibration_leads: list[EvalLead]) -> float:
    """Base rate of blocked companies, measured on calibration data only."""
    if not calibration_leads:
        return 0.0
    companies = {x.company.company_id: x.company.disqualifying_flag for x in calibration_leads}
    return sum(1 for flagged in companies.values() if flagged) / len(companies)


@dataclass
class AdaptiveVoI:
    """Buy the next provider only while it is expected to pay for itself."""

    model: ConfidenceModel
    disqualifier_rate: float
    budget_usd_cap: float = 0.50
    latency_penalty_usd_per_sec: float = 0.01
    value_scale: float = 1.0
    """Cost knob. Scaling perceived business value up buys more; down buys less.

    Sweeping this traces the policy's cost/quality frontier, the same way
    RouteLLM sweeps a cost threshold rather than reporting a single point."""

    max_steps: int = 8

    @property
    def name(self) -> str:
        return f"adaptive_voi_x{self.value_scale:g}"

    def _latency_penalty(self, spec: ProviderSpec) -> float:
        return (spec.p50_latency_ms / 1000.0) * self.latency_penalty_usd_per_sec

    def evaluate_candidates(
        self,
        facts: Mapping[str, object],
        called: set[str],
        value: float,
        decision_wrong_probability: float,
    ) -> list[VoIEvaluation]:
        return [
            VoIEvaluation(
                candidate_provider=name,
                p_flips_decision=estimate_flip_probability(
                    facts, BY_NAME[name], self.disqualifier_rate, decision_wrong_probability
                ),
                business_value=value,
                expected_cost=expected_cost(BY_NAME[name]),
                latency_penalty=self._latency_penalty(BY_NAME[name]),
            )
            for name in remaining_providers(called)
        ]

    def run(self, lead: EvalLead, ctx: RunContext) -> PolicyOutcome:
        marker = len(ctx.ledger.records)
        results: dict[str, ProviderResult] = {}
        trace: list[VoIEvaluation] = []
        value = business_value(lead, self.value_scale)
        spent = 0.0
        stop_reason = "max_steps"

        for _ in range(self.max_steps):
            scoring = score_results(results)

            # Hard, deterministic stop: no unbought evidence could move this.
            # Kept separate from the confidence gate on purpose — settled means
            # "nothing left worth buying", not "certainly correct".
            if scoring.bounds.is_settled:
                stop_reason = "decision_settled"
                break

            confidence = self.model.predict(scoring)
            if results and confidence >= self.model.tau:
                stop_reason = "confidence_reached"
                break

            candidates = self.evaluate_candidates(
                scoring.facts, set(results), value, 1.0 - confidence
            )
            if not candidates:
                stop_reason = "all_providers_called"
                break

            trace.extend(candidates)

            # Restrict to what the remaining budget allows *before* choosing.
            # Picking the best candidate and then aborting when it is
            # unaffordable would abandon leads entirely rather than fall back to
            # a cheaper source — and would make higher business value buy
            # *less*, since the priciest provider tends to win the argmax.
            affordable = [
                e
                for e in candidates
                if spent + BY_NAME[e.candidate_provider].base_cost_usd <= self.budget_usd_cap
            ]
            if not affordable:
                stop_reason = "budget_exhausted"
                break

            best = max(
                affordable,
                key=lambda e: (e.net_evoi, -BY_NAME[e.candidate_provider].base_cost_usd),
            )

            if best.net_evoi <= 0:
                stop_reason = "no_positive_evoi"
                break

            result, cached = ctx.fetch(best.candidate_provider, lead)
            results[best.candidate_provider] = result
            if not cached:
                spent += result.cost_usd

        scoring = score_results(results)
        confidence = self.model.predict(scoring)
        cost, latency, cache_hits = collect_costs(ctx, marker)

        return PolicyOutcome(
            decision=scoring.decision,
            confidence=confidence,
            autonomous=confidence >= self.model.tau,
            providers_called=tuple(results),
            cost_usd=cost,
            latency_ms=latency,
            cache_hits=cache_hits,
            stop_reason=stop_reason,
            scoring=scoring,
            voi_trace=tuple(trace),
        )
