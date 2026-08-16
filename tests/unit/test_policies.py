"""Policy behaviour, fairness of the comparison, and verdict logic.

The benchmark's credibility rests on two things being true: every policy faces
identical conditions, and the verdict is computed from the numbers rather than
chosen. Both are asserted here.
"""

from __future__ import annotations

import pytest
from bench.metrics import counterfactual_regret, evaluate_verdict

from arie.confidence.model import ConfidenceModel, fit_confidence_model
from arie.core.types import Decision
from arie.evalgen.schema import EvalLead
from arie.policy.adaptive import (
    AdaptiveVoI,
    estimate_disqualifier_rate,
    expected_cost,
    information_share,
    marginal_flip_probability,
)
from arie.policy.base import EvidenceCache, RunContext
from arie.policy.baselines import (
    FullEnrichment,
    TunedWaterfall,
    providers_up_to_tier,
)
from arie.policy.production import CalibratedBoundsPolicy
from arie.policy.runner import evaluate_policy, summarise
from arie.providers.catalog import BY_NAME, CATALOG
from arie.providers.simulated import CallLedger, build_from_leads

TARGET_ERROR_RATE = 0.05


@pytest.fixture(scope="module")
def model(calibration_split: list[EvalLead]) -> ConfidenceModel:
    return fit_confidence_model(calibration_split, target_error_rate=TARGET_ERROR_RATE)


@pytest.fixture(scope="module")
def make_ctx(leads: list[EvalLead]):  # type: ignore[no-untyped-def]
    _, registry = build_from_leads(leads)

    def factory() -> RunContext:
        return RunContext(registry=registry, ledger=CallLedger(), cache=EvidenceCache())

    return factory


@pytest.fixture(scope="module")
def sample(test_split: list[EvalLead]) -> list[EvalLead]:
    return test_split[:60]


# --- full enrichment ---------------------------------------------------------


