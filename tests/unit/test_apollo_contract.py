"""The Apollo person-enrichment contract — fixtures only, no API calls (Phase 8).

**Nothing in this file touches the network or reads an API key.** That is the
point of the phase: the shape of what Apollo must return, and what ARIE will do
with it, is reviewable before a paid provider is wired in.

The fixtures in ``tests/fixtures/apollo/`` are hand-written from Apollo's
documented People Enrichment response shape. They are the *contract*: if the
live payload differs, the fix belongs in ``extract_apollo_person``, not in the
taxonomy and not in the scorer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arie.normalization.taxonomy import CANONICAL_FUNCTIONS, CANONICAL_SENIORITIES
from arie.providers.apollo_contract import (
    APOLLO_PROVIDER_NAME,
    APOLLO_PROVIDES_FIELDS,
    extract_apollo_person,
    normalize_apollo_person,
    normalized_identity,
)
from arie.scoring.rules import SCORED_FIELDS, field_points, is_unknown

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "apollo"


def _fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload


# ------------------------------------------------------------ the declaration --


def test_the_provider_declares_exactly_the_two_fields_it_closes_the_gap_on() -> None:
    """Abstract supplies only company firmographics; seniority and function are
    35 of the scorer's 100 reachable points and are unknown for every live lead
    without a person provider. Over-declaring would make the EVoI controller
    think a call is worth more than it is."""
    assert APOLLO_PROVIDES_FIELDS == ("title_seniority", "title_function")
    for field_name in APOLLO_PROVIDES_FIELDS:
        assert field_name in SCORED_FIELDS


def test_apollo_is_wired_second_in_the_live_acquisition_order() -> None:
    """This test used to assert the opposite — that Apollo was *not* registered
    — because the phase before this one deliberately shipped the normalization
    contract without a transport, so the mapping could be reviewed before a
    paid provider was wired in. That review happened; the adapter now exists in
    ``arie.providers.live_apollo``.

    What is worth pinning now is the ordering, not the mere membership. Apollo
    must come *after* the company provider: it costs an order of magnitude more
    and its evidence is per-person, so it can never be amortised across a
    company the way firmographics can. A refactor that reorders this tuple
    would silently start paying for person enrichment on leads that were
    already a confident reject on firmographics alone."""
    from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES
    from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME

    assert REGISTERED_LIVE_PROVIDER_NAMES == (ABSTRACT_PROVIDER_NAME, APOLLO_PROVIDER_NAME)


def test_the_contract_module_has_no_http_client_and_no_credentials() -> None:
    """Still true, and still worth asserting now that a transport exists.

    Wiring Apollo added ``arie.providers.live_apollo``; it did not move the
    network into this module. The split is what lets the vocabulary mapping —
    the part that decides what a job title is *worth* — be read, reviewed, and
    exhaustively fixture-tested without a key, a client, or a mock. If a future
    change puts an ``httpx`` import here, that property is gone.

    Checked against what the module *imports and defines*, not against its
    prose — the docstring names `httpx.Client` and `APOLLO_API_KEY` precisely
    to say they are absent, and a naive text search would read that as their
    presence."""
    import arie.providers.apollo_contract as apollo

    source_path = apollo.__file__
    assert source_path is not None
    code_lines = [
        line
        for line in Path(source_path).read_text(encoding="utf-8").splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert not any("httpx" in line or "config" in line for line in code_lines)

    assert not hasattr(apollo, "fetch")
    assert not hasattr(apollo, "httpx")
    assert not any("API_KEY" in name for name in vars(apollo))


def test_apollo_is_nonetheless_already_covered_by_the_spend_caps() -> None:
    """Covered *before* it can spend, not after someone remembers. No
    `provider_calls` row carries this name today, so the budget query sums
    zero for it — it costs nothing to be early and everything to be late."""
    from arie.live.providers import LIVE_PROVIDER_NAMES

    assert APOLLO_PROVIDER_NAME in LIVE_PROVIDER_NAMES


# --------------------------------------------------------- structured enums --


def test_a_full_response_uses_apollos_own_enums() -> None:
    report = normalize_apollo_person(_fixture("person_full"))

    assert report.provider == APOLLO_PROVIDER_NAME
    assert report.entity_type == "person"
    assert report.fields == {"title_seniority": "vp", "title_function": "operations"}
    assert report.unmapped == ()


def test_the_extraction_step_returns_apollos_vocabulary_not_aries() -> None:
    """The split that makes a second provider cheap: extraction renames and
    picks, normalization interprets. `extract_apollo_person`'s output is
    deliberately still unsafe to score."""
    extracted = extract_apollo_person(_fixture("person_full"))
    assert extracted == {"title_seniority": "vp", "title_function": "operations"}


def test_the_revenue_operations_subdepartment_folds_onto_the_scorers_vocabulary() -> None:
    report = normalize_apollo_person(_fixture("person_full"))
    assert report.fields["title_function"] == "operations"
    assert field_points("title_function", "operations") > 0.0


# ------------------------------------------------------------ title fallback --


def test_a_response_with_no_enums_falls_back_to_the_title() -> None:
    """Apollo omits `seniority`/`departments` for a meaningful share of
    records. Without the fallback those leads would be permanently unknown on
    both fields."""
    report = normalize_apollo_person(_fixture("person_title_only"))
    assert report.fields == {"title_seniority": "director", "title_function": "marketing"}


def test_the_fallback_is_a_last_resort_not_a_blend() -> None:
    """When Apollo's enum is present it wins outright. Mixing a parsed title
    into a present enum would make the result depend on which of two
    disagreeing sources happened to parse, with no way to tell afterwards."""
    payload = {
        "person": {
            "title": "Chief Executive Officer",  # would parse as c_level
            "seniority": "manager",  # Apollo's own answer
            "departments": ["finance"],
        }
    }
    report = normalize_apollo_person(payload)
    assert report.fields["title_seniority"] == "manager"
    assert report.fields["title_function"] == "finance"


# ------------------------------------------------- unmappable and not-found --


def test_unmappable_vendor_vocabulary_is_reported_never_scored() -> None:
    """The Phase 4 semantics, applied to the provider ARIE has not wired yet —
    so the mistake cannot be made a second time on a second vendor."""
    report = normalize_apollo_person(_fixture("person_unmappable_vocabulary"))

    assert report.fields == {}
    assert not report.has_usable_fields
    assert {u.field_name for u in report.unmapped} == {"title_seniority", "title_function"}
    assert {u.raw for u in report.unmapped} == {
        "sparkle_tier_3",
        "chaos_engineering_of_feelings",
    }


def test_a_person_not_found_yields_nothing_and_reports_nothing_unmapped() -> None:
    """`{"person": null}` is a MISS, not an error and not an unmapped value —
    the same treatment `arie.providers.live_abstract` gives an empty 200."""
    report = normalize_apollo_person(_fixture("person_not_found"))
    assert report.fields == {}
    assert report.unmapped == ()


def test_an_empty_payload_is_handled_without_raising() -> None:
    assert normalize_apollo_person({}).fields == {}


# ------------------------------------------------------- the deferred fields --


def test_apollos_intent_and_trigger_data_is_visible_and_deliberately_unused() -> None:
    """Apollo returns intent strength, intent topics, job-change dates, and
    funding rounds. Live V1 defers all of them: `buying_intent` is the single
    largest field in the ruleset (20 points) and this vendor's methodology is
    not inspectable. The fixture carries them precisely so this test can prove
    they were seen and not taken."""
    payload = _fixture("person_intent_fields_present")
    assert "intent_strength" in payload["person"]
    assert "latest_funding_stage" in payload["person"]["organization"]

    report = normalize_apollo_person(payload)
    assert set(report.fields) <= set(APOLLO_PROVIDES_FIELDS)
    assert "buying_intent" not in report.fields
    assert "recent_trigger_event" not in report.fields
    assert "disqualifying_flag" not in report.fields


def test_no_disqualifying_flag_is_ever_manufactured() -> None:
    """The scorer treats `disqualifying_flag` as absolute — a fabricated one
    zeroes the lead outright. Apollo returns no such field, and inferring one
    from "this looks like a freelancer" is exactly the invention `arie.icp`
    refuses to make."""
    for name in ("person_full", "person_title_only", "person_intent_fields_present"):
        assert "disqualifying_flag" not in normalize_apollo_person(_fixture(name)).fields


# ------------------------------------------------------------- the invariant --


@pytest.mark.parametrize(
    "name",
    [
        "person_full",
        "person_title_only",
        "person_unmappable_vocabulary",
        "person_not_found",
        "person_intent_fields_present",
    ],
)
def test_no_raw_apollo_vocabulary_can_reach_the_scorer(name: str) -> None:
    report = normalize_apollo_person(_fixture(name))
    for field_name, value in report.fields.items():
        assert field_name in APOLLO_PROVIDES_FIELDS
        assert not is_unknown(value)
        allowed = CANONICAL_SENIORITIES if field_name == "title_seniority" else CANONICAL_FUNCTIONS
        assert value in allowed


# ---------------------------------------------------------------- identity --


def test_identity_fields_are_carried_for_the_receipt() -> None:
    identity = normalized_identity(_fixture("person_full"))

    assert identity.full_name == "Dana Okafor"
    assert identity.title == "VP of Revenue Operations"
    assert identity.email == "dana.okafor@northwind-analytics.test"
    assert identity.linkedin_url == "https://www.linkedin.com/in/dana-okafor-example"
    assert identity.organization_name == "Northwind Analytics"
    assert identity.organization_domain == "northwind-analytics.test"


def test_a_name_is_assembled_from_parts_when_apollo_omits_the_whole() -> None:
    identity = normalized_identity({"person": {"first_name": "Kit", "last_name": "Aluko"}})
    assert identity.full_name == "Kit Aluko"


def test_no_identity_field_is_a_scored_field() -> None:
    """Identity is for the receipt and the reviewer. None of these names is in
    `SCORED_FIELDS`, so none can reach the scorer through the normalization
    contract even by accident."""
    identity = normalized_identity(_fixture("person_full"))
    assert set(identity.audit()).isdisjoint(SCORED_FIELDS)


def test_identity_audit_omits_absent_fields() -> None:
    assert normalized_identity(_fixture("person_not_found")).audit() == {}
