"""The bake-off harness — metrics math, spend discipline, and the mock run.

The harness is the instrument the waterfall decision will be made with, so its
arithmetic gets the same treatment as the scorer's: pure functions, asserted on
hand-computable inputs. The end-to-end mock run proves the whole loop —
identities → adapters → records → summary → report — with zero credentials and
zero spend.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import scripts.provider_bakeoff as provider_bakeoff
from scripts.provider_bakeoff import (
    BakeoffIdentity,
    BakeoffRecord,
    _cache_key,
    _percentile,
    _record_from_result,
    agreement_summary,
    build_mock_providers,
    build_real_providers,
    build_summary,
    load_identities,
    overlap_summary,
    render_report,
    run_bakeoff,
    summarize_provider,
)

from arie.config import ApolloPersonConfig, HunterConfig, LiveProviderConfig
from arie.core.types import ProviderResult, ProviderStatus
from arie.providers import live_abstract, live_apollo, live_hunter
from arie.providers.apollo_contract import APOLLO_PROVIDER_NAME
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.providers.live_hunter import HunterEnrichmentProvider

_EXAMPLE_CSV = (
    Path(__file__).parent.parent.parent / "data" / "evaluation" / "identities.example.csv"
)


def _record(
    provider: str,
    email: str,
    *,
    status: str = "success",
    served_from: str = "live_call",
    seniority: str | None = None,
    function: str | None = None,
    raw_title: str | None = None,
    industry: str | None = None,
    employees: int | None = None,
    latency: float = 100.0,
    cost: float = 0.01,
    credits: float | None = 1.0,
    identity_verdict: str | None = None,
    person_evidence_usable: bool | None = None,
) -> BakeoffRecord:
    usable = tuple(
        name
        for name, value in (("title_function", function), ("title_seniority", seniority))
        if value
    )
    return BakeoffRecord(
        provider=provider,
        email=email,
        served_from=served_from,
        status=status,
        raw_title=raw_title,
        title_seniority=seniority,
        title_function=function,
        usable_fields=usable,
        company_industry=industry,
        company_employee_count=employees,
        fields_returned=usable,
        latency_ms=latency,
        cache_hit=False,
        credits_consumed=credits if status == "success" else None,
        cost_usd=cost if status == "success" else 0.0,
        cost_basis="modelled_credit_equivalent" if status == "success" else None,
        error_kind="server_error" if status == "error" else None,
        called_at="2026-08-27T00:00:00",
        identity_verdict=identity_verdict,
        person_evidence_usable=person_evidence_usable,
    )


# ------------------------------------------------------------------- metrics --


def test_provider_summary_rates_are_hand_computable() -> None:
    records = [
        _record(APOLLO_PROVIDER_NAME, "a@x.test", seniority="vp", function="sales", raw_title="VP"),
        _record(APOLLO_PROVIDER_NAME, "b@x.test", status="miss", credits=None),
        _record(APOLLO_PROVIDER_NAME, "c@x.test", status="error", credits=None),
        _record(APOLLO_PROVIDER_NAME, "d@x.test", seniority="vp", raw_title="VP of Something"),
    ]
    summary = summarize_provider(records, APOLLO_PROVIDER_NAME)

    assert summary["identities_attempted"] == 4
    assert summary["match_rate"] == pytest.approx(0.5)
    assert summary["title_return_rate"] == pytest.approx(0.5)
    assert summary["canonical_seniority_rate"] == pytest.approx(0.5)
    assert summary["canonical_function_rate"] == pytest.approx(0.25)
    assert summary["usable_evidence_rate"] == pytest.approx(0.5)
    assert summary["miss_rate"] == pytest.approx(0.25)
    assert summary["error_rate"] == pytest.approx(0.25)
    assert summary["credits_consumed"] == pytest.approx(2.0)
    assert summary["modelled_cost_usd"] == pytest.approx(0.02)
    assert summary["cost_per_match_usd"] == pytest.approx(0.01)
    # 3 usable fields across the two matches -> 0.02 / 3
    assert summary["cost_per_usable_field_usd"] == pytest.approx(0.00667, abs=1e-5)


def test_cache_served_records_count_toward_rates_but_not_spend() -> None:
    """A cached answer is still that provider's answer (rates), but this run
    did not pay for it (cost/credits/latency stay live-only)."""
    records = [
        _record(HUNTER_PROVIDER_NAME, "a@x.test", seniority="vp", function="sales", cost=0.0049),
        _record(
            HUNTER_PROVIDER_NAME,
            "b@x.test",
            served_from="cache_file",
            seniority="director",
            function="marketing",
            cost=0.0049,
            credits=0.2,
        ),
    ]
    summary = summarize_provider(records, HUNTER_PROVIDER_NAME)
    assert summary["identities_attempted"] == 2
    assert summary["match_rate"] == pytest.approx(1.0)
    assert summary["live_calls"] == 1
    assert summary["served_from_cache_file"] == 1
    assert summary["modelled_cost_usd"] == pytest.approx(0.0049)


def test_skipped_records_never_dilute_the_rates() -> None:
    records = [
        _record(APOLLO_PROVIDER_NAME, "a@x.test", seniority="vp", function="sales"),
        _record(APOLLO_PROVIDER_NAME, "b@x.test", status="skipped", served_from="skipped_budget"),
    ]
    summary = summarize_provider(records, APOLLO_PROVIDER_NAME)
    assert summary["identities_attempted"] == 1
    assert summary["match_rate"] == pytest.approx(1.0)
    assert summary["skipped"] == 1


def test_percentiles_are_stdlib_exact_on_small_samples() -> None:
    assert _percentile([], 0.5) is None
    assert _percentile([100.0], 0.95) == 100.0
    assert _percentile([10.0, 20.0, 30.0, 40.0, 100.0], 0.5) == 30.0
    assert _percentile([10.0, 20.0, 30.0, 40.0, 100.0], 0.95) == 100.0


def test_empty_denominators_read_as_none_not_zero() -> None:
    """No calls is 'no data', not 'a 0% match rate' — a provider that was
    never attempted must not chart as the worst provider."""
    summary = summarize_provider([], APOLLO_PROVIDER_NAME)
    assert summary["match_rate"] is None
    assert summary["cost_per_match_usd"] is None


# ----------------------------------------------------------------- agreement --


def test_agreement_summary_counts_and_surfaces_conflicts() -> None:
    records = [
        _record(HUNTER_PROVIDER_NAME, "a@x.test", seniority="vp", function="sales"),
        _record(APOLLO_PROVIDER_NAME, "a@x.test", seniority="vp", function="sales"),
        _record(HUNTER_PROVIDER_NAME, "b@x.test", seniority="director", function="marketing"),
        _record(APOLLO_PROVIDER_NAME, "b@x.test", seniority="vp", function="sales"),
        _record(HUNTER_PROVIDER_NAME, "c@x.test", status="miss"),
        _record(APOLLO_PROVIDER_NAME, "c@x.test", seniority="vp", function="sales"),
    ]
    summary = agreement_summary(records)
    assert summary["identities_compared"] == 3
    # Rates are rounded to 4 dp at the source, so compare against the rounded
    # figure rather than 1/3.
    assert summary["rates"]["agree"] == pytest.approx(0.3333)
    assert summary["rates"]["conflict"] == pytest.approx(0.3333)
    assert summary["rates"]["partial"] == pytest.approx(0.3333)
    assert [c["email"] for c in summary["conflicts"]] == ["b@x.test"]


def test_an_identity_with_one_person_provider_is_not_compared() -> None:
    """Comparison requires two voices; counting a solo answer as agreement (or
    anything else) would fabricate a comparison that never happened."""
    records = [_record(HUNTER_PROVIDER_NAME, "solo@x.test", seniority="vp", function="sales")]
    assert agreement_summary(records)["identities_compared"] == 0


# ------------------------------------------------------------------- overlap --


def test_overlap_separates_both_usable_from_provider_only() -> None:
    records = [
        _record(HUNTER_PROVIDER_NAME, "a@x.test", seniority="vp", function="sales"),
        _record(APOLLO_PROVIDER_NAME, "a@x.test", seniority="vp", function="sales"),
        _record(HUNTER_PROVIDER_NAME, "b@x.test", seniority="director", function="marketing"),
        _record(APOLLO_PROVIDER_NAME, "b@x.test", status="miss"),
    ]
    overlap = overlap_summary(records)
    assert overlap["person_evidence"]["both_providers_usable"] == 1
    assert overlap["person_evidence"]["provider_only"][HUNTER_PROVIDER_NAME] == 1
    assert overlap["person_evidence"]["provider_only"][APOLLO_PROVIDER_NAME] == 0


def test_company_overlap_compares_hunter_preview_against_abstract() -> None:
    records = [
        _record(ABSTRACT_PROVIDER_NAME, "a@x.test", industry="software", employees=240),
        _record(
            HUNTER_PROVIDER_NAME, "a@x.test", seniority="vp", industry="software", employees=250
        ),
        _record(ABSTRACT_PROVIDER_NAME, "b@x.test", industry="financial_services", employees=30),
        _record(
            HUNTER_PROVIDER_NAME, "b@x.test", seniority="vp", industry="software", employees=260
        ),
    ]
    company = overlap_summary(records)["company_hunter_vs_abstract"]
    assert company["industry_pairs"] == 2
    assert company["industry_agreement_rate"] == pytest.approx(0.5)
    assert company["employee_count_pairs"] == 2
    # 240 vs 250 is within 25%; 30 vs 260 is not.
    assert company["employee_count_within_25pct_rate"] == pytest.approx(0.5)


# ------------------------------------------------------------- the mock run --


def test_the_mock_run_is_deterministic_and_spend_bounded(tmp_path: Path) -> None:
    identities = load_identities(_EXAMPLE_CSV)
    assert len(identities) == 12

    providers = build_mock_providers(identities)
    records, spent = run_bakeoff(identities, providers, cached={}, max_spend_usd=1.0)

    assert len(records) == len(identities) * 3
    assert all(record.served_from == "live_call" for record in records)
    assert spent > 0.0
    assert spent <= 1.0

    summary = build_summary(records, with_sufficiency=False)
    blocks = {block["provider"]: block for block in summary["providers"]}
    assert blocks[ABSTRACT_PROVIDER_NAME]["match_rate"] == pytest.approx(1.0)
    # The persona script plants exactly two engineered conflicts.
    assert len(summary["agreement"]["conflicts"]) == 2
    report = render_report(summary)
    assert "PROVIDER BAKE-OFF" in report
    assert "modelled credit equivalent" in report


def test_the_spend_ceiling_stops_submission_predictively() -> None:
    """A ceiling below the run's full price: cheap calls proceed until the
    next call's worst case would cross the line, and everything after is a
    visible skip — never a half-made call, never an overshoot."""
    identities = load_identities(_EXAMPLE_CSV)[:6]
    providers = build_mock_providers(identities)
    ceiling = 0.03
    records, spent = run_bakeoff(identities, providers, cached={}, max_spend_usd=ceiling)

    assert spent <= ceiling
    assert any(record.served_from == "skipped_budget" for record in records)


def test_resuming_from_cache_adds_no_spend_and_marks_provenance() -> None:
    identities = load_identities(_EXAMPLE_CSV)[:4]
    providers = build_mock_providers(identities)
    first_records, first_spent = run_bakeoff(identities, providers, cached={}, max_spend_usd=1.0)
    assert first_spent > 0.0

    cached = {
        record.cache_key(): record
        for record in first_records
        if record.status in ("success", "miss")
    }
    second_records, second_spent = run_bakeoff(
        identities, providers, cached=cached, max_spend_usd=1.0
    )
    assert second_spent == 0.0
    assert all(record.served_from == "cache_file" for record in second_records)


def test_cache_keys_are_scoped_to_provider_and_identity() -> None:
    """Two colleagues must never share a person-provider cache entry, and one
    person's Hunter and Apollo results must never collide — the same scoping
    rule the evidence store enforces, applied to the harness's file cache."""
    assert _cache_key(APOLLO_PROVIDER_NAME, "a@x.test") != _cache_key(
        APOLLO_PROVIDER_NAME, "b@x.test"
    )
    assert _cache_key(APOLLO_PROVIDER_NAME, "a@x.test") != _cache_key(
        HUNTER_PROVIDER_NAME, "a@x.test"
    )


