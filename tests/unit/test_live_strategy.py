"""The live strategy vocabulary, the agreement classifier, and the order knob.

Everything here is pure — the strategies' *behaviour* (parallel calls, budget,
cooldown, receipts) lives in ``tests/integration/test_live_evaluation_integration.py``
against a real database.
"""

from __future__ import annotations

import pytest

from arie.config import LiveBudgetConfig, LiveStrategyConfig
from arie.live.evaluation import (
    AGREE,
    CONFLICT,
    PARTIAL,
    UNKNOWN_AGREEMENT,
    classify_agreement,
    classify_field,
    overall_agreement,
)
from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES, acquisition_order
from arie.live.strategy import (
    EVALUATION_PARALLEL,
    EVALUATION_POLICY_NAME,
    LEGACY_LIVE_POLICY_NAME,
    LIVE_POLICY_NAMES,
    OPTIMIZED,
    OPTIMIZED_POLICY_NAME,
    UnsupportedLiveStrategyError,
    resolve_strategy,
)
from arie.providers.base import EnrichmentProvider

# ----------------------------------------------------------------- strategy --


def test_the_default_strategy_is_optimized() -> None:
    """Calling every provider forever would defeat ARIE's purpose — the
    experiment mode must always be an explicit opt-in, never what a deployment
    gets by not setting a variable."""
    assert resolve_strategy(LiveStrategyConfig(strategy="optimized")) == OPTIMIZED
    assert LiveStrategyConfig().strategy == OPTIMIZED


def test_evaluation_parallel_resolves() -> None:
    assert (
        resolve_strategy(LiveStrategyConfig(strategy="evaluation_parallel")) == EVALUATION_PARALLEL
    )


def test_a_typo_fails_loudly_at_resolution_not_silently_at_runtime() -> None:
    with pytest.raises(UnsupportedLiveStrategyError, match="parallel_evaluation"):
        resolve_strategy(LiveStrategyConfig(strategy="parallel_evaluation"))


def test_a_bad_strategy_value_does_not_crash_config_construction() -> None:
    """The lazy-validation property the simulated demo depends on: a garbage
    LIVE_PROVIDER_STRATEGY constructs fine (config is imported everywhere) and
    only *resolving* it — which only the live builder does — raises."""
    config = LiveStrategyConfig(strategy="garbage")
    assert config.strategy == "garbage"
    with pytest.raises(UnsupportedLiveStrategyError):
        resolve_strategy(config)


def test_every_policy_name_generation_is_recognised_as_live() -> None:
    """Stored receipts carry policy names forever; the recognised set must
    cover all three generations or old receipts silently lose their live
    catalogue on the receipt endpoint."""
    assert {
        LEGACY_LIVE_POLICY_NAME,
        OPTIMIZED_POLICY_NAME,
        EVALUATION_POLICY_NAME,
    } == LIVE_POLICY_NAMES


# ---------------------------------------------------------- evaluation budget --


def test_the_evaluation_budget_is_a_separate_cap_not_a_bypass() -> None:
    budget = LiveBudgetConfig(daily_usd=2.0, per_lead_usd=0.05, evaluation_per_lead_usd=0.10)
    evaluation = budget.for_evaluation()
    assert evaluation.per_lead_usd == pytest.approx(0.10)
    assert evaluation.daily_usd == pytest.approx(2.0)  # the shared ceiling survives


def test_an_evaluation_cap_above_the_daily_cap_is_refused_when_used() -> None:
    """Refused at ``for_evaluation()`` — the moment the evaluation budget is
    actually taken up — rather than at construction, so the legitimate
    zero-daily "block all spending" config stays constructible while an
    evaluation worker with a nonsensical cap still refuses to start."""
    config = LiveBudgetConfig(daily_usd=0.05, per_lead_usd=0.01, evaluation_per_lead_usd=0.10)
    with pytest.raises(ValueError, match="LIVE_EVALUATION_PER_LEAD_BUDGET_USD"):
        config.for_evaluation()


def test_a_zero_daily_budget_config_is_still_constructible() -> None:
    """The panic-stop configuration: daily 0 blocks every call. The evaluation
    default must not make it unconstructible."""
    config = LiveBudgetConfig(daily_usd=0.0, per_lead_usd=0.0)
    assert config.daily_usd == 0.0


