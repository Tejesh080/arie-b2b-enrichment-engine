"""The Hunter enrichment contract — fixtures only, no API calls.

Same phase discipline as ``test_apollo_contract.py``: the raw→canonical
mapping is reviewable and fully tested before (and independently of) any
transport. The fixtures in ``tests/fixtures/hunter/`` are hand-written from
Hunter's documented Clearbit-style response shape; if the live payload
differs, the fix belongs in ``extract_hunter_person``/``extract_hunter_company``
— not in the taxonomy, not in the scorer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arie.normalization.taxonomy import CANONICAL_FUNCTIONS, CANONICAL_SENIORITIES
from arie.providers.hunter_contract import (
    HUNTER_PROVIDER_NAME,
    HUNTER_PROVIDES_FIELDS,
    extract_hunter_company,
    normalize_hunter_company,
    normalize_hunter_person,
    normalized_identity,
)
from arie.scoring.rules import SCORED_FIELDS

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "hunter"


def _fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload


# ------------------------------------------------------------ the declaration --


def test_hunter_declares_exactly_the_two_person_fields() -> None:
    """The same two fields Apollo declares — the overlap is deliberate (it is
    what makes cross-provider agreement measurable) — and nothing company-
    shaped, however much company data a combined response carries. Whether
    Hunter's company half is trustworthy enough to score from is the bake-off's
    question; declaring the fields here would answer it by default."""
    assert HUNTER_PROVIDES_FIELDS == ("title_seniority", "title_function")
    for field_name in HUNTER_PROVIDES_FIELDS:
        assert field_name in SCORED_FIELDS
    assert "employee_count" not in HUNTER_PROVIDES_FIELDS
    assert "industry" not in HUNTER_PROVIDES_FIELDS


def test_hunter_is_registered_between_abstract_and_apollo() -> None:
    """Cheapest-first among the person providers: Hunter's modelled $0.0049
    must be tried before Apollo's $0.0196, and both after Abstract's $0.00165.
    A reorder is a silent change to what every live lead costs."""
    from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES
    from arie.providers.apollo_contract import APOLLO_PROVIDER_NAME
    from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME

    assert REGISTERED_LIVE_PROVIDER_NAMES == (
        ABSTRACT_PROVIDER_NAME,
        HUNTER_PROVIDER_NAME,
        APOLLO_PROVIDER_NAME,
    )


def test_the_contract_module_has_no_http_client_and_no_credentials() -> None:
    """The same structural property the Apollo contract pins, checked the same
    way — against imports and definitions, not prose."""
    import arie.providers.hunter_contract as hunter

    source_path = hunter.__file__
    assert source_path is not None
    code_lines = [
        line
        for line in Path(source_path).read_text(encoding="utf-8").splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert not any("httpx" in line or "config" in line for line in code_lines)
    assert not hasattr(hunter, "fetch")
    assert not any("API_KEY" in name for name in vars(hunter))


# ----------------------------------------------- the seniority-precedence rule --


def test_a_vp_title_beats_the_coarse_executive_enum() -> None:
    """THE load-bearing Hunter-specific decision. Hunter's five-value seniority
    ladder folds C-level and VP into one ``executive`` bucket; the canonical
    ladder prices those rungs differently (20.0 vs 18.0). ``combined_full``
    carries title "VP of Sales" *and* enum "executive" — enum-first (Apollo's
    rule) would score this VP as a C-level, a systematic over-credit. The title
    wins, and the person is a vp."""
    report = normalize_hunter_person(_fixture("combined_full"))
    assert report.fields["title_seniority"] == "vp"


def test_the_enum_is_the_fallback_when_the_title_says_nothing() -> None:
    """``combined_enum_only`` has a null title and enum ``executive``. With no
    finer-grained signal to prefer, the coarse enum is honestly the best
    available answer, and ``executive`` maps to c_level."""
    report = normalize_hunter_person(_fixture("combined_enum_only"))
    assert report.fields["title_seniority"] == "c_level"
    assert report.fields["title_function"] == "marketing"


def test_function_keeps_the_enum_first_rule() -> None:
    """No coarseness problem on the role axis, so Apollo's enum-first rule
    holds: role ``sales`` is used even though the title ("VP of Sales") would
    parse to the same place — and the pair proves the asymmetry is deliberate,
    because the same fixture exercises both orderings at once."""
    report = normalize_hunter_person(_fixture("combined_full"))
    assert report.fields["title_function"] == "sales"


# -------------------------------------------------------------- the fixtures --


def test_a_person_only_response_still_normalizes() -> None:
    report = normalize_hunter_person(_fixture("combined_person_only"))
    assert report.fields == {"title_function": "marketing", "title_seniority": "director"}


def test_the_people_find_envelope_is_accepted_too() -> None:
    """``person_title_only`` uses the flat ``people/find`` envelope (person
    directly under ``data``), so pointing ``HUNTER_BASE_URL`` at the narrower
    endpoint needs no second contract module."""
    report = normalize_hunter_person(_fixture("person_title_only"))
    assert report.fields == {"title_function": "marketing", "title_seniority": "director"}


def test_unmappable_vocabulary_is_reported_never_scored() -> None:
    report = normalize_hunter_person(_fixture("combined_unmappable"))
    assert report.fields == {}
    assert {item.field_name for item in report.unmapped} == {
        "title_seniority",
        "title_function",
    }


def test_every_canonical_value_ever_emitted_is_in_the_closed_sets() -> None:
    for name in (
        "combined_full",
        "combined_person_only",
        "combined_enum_only",
        "person_title_only",
        "combined_unmappable",
    ):
        report = normalize_hunter_person(_fixture(name))
        if "title_seniority" in report.fields:
            assert report.fields["title_seniority"] in CANONICAL_SENIORITIES
        if "title_function" in report.fields:
            assert report.fields["title_function"] in CANONICAL_FUNCTIONS


def test_the_not_found_error_body_yields_nothing() -> None:
    report = normalize_hunter_person(_fixture("not_found"))
    assert report.fields == {}
    assert report.unmapped == ()


# --------------------------------------------------------------- company half --


def test_company_extraction_yields_abstracts_two_fields_in_raw_vocabulary() -> None:
    raw = extract_hunter_company(_fixture("combined_full"))
    assert raw == {"employee_count": 240, "industry": "Computer Software"}


def test_company_normalization_reaches_the_same_canonical_values_as_abstract_would() -> None:
    """The whole point of carrying the company half: Hunter's "Computer
    Software" and Abstract's "Computer Software" must land on the same
    canonical value, or the overlap comparison would measure the taxonomy
    rather than the vendors."""
    report = normalize_hunter_company(_fixture("combined_full"))
    assert report.fields == {"employee_count": 240, "industry": "software"}


def test_an_employees_range_is_not_parsed_into_a_count() -> None:
    """``combined_enum_only`` has ``employees: null`` and a "11-50" range. A
    range is not a count; inventing its midpoint would manufacture precision.
    The count is simply absent."""
    report = normalize_hunter_company(_fixture("combined_enum_only"))
    assert "employee_count" not in report.fields
    assert report.fields["industry"] == "financial_services"


def test_a_missing_company_half_yields_an_empty_report() -> None:
    report = normalize_hunter_company(_fixture("combined_person_only"))
    assert report.fields == {}
    assert report.unmapped == ()


# ------------------------------------------------------------------- identity --


def test_identity_is_carried_for_review_and_never_overlaps_scored_fields() -> None:
    identity = normalized_identity(_fixture("combined_full"))
    audit = identity.audit()
    assert audit["full_name"] == "Dana Okafor"
    assert audit["title"] == "VP of Sales"
    assert audit["employer_domain"] == "northwind-analytics.test"
    assert not set(audit) & set(SCORED_FIELDS)


def test_identity_falls_back_to_given_plus_family_name() -> None:
    body = {
        "data": {
            "name": {"givenName": "Sam", "familyName": "Reyes"},
            "employment": {"title": "Director of Marketing"},
        }
    }
    assert normalized_identity(body).full_name == "Sam Reyes"