def test_a_free_mail_identity_skips_the_company_provider_only(tmp_path: Path) -> None:
    identities = [BakeoffIdentity(email="someone@gmail.com", domain=None, persona="agree")]
    providers = build_mock_providers(identities)
    records, _ = run_bakeoff(identities, providers, cached={}, max_spend_usd=1.0)

    by_provider = {record.provider: record for record in records}
    assert by_provider[ABSTRACT_PROVIDER_NAME].served_from == "skipped_no_domain"
    assert by_provider[HUNTER_PROVIDER_NAME].served_from == "live_call"
    assert by_provider[APOLLO_PROVIDER_NAME].served_from == "live_call"


def _patch_provider_configs(
    monkeypatch: pytest.MonkeyPatch, *, abstract_key: str, hunter_key: str, apollo_key: str
) -> None:
    """Patch every place a key is read from: the bake-off module's gate check
    *and* each adapter module's own singleton, which is what a bare
    ``.build()`` call (no explicit ``config=``) actually reads."""
    abstract_config = LiveProviderConfig(api_key=abstract_key)
    hunter_config = HunterConfig(api_key=hunter_key)
    apollo_config = ApolloPersonConfig(api_key=apollo_key)
    for module in (provider_bakeoff, live_abstract):
        monkeypatch.setattr(module, "LIVE_PROVIDER", abstract_config)
    for module in (provider_bakeoff, live_hunter):
        monkeypatch.setattr(module, "HUNTER", hunter_config)
    for module in (provider_bakeoff, live_apollo):
        monkeypatch.setattr(module, "APOLLO_PERSON", apollo_config)