# -------------------------------------------------------------- order override --


def _stub(name: str) -> EnrichmentProvider:
    from tests.unit.test_live_provider_order import _StubProvider

    provider: EnrichmentProvider = _StubProvider(name=name)
    return provider


def test_the_order_override_reorders_registered_providers() -> None:
    abstract, hunter, apollo = REGISTERED_LIVE_PROVIDER_NAMES
    providers = [_stub(abstract), _stub(hunter), _stub(apollo)]
    reordered = acquisition_order(providers, order=(apollo, hunter))
    assert [p.name for p in reordered] == [apollo, hunter, abstract]


def test_an_order_listing_a_subset_keeps_unlisted_providers_after_in_default_order() -> None:
    abstract, hunter, apollo = REGISTERED_LIVE_PROVIDER_NAMES
    providers = [_stub(apollo), _stub(abstract), _stub(hunter)]
    reordered = acquisition_order(providers, order=(hunter,))
    assert [p.name for p in reordered] == [hunter, abstract, apollo]


def test_a_typoed_order_name_raises_instead_of_silently_running_the_default() -> None:
    """A misconfigured experiment that quietly runs the default order would
    produce data labelled as one waterfall and measured on another."""
    with pytest.raises(ValueError, match="unregistered"):
        acquisition_order([], order=("hunter_combined_enrichmnet",))


def test_no_override_means_the_registered_default() -> None:
    abstract, hunter, apollo = REGISTERED_LIVE_PROVIDER_NAMES
    providers = [_stub(apollo), _stub(hunter), _stub(abstract)]
    assert [p.name for p in acquisition_order(providers)] == [
        abstract,
        hunter,
        apollo,
    ]


# ------------------------------------------------------------ agreement rules --


def test_matching_canonical_values_agree() -> None:
    assert classify_field({"hunter": "vp", "apollo": "vp"}) == AGREE


def test_different_canonical_values_conflict() -> None:
    assert classify_field({"hunter": "director", "apollo": "vp"}) == CONFLICT


def test_one_usable_answer_is_partial_coverage_not_agreement() -> None:
    """One voice cannot corroborate itself — PARTIAL is a coverage fact, and
    counting it as agreement would inflate every agreement metric with cases
    where no comparison happened."""
    assert classify_field({"hunter": "vp", "apollo": None}) == PARTIAL
    assert classify_field({"hunter": "vp"}) == PARTIAL


def test_the_unknown_sentinel_counts_as_no_answer() -> None:
    assert classify_field({"hunter": "unknown", "apollo": "vp"}) == PARTIAL
    assert classify_field({"hunter": "unknown", "apollo": None}) == UNKNOWN_AGREEMENT


def test_per_field_classification_reads_provider_result_shaped_dicts() -> None:
    verdicts = classify_agreement(
        {
            "hunter": {"title_seniority": "vp", "title_function": "sales"},
            "apollo": {"title_seniority": "vp", "title_function": "marketing"},
        },
        ("title_seniority", "title_function"),
    )
    assert verdicts == {"title_seniority": AGREE, "title_function": CONFLICT}


def test_the_overall_rollup_is_worst_first() -> None:
    """A single conflicted field makes the comparison a conflict — the roll-up
    exists to route attention, and 'mostly agreed' is exactly how a real
    disagreement gets ignored."""
    assert overall_agreement({"a": AGREE, "b": CONFLICT}) == CONFLICT
    assert overall_agreement({"a": AGREE, "b": PARTIAL}) == AGREE
    assert overall_agreement({"a": PARTIAL, "b": UNKNOWN_AGREEMENT}) == PARTIAL
    assert overall_agreement({}) == UNKNOWN_AGREEMENT


def test_no_rule_anywhere_prefers_one_vendor() -> None:
    """Symmetry check: swapping the providers' labels never changes a verdict.
    Choosing a winner is the bake-off's job, on measured data — not a
    classification default."""
    pairs = [("vp", "director"), ("vp", "vp"), ("vp", None), (None, None)]
    for hunter_value, apollo_value in pairs:
        forward = classify_field({"hunter": hunter_value, "apollo": apollo_value})
        backward = classify_field({"hunter": apollo_value, "apollo": hunter_value})
        assert forward == backward
