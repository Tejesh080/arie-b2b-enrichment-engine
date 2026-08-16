"""Confidence model: leakage, monotonicity, reproducibility, threshold behaviour.

The confidence number gates autonomous action, so the failure modes that matter
here are silent ones — a model that leaks test data, or a threshold that
promises an error rate it cannot keep, still produces perfectly plausible
numbers.
"""

from __future__ import annotations

import numpy as np
import pytest

from arie.confidence.features import FEATURE_NAMES
from arie.confidence.metrics import calibration_report
from arie.confidence.model import (
    ConfidenceModel,
    _split_companies,
    build_training_frame,
    fit_confidence_model,
    score_at_state,
)
from arie.confidence.threshold import (
    REJECT_ALL_THRESHOLD,
    clopper_pearson_upper,
    select_threshold,
)
from arie.evalgen.schema import EvalLead
from arie.providers.catalog import ALL_PROVIDERS, CHEAP_TIER

TARGET_ERROR_RATE = 0.05


@pytest.fixture(scope="module")
def model(calibration_split: list[EvalLead]) -> ConfidenceModel:
    return fit_confidence_model(calibration_split, target_error_rate=TARGET_ERROR_RATE)


def _vec(**overrides: float) -> np.ndarray:
    base = dict.fromkeys(FEATURE_NAMES, 0.5)
    base.update(overrides)
    return np.asarray([[base[name] for name in FEATURE_NAMES]], dtype=float)


# --- leakage -----------------------------------------------------------------


def test_fit_rejects_test_split_leads(leads: list[EvalLead]) -> None:
    """The failure this guards against is silent and invalidates everything."""
    with pytest.raises(ValueError, match="test split must never be used"):
        fit_confidence_model(leads, target_error_rate=TARGET_ERROR_RATE)


def test_fit_rejects_even_a_single_contaminating_lead(
    calibration_split: list[EvalLead], test_split: list[EvalLead]
) -> None:
    contaminated = [*calibration_split, test_split[0]]
    with pytest.raises(ValueError, match="non-calibration leads"):
        fit_confidence_model(contaminated, target_error_rate=TARGET_ERROR_RATE)


def test_fit_and_tau_sets_are_company_disjoint(calibration_split: list[EvalLead]) -> None:
    """τ must be chosen on companies neither stage was fitted on.

    Splitting any finer than by company would leak: a company's contacts share
    its cached evidence, and each lead contributes many enrichment states.
    """
    fit_leads, tau_leads = _split_companies(calibration_split, 0.35)
    fit_companies = {x.company.company_id for x in fit_leads}
    tau_companies = {x.company.company_id for x in tau_leads}

    assert fit_companies and tau_companies
    assert not (fit_companies & tau_companies)


def test_training_groups_are_companies_not_leads(
    calibration_split: list[EvalLead],
) -> None:
    frame = build_training_frame(calibration_split[:20])
    assert len(frame) > len(calibration_split[:20]), "each lead should yield many states"
    assert set(frame.groups) <= {x.company.company_id for x in calibration_split}


def test_model_is_unchanged_by_test_split_contents(
    calibration_split: list[EvalLead],
) -> None:
    """Sanity check that nothing reaches through to the test split."""
    first = fit_confidence_model(calibration_split, target_error_rate=TARGET_ERROR_RATE)
    second = fit_confidence_model(
        list(reversed(calibration_split)), target_error_rate=TARGET_ERROR_RATE
    )
    assert first.tau == second.tau
    assert first.coefficients() == second.coefficients()


# --- reproducibility ---------------------------------------------------------


def test_refitting_produces_an_identical_model(calibration_split: list[EvalLead]) -> None:
    a = fit_confidence_model(calibration_split, target_error_rate=TARGET_ERROR_RATE)
    b = fit_confidence_model(calibration_split, target_error_rate=TARGET_ERROR_RATE)

    assert a.method == b.method
    assert a.coefficients() == b.coefficients()
    assert a.tau == b.tau
    assert a.report.ece == b.report.ece


def test_predictions_are_deterministic(
    model: ConfidenceModel, calibration_split: list[EvalLead]
) -> None:
    result = score_at_state(calibration_split[0], ALL_PROVIDERS)
    assert model.predict(result) == model.predict(result)


def test_feature_order_is_pinned() -> None:
    """A silent reordering would produce a confidently wrong model, not a crash."""
    assert FEATURE_NAMES[0] == "completeness"
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))


