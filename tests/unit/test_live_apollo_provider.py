"""The Apollo person-enrichment adapter — every test here is hermetic.

``httpx.MockTransport`` stands in for Apollo's People Enrichment endpoint, the
same idiom ``tests/unit/test_live_abstract_provider.py`` and
``tests/unit/test_llm_deepseek_client.py`` use — nothing here needs
``APOLLO_API_KEY``, network access, or a spent credit, and every failure mode
(timeout, auth, rate limit, malformed body, ...) is deterministic and
reproducible offline.

Division of labour with ``tests/unit/test_apollo_contract.py``: that file owns
the raw→canonical *mapping* (it is the fixture-first contract, and it needs no
transport at all). This file owns everything the transport adds — the request
Apollo actually receives, the status-code table, and the credit-based cost
model, which is where this adapter genuinely differs from Abstract's.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from arie.config import ApolloPersonConfig
from arie.core.types import Entity, ProviderStatus
from arie.normalization.taxonomy import CANONICAL_FUNCTIONS, CANONICAL_SENIORITIES
from arie.providers.live_apollo import (
    APOLLO_PROVIDER_NAME,
    APOLLO_PROVIDES_FIELDS,
    ApolloPersonConfigurationError,
    ApolloPersonEnrichmentProvider,
)
from arie.scoring.rules import SCORED_FIELDS

_FAKE_KEY = "apollo-live-test-secret-do-not-leak"
_COST = 0.0196
_FIXTURES = Path(__file__).parent.parent / "fixtures" / "apollo"


def _fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload


def _config(**overrides: object) -> ApolloPersonConfig:
    defaults: dict[str, object] = {
        "api_key": _FAKE_KEY,
        "base_url": "https://fake.apollo.test/api/v1/people/match",
        "timeout_seconds": 5.0,
        "cost_usd_per_success": _COST,
    }
    defaults.update(overrides)
    return ApolloPersonConfig(**defaults)  # type: ignore[arg-type]


def _provider_with(
    handler: Callable[[httpx.Request], httpx.Response], **config_overrides: object
) -> ApolloPersonEnrichmentProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ApolloPersonEnrichmentProvider(config=_config(**config_overrides), client=client)


def _entity(email: str = "dana.okafor@northwind-analytics.test") -> Entity:
    return Entity(entity_type="person", entity_id=uuid.uuid4(), canonical_key=email)


def _json_response(status_code: int, body: object) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return handler


def _person(**fields: Any) -> dict[str, Any]:
    return {"person": fields}


# --------------------------------------------------------------- declaration --


def test_it_declares_the_person_entity_type_and_exactly_two_scored_fields() -> None:
    """The live loop dispatches on ``entity_type`` to decide which of a lead's
    identifiers to hand a provider. Declaring ``company`` here would make ARIE
    call a person endpoint with a domain."""
    provider = _provider_with(_json_response(200, {"person": None}))

    assert provider.name == APOLLO_PROVIDER_NAME
    assert provider.entity_type == "person"
    assert provider.provides_fields == APOLLO_PROVIDES_FIELDS
    assert set(provider.provides_fields) <= set(SCORED_FIELDS)
    assert provider.base_cost_usd == pytest.approx(_COST)


# -------------------------------------------------------------- construction --


def test_missing_api_key_raises_at_construction_not_at_call_time() -> None:
    """Same rule P5 set for Abstract: a live worker with a missing credential
    must fail loudly at startup, never silently run a half-blind pipeline that
    still reports coverage and cost."""
    with pytest.raises(ApolloPersonConfigurationError, match="APOLLO_API_KEY"):
        ApolloPersonEnrichmentProvider.build(config=ApolloPersonConfig(api_key=""))


def test_an_injected_client_bypasses_the_key_check() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_json_response(200, {"person": None})))
    provider = ApolloPersonEnrichmentProvider.build(
        config=ApolloPersonConfig(api_key=""), client=client
    )
    assert provider.config.api_key == ""


def test_wrong_entity_type_raises_value_error() -> None:
    """A caller bug, not an operational outcome — the one thing ``fetch`` is
    still allowed to raise for."""
    provider = _provider_with(_json_response(200, {"person": None}))
    company_entity = Entity(entity_type="company", entity_id=uuid.uuid4(), canonical_key="acme.com")
    with pytest.raises(ValueError, match="person"):
        provider.fetch(company_entity)


# ------------------------------------------------------------- the request --


def test_the_request_matches_apollos_documented_contract() -> None:
    """POST, ``x-api-key`` header, email as a query parameter.

    Pinned because all three were verified against Apollo's published docs
    rather than inferred from the fixtures, and a silent drift (GET, or a
    ``api_key`` query parameter copied from the Abstract adapter) would fail
    against the real API in a way no fixture test would catch."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_person(title="VP Sales", seniority="vp"))

    provider = _provider_with(handler)
    provider.fetch(_entity("dana@northwind-analytics.test"))

    assert seen["method"] == "POST"
    assert seen["url"].startswith("https://fake.apollo.test/api/v1/people/match")
    assert seen["headers"]["x-api-key"] == _FAKE_KEY
    assert seen["params"]["email"] == "dana@northwind-analytics.test"


