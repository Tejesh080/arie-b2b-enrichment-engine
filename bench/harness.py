"""One benchmark run, reusable across the single-seed CLI and the seed sweep.

Extracted so both entry points execute identical logic. A sweep that ran a
subtly different pipeline than the headline result would be measuring its own
divergence rather than the stability of the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arie.confidence.model import ConfidenceModel, fit_confidence_model
from arie.config import POLICY
from arie.evalgen.generator import generate_dataset
from arie.evalgen.schema import EvalLead
from arie.policy.adaptive import AdaptiveVoI, estimate_disqualifier_rate
from arie.policy.base import EvidenceCache, RunContext
from arie.policy.baselines import (
    TIER_ORDER,
    FullEnrichment,
    WaterfallTuning,
    tune_waterfall,
)
from arie.policy.escalation_aware import EscalationAwareVoI
from arie.policy.production import CalibratedBoundsPolicy
from arie.policy.runner import PolicySummary, evaluate_policy
from arie.providers.simulated import CallLedger, build_from_leads
from bench.cost_model import HUMAN_REVIEW_PRICES
from bench.metrics import CounterfactualRegret, Verdict, counterfactual_regret, evaluate_verdict

VALUE_SCALES: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 8.0)


@dataclass(frozen=True)
class BenchmarkRun:
    seed: int
    dataset_sha256: str
    n_calibration: int
    n_test: int

    confidence_method: str
    confidence_ece: float
    tau: float
    disqualifier_rate: float

    full: PolicySummary
    production: PolicySummary
    waterfalls: tuple[PolicySummary, ...]
    adaptives: tuple[PolicySummary, ...]

    best_waterfall: PolicySummary
    best_adaptive: PolicySummary

    verdict: Verdict
    regrets: tuple[CounterfactualRegret, ...]

    escalation_aware: dict[float, PolicySummary] = field(default_factory=dict)
    """One entry per review price, each fitted to assume that price."""

    tunings: dict[str, WaterfallTuning] = field(default_factory=dict)

    @property
    def all_summaries(self) -> list[PolicySummary]:
        return [
            self.full,
            self.production,
            *self.waterfalls,
            *self.adaptives,
            *self.escalation_aware.values(),
        ]

    @property
    def cost_saving_vs_waterfall(self) -> float:
        base = self.best_waterfall.mean_cost_usd
        return (base - self.best_adaptive.mean_cost_usd) / base if base else 0.0

    @property
    def agreement_gap_vs_waterfall_pp(self) -> float:
        return round(
            (self.best_waterfall.decision_agreement - self.best_adaptive.decision_agreement) * 100,
            4,
        )

    @property
    def evoi_saving_vs_production(self) -> float:
        """Negative means the EVoI layer costs more than the policy without it."""
        base = self.production.mean_cost_usd
        return (base - self.best_adaptive.mean_cost_usd) / base if base else 0.0

    @property
    def production_saving_vs_waterfall(self) -> float:
        base = self.best_waterfall.mean_cost_usd
        return (base - self.production.mean_cost_usd) / base if base else 0.0

    @property
    def production_agreement_gap_pp(self) -> float:
        return round(
            (self.best_waterfall.decision_agreement - self.production.decision_agreement) * 100,
            4,
        )


def _context_factory(leads: list[EvalLead]):  # type: ignore[no-untyped-def]
    _, registry = build_from_leads(leads)

    def make() -> RunContext:
        # Fresh ledger and cache per policy: a warm cache carried between
        # policies would hand later ones free evidence.
        return RunContext(registry=registry, ledger=CallLedger(), cache=EvidenceCache())

    return make


def _best(summaries: list[PolicySummary]) -> PolicySummary:
    return max(summaries, key=lambda s: (s.decision_agreement, -s.mean_cost_usd))


def run_once(seed: int, model: ConfidenceModel | None = None) -> BenchmarkRun:
    """Generate, fit, tune, and evaluate for a single dataset seed."""
    leads, manifest = generate_dataset(seed=seed)
    calibration = [x for x in leads if x.split == "calibration"]
    test = [x for x in leads if x.split == "test"]

    # Fitted per seed: the confidence model and the waterfall gate belong to the
    # dataset they were calibrated on. Reusing one seed's model across all seeds
    # would leak and would not measure what a fresh deployment would see.
    if model is None:
        model = fit_confidence_model(
            calibration, target_error_rate=POLICY.target_autonomous_error_rate
        )
    disqualifier_rate = estimate_disqualifier_rate(calibration)

    make_calibration_ctx = _context_factory(leads)
    make_test_ctx = _context_factory(leads)

    tunings = {
        tier: tune_waterfall(calibration, model, make_calibration_ctx, tier) for tier in TIER_ORDER
    }

    full = evaluate_policy(FullEnrichment(model=model), test, make_test_ctx)
    production = evaluate_policy(CalibratedBoundsPolicy(model=model), test, make_test_ctx)
    waterfalls = [
        evaluate_policy(tunings[tier].build(model), test, make_test_ctx) for tier in TIER_ORDER
    ]
    adaptives = [
        evaluate_policy(
            AdaptiveVoI(
                model=model,
                disqualifier_rate=disqualifier_rate,
                budget_usd_cap=POLICY.lead_budget_usd_cap,
                latency_penalty_usd_per_sec=POLICY.latency_penalty_usd_per_sec,
                value_scale=scale,
            ),
            test,
            make_test_ctx,
        )
        for scale in VALUE_SCALES
    ]

    # One escalation-aware policy per review price, each told the price it will
    # actually be judged at. That is the honest setting: a policy that must
    # guess the cost of a human is solving a different problem.
    escalation_aware = {
        price: evaluate_policy(
            EscalationAwareVoI(
                model=model,
                disqualifier_rate=disqualifier_rate,
                budget_usd_cap=POLICY.lead_budget_usd_cap,
                latency_penalty_usd_per_sec=POLICY.latency_penalty_usd_per_sec,
                value_scale=1.0,
                human_review_usd=price,
            ),
            test,
            make_test_ctx,
        )
        for price in HUMAN_REVIEW_PRICES
    }

    best_waterfall = _best(waterfalls)
    best_adaptive = _best(adaptives)

    return BenchmarkRun(
        escalation_aware=escalation_aware,
        seed=seed,
        dataset_sha256=manifest.content_sha256,
        n_calibration=len(calibration),
        n_test=len(test),
        confidence_method=model.method,
        confidence_ece=model.report.ece,
        tau=model.tau,
        disqualifier_rate=disqualifier_rate,
        full=full,
        production=production,
        waterfalls=tuple(waterfalls),
        adaptives=tuple(adaptives),
        best_waterfall=best_waterfall,
        best_adaptive=best_adaptive,
        verdict=evaluate_verdict(full, best_waterfall, best_adaptive),
        regrets=(
            counterfactual_regret(full, best_waterfall),
            counterfactual_regret(full, production),
            counterfactual_regret(full, best_adaptive),
        ),
        tunings=tunings,
    )