def test_full_enrichment_calls_every_provider(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    summary = evaluate_policy(FullEnrichment(model=model), sample, make_ctx)
    assert summary.mean_calls == pytest.approx(len(CATALOG))


def test_full_enrichment_is_the_cost_ceiling(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    """No policy may cost more than buying everything."""
    full = evaluate_policy(FullEnrichment(model=model), sample, make_ctx)
    for policy in (
        CalibratedBoundsPolicy(model=model),
        TunedWaterfall(model=model, max_tier="expensive"),
        AdaptiveVoI(model=model, disqualifier_rate=0.07),
    ):
        summary = evaluate_policy(policy, sample, make_ctx)
        assert summary.mean_cost_usd <= full.mean_cost_usd + 1e-9


# --- waterfall ---------------------------------------------------------------


def test_waterfall_respects_its_tier_limit(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    for tier in ("free", "cheap", "mid"):
        allowed = set(providers_up_to_tier(tier))
        ctx = make_ctx()
        policy = TunedWaterfall(model=model, max_tier=tier, gate_upper_bound=0.0)
        for lead in sample[:10]:
            outcome = policy.run(lead, ctx)
            assert set(outcome.providers_called) <= allowed


def test_deeper_tiers_never_cost_less(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    costs = [
        evaluate_policy(
            TunedWaterfall(model=model, max_tier=tier, gate_upper_bound=0.0), sample, make_ctx
        ).mean_cost_usd
        for tier in ("free", "cheap", "mid", "expensive")
    ]
    assert costs == sorted(costs)


def test_aggressive_gate_reduces_spend(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    """The gate must actually gate, or the baseline is weaker than claimed."""
    ungated = evaluate_policy(
        TunedWaterfall(model=model, max_tier="expensive", gate_upper_bound=0.0), sample, make_ctx
    )
    gated = evaluate_policy(
        TunedWaterfall(
            model=model,
            max_tier="expensive",
            gate_upper_bound=40.0,
            gate_mode="current_score",
        ),
        sample,
        make_ctx,
    )
    assert gated.mean_cost_usd < ungated.mean_cost_usd
    assert "gated_out_of_icp" in gated.stop_reasons


# --- adaptive ----------------------------------------------------------------


def test_higher_business_value_buys_more(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    """Regression test for a real inversion.

    Budget affordability was originally checked *after* choosing the best
    candidate, aborting the lead when it was too expensive. Because the priciest
    provider tends to win the argmax, raising business value made the policy buy
    *less* — and at high values, nothing at all.
    """
    spend = [
        evaluate_policy(
            AdaptiveVoI(model=model, disqualifier_rate=0.07, value_scale=scale),
            sample,
            make_ctx,
        ).mean_cost_usd
        for scale in (0.05, 0.25, 1.0)
    ]
    assert spend == sorted(spend), f"spend must not fall as value rises: {spend}"
    assert spend[-1] > 0.0


def test_budget_cap_falls_back_to_affordable_providers(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    """A tight budget must degrade gracefully, not abandon the lead."""
    cheapest = min(spec.base_cost_usd for spec in CATALOG if spec.base_cost_usd > 0)
    policy = AdaptiveVoI(
        model=model, disqualifier_rate=0.07, budget_usd_cap=cheapest * 2, value_scale=8.0
    )
    summary = evaluate_policy(policy, sample, make_ctx)
    assert summary.mean_calls > 0, "tight budget abandoned every lead"
    assert summary.mean_cost_usd <= cheapest * 2 + 1e-9


def test_budget_cap_is_never_exceeded(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    ctx = make_ctx()
    policy = AdaptiveVoI(model=model, disqualifier_rate=0.07, budget_usd_cap=0.05, value_scale=8.0)
    for lead in sample[:20]:
        assert policy.run(lead, ctx).cost_usd <= 0.05 + 1e-9


def test_adaptive_stops_when_the_decision_is_settled(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    summary = evaluate_policy(
        AdaptiveVoI(model=model, disqualifier_rate=0.07, value_scale=1.0), sample, make_ctx
    )
    assert "decision_settled" in summary.stop_reasons


def test_adaptive_buys_fewer_calls_than_full(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    full = evaluate_policy(FullEnrichment(model=model), sample, make_ctx)
    adaptive = evaluate_policy(
        AdaptiveVoI(model=model, disqualifier_rate=0.07, value_scale=1.0), sample, make_ctx
    )
    assert adaptive.mean_calls < full.mean_calls


# --- the flip estimator ------------------------------------------------------


def test_provider_offering_nothing_new_has_no_information_share() -> None:
    spec = BY_NAME["firmographics_basic"]
    known = {name: "software" for name in spec.provides_fields}
    assert information_share(known, spec) == 0.0


def test_information_share_shrinks_as_fields_become_known() -> None:
    spec = BY_NAME["firmographics_basic"]
    empty = information_share({}, spec)
    partial = information_share({"industry": "software"}, spec)
    assert empty > partial >= 0.0


def test_marginal_flip_is_zero_when_no_value_could_cross_a_threshold() -> None:
    """The myopia that made a pure marginal estimator paralyse the policy.

    From an empty state no single provider supplies enough points to reach the
    reject boundary, so every candidate looks worthless. Documented as a test so
    the reason the estimator is only a *floor* stays visible.
    """
    spec = BY_NAME["inbound_payload"]
    assert marginal_flip_probability({}, spec, disqualifier_rate=0.07) == 0.0


def test_expected_cost_accounts_for_miss_billing() -> None:
    billed = BY_NAME["intent_signals"]
    not_billed = BY_NAME["firmographics_basic"]
    assert expected_cost(billed) == billed.base_cost_usd
    assert expected_cost(not_billed) < not_billed.base_cost_usd


def test_disqualifier_rate_is_measured_per_company(calibration_split) -> None:  # type: ignore[no-untyped-def]
    rate = estimate_disqualifier_rate(calibration_split)
    assert 0.0 < rate < 0.5
    assert estimate_disqualifier_rate([]) == 0.0


# --- fairness of the comparison ---------------------------------------------


def test_policies_are_deterministic(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    for policy_factory in (
        lambda: FullEnrichment(model=model),
        lambda: TunedWaterfall(model=model, max_tier="mid"),
        lambda: AdaptiveVoI(model=model, disqualifier_rate=0.07),
        lambda: CalibratedBoundsPolicy(model=model),
    ):
        first = evaluate_policy(policy_factory(), sample, make_ctx)
        second = evaluate_policy(policy_factory(), sample, make_ctx)
        assert first.decision_agreement == second.decision_agreement
        assert first.mean_cost_usd == second.mean_cost_usd


def test_lead_order_does_not_change_results(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    """Cache behaviour depends on the order companies are met.

    The runner sorts leads so every policy meets them identically; without that,
    cache hit rates would differ between strategies and contaminate the cost
    comparison.
    """
    forward = evaluate_policy(AdaptiveVoI(model=model, disqualifier_rate=0.07), sample, make_ctx)
    backward = evaluate_policy(
        AdaptiveVoI(model=model, disqualifier_rate=0.07), list(reversed(sample)), make_ctx
    )
    assert forward.mean_cost_usd == backward.mean_cost_usd
    assert forward.decision_agreement == backward.decision_agreement


def test_cache_makes_repeat_companies_free(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    summary = evaluate_policy(FullEnrichment(model=model), sample, make_ctx)
    assert summary.cache_hit_rate > 0.0, "no cache hits — company reuse is not being exercised"


def test_every_policy_satisfies_the_interface(model) -> None:  # type: ignore[no-untyped-def]
    for policy in (
        FullEnrichment(model=model),
        TunedWaterfall(model=model),
        AdaptiveVoI(model=model, disqualifier_rate=0.07),
        CalibratedBoundsPolicy(model=model),
    ):
        assert isinstance(policy.name, str)
        assert callable(policy.run)


# --- regret and verdict ------------------------------------------------------


def _summary(name: str, leads: list[EvalLead], decisions: list[Decision], cost: float):  # type: ignore[no-untyped-def]
    from arie.policy.base import PolicyOutcome
    from arie.policy.evidence_view import score_results

    scoring = score_results({})
    outcomes = [
        PolicyOutcome(
            decision=decision,
            confidence=0.5,
            autonomous=False,
            providers_called=(),
            cost_usd=cost,
            latency_ms=0.0,
            cache_hits=0,
            stop_reason="test",
            scoring=scoring,
        )
        for decision in decisions
    ]
    return summarise(name, leads, outcomes)


def test_regret_separates_improvement_from_harm(test_split: list[EvalLead]) -> None:
    leads = sorted(test_split[:4], key=lambda x: x.eval_lead_id)
    oracle = [x.oracle_decision for x in leads]
    flipped = [Decision.REJECT if d is not Decision.REJECT else Decision.AUTO_ROUTE for d in oracle]

    reference = _summary("ref", leads, list(oracle), cost=1.0)
    policy = _summary("cheap", leads, flipped, cost=0.5)
    regret = counterfactual_regret(reference, policy)

    assert regret.changed == len(leads)
    assert regret.worsened == len(leads)
    assert regret.improved == 0
    assert regret.cost_saved_pct == pytest.approx(0.5)


def test_identical_decisions_produce_no_regret(test_split: list[EvalLead]) -> None:
    leads = sorted(test_split[:5], key=lambda x: x.eval_lead_id)
    oracle = [x.oracle_decision for x in leads]
    reference = _summary("ref", leads, list(oracle), cost=1.0)
    policy = _summary("same", leads, list(oracle), cost=0.4)
    regret = counterfactual_regret(reference, policy)

    assert regret.changed == 0
    assert regret.worsened == 0
    assert regret.net_agreement_delta == 0.0


def test_verdict_reports_a_weak_result(test_split: list[EvalLead]) -> None:
    """A cheap policy that degrades quality must not be scored as a win."""
    leads = sorted(test_split[:100], key=lambda x: x.eval_lead_id)
    oracle = [x.oracle_decision for x in leads]
    degraded = [Decision.REJECT] * len(leads)

    full = _summary("full", leads, list(oracle), cost=1.0)
    waterfall = _summary("waterfall", leads, list(oracle), cost=0.9)
    adaptive = _summary("adaptive", leads, degraded, cost=0.01)

    verdict = evaluate_verdict(full, waterfall, adaptive)
    assert verdict.outcome == "WEAK"
    assert not verdict.holds


def test_verdict_falsifies_when_waterfall_is_not_beaten(test_split: list[EvalLead]) -> None:
    """Beating only the naive baseline is explicitly not enough."""
    leads = sorted(test_split[:100], key=lambda x: x.eval_lead_id)
    oracle = [x.oracle_decision for x in leads]

    full = _summary("full", leads, list(oracle), cost=1.0)
    waterfall = _summary("waterfall", leads, list(oracle), cost=0.10)
    adaptive = _summary("adaptive", leads, list(oracle), cost=0.50)

    verdict = evaluate_verdict(full, waterfall, adaptive)
    assert verdict.outcome == "THESIS_FALSIFIED"
    assert not verdict.holds


def test_verdict_recognises_a_strong_result(test_split: list[EvalLead]) -> None:
    leads = sorted(test_split[:100], key=lambda x: x.eval_lead_id)
    oracle = [x.oracle_decision for x in leads]

    full = _summary("full", leads, list(oracle), cost=1.0)
    waterfall = _summary("waterfall", leads, list(oracle), cost=0.9)
    adaptive = _summary("adaptive", leads, list(oracle), cost=0.2)

    verdict = evaluate_verdict(full, waterfall, adaptive)
    assert verdict.outcome == "THESIS_HOLDS_STRONGLY"
    assert verdict.holds