def test_personal_email_and_phone_reveal_are_always_opted_out_of() -> None:
    """Two independent reasons, either sufficient: a revealed mobile number
    costs eight extra credits (a 9x cost multiplier arriving silently), and
    ARIE scores neither — they would be PII fetched for no decision-relevant
    purpose. Apollo defaults both to false today, which is exactly why the
    explicit value is worth pinning: a vendor changing its own default must not
    be able to change ARIE's cost or its PII footprint."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"person": None})

    _provider_with(handler).fetch(_entity())

    assert seen["reveal_personal_emails"] == "false"
    assert seen["reveal_phone_number"] == "false"


def test_the_api_key_never_appears_in_the_request_url() -> None:
    """The material difference from ``live_abstract``, where Abstract publishes
    no header mechanism and the key necessarily rides in the URL. Apollo has
    one, so the credential must never reach a URL that a proxy, an access log,
    or an ``httpx`` exception's ``str()`` could capture."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"person": None})

    _provider_with(handler).fetch(_entity())
    assert _FAKE_KEY not in seen["url"]


# ----------------------------------------------------------------- success --


def test_success_maps_a_title_onto_canonical_seniority_and_function() -> None:
    provider = _provider_with(
        _json_response(200, _person(title="VP of Sales", seniority="vp", departments=["sales"]))
    )
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.SUCCESS
    assert result.fields == {"title_seniority": "vp", "title_function": "sales"}
    assert result.confidence > 0.0
    assert result.cost_usd == pytest.approx(_COST)


@pytest.mark.parametrize(
    ("title", "seniority", "function"),
    [
        ("Vice President of Sales", "vp", "sales"),
        ("VP Sales", "vp", "sales"),
        ("SVP, Global Sales", "vp", "sales"),
        # `head` and `revenue_operations` are deliberately NOT canonical values.
        # Adding them would create vocabulary `arie.scoring.rules` has no weight
        # for, so an ICP target would score 0.0 while *looking* assessed — the
        # exact unknown-vs-negative confusion the taxonomy layer exists to
        # prevent. "Head of" folds to `director` on the taxonomy's stated
        # conservative bias (one extra human review costs less than an
        # over-credited auto-route) and revenue operations folds to
        # `operations`. See arie.normalization.taxonomy for both arguments.
        ("Head of Revenue Operations", "director", "operations"),
        ("Chief Revenue Officer", "c_level", "sales"),
        ("Director of Marketing", "director", "marketing"),
        ("Growth Lead", "manager", "marketing"),
        ("Sales Manager", "manager", "sales"),
        # Outside the reference ICP, and therefore KNOWN-negative rather than
        # unknown: an engineer is genuinely assessed, scores what the ruleset
        # says an engineer scores, and tightens the bounds accordingly.
        ("Software Engineer", "ic", "engineering"),
    ],
)
def test_the_title_vocabulary_the_live_pipeline_will_actually_meet(
    title: str, seniority: str, function: str
) -> None:
    """Titles a real B2B pipeline sees daily, asserted end-to-end through the
    adapter rather than only through the contract module, so a transport-layer
    change cannot quietly stop calling the normalizer."""
    provider = _provider_with(_json_response(200, _person(title=title)))
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.SUCCESS
    assert result.fields["title_seniority"] == seniority
    assert result.fields["title_function"] == function
    assert result.fields["title_seniority"] in CANONICAL_SENIORITIES
    assert result.fields["title_function"] in CANONICAL_FUNCTIONS


