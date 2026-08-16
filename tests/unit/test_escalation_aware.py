"""The escalation-aware policy, and its relationship to the original.

Two properties matter most here. The new policy must reduce *exactly* to the old
one when review is free — otherwise the comparison between them measures an
unrelated change. And it must buy more as review gets expensive, since that is
the entire reason it exists.
"""

from __future__ import annotations

import pytest

from arie.confidence.model import ConfidenceModel, fit_confidence_model
from arie.config import POLICY
from arie.core.types import VoIEvaluation
from arie.evalgen.schema import EvalLead
from arie.policy.adaptive import AdaptiveVoI
from arie.policy.base import EvidenceCache, RunContext
from arie.policy.escalation_aware import EscalationAwareVoI, project_features_after
from arie.policy.evidence_view import score_results
from arie.policy.runner import evaluate_policy
from arie.providers.catalog import BY_NAME
from arie.providers.simulated import CallLedger, build_from_leads


@pytest.fixture(scope="module")
def model(calibration_split: list[EvalLead]) -> ConfidenceModel:
    return fit_confidence_model(
        calibration_split, target_error_rate=POLICY.target_autonomous_error_rate
    )


@pytest.fixture(scope="module")
def make_ctx(leads: list[EvalLead]):  # type: ignore[no-untyped-def]
    _, registry = build_from_leads(leads)

    def factory() -> RunContext:
        return RunContext(registry=registry, ledger=CallLedger(), cache=EvidenceCache())

    return factory


@pytest.fixture(scope="module")
def sample(test_split: list[EvalLead]) -> list[EvalLead]:
    return test_split[:60]


# --- reduction to the original ----------------------------------------------


def test_free_review_reproduces_the_original_policy(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    """At zero review cost the new term vanishes and behaviour must match.

    Without this, any measured difference between the two policies could be an
    incidental implementation divergence rather than the escalation term.
    """
    original = evaluate_policy(
        AdaptiveVoI(model=model, disqualifier_rate=0.07, value_scale=1.0), sample, make_ctx
    )
    reduced = evaluate_policy(
        EscalationAwareVoI(
            model=model, disqualifier_rate=0.07, value_scale=1.0, human_review_usd=0.0
        ),
        sample,
        make_ctx,
    )
    assert reduced.mean_cost_usd == pytest.approx(original.mean_cost_usd)
    assert reduced.decision_agreement == pytest.approx(original.decision_agreement)
    assert reduced.mean_calls == pytest.approx(original.mean_calls)


def test_escalation_value_defaults_to_zero() -> None:
    """The original policy must be unaffected by the new field existing."""
    evaluation = VoIEvaluation(
        candidate_provider="x",
        p_flips_decision=0.5,
        business_value=10.0,
        expected_cost=1.0,
        latency_penalty=0.5,
    )
    assert evaluation.escalation_value == 0.0
    assert evaluation.net_evoi == pytest.approx(0.5 * 10.0 - 1.0 - 0.5)


def test_escalation_value_enters_the_net_score() -> None:
    base = VoIEvaluation("x", 0.1, 10.0, 1.0, 0.5)
    with_escalation = VoIEvaluation("x", 0.1, 10.0, 1.0, 0.5, escalation_value=2.0)
    assert with_escalation.net_evoi == pytest.approx(base.net_evoi + 2.0)


# --- responsiveness to review price -----------------------------------------


def test_expensive_review_buys_more_evidence(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    """The reason the policy exists.

    Skipped when the confidence model cannot certify any threshold: with
    tau above 1 no amount of evidence produces autonomy, so avoiding a review is
    impossible and buying more is correctly worthless.
    """
    if model.tau > 1.0:
        pytest.skip("no achievable tau on this calibration split; autonomy is unreachable")

    spend = [
        evaluate_policy(
            EscalationAwareVoI(
                model=model, disqualifier_rate=0.07, value_scale=1.0, human_review_usd=price
            ),
            sample,
            make_ctx,
        ).mean_cost_usd
        for price in (0.0, 1.0, 5.0)
    ]
    assert spend == sorted(spend), f"spend must not fall as review gets pricier: {spend}"
    assert spend[-1] > spend[0]


def test_expensive_review_raises_autonomy(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    if model.tau > 1.0:
        pytest.skip("no achievable tau on this calibration split")

    cheap = evaluate_policy(
        EscalationAwareVoI(
            model=model, disqualifier_rate=0.07, value_scale=1.0, human_review_usd=0.0
        ),
        sample,
        make_ctx,
    )
    dear = evaluate_policy(
        EscalationAwareVoI(
            model=model, disqualifier_rate=0.07, value_scale=1.0, human_review_usd=5.0
        ),
        sample,
        make_ctx,
    )
    assert dear.autonomous_rate >= cheap.autonomous_rate


def test_budget_cap_still_binds(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    """A large review price must not let the policy spend without limit."""
    ctx = make_ctx()
    policy = EscalationAwareVoI(
        model=model,
        disqualifier_rate=0.07,
        budget_usd_cap=0.05,
        value_scale=1.0,
        human_review_usd=1000.0,
    )
    for lead in sample[:20]:
        assert policy.run(lead, ctx).cost_usd <= 0.05 + 1e-9


def test_policy_is_deterministic(sample, make_ctx, model) -> None:  # type: ignore[no-untyped-def]
    def build() -> EscalationAwareVoI:
        return EscalationAwareVoI(
            model=model, disqualifier_rate=0.07, value_scale=1.0, human_review_usd=2.5
        )

    first = evaluate_policy(build(), sample, make_ctx)
    second = evaluate_policy(build(), sample, make_ctx)
    assert first.mean_cost_usd == second.mean_cost_usd
    assert first.decision_agreement == second.decision_agreement


# --- the projection ----------------------------------------------------------


def test_projection_increases_completeness(test_split: list[EvalLead]) -> None:
    from arie.confidence.features import FEATURE_NAMES, extract_features

    scoring = score_results({})
    spec = BY_NAME["firmographics_basic"]
    before = extract_features(scoring)
    after = dict(zip(FEATURE_NAMES, project_features_after(scoring, spec), strict=True))

    assert after["completeness"] > before["completeness"]
    assert after["unknown_field_ratio"] < before["unknown_field_ratio"]
    assert after["bounds_width"] <= before["bounds_width"]


def test_projection_is_a_noop_when_nothing_new_is_offered() -> None:
    """A provider that repeats what we know cannot move the projection."""
    from arie.confidence.features import FEATURE_NAMES, extract_features

    spec = BY_NAME["firmographics_basic"]
    known: dict[str, object] = dict.fromkeys(spec.provides_fields, "software")
    known["employee_count"] = 120
    scoring = score_results({})
    # Build a scoring result that already knows this provider's fields.
    from arie.scoring.engine import score_resolved

    resolved = score_resolved(known)
    projected = dict(zip(FEATURE_NAMES, project_features_after(resolved, spec), strict=True))
    assert projected == extract_features(resolved)
    assert scoring is not resolved  # sanity: distinct states


def test_projection_stays_within_feature_bounds(test_split: list[EvalLead]) -> None:
    from arie.confidence.features import FEATURE_NAMES

    scoring = score_results({})
    for name in ("deep_research", "intent_signals", "inbound_payload"):
        values = project_features_after(scoring, BY_NAME[name])
        assert len(values) == len(FEATURE_NAMES)
        assert all(0.0 <= v <= 1.0 for v in values)
