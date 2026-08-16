"""Scoring mechanics — tested against a small synthetic corpus, not the real
labeled samples. `arie.llm.eval`'s actual corpus can grow or be relabeled
without touching what's under test here: that the *scoring arithmetic* is
correct, independent of what it happens to be scoring.
"""

from __future__ import annotations

import pytest

from arie.llm.eval import compute_delta, evaluate_extractor
from arie.llm.schema import ExpectedLabel, ExtractedSignal, LabeledSample

_SAMPLES = (
    LabeledSample("a", "text a", ExpectedLabel(True, "funding_event", False)),
    LabeledSample("b", "text b", ExpectedLabel(False, None, True)),
    LabeledSample("c", "text c", ExpectedLabel(False, None, False)),
)


def _signal(has_buying_intent: bool, trigger: str | None, disqualifying: bool) -> ExtractedSignal:
    return ExtractedSignal(
        has_buying_intent=has_buying_intent,
        trigger_event_category=trigger,  # type: ignore[arg-type]
        disqualifying_signal=disqualifying,
        confidence=1.0,
        rationale="synthetic",
    )


def test_a_perfect_extractor_scores_every_field_at_100_percent() -> None:
    answers = {
        "a": _signal(True, "funding_event", False),
        "b": _signal(False, None, True),
        "c": _signal(False, None, False),
    }
    report = evaluate_extractor("perfect", lambda text: answers[text[-1]], _SAMPLES)

    assert report.buying_intent.accuracy == 1.0
    assert report.trigger_category.accuracy == 1.0
    assert report.disqualifying_signal.accuracy == 1.0
    assert report.exact_match.accuracy == 1.0
    assert report.failed_extractions == ()


def test_an_extractor_that_always_returns_none_scores_zero_not_excluded() -> None:
    """A failed extraction must count against the denominator, not be dropped
    from it — otherwise an extractor that gives up on every hard case would
    report a perfect score on an empty set of "answered" questions."""
    report = evaluate_extractor("always fails", lambda _text: None, _SAMPLES)

    assert report.n_samples == 3
    assert report.buying_intent.accuracy == 0.0
    assert report.trigger_category.accuracy == 0.0
    assert report.disqualifying_signal.accuracy == 0.0
    assert report.exact_match.accuracy == 0.0
    assert report.failed_extractions == ("a", "b", "c")


def test_exact_match_requires_every_field_correct_not_a_majority() -> None:
    """One field wrong is enough to fail exact_match, even if the other two
    are right — a 2-out-of-3 answer is not "close enough" for a signal a
    future caller might act on."""
    almost_right = _signal(True, "funding_event", True)  # disqualifying flipped
    report = evaluate_extractor("almost right", lambda _text: almost_right, _SAMPLES[:1])

    assert report.buying_intent.accuracy == 1.0
    assert report.trigger_category.accuracy == 1.0
    assert report.disqualifying_signal.accuracy == 0.0
    assert report.exact_match.accuracy == 0.0


def test_partial_correctness_is_scored_per_field_independently() -> None:
    def extractor(text: str) -> ExtractedSignal:
        # Gets buying_intent right on every sample, everything else wrong.
        return _signal(text.endswith("a"), "leadership_change", not text.endswith("b"))

    report = evaluate_extractor("mixed", extractor, _SAMPLES)

    assert report.buying_intent.accuracy == 1.0
    assert report.trigger_category.correct == 0
    assert report.disqualifying_signal.correct == 0


def test_compute_delta_is_challenger_minus_baseline() -> None:
    perfect = evaluate_extractor(
        "perfect",
        lambda text: {
            "a": _signal(True, "funding_event", False),
            "b": _signal(False, None, True),
            "c": _signal(False, None, False),
        }[text[-1]],
        _SAMPLES,
    )
    always_wrong = evaluate_extractor(
        "wrong", lambda _text: _signal(False, "expansion", False), _SAMPLES
    )

    delta = compute_delta(baseline=always_wrong, challenger=perfect)

    assert delta.exact_match_delta > 0, "the better extractor must show a positive delta"
    assert delta.disqualifying_signal_delta == 1.0 - always_wrong.disqualifying_signal.accuracy

    reversed_delta = compute_delta(baseline=perfect, challenger=always_wrong)
    assert reversed_delta.exact_match_delta < 0, "reversing baseline/challenger flips the sign"


def test_compute_delta_rejects_mismatched_corpora() -> None:
    full = evaluate_extractor("full", lambda _text: None, _SAMPLES)
    partial = evaluate_extractor("partial", lambda _text: None, _SAMPLES[:1])

    with pytest.raises(ValueError, match="not comparable"):
        compute_delta(baseline=full, challenger=partial)
