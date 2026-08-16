"""The two baselines the adaptive policy must beat.

``FullEnrichment`` is the naive one — call everything, every lead. It is the
comparison most write-ups in this space make, and beating it proves very
little.

``TunedWaterfall`` is the honest one. It implements what a competent GTM team
actually does: order providers cheapest-first so expensive vendors see the
fewest records, and gate rows out early when cheap evidence already shows they
cannot qualify. Its parameters are tuned on the calibration split. If the
adaptive policy cannot beat *this*, the thesis fails, and the pre-registered
criteria say so explicitly.

What the waterfall deliberately does **not** get: expected-value-of-information
reasoning, calibrated confidence, or decision-stability bounds. Those are the
contribution under test. Giving them to the baseline would make the comparison
vacuous; withholding standard practice would make it dishonest.
"""

from __future__ import annotations

from dataclasses import dataclass

from arie.confidence.model import ConfidenceModel
from arie.core.types import ProviderResult
from arie.evalgen.schema import EvalLead
from arie.policy.base import PolicyOutcome, RunContext, collect_costs
from arie.policy.evidence_view import score_results
from arie.providers.catalog import CATALOG
from arie.scoring.rules import REJECT_THRESHOLD

# Catalogue tiers, cheapest first. The waterfall's depth knob walks this list.
TIER_ORDER: tuple[str, ...] = ("free", "cheap", "mid", "expensive")


def providers_up_to_tier(max_tier: str) -> list[str]:
    limit = TIER_ORDER.index(max_tier)
    return [spec.name for spec in CATALOG if TIER_ORDER.index(spec.tier) <= limit]


@dataclass
class FullEnrichment:
    """Call every provider for every lead. The naive baseline."""

    model: ConfidenceModel

    @property
    def name(self) -> str:
        return "full_enrichment"

    def run(self, lead: EvalLead, ctx: RunContext) -> PolicyOutcome:
        marker = len(ctx.ledger.records)
        results: dict[str, ProviderResult] = {}

        for spec in CATALOG:
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
            stop_reason="all_providers_called",
            scoring=scoring,
        )


@dataclass
class TunedWaterfall:
    """Cheapest-first enrichment with an early out-of-ICP gate.

    Two levers, both standard practice:

    * **Depth** (`max_tier`) — how far down the price ladder to go. This is the
      cost knob that traces the baseline's frontier.
    * **Gate** (`gate_upper_bound`) — after the free tier, abandon a lead whose
      best *possible* score still falls below the qualifying band. Clay calls
      this a conditional run; it is the single largest saving available without
      any decision theory.

    ``gate_upper_bound`` is fitted on calibration data by
    :func:`tune_waterfall`, never on test.
    """

    model: ConfidenceModel
    max_tier: str = "expensive"
    gate_upper_bound: float = REJECT_THRESHOLD
    gate_mode: str = "upper_bound"
    """How the gate judges a lead.

    ``upper_bound`` is the sound test — abandon only when the best *possible*
    score still cannot qualify. It fires rarely, because early on the unknown
    fields keep the ceiling high.

    ``current_score`` is what practitioners actually configure: abandon when the
    score *so far* looks weak. It is aggressive and sometimes wrong, but it
    saves far more. Both are offered and calibration picks the winner, so the
    baseline is not handicapped by our preference for the principled option.
    """

    gate_after_tier: str = "free"

    @property
    def name(self) -> str:
        return f"waterfall_{self.max_tier}"

    def _gated_out(self, results: dict[str, ProviderResult]) -> bool:
        if not results:
            return False
        scoring = score_results(results)
        value = scoring.bounds.upper if self.gate_mode == "upper_bound" else scoring.total_score
        return value < self.gate_upper_bound

    def run(self, lead: EvalLead, ctx: RunContext) -> PolicyOutcome:
        marker = len(ctx.ledger.records)
        results: dict[str, ProviderResult] = {}
        allowed = providers_up_to_tier(self.max_tier)
        gate_index = TIER_ORDER.index(self.gate_after_tier)
        stop_reason = "tier_limit"
        gated = False

        for spec in CATALOG:
            if spec.name not in allowed:
                continue

            # Gate check fires once, at the boundary just past the gating tier:
            # cheap evidence is in hand, nothing expensive has been bought yet.
            if not gated and TIER_ORDER.index(spec.tier) > gate_index:
                gated = True
                if self._gated_out(results):
                    stop_reason = "gated_out_of_icp"
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


