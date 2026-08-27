"""The Hunter enrichment adapter — every test here is hermetic.

``httpx.MockTransport`` stands in for Hunter's Combined Enrichment endpoint —
nothing here needs ``HUNTER_API_KEY``, network access, or a spent credit.

Division of labour with ``test_hunter_contract.py``: that file owns the
raw→canonical mapping (including the title-beats-coarse-enum seniority rule).
This file owns what the transport adds — the request Hunter receives, the
404-is-a-miss rule, Hunter's inverted 403/429 table, and the 0.2-credit cost
model.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from arie.config import HunterConfig
from arie.core.types import Entity, ProviderStatus
from arie.providers.live_hunter import (
    HUNTER_PROVIDER_NAME,
    HUNTER_PROVIDES_FIELDS,
    HunterConfigurationError,
    HunterEnrichmentProvider,
)
from arie.scoring.rules import SCORED_FIELDS

_FAKE_KEY = "hunter-live-test-secret-do-not-leak"
_COST = 0.0049
_FIXTURES = Path(__file__).parent.parent / "fixtures" / "hunter"


def _fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload


def _config(**overrides: object) -> HunterConfig:
    defaults: dict[str, object] = {
        "api_key": _FAKE_KEY,
        "base_url": "https://fake.hunter.test/v2/combined/find",
        "timeout_seconds": 5.0,
        "cost_usd_per_success": _COST,
    }
    defaults.update(overrides)
    return HunterConfig(**defaults)  # type: ignore[arg-type]


def _provider_with(
    handler: Callable[[httpx.Request], httpx.Response], **config_overrides: object
) -> HunterEnrichmentProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HunterEnrichmentProvider(config=_config(**config_overrides), client=client)


def _entity(email: str = "dana.okafor@northwind-analytics.test") -> Entity:
    return Entity(entity_type="person", entity_id=uuid.uuid4(), canonical_key=email)


def _json_response(status_code: int, body: object) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return handler


# --------------------------------------------------------------- declaration --


def test_it_declares_the_person_entity_type_and_the_two_scored_fields() -> None:
    provider = _provider_with(_json_response(404, _fixture("not_found")))

    assert provider.name == HUNTER_PROVIDER_NAME
    assert provider.entity_type == "person"
    assert provider.provides_fields == HUNTER_PROVIDES_FIELDS
    assert set(provider.provides_fields) <= set(SCORED_FIELDS)
    assert provider.base_cost_usd == pytest.approx(_COST)


# -------------------------------------------------------------- construction --


def test_missing_api_key_raises_at_construction_not_at_call_time() -> None:
    with pytest.raises(HunterConfigurationError, match="HUNTER_API_KEY"):
        HunterEnrichmentProvider.build(config=HunterConfig(api_key=""))


def test_an_injected_client_bypasses_the_key_check() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_json_response(404, {})))
    provider = HunterEnrichmentProvider.build(config=HunterConfig(api_key=""), client=client)
    assert provider.config.api_key == ""


def test_wrong_entity_type_raises_value_error() -> None:
    provider = _provider_with(_json_response(404, {}))
    company_entity = Entity(entity_type="company", entity_id=uuid.uuid4(), canonical_key="acme.com")
    with pytest.raises(ValueError, match="person"):
        provider.fetch(company_entity)


# --------------------------------------------------------------- the request --


def test_the_request_matches_hunters_documented_contract() -> None:
    """GET, ``X-API-KEY`` header, email as the one query parameter — all three
    verified against Hunter's published docs, and all three different from at
    least one sibling adapter (Apollo POSTs; Abstract keys in the URL)."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=_fixture("combined_full"))

    _provider_with(handler).fetch(_entity("dana@northwind-analytics.test"))

    assert seen["method"] == "GET"
    assert seen["url"].startswith("https://fake.hunter.test/v2/combined/find")
    assert seen["headers"]["x-api-key"] == _FAKE_KEY
    assert seen["params"] == {"email": "dana@northwind-analytics.test"}


def test_the_api_key_never_appears_in_the_request_url() -> None:
    """Hunter documents a query-parameter mechanism too; this adapter must
    never use it. A key in a URL reaches proxy logs and exception reprs."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(404, json=_fixture("not_found"))

    _provider_with(handler).fetch(_entity())
    assert _FAKE_KEY not in seen["url"]


# ------------------------------------------------------------------ success --


def test_success_maps_the_person_half_onto_canonical_evidence() -> None:
    result = _provider_with(_json_response(200, _fixture("combined_full"))).fetch(_entity())

    assert result.status is ProviderStatus.SUCCESS
    assert result.fields == {"title_seniority": "vp", "title_function": "sales"}
    assert result.cost_usd == pytest.approx(_COST)
    assert result.confidence > 0.0


def test_a_billed_call_records_hunters_own_fractional_credit_unit() -> None:
    """0.2 credits, not 1 — Hunter meters enrichments fractionally, and the
    ledger's ``credits_used`` column is NUMERIC for exactly this row."""
    result = _provider_with(_json_response(200, _fixture("combined_full"))).fetch(_entity())

    assert result.raw["credits_consumed"] == pytest.approx(0.2)
    assert result.raw["cost_basis"] == "modelled_credit_equivalent"


