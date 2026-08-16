"""The deterministic baseline — pure functions, no mocking needed.

These pin the phrase-matching behaviour itself, including its known failure
modes (negation blindness, a keyword matching in the wrong context) — the
baseline is *supposed* to be beatable in exactly these ways, and a test suite
for it should say so rather than only testing the cases it gets right.
"""

from __future__ import annotations

from arie.llm.baseline import extract_signal_deterministic
from arie.llm.eval import LABELED_SAMPLES, evaluate_extractor


def test_matches_a_buying_intent_phrase() -> None:
    result = extract_signal_deterministic("We want to get started as soon as possible.")
    assert result.has_buying_intent is True
    assert result.disqualifying_signal is False
    assert result.trigger_event_category is None


def test_matches_a_disqualifying_phrase() -> None:
    result = extract_signal_deterministic("Please remove me, I'm not interested.")
    assert result.disqualifying_signal is True
    assert result.has_buying_intent is False


def test_matches_each_trigger_category() -> None:
    cases = {
        "funding_event": "We just closed our Series B round.",
        "leadership_change": "We hired a VP of Sales last quarter.",
        "expansion": "We opened a new office in Denver.",
        "product_or_technology_change": "We are replacing our legacy platform.",
    }
    for expected_category, text in cases.items():
        result = extract_signal_deterministic(text)
        assert result.trigger_event_category == expected_category, text


def test_no_phrase_present_yields_all_false() -> None:
    result = extract_signal_deterministic("What are your office hours?")
    assert result.has_buying_intent is False
    assert result.disqualifying_signal is False
    assert result.trigger_event_category is None
    assert result.trigger_event_detail is None


def test_confidence_is_always_certain() -> None:
    """A rule-based match makes no claim about the world, only about itself:
    the rule either fired or it didn't, which is why confidence is always 1.0
    rather than an attempt at calibration a keyword list can't back up."""
    assert extract_signal_deterministic("hello").confidence == 1.0
    assert extract_signal_deterministic("we want to get started").confidence == 1.0


def test_matching_is_case_insensitive() -> None:
    assert extract_signal_deterministic("WE WANT TO GET STARTED").has_buying_intent is True


def test_rationale_names_what_matched() -> None:
    result = extract_signal_deterministic("We want to get started right away.")
    assert "want to get started" in result.rationale
    result_empty = extract_signal_deterministic("hello")
    assert "no phrase" in result_empty.rationale


def test_known_failure_mode_negation_is_not_understood() -> None:
    """The baseline has no grammar — this is exactly the gap the LLM comparison
    exists to measure, not a bug to fix in the baseline itself. Fixing it would
    make the baseline a worse fair comparison, not a better one."""
    result = extract_signal_deterministic("We are not ready to buy yet.")
    assert result.has_buying_intent is True, (
        "documents the known false positive: 'ready to buy' matches even negated"
    )


def test_known_failure_mode_keyword_in_wrong_context() -> None:
    result = extract_signal_deterministic(
        "A friend who works in procurement at another company recommended I reach out."
    )
    assert result.has_buying_intent is True, (
        "documents the known false positive: 'procurement' matches regardless of whose"
    )


def test_the_labeled_corpus_is_not_degenerate_for_the_baseline() -> None:
    """Guards the corpus itself, not the baseline: if every sample happened to
    use exactly the baseline's own phrase list (or none of it), the delta
    measurement in bench/llm_signal_eval.py would be measuring something
    trivial. The baseline must get a genuine mix of hits and misses.
    """
    report = evaluate_extractor("baseline", extract_signal_deterministic)
    assert report.n_samples == len(LABELED_SAMPLES)
    for field in (report.buying_intent, report.trigger_category, report.disqualifying_signal):
        assert 0 < field.correct < field.total, (
            f"{field.field} is all-right or all-wrong ({field.correct}/{field.total}) — "
            "the corpus should mix phrases the baseline catches with ones it doesn't"
        )
    assert 0 < report.exact_match.correct < report.exact_match.total
    assert not report.failed_extractions, "the baseline function never returns None"