def test_apollos_own_enum_is_preferred_over_parsing_its_prose() -> None:
    """A vendor's structured answer beats parsing its free text. Pinned with a
    payload where the two genuinely disagree, so the assertion cannot pass by
    coincidence: the title parses to c_level, the enum says director."""
    provider = _provider_with(
        _json_response(200, _person(title="Chief Revenue Officer", seniority="director"))
    )
    result = provider.fetch(_entity())
    assert result.fields["title_seniority"] == "director"


def test_a_title_only_response_still_yields_evidence() -> None:
    """Apollo omits the seniority/department enums for a meaningful share of
    records. Without the title fallback those leads would be permanently
    unknown on 35 of the scorer's 100 points."""
    result = _provider_with(_json_response(200, _fixture("person_title_only"))).fetch(_entity())

    assert result.status is ProviderStatus.SUCCESS
    assert result.fields == {"title_seniority": "director", "title_function": "marketing"}


def test_a_partial_result_yields_the_field_that_mapped_and_drops_the_one_that_did_not() -> None:
    """ "Student" carries a real seniority signal and no function signal. The
    honest outcome is one field of evidence and one field left unknown — not a
    failed call, and not a fabricated function."""
    provider = _provider_with(_json_response(200, _person(title="Student")))
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.SUCCESS
    assert result.fields == {"title_seniority": "ic"}
    assert result.raw["normalization"]["unmapped"] == [
        {"field": "title_function", "raw": "Student"}
    ]


def test_the_matched_identity_is_carried_for_review_but_never_as_evidence() -> None:
    """A reviewer must be able to check *who* ARIE matched — a wrong person is
    the failure mode person enrichment has and company enrichment does not.
    None of these is a SCORED_FIELD, so none can reach the scorer."""
    result = _provider_with(_json_response(200, _fixture("person_full"))).fetch(_entity())

    identity = result.raw["matched_identity"]
    assert identity["full_name"] == "Dana Okafor"
    assert identity["title"] == "VP of Revenue Operations"
    assert identity["organization_domain"] == "northwind-analytics.test"
    assert not set(identity) & set(SCORED_FIELDS)


def test_apollos_intent_and_funding_signals_are_not_turned_into_evidence() -> None:
    """Apollo ships ``intent_strength``/``latest_funding_stage``; ARIE scores
    ``buying_intent`` (20 points, the single largest field) and
    ``recent_trigger_event``. Wiring a vendor's unvalidatable model into the
    largest weight in the ruleset is exactly the shortcut this project refuses
    — the fields stay unknown, and the bounds stay honestly wide."""
    result = _provider_with(_json_response(200, _fixture("person_intent_fields_present"))).fetch(
        _entity()
    )

    assert set(result.fields) == {"title_seniority", "title_function"}
    assert "buying_intent" not in result.fields
    assert "recent_trigger_event" not in result.fields


# ------------------------------------------------------------------- misses --


def test_no_match_is_a_miss_that_costs_nothing() -> None:
    """Apollo's documented metering consumes no credit when it finds nothing.
    This is the one place the cost model genuinely diverges from
    ``live_abstract``, which bills every lookup because Abstract's pricing page
    says a submitted domain consumes a credit regardless of success."""
    result = _provider_with(_json_response(200, _fixture("person_not_found"))).fetch(_entity())

    assert result.status is ProviderStatus.MISS
    assert result.fields == {}
    assert result.cost_usd == 0.0
    assert "credits_consumed" not in result.raw


def test_an_empty_body_is_treated_as_a_free_miss_not_an_error() -> None:
    result = _provider_with(_json_response(200, {})).fetch(_entity())
    assert result.status is ProviderStatus.MISS
    assert result.cost_usd == 0.0