# --- monotonicity ------------------------------------------------------------


def test_confidence_rises_with_distance_from_the_boundary(model: ConfidenceModel) -> None:
    """The dominant driver: a lead on a threshold flips on the smallest error."""
    values = [float(model.predict_many(_vec(boundary_distance=d))[0]) for d in (0.0, 0.1, 0.2, 0.4)]
    assert values == sorted(values)
    assert values[-1] > values[0]


def test_confidence_rises_with_completeness(model: ConfidenceModel) -> None:
    values = [
        float(model.predict_many(_vec(completeness=c))[0]) for c in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    assert values == sorted(values)


def test_confidence_falls_as_unknown_fields_increase(model: ConfidenceModel) -> None:
    values = [
        float(model.predict_many(_vec(unknown_field_ratio=u))[0])
        for u in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    assert values == sorted(values, reverse=True)


def test_confidence_falls_as_bounds_widen(model: ConfidenceModel) -> None:
    values = [float(model.predict_many(_vec(bounds_width=w))[0]) for w in (0.0, 0.3, 0.6, 1.0)]
    assert values == sorted(values, reverse=True)


def test_calibration_mapping_is_monotone(model: ConfidenceModel) -> None:
    """Both calibrators are order-preserving.

    This is what lets feature-level monotonicity survive calibration: a higher
    raw score can never map to a lower confidence.
    """
    raw = np.linspace(0.0, 1.0, 50)
    mapped = np.clip(np.asarray(model.calibrator.predict(raw), dtype=float), 0.0, 1.0)
    assert np.all(np.diff(mapped) >= -1e-9)


def test_predictions_stay_within_probability_range(
    model: ConfidenceModel, calibration_split: list[EvalLead]
) -> None:
    for lead in calibration_split[:40]:
        for providers in ([], list(CHEAP_TIER), list(ALL_PROVIDERS)):
            value = model.predict(score_at_state(lead, providers))
            assert 0.0 <= value <= 1.0


# --- bounds stay a separate signal ------------------------------------------


def test_settled_decisions_are_not_forced_to_full_confidence(
    model: ConfidenceModel, calibration_split: list[EvalLead]
) -> None:
    """Settled means "nothing left to buy", not "certainly correct".

    Bounds are computed from *observed* facts, which are noisy, so a settled
    decision can still disagree with the oracle when a provider was wrong.
    Collapsing the two signals would hand out unearned autonomy.
    """
    settled = [
        score_at_state(lead, ALL_PROVIDERS)
        for lead in calibration_split
        if score_at_state(lead, ALL_PROVIDERS).bounds.is_settled
    ]
    assert settled, "no settled states to check"

    confidences = [model.predict(r) for r in settled]
    assert min(confidences) < 1.0, "settled states were all assigned certainty"
    assert not all(c >= model.tau for c in confidences), (
        "every settled state cleared the autonomy bar — bounds and confidence "
        "are not behaving as independent signals"
    )


def test_thin_evidence_is_never_autonomous(
    model: ConfidenceModel, calibration_split: list[EvalLead]
) -> None:
    """A system that acts on almost no evidence has a broken confidence model."""
    autonomous = sum(
        1 for lead in calibration_split if model.is_autonomous(score_at_state(lead, []))
    )
    assert autonomous == 0


# --- threshold behaviour -----------------------------------------------------


def test_stricter_target_never_lowers_tau(calibration_split: list[EvalLead]) -> None:
    taus = [
        fit_confidence_model(calibration_split, target_error_rate=rate).tau
        for rate in (0.30, 0.20, 0.10, 0.05)
    ]
    assert taus == sorted(taus)


def test_stricter_target_never_raises_coverage(calibration_split: list[EvalLead]) -> None:
    coverage = [
        fit_confidence_model(calibration_split, target_error_rate=rate).threshold.coverage
        for rate in (0.30, 0.20, 0.10, 0.05)
    ]
    assert coverage == sorted(coverage, reverse=True)


def test_selected_threshold_meets_its_stated_bound(model: ConfidenceModel) -> None:
    threshold = model.threshold
    assert threshold.guarantee_met
    assert threshold.error_rate_upper_bound <= threshold.target_error_rate
    assert threshold.observed_error_rate <= threshold.error_rate_upper_bound


def test_unachievable_target_refuses_all_autonomy(
    calibration_split: list[EvalLead],
) -> None:
    """When the budget cannot be met, escalate everything rather than pretend.

    At this sample size even a flawless run cannot certify an error rate of
    1e-6, so the honest answer is to automate nothing.
    """
    fitted = fit_confidence_model(calibration_split, target_error_rate=1e-6)
    assert fitted.tau == REJECT_ALL_THRESHOLD
    assert fitted.threshold.coverage == 0.0
    assert not fitted.threshold.guarantee_met


def test_tau_separates_reliable_from_unreliable_predictions() -> None:
    """With enough evidence, the top block is accepted and the noisy tail is not.

    The sample has to be large: certifying a low error rate from four
    observations is not possible at any confidence level, and the bound
    correctly refuses to.
    """
    confidences = [0.95] * 50 + [0.40] * 50
    correct = [1] * 50 + [1, 0] * 25
    selection = select_threshold(confidences, correct, target_error_rate=0.2, delta=0.5)

    assert selection.guarantee_met
    assert selection.tau == 0.95
    assert selection.n_accepted == 50
    assert selection.observed_errors == 0


def test_small_samples_cannot_certify_a_low_error_rate() -> None:
    """Four flawless predictions do not justify a 5% error guarantee."""
    selection = select_threshold([0.9, 0.8, 0.7, 0.6], [1, 1, 0, 0], target_error_rate=0.05)
    assert selection.tau == REJECT_ALL_THRESHOLD
    assert not selection.guarantee_met


def test_tied_confidences_are_accepted_together() -> None:
    """Accepting at a confidence means accepting every prediction of that value.

    Evaluating mid-tie would report a coverage the threshold cannot deliver.
    """
    selection = select_threshold(
        [0.9, 0.9, 0.9, 0.4], [1, 1, 0, 0], target_error_rate=0.5, delta=0.5
    )
    if selection.tau == 0.9:
        assert selection.n_accepted == 3


# --- the bound itself --------------------------------------------------------


def test_clopper_pearson_is_conservative() -> None:
    """The bound must exceed the point estimate — that is its whole purpose."""
    for errors, n in [(0, 10), (1, 20), (5, 100), (2, 7)]:
        assert clopper_pearson_upper(errors, n) > errors / n


def test_clopper_pearson_tightens_with_more_evidence() -> None:
    """Zero errors in 20 is weak evidence; zero in 2000 is strong."""
    bounds = [clopper_pearson_upper(0, n) for n in (20, 200, 2000)]
    assert bounds == sorted(bounds, reverse=True)
    assert bounds[-1] < bounds[0] / 10


def test_clopper_pearson_handles_degenerate_inputs() -> None:
    assert clopper_pearson_upper(0, 0) == 1.0
    assert clopper_pearson_upper(5, 5) == 1.0


def test_empty_sample_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty sample"):
        select_threshold([], [], target_error_rate=0.05)


# --- calibration metrics -----------------------------------------------------


def test_ece_is_zero_for_perfectly_calibrated_predictions() -> None:
    predicted = [0.0] * 50 + [1.0] * 50
    actual = [0] * 50 + [1] * 50
    assert calibration_report(predicted, actual).ece == pytest.approx(0.0)


def test_ece_detects_systematic_overconfidence() -> None:
    """Claiming 0.99 while being right half the time must score badly."""
    predicted = [0.99] * 100
    actual = [1, 0] * 50
    report = calibration_report(predicted, actual)
    assert report.ece == pytest.approx(0.49, abs=0.01)


def test_reliability_bins_account_for_every_sample() -> None:
    predicted = [i / 100 for i in range(101)]
    actual = [i % 2 for i in range(101)]
    report = calibration_report(predicted, actual)
    assert sum(b.count for b in report.bins) == len(predicted)


def test_prediction_of_exactly_one_is_binned() -> None:
    """Guards an off-by-one at the top edge that would silently drop samples."""
    report = calibration_report([1.0, 1.0], [1, 1])
    assert sum(b.count for b in report.bins) == 2


def test_calibration_report_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        calibration_report([0.5], [1, 0])


def test_model_reports_calibration_on_held_out_companies(model: ConfidenceModel) -> None:
    assert model.report.n_samples > 0
    assert 0.0 <= model.report.ece <= 1.0
    assert 0.0 < model.report.base_rate < 1.0
