"""The production policy: deterministic bounds plus a calibrated confidence gate.

This is the recommended policy, and it arrived here by losing an argument.

The project set out to show that expected-value-of-information reasoning would
beat fixed enrichment pipelines. It was built, and an ablation was built
alongside it to check that any win was attributable to the EVoI machinery rather
than to the score bounds underneath. Across ten seeds the ablation won: cheaper
on nine of ten, at equal or better decision agreement, and cheapest on total
cost at every human-review price tested. So the ablation is the product and the
sophisticated version is the documented negative result.

Full numbers and methodology: docs/05-results.md.
Why EVoI did not pay off: docs/adr/0004-evoi-is-a-negative-result.md.

The policy itself is deliberately simple:

1. Walk providers cheapest-first.
2. Stop when the decision is **provably settled** — no unbought evidence could
   change it.
3. Stop when **calibrated confidence** clears tau, meaning the decision can be
   taken without a human.
4. Otherwise buy the next provider.

Both stopping rules are load-bearing and they answer different questions.
Settled asks "can anything still change this?"; confidence asks "is this right?"
Bounds are computed from observed facts, which are noisy, so a settled decision
can still be wrong — which is why neither rule subsumes the other.
"""

from __future__ import annotations

from dataclasses import dataclass

from arie.confidence.model import ConfidenceModel
from arie.core.types import ProviderResult
from arie.evalgen.schema import EvalLead
from arie.policy.base import PolicyOutcome, RunContext, collect_costs
from arie.policy.evidence_view import score_results
from arie.providers.catalog import CATALOG


@dataclass
class CalibratedBoundsPolicy:
    """Cheapest-first acquisition, stopped by score bounds or calibrated confidence."""

    model: ConfidenceModel

    @property
    def name(self) -> str:
        return "calibrated_bounds"

    def run(self, lead: EvalLead, ctx: RunContext) -> PolicyOutcome:
        marker = len(ctx.ledger.records)
        results: dict[str, ProviderResult] = {}
        stop_reason = "all_providers_called"

        for spec in CATALOG:
            scoring = score_results(results)

            # Nothing unbought could move the decision — buying more cannot help.
            if scoring.bounds.is_settled:
                stop_reason = "decision_settled"
                break

            # Confident enough to act without a person. Note this fires *more*
            # often than buying everything does: extra evidence can introduce
            # conflict between sources and pull confidence back down.
            if results and self.model.predict(scoring) >= self.model.tau:
                stop_reason = "confidence_reached"
                break

            result, _ = ctx.fetch(spec.name, lead)
            results[spec.name] = result

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
        )