def test_a_matched_person_in_unmappable_vocabulary_is_a_billed_miss() -> None:
    """The other half of the same distinction, and the reason the two MISSes
    cannot share a billing rule. Apollo found a person and spent the credit;
    ARIE could not read what it said. Charging zero would hide real spend, and
    reporting SUCCESS would put nothing-shaped evidence on the lead."""
    result = _provider_with(_json_response(200, _fixture("person_unmappable_vocabulary"))).fetch(
        _entity()
    )

    assert result.status is ProviderStatus.MISS
    assert result.fields == {}
    assert result.cost_usd == pytest.approx(_COST)
    assert {item["field"] for item in result.raw["normalization"]["unmapped"]} == {
        "title_seniority",
        "title_function",
    }


def test_a_billed_call_records_the_vendors_own_unit_alongside_the_modelled_dollars() -> None:
    """ "$0.0196" is one credit converted at a stated rate, not a price Apollo
    quoted. A ledger row or a span that shows only the dollars invites reading
    a modelled figure as billed spend."""
    result = _provider_with(_json_response(200, _person(title="VP Sales"))).fetch(_entity())

    assert result.raw["credits_consumed"] == 1
    assert result.raw["cost_basis"] == "modelled_credit_equivalent"


# --------------------------------------------------------------- failures --


@pytest.mark.parametrize(
    ("status_code", "error_kind"),
    [
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "rate_limited"),
        (422, "unprocessable_request"),
        (500, "server_error"),
        (503, "server_error"),
        (418, "unexpected_status:418"),
    ],
)
def test_every_http_failure_is_a_result_never_an_exception(
    status_code: int, error_kind: str
) -> None:
    """A vendor being down must not fail the job: the worker would retry, the
    lead would eventually dead-letter, and a transport failure at one vendor is
    not a reason to lose a lead. Zero cost on every one of them — a failed call
    bought nothing."""
    result = _provider_with(_json_response(status_code, {"error": "nope"})).fetch(_entity())

    assert result.status is ProviderStatus.ERROR
    assert result.raw["error_kind"] == error_kind
    assert result.cost_usd == 0.0
    assert result.fields == {}


def test_a_timeout_is_reported_as_timeout_not_as_a_generic_error() -> None:
    """Distinct from ERROR because they call for different operator responses —
    a timeout says raise ``APOLLO_TIMEOUT_SECONDS`` or check the network, an
    error says check the credential or the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    result = _provider_with(handler).fetch(_entity())
    assert result.status is ProviderStatus.TIMEOUT
    assert result.raw["error_kind"] == "timeout"
    assert result.cost_usd == 0.0


def test_a_transport_error_names_the_exception_type_and_nothing_else() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = _provider_with(handler).fetch(_entity())
    assert result.status is ProviderStatus.ERROR
    assert result.raw["error_kind"] == "transport_error:ConnectError"


def test_malformed_json_is_an_error_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>totally not json</html>")

    result = _provider_with(handler).fetch(_entity())
    assert result.status is ProviderStatus.ERROR
    assert result.raw["error_kind"] == "malformed_response"


def test_a_json_body_that_is_not_an_object_is_an_error() -> None:
    """A 200 carrying ``[]`` or ``"ok"`` is not a contract this adapter can
    read. Distinguished from a miss deliberately: a miss is Apollo answering,
    and this is Apollo answering in a shape the contract does not describe."""
    result = _provider_with(_json_response(200, ["unexpected"])).fetch(_entity())
    assert result.status is ProviderStatus.ERROR
    assert result.raw["error_kind"] == "malformed_response"


@pytest.mark.parametrize(
    "handler_factory",
    [
        lambda: _json_response(401, {"error": "bad key"}),
        lambda: _json_response(500, {"error": "boom"}),
    ],
)
def test_no_failure_path_leaks_the_api_key(
    handler_factory: Callable[[], Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Asserted over the whole serialized result, not one field: ``raw`` is put
    on spans and into ``provider_calls``, and a credential reaching either is
    the failure this adapter's error handling is shaped around."""
    result = _provider_with(handler_factory()).fetch(_entity())
    assert _FAKE_KEY not in json.dumps(result.raw)
