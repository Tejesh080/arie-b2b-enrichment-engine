"""The one real enrichment adapter (post-M1 P5) — every test here is hermetic.

`httpx.MockTransport` stands in for Abstract API's Company Enrichment
endpoint, the same idiom `tests/unit/test_llm_deepseek_client.py` uses for
DeepSeek — nothing here needs `ABSTRACT_COMPANY_API_KEY`, network access, or
real spend, and every failure mode (timeout, auth, rate limit, malformed body,
...) is deterministic and reproducible offline.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import httpx
import pytest

from arie.config import LiveProviderConfig
from arie.core.types import Entity, ProviderStatus
from arie.providers.live_abstract import (
    AbstractCompanyConfigurationError,
    AbstractCompanyEnrichmentProvider,
)

_FAKE_KEY = "sk-live-test-secret-do-not-leak"


def _config(**overrides: object) -> LiveProviderConfig:
    defaults: dict[str, object] = {
        "api_key": _FAKE_KEY,
        "base_url": "https://fake.test/v2",
        "timeout_seconds": 5.0,
        "cost_usd_per_call": 0.002,
    }
    defaults.update(overrides)
    return LiveProviderConfig(**defaults)  # type: ignore[arg-type]


def _provider_with(
    handler: Callable[[httpx.Request], httpx.Response], **config_overrides: object
) -> AbstractCompanyEnrichmentProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AbstractCompanyEnrichmentProvider(config=_config(**config_overrides), client=client)


def _entity(domain: str = "acme.com") -> Entity:
    return Entity(entity_type="company", entity_id=uuid.uuid4(), canonical_key=domain)


def _json_response(status_code: int, body: object) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return handler


# ------------------------------------------------------------- construction --


def test_missing_api_key_raises_at_construction_not_at_call_time() -> None:
    with pytest.raises(AbstractCompanyConfigurationError):
        AbstractCompanyEnrichmentProvider.build(config=LiveProviderConfig(api_key=""))


def test_an_injected_client_bypasses_the_key_check() -> None:
    """Mirrors DeepSeekSignalExtractor's own test-injection escape hatch."""
    client = httpx.Client(transport=httpx.MockTransport(_json_response(200, {})))
    provider = AbstractCompanyEnrichmentProvider.build(
        config=LiveProviderConfig(api_key=""), client=client
    )
    assert provider.config.api_key == ""


def test_wrong_entity_type_raises_value_error() -> None:
    provider = _provider_with(_json_response(200, {}))
    person_entity = Entity(entity_type="person", entity_id=uuid.uuid4(), canonical_key="a@b.com")
    with pytest.raises(ValueError, match="company"):
        provider.fetch(person_entity)


# ----------------------------------------------------------------- success --


def test_success_maps_employee_count_and_industry() -> None:
    provider = _provider_with(_json_response(200, {"employee_count": 5000, "industry": "Software"}))
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.SUCCESS
    assert result.fields == {"employee_count": 5000, "industry": "software"}
    assert result.cost_usd == pytest.approx(0.002)
    assert result.confidence > 0.0


def test_industry_is_lowercased_and_stripped() -> None:
    provider = _provider_with(_json_response(200, {"industry": "  Financial Technology  "}))
    result = provider.fetch(_entity())
    assert result.fields["industry"] == "financial technology"


def test_a_float_employee_count_is_coerced_to_int() -> None:
    provider = _provider_with(_json_response(200, {"employee_count": 42.0}))
    result = provider.fetch(_entity())
    assert result.fields["employee_count"] == 42


def test_only_the_two_declared_fields_are_ever_surfaced() -> None:
    """Extra response fields (description, linkedin_url, ...) are real but
    out of this adapter's declared scope — never silently smuggled through."""
    provider = _provider_with(
        _json_response(
            200,
            {
                "employee_count": 10,
                "industry": "logistics",
                "description": "a company",
                "linkedin_url": "https://linkedin.com/company/acme",
            },
        )
    )
    result = provider.fetch(_entity())
    assert set(result.fields) == {"employee_count", "industry"}