@dataclass
class BoundsOnlyStopping:
    """Ablation: deterministic stopping, no expected-value reasoning.

    Buys cheapest-first and stops the moment the decision is provably settled or
    confidence clears tau. It has every stopping signal the adaptive policy has
    **except** EVoI-guided ordering.

    This exists to answer the question a sceptical reviewer asks first: the
    adaptive policy stops most leads on `decision_settled`, so is the
    value-of-information machinery contributing anything, or are score bounds
    doing all the work? Without this comparison the headline number could not be
    attributed to the mechanism it claims.
    """

    model: ConfidenceModel

    @property
    def name(self) -> str:
        return "ablation_bounds_only"

    def run(self, lead: EvalLead, ctx: RunContext) -> PolicyOutcome:
        marker = len(ctx.ledger.records)
        results: dict[str, ProviderResult] = {}
        stop_reason = "all_providers_called"

        for spec in CATALOG:
            scoring = score_results(results)
            if scoring.bounds.is_settled:
                stop_reason = "decision_settled"
                break
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


@dataclass(frozen=True)
class WaterfallTuning:
    max_tier: str
    gate_upper_bound: float
    gate_mode: str
    calibration_agreement: float
    calibration_cost: float

    def build(self, model: ConfidenceModel) -> TunedWaterfall:
        return TunedWaterfall(
            model=model,
            max_tier=self.max_tier,
            gate_upper_bound=self.gate_upper_bound,
            gate_mode=self.gate_mode,
        )


# (mode, threshold) pairs, spanning "never gate" through both gating styles.
GATE_GRID: tuple[tuple[str, float], ...] = (
    ("upper_bound", 0.0),  # gate disabled
    ("upper_bound", 40.0),
    ("upper_bound", 50.0),
    ("upper_bound", REJECT_THRESHOLD),
    ("upper_bound", 65.0),
    ("upper_bound", 80.0),
    ("current_score", 10.0),
    ("current_score", 20.0),
    ("current_score", 30.0),
    ("current_score", 40.0),
)


def tune_waterfall(
    calibration_leads: list[EvalLead],
    model: ConfidenceModel,
    make_context: object,
    max_tier: str,
) -> WaterfallTuning:
    """Pick the gate that maximises calibration agreement at this depth.

    Ties break toward the cheaper gate, so the baseline is handed the strongest
    honest configuration rather than merely a working one — a weak baseline
    would make any subsequent win meaningless. Selection happens entirely on
    calibration leads; the chosen configuration is then run once on test.
    """
    from arie.policy.runner import evaluate_policy  # local import avoids a cycle

    best: WaterfallTuning | None = None
    for mode, gate in GATE_GRID:
        policy = TunedWaterfall(
            model=model, max_tier=max_tier, gate_upper_bound=gate, gate_mode=mode
        )
        summary = evaluate_policy(policy, calibration_leads, make_context)  # type: ignore[arg-type]
        candidate = WaterfallTuning(
            max_tier=max_tier,
            gate_upper_bound=gate,
            gate_mode=mode,
            calibration_agreement=summary.decision_agreement,
            calibration_cost=summary.mean_cost_usd,
        )
        if best is None or (candidate.calibration_agreement, -candidate.calibration_cost) > (
            best.calibration_agreement,
            -best.calibration_cost,
        ):
            best = candidate

    assert best is not None
    return best