def test_the_company_half_rides_as_audit_preview_never_as_fields() -> None:
    """A combined response carries Abstract's two fields. They must appear in
    the comparison preview (canonical audit form) and must NOT appear in
    ``fields`` — the handler persists ``fields``, and Hunter's company data is
    not evidence until the bake-off says it deserves to be."""
    result = _provider_with(_json_response(200, _fixture("combined_full"))).fetch(_entity())

    assert "employee_count" not in result.fields
    assert "industry" not in result.fields
    preview = result.raw["company_preview"]
    assert {item["field"]: item["canonical"] for item in preview["mapped"]} == {
        "employee_count": 240,
        "industry": "software",
    }


def test_the_matched_identity_is_carried_for_review() -> None:
    result = _provider_with(_json_response(200, _fixture("combined_full"))).fetch(_entity())
    identity = result.raw["matched_identity"]
    assert identity["full_name"] == "Dana Okafor"
    assert identity["title"] == "VP of Sales"


# ------------------------------------------------------------------- misses --


def test_a_404_is_hunters_documented_no_match_and_costs_nothing() -> None:
    """The one adapter where a 4xx is an ordinary miss. Mapping Hunter's 404 to
    ERROR would report a healthy vendor as broken on every lead it simply
    doesn't know — and would charge the failure against the provider in every
    bake-off metric."""
    result = _provider_with(_json_response(404, _fixture("not_found"))).fetch(_entity())

    assert result.status is ProviderStatus.MISS
    assert result.fields == {}
    assert result.cost_usd == 0.0
    assert "error_kind" not in result.raw
    assert "credits_consumed" not in result.raw


def test_an_off_contract_empty_200_is_a_free_miss() -> None:
    result = _provider_with(_json_response(200, {"data": {}})).fetch(_entity())
    assert result.status is ProviderStatus.MISS
    assert result.cost_usd == 0.0


def test_a_matched_person_in_unmappable_vocabulary_is_a_billed_miss() -> None:
    """Hunter delivered a record — 0.2 credits consumed — but nothing mapped.
    Same two-kinds-of-MISS distinction as the Apollo adapter, same reasoning:
    charging zero hides real spend, reporting SUCCESS ships empty evidence."""
    result = _provider_with(_json_response(200, _fixture("combined_unmappable"))).fetch(_entity())

    assert result.status is ProviderStatus.MISS
    assert result.fields == {}
    assert result.cost_usd == pytest.approx(_COST)
    assert {item["field"] for item in result.raw["normalization"]["unmapped"]} == {
        "title_seniority",
        "title_function",
    }


# ----------------------------------------------------------------- failures --


@pytest.mark.parametrize(
    ("status_code", "error_kind"),
    [
        (401, "authentication_failed"),
        # Hunter's documented table, not the common convention: 403 is "rate
        # limit reached" and 429 is "usage quota exceeded". Swapped mappings
        # would either cool Hunter down for an hour on a burst, or hammer a
        # dead quota all day.
        (403, "rate_limited"),
        (429, "quota_exhausted"),
        (400, "unprocessable_request"),
        (422, "unprocessable_request"),
        (500, "server_error"),
        (503, "server_error"),
        (418, "unexpected_status:418"),
    ],
)
def test_every_http_failure_is_a_result_never_an_exception(
    status_code: int, error_kind: str
) -> None:
    result = _provider_with(
        _json_response(status_code, {"errors": [{"id": "x", "code": status_code}]})
    ).fetch(_entity())

    assert result.status is ProviderStatus.ERROR
    assert result.raw["error_kind"] == error_kind
    assert result.cost_usd == 0.0
    assert result.fields == {}


def test_a_timeout_is_reported_as_timeout() -> None:
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


@pytest.mark.parametrize(
    "handler_factory",
    [
        lambda: _json_response(401, {"errors": [{"id": "unauthorized", "code": 401}]}),
        lambda: _json_response(500, {"errors": [{"id": "boom", "code": 500}]}),
    ],
)
def test_no_failure_path_leaks_the_api_key(
    handler_factory: Callable[[], Callable[[httpx.Request], httpx.Response]],
) -> None:
    result = _provider_with(handler_factory()).fetch(_entity())
    assert _FAKE_KEY not in json.dumps(result.raw)