def test_identity_metrics_distinguish_verified_from_merely_matched() -> None:
    """The Stripe case, as a summary metric: 4 provider matches, but only one
    identity was checked and it was a MISMATCH — match_rate must stay high
    while verified_person_rate reports the opposite story."""
    records = [
        _record(
            HUNTER_PROVIDER_NAME,
            "patrick@stripe.test",
            seniority="ic",
            function="engineering",
            identity_verdict="MISMATCH",
            person_evidence_usable=False,
        ),
        _record(HUNTER_PROVIDER_NAME, "b@x.test", seniority="vp", function="sales"),
        _record(HUNTER_PROVIDER_NAME, "c@x.test", seniority="c_level"),
        _record(HUNTER_PROVIDER_NAME, "d@x.test", status="miss", credits=None),
    ]
    summary = summarize_provider(records, HUNTER_PROVIDER_NAME)

    assert summary["identities_attempted"] == 4
    assert summary["match_rate"] == pytest.approx(0.75)
    assert summary["identity_checked_count"] == 1
    assert summary["verified_person_rate"] == pytest.approx(0.0)
    assert summary["identity_mismatch_rate"] == pytest.approx(0.25)
    # 2 of 3 matches are usable (the mismatch is excluded; the two unchecked
    # successes are NOT excluded — no name to check is not proof of a bad
    # match, see summarize_provider's docstring).
    assert summary["usable_person_evidence_rate"] == pytest.approx(0.5)


