"""The provider→scorer adapter boundary.

The invariant under test: **no raw provider vocabulary reaches the scorer.**
Everything crossing this boundary is either a member of a closed canonical set,
a validated number, or absent-and-reported.
"""

from __future__ import annotations

import pytest

from arie.normalization.contract import (
    COMPANY_NORMALIZERS,
    PERSON_NORMALIZERS,
    NormalizationReport,
    normalize_provider_fields,
)
from arie.normalization.taxonomy import (
    CANONICAL_FUNCTIONS,
    CANONICAL_INDUSTRIES,
    CANONICAL_SENIORITIES,
    UNKNOWN,
)
from arie.scoring.rules import SCORED_FIELDS, is_unknown

_PROVIDER = "test_provider"


def _company(**raw: object) -> NormalizationReport:
    return normalize_provider_fields(provider=_PROVIDER, entity_type="company", raw_fields=raw)


def _person(**raw: object) -> NormalizationReport:
    return normalize_provider_fields(provider=_PROVIDER, entity_type="person", raw_fields=raw)


# ------------------------------------------------------- the happy boundary --


def test_a_mappable_response_yields_only_canonical_values() -> None:
    report = _company(industry="Computer Software", employee_count=420)

    assert report.fields == {"industry": "software", "employee_count": 420}
    assert report.unmapped == ()
    assert report.has_usable_fields


def test_person_fields_normalize_through_the_same_boundary() -> None:
    report = _person(title_seniority="VP", title_function="Revenue Operations")
    assert report.fields == {"title_seniority": "vp", "title_function": "operations"}


# ------------------------------------------------- the unknown/absent split --


def test_an_unmappable_value_is_reported_and_never_emitted() -> None:
    report = _company(industry="Pet Grooming Franchises", employee_count=420)

    assert "industry" not in report.fields
    assert report.fields == {"employee_count": 420}
    assert [(u.field_name, u.raw) for u in report.unmapped] == [
        ("industry", "Pet Grooming Franchises")
    ]


def test_the_unknown_sentinel_is_never_emitted_as_a_field_value() -> None:
    """The boundary reports unknowns rather than storing them. An UNKNOWN-valued
    evidence row would look, to the acquisition loop's "do I have this field?"
    check, exactly like a real one."""
    report = _company(industry="Artisanal Widget Refurbishment", employee_count=-5)

    assert report.fields == {}
    assert not report.has_usable_fields
    assert UNKNOWN not in report.fields.values()
    assert {u.field_name for u in report.unmapped} == {"industry", "employee_count"}


def test_an_absent_value_is_not_an_unmapped_value() -> None:
    """ "The provider said nothing" and "the provider said something unusable"
    are different facts. Only the second is worth an audit entry — the first is
    the ordinary case for every field a provider does not cover."""
    report = _company(industry=None, employee_count="   ")

    assert report.fields == {}
    assert report.unmapped == ()


# ------------------------------------------------------------ field naming --


def test_a_field_with_no_registered_normalizer_cannot_cross_the_boundary() -> None:
    report = _company(
        industry="Software",
        description="a company that does things",
        linkedin_url="https://example.test/acme",
    )

    assert report.fields == {"industry": "software"}
    assert set(report.ignored_fields) == {"description", "linkedin_url"}


def test_the_deferred_fields_have_no_normalizer_and_so_cannot_be_supplied() -> None:
    """Live V1 defers buying_intent, recent_trigger_event, and
    disqualifying_flag: no live source ARIE has is trustworthy for them. The
    absence of a normalizer is what makes that a structural guarantee rather
    than a convention — a provider claiming one is silently ignored."""
    deferred = {"buying_intent", "recent_trigger_event", "disqualifying_flag"}
    assert deferred.isdisjoint(COMPANY_NORMALIZERS)
    assert deferred.isdisjoint(PERSON_NORMALIZERS)

    report = _company(buying_intent=0.99, recent_trigger_event="series_b", disqualifying_flag=True)
    assert report.fields == {}
    assert set(report.ignored_fields) == deferred


def test_every_normalized_field_name_is_one_the_scorer_actually_consumes() -> None:
    for field_name in (*COMPANY_NORMALIZERS, *PERSON_NORMALIZERS):
        assert field_name in SCORED_FIELDS


# ------------------------------------------------------- the core invariant --

_ADVERSARIAL_INDUSTRIES = (
    "Computer Software",
    "computer software",
    "SaaS",
    "software development",
    "Hospital & Health Care",
    "Financial Services",
    "Non-profit Organization Management",
    "Pet Grooming Franchises",
    "",
    "   ",
    "123",
    "<script>alert(1)</script>",
)


@pytest.mark.parametrize("raw", _ADVERSARIAL_INDUSTRIES)
def test_no_raw_provider_vocabulary_ever_reaches_the_scorer(raw: str) -> None:
    """The one-line statement of what this whole layer is for."""
    report = _company(industry=raw)
    if "industry" in report.fields:
        assert report.fields["industry"] in CANONICAL_INDUSTRIES
        assert not is_unknown(report.fields["industry"])


@pytest.mark.parametrize("raw", ["VP", "vice president", "Zookeeper", "", "chief"])
def test_person_vocabulary_is_canonical_or_absent(raw: str) -> None:
    report = _person(title_seniority=raw, title_function=raw)
    for field_name, allowed in (
        ("title_seniority", CANONICAL_SENIORITIES),
        ("title_function", CANONICAL_FUNCTIONS),
    ):
        if field_name in report.fields:
            assert report.fields[field_name] in allowed
            assert not is_unknown(report.fields[field_name])


# ----------------------------------------------------------------- audit --


def test_audit_carries_the_raw_value_so_a_missing_alias_is_actionable() -> None:
    audit = _company(industry="Pet Grooming Franchises").audit()
    assert audit == {
        "mapped": [],
        "unmapped": [{"field": "industry", "raw": "Pet Grooming Franchises"}],
    }


def test_audit_records_the_raw_to_canonical_pair_for_every_mapped_field() -> None:
    """The canonical value alone cannot be audited. "industry: software" does
    not say whether the vendor wrote "Computer Software", "SaaS", or something
    a rule matched by accident — and a mapping being wrong in a way that looks
    reasonable downstream is exactly this layer's risk."""
    audit = _company(industry="Computer Software", employee_count="51-200").audit()
    assert audit["mapped"] == [
        {"field": "employee_count", "raw": "51-200", "canonical": 51},
        {"field": "industry", "raw": "Computer Software", "canonical": "software"},
    ]
    assert audit["unmapped"] == []


def test_the_mapped_pairs_agree_with_the_fields_they_describe() -> None:
    report = _company(industry="SaaS", employee_count=420)
    assert {item.field_name: item.canonical for item in report.mapped} == report.fields


def test_audit_truncates_a_pathologically_long_raw_value() -> None:
    """`audit()` output lands on spans and in `ProviderResult.raw`, both of
    which are logged. A vendor returning a paragraph must not become a log
    line nobody can read."""
    audit = _company(industry="x" * 5000).audit()
    assert len(audit["unmapped"][0]["raw"]) <= 120


def test_normalization_is_deterministic_and_order_independent() -> None:
    first = _company(industry="SaaS", employee_count="51-200")
    second = normalize_provider_fields(
        provider=_PROVIDER,
        entity_type="company",
        raw_fields={"employee_count": "51-200", "industry": "SaaS"},
    )
    assert first.fields == second.fields
