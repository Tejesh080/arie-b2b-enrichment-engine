"""Adaptive acquisition that prices the human it would otherwise escalate to.

``AdaptiveVoI`` optimises API spend subject to decision quality, and does it
well. Pricing human review exposed the flaw in that objective: the policy's
dominant stop reason is `decision_settled`, which fires when no unbought
evidence could change the decision — but settling says nothing about whether
confidence has cleared tau. So it stops exactly where one more call would have
bought *autonomy*, and every lead it stops short on lands on a person who costs
far more than the call would have.

This version adds the missing term::

    EVoI(p) = P(p flips the decision) * business_value
              + P(p carries us past tau) * human_review_usd     <-- new
              - expected_cost(p)
              - latency_penalty(p)

With review priced at zero it reduces to the original policy. As review gets
expensive it buys more, and it buys specifically the evidence that produces
confidence rather than the evidence that settles bounds. The original is left
untouched so the two can be compared on the same data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from arie.confidence.features import FEATURE_NAMES, extract_features
from arie.core.types import ProviderResult, VoIEvaluation
from arie.evalgen.schema import EvalLead
from arie.policy.adaptive import AdaptiveVoI, estimate_flip_probability, expected_cost
from arie.policy.base import PolicyOutcome, RunContext, business_value, collect_costs
from arie.policy.evidence_view import remaining_providers, score_results
from arie.providers.catalog import BY_NAME, ProviderSpec
from arie.scoring.engine import ScoringResult
from arie.scoring.rules import (
    COMPLETENESS_WEIGHTS,
    MAX_FIELD_POINTS,
    MAX_TOTAL_SCORE,
    SCORED_FIELDS,
)


def project_features_after(result: ScoringResult, spec: ProviderSpec) -> list[float]:
    """Feature vector as it would look if `spec` returned everything it covers.

    A deliberately *optimistic* projection — it assumes the call succeeds and
    resolves every field it advertises. The optimism is corrected downstream by
    multiplying through the provider's coverage rate, which keeps the
    approximation in one place instead of smearing it across the estimate.

    Only the features that a successful call mechanically moves are projected;
    conflict and source-confidence terms depend on values we have not seen and
    are left as they are.
    """
    features = extract_features(result)
    unknown = [name for name in SCORED_FIELDS if result.facts.get(name) is None]
    supplied = [name for name in spec.provides_fields if name in unknown]
    if not supplied:
        return [features[name] for name in FEATURE_NAMES]

    total_weight = sum(COMPLETENESS_WEIGHTS.values())
    added_weight = sum(COMPLETENESS_WEIGHTS.get(name, 0.0) for name in supplied)
    resolved_gain = sum(MAX_FIELD_POINTS.get(name, 0.0) for name in supplied)

    features["completeness"] = min(1.0, features["completeness"] + added_weight / total_weight)
    features["unknown_field_ratio"] = max(
        0.0, features["unknown_field_ratio"] - len(supplied) / len(SCORED_FIELDS)
    )
    features["evidence_density"] = min(
        1.0, features["evidence_density"] + len(supplied) / (len(SCORED_FIELDS) * 2.0)
    )
    features["bounds_width"] = max(0.0, features["bounds_width"] - resolved_gain / MAX_TOTAL_SCORE)
    return [features[name] for name in FEATURE_NAMES]


@dataclass
class EscalationAwareVoI(AdaptiveVoI):
    """Adaptive acquisition that also values avoiding a human review."""

    human_review_usd: float = 0.0

    @property
    def name(self) -> str:
        return f"escalation_aware_h{self.human_review_usd:g}"

    def _escalation_values(
        self, scoring: ScoringResult, confidence: float, candidates: list[str]
    ) -> dict[str, float]:
        """Expected review cost avoided, per candidate provider.

        Credit is *graded* by how much of the gap to tau a call is expected to
        close, not awarded only when the projection lands past it. An
        all-or-nothing rule reproduces the myopia that paralysed the first
        estimator: from a cold start no single call reaches tau, every candidate
        scores zero, and the policy never begins.
        """
        if self.human_review_usd <= 0 or confidence >= self.model.tau:
            return dict.fromkeys(candidates, 0.0)

        gap = self.model.tau - confidence
        if gap <= 0:
            return dict.fromkeys(candidates, 0.0)

        projected = np.asarray(
            [project_features_after(scoring, BY_NAME[name]) for name in candidates], dtype=float
        )
        confidences = self.model.predict_many(projected)

        values: dict[str, float] = {}
        for name, projected_confidence in zip(candidates, confidences, strict=True):
            closed = max(0.0, min(1.0, (float(projected_confidence) - confidence) / gap))
            values[name] = BY_NAME[name].base_coverage * closed * self.human_review_usd
        return values

    def evaluate_candidates_with_escalation(
        self,
        scoring: ScoringResult,
        called: set[str],
        value: float,
        confidence: float,
    ) -> list[VoIEvaluation]:
        candidates = remaining_providers(called)
        escalation = self._escalation_values(scoring, confidence, candidates)
        facts: Mapping[str, object] = scoring.facts

        return [
            VoIEvaluation(
                candidate_provider=name,
                p_flips_decision=estimate_flip_probability(
                    facts, BY_NAME[name], self.disqualifier_rate, 1.0 - confidence
                ),
                business_value=value,
                expected_cost=expected_cost(BY_NAME[name]),
                latency_penalty=self._latency_penalty(BY_NAME[name]),
                escalation_value=escalation[name],
            )
            for name in candidates
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
            confidence = self.model.predict(scoring)

            if results and confidence >= self.model.tau:
                stop_reason = "confidence_reached"
                break

            # Bounds settling no longer stops the loop by itself. Once review is
            # priced, "nothing more can change the decision" and "nothing more is
            # worth buying" are different claims: more evidence may still raise
            # confidence past tau and save a review. The EVoI comparison decides,
            # and it correctly stops when nothing further can help.
            if scoring.bounds.is_settled and self.human_review_usd <= 0:
                stop_reason = "decision_settled"
                break

            candidates = self.evaluate_candidates_with_escalation(
                scoring, set(results), value, confidence
            )
            if not candidates:
                stop_reason = "all_providers_called"
                break
            trace.extend(candidates)

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