# --------------------------------------------------------- partial / missing --


def test_a_bad_employee_count_type_is_dropped_not_fatal() -> None:
    """A malformed individual field degrades to 'unknown for that field', not
    a whole-response failure — item 17's 'valid response but missing desired
    field' case, produced by a field that can't be coerced rather than one
    that's absent."""
    provider = _provider_with(
        _json_response(200, {"employee_count": "not-a-number", "industry": "fintech"})
    )
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.SUCCESS
    assert result.fields == {"industry": "fintech"}


def test_no_usable_field_at_all_is_a_billed_miss() -> None:
    """Abstract's own pricing docs: a submitted domain consumes a credit
    regardless of response success -- a miss is billed, matching
    `arie.providers.catalog`'s `bill_on_miss` convention."""
    provider = _provider_with(_json_response(200, {"description": "no firmographic match"}))
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.MISS
    assert result.fields == {}
    assert result.confidence == 0.0
    assert result.cost_usd == pytest.approx(0.002)


# --------------------------------------------------------------- failures --


def test_timeout_is_reported_as_timeout_status_at_zero_cost() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    provider = _provider_with(handler)
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.TIMEOUT
    assert result.cost_usd == 0.0
    assert result.fields == {}


def test_a_connection_error_is_reported_as_error_at_zero_cost() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider = _provider_with(handler)
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.ERROR
    assert result.cost_usd == 0.0


@pytest.mark.parametrize("status_code", [401, 403])
def test_authentication_failure_is_free_and_not_a_miss(status_code: int) -> None:
    provider = _provider_with(_json_response(status_code, {"error": "invalid api key"}))
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.ERROR
    assert result.cost_usd == 0.0
    assert result.raw["error_kind"] == "authentication_failed"


def test_rate_limit_is_free_and_not_a_miss() -> None:
    provider = _provider_with(_json_response(429, {"error": "rate limited"}))
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.ERROR
    assert result.cost_usd == 0.0
    assert result.raw["error_kind"] == "rate_limited"


def test_insufficient_credits_is_free_and_not_a_miss() -> None:
    provider = _provider_with(_json_response(422, {"error": "insufficient credits"}))
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.ERROR
    assert result.cost_usd == 0.0
    assert result.raw["error_kind"] == "insufficient_credits"


def test_server_error_is_free_and_not_a_miss() -> None:
    provider = _provider_with(_json_response(500, {"error": "internal"}))
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.ERROR
    assert result.cost_usd == 0.0
    assert result.raw["error_kind"] == "server_error"


def test_malformed_json_body_is_free_and_not_a_miss() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all {{{")

    provider = _provider_with(handler)
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.ERROR
    assert result.cost_usd == 0.0
    assert result.raw["error_kind"] == "malformed_response"


def test_a_json_array_body_is_treated_as_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([1, 2, 3]).encode())

    provider = _provider_with(handler)
    result = provider.fetch(_entity())

    assert result.status is ProviderStatus.ERROR
    assert result.raw["error_kind"] == "malformed_response"


# ------------------------------------------------------------ never leaks --


def test_the_api_key_never_appears_in_the_result() -> None:
    """Every status branch's `raw` dict must be safe to log or persist —
    never containing the querystring api_key value."""
    for handler in (
        _json_response(200, {"employee_count": 5, "industry": "software"}),
        _json_response(401, {"error": "bad key"}),
        _json_response(500, {"error": "oops"}),
    ):
        provider = _provider_with(handler)
        result = provider.fetch(_entity())
        serialized = json.dumps(result.raw, default=str)
        assert _FAKE_KEY not in serialized


def test_a_transport_error_message_never_contains_the_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # httpx bakes the full request URL (including ?api_key=...) into this
        # exception's own message -- the adapter must never surface exc's str().
        raise httpx.ConnectError(f"connect failed: {request.url}", request=request)

    provider = _provider_with(handler)
    result = provider.fetch(_entity())
    assert _FAKE_KEY not in json.dumps(result.raw, default=str)