def test_an_unchecked_success_counts_as_usable_not_excluded() -> None:
    """No expected_full_name on file (the ordinary real-world case) must not
    quietly read as "unusable" — that would be exactly the UNKNOWN-becomes-
    negative-evidence bug this package exists to avoid."""
    records = [_record(HUNTER_PROVIDER_NAME, "a@x.test", seniority="vp", function="sales")]
    summary = summarize_provider(records, HUNTER_PROVIDER_NAME)
    assert summary["identity_checked_count"] == 0
    assert summary["usable_person_evidence_rate"] == pytest.approx(1.0)


def _fake_hunter() -> HunterEnrichmentProvider:
    return HunterEnrichmentProvider(config=HunterConfig(api_key="test-key"), client=httpx.Client())


def test_record_from_result_runs_identity_validation_for_person_success_with_expected_name() -> (
    None
):
    identity = BakeoffIdentity(
        email="patrick@stripe.test",
        domain="stripe.test",
        expected_full_name="Patrick Collison",
    )
    result = ProviderResult(
        fields={"title_seniority": "ic", "title_function": "engineering"},
        confidence=0.75,
        cost_usd=0.0049,
        latency_ms=500.0,
        status=ProviderStatus.SUCCESS,
        raw={
            "matched_identity": {
                "full_name": "Patrick Bosmans",
                "email": "patrick@stripe.test",
                "employer_domain": "stripe.test",
                "title": "IT Administrator",
            }
        },
    )

    record = _record_from_result(_fake_hunter(), identity, result, served_from="live_call")
    assert record.identity_verdict == "MISMATCH"
    assert record.person_evidence_usable is False


def test_record_from_result_skips_identity_check_with_no_expected_name() -> None:
    identity = BakeoffIdentity(email="a@x.test", domain="x.test")
    result = ProviderResult(
        fields={"title_seniority": "vp"},
        confidence=0.75,
        cost_usd=0.0049,
        latency_ms=500.0,
        status=ProviderStatus.SUCCESS,
        raw={"matched_identity": {"full_name": "Someone", "employer_domain": "x.test"}},
    )

    record = _record_from_result(_fake_hunter(), identity, result, served_from="live_call")
    assert record.identity_verdict is None
    assert record.person_evidence_usable is None


def test_build_real_providers_omits_apollo_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Abstract + Hunter configured, Apollo not: a two-provider run, not a refusal."""
    _patch_provider_configs(monkeypatch, abstract_key="fake", hunter_key="fake", apollo_key="")

    providers = build_real_providers()
    names = {provider.name for provider in providers}
    assert names == {ABSTRACT_PROVIDER_NAME, HUNTER_PROVIDER_NAME}


def test_build_real_providers_includes_apollo_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_provider_configs(monkeypatch, abstract_key="fake", hunter_key="fake", apollo_key="fake")

    providers = build_real_providers()
    names = {provider.name for provider in providers}
    assert names == {ABSTRACT_PROVIDER_NAME, HUNTER_PROVIDER_NAME, APOLLO_PROVIDER_NAME}


@pytest.mark.parametrize(
    ("abstract_key", "hunter_key"),
    [("", "fake"), ("fake", "")],
)
def test_build_real_providers_still_refuses_missing_abstract_or_hunter(
    monkeypatch: pytest.MonkeyPatch, abstract_key: str, hunter_key: str
) -> None:
    """Apollo is optional; the company provider and the cheaper person provider are not."""
    _patch_provider_configs(
        monkeypatch, abstract_key=abstract_key, hunter_key=hunter_key, apollo_key="fake"
    )

    with pytest.raises(SystemExit):
        build_real_providers()
