"""``classify_numeric_agreement`` — the company-headcount comparison tier.

``classify_field``/``classify_agreement``/``overall_agreement`` (categorical,
exact-match) are already exercised via ``tests/unit/test_provider_bakeoff.py``
and the evaluation-parallel integration suite; this file covers only the
numeric-with-tolerance classifier added for company evidence comparison.
"""

from __future__ import annotations

from arie.live.evaluation import (
    AGREE,
    APPROXIMATE_AGREEMENT,
    CONFLICT,
    UNKNOWN_AGREEMENT,
    classify_numeric_agreement,
)


def test_a_single_usable_value_is_unknown_not_agreement() -> None:
    assert classify_numeric_agreement({"abstract": 3037}) == UNKNOWN_AGREEMENT


def test_no_usable_values_is_unknown() -> None:
    assert classify_numeric_agreement({"abstract": None, "hunter": None}) == UNKNOWN_AGREEMENT


def test_identical_values_agree() -> None:
    assert classify_numeric_agreement({"abstract": 240, "hunter": 240}) == AGREE


def test_values_within_five_percent_agree() -> None:
    assert classify_numeric_agreement({"abstract": 1000, "hunter": 1040}) == AGREE


def test_values_between_five_and_twenty_five_percent_are_approximate() -> None:
    assert classify_numeric_agreement({"abstract": 1000, "hunter": 1200}) == APPROXIMATE_AGREEMENT


def test_the_real_stripe_case_is_a_conflict() -> None:
    """Abstract said 3,037; Hunter's band-lower-bound (post K/M-parser fix)
    said 10,000 — a ~70% gap. Section 9 of the 2026-08-30 stabilization ask:
    this must classify as a real disagreement, not UNKNOWN."""
    assert classify_numeric_agreement({"abstract": 3037, "hunter": 10000}) == CONFLICT


def test_both_exactly_zero_agrees() -> None:
    assert classify_numeric_agreement({"abstract": 0, "hunter": 0}) == AGREE


def test_three_providers_uses_the_widest_pairwise_spread() -> None:
    """Two agree closely; the third is far off — the group must not agree."""
    assert classify_numeric_agreement({"a": 1000, "b": 1010, "c": 5000}) == CONFLICT


def test_thresholds_are_configurable() -> None:
    assert classify_numeric_agreement({"a": 1000, "b": 1200}, approximate_tolerance=0.1) == CONFLICT
